from pathlib import Path
from datetime import date, datetime, timedelta, timezone
import json
import os

import pandas as pd
import pyarrow.parquet as pq
import wrds


ROOT = Path(__file__).resolve().parents[2]

SOURCE_SCHEMA = "crsp"
SOURCE_TABLE = "stkdlysecurityprimarydata"

# RAW에서는 vendor/source table 이름을 최대한 그대로 남긴다.
RAW_ROOT = (
    ROOT
    / "data"
    / "raw"
    / "crsp"
    / SOURCE_TABLE
)


def parse_wrds_date(value) -> date:
    return date.fromisoformat(str(value)[:10])


def first_day_next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)

    return date(d.year, d.month + 1, 1)


def get_source_coverage(db) -> tuple[date, date]:
    """
    Return the minimum and maximum dates currently available
    in the WRDS CRSP source table.
    """

    result = db.raw_sql(
        f"""
        SELECT
            MIN(dlycaldt) AS min_date,
            MAX(dlycaldt) AS max_date
        FROM {SOURCE_SCHEMA}.{SOURCE_TABLE}
        """
    )

    min_value = result.loc[0, "min_date"]
    max_value = result.loc[0, "max_date"]

    if min_value is None or max_value is None:
        raise RuntimeError(
            "Could not determine CRSP source coverage."
        )

    return (
        parse_wrds_date(min_value),
        parse_wrds_date(max_value),
    )


def output_path(year: int, month: int) -> Path:
    return (
        RAW_ROOT
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "part-000.parquet"
    )


def success_path(year: int, month: int) -> Path:
    return (
        RAW_ROOT
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "_SUCCESS.json"
    )


def download_month(
    db,
    month_start: date,
    requested_start: date,
    requested_end: date,
    source_min_date: date,
    source_max_date: date,
    force: bool = False,
):
    next_month = first_day_next_month(month_start)

    query_start = max(
        month_start,
        requested_start,
        source_min_date,
    )

    query_end = min(
        next_month,
        requested_end + timedelta(days=1),
        source_max_date + timedelta(days=1),
    )

    if query_start >= query_end:
        return

    year = month_start.year
    month = month_start.month

    final_path = output_path(year, month)
    done_path = success_path(year, month)

    temp_path = final_path.with_name(
        final_path.name + ".tmp"
    )

    temp_done_path = done_path.with_name(
        done_path.name + ".tmp"
    )

    final_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # A partition counts as complete only when both
    # parquet and _SUCCESS.json exist.
    if (
        final_path.exists()
        and done_path.exists()
        and not force
    ):
        print(
            f"[SKIP] {year}-{month:02d} "
            f"(committed)"
        )
        return

    # Incomplete/stale files from failed runs.
    for path in [
        temp_path,
        temp_done_path,
    ]:
        if path.exists():
            print(f"[CLEAN] {path}")
            path.unlink()

    # If only one of parquet / success marker exists,
    # regard the partition as incomplete and rebuild it.
    if not force:
        if final_path.exists() != done_path.exists():
            print(
                f"[REBUILD] incomplete partition "
                f"{year}-{month:02d}"
            )

            if final_path.exists():
                final_path.unlink()

            if done_path.exists():
                done_path.unlink()

    elif force:
        for path in [final_path, done_path]:
            if path.exists():
                path.unlink()

    print(
        f"[DOWNLOAD] "
        f"{query_start} <= date < {query_end}"
    )

    sql = f"""
    SELECT
        permno,
        dlycaldt,
        dlydelflg,
        dlyprc,
        dlyprcflg,
        dlycap,
        dlycapflg,
        dlyret,
        dlyretx,
        dlyretmissflg,
        dlydistretflg,
        dlyvol

    FROM {SOURCE_SCHEMA}.{SOURCE_TABLE}

    WHERE dlycaldt >= '{query_start}'
      AND dlycaldt <  '{query_end}'
    """

    df = db.raw_sql(sql)

    row_count = len(df)

    if row_count == 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            "WRDS returned zero rows."
        )

    # --------------------------------------------------
    # Key validation
    # --------------------------------------------------

    null_permno = df["permno"].isna().sum()
    null_date = df["dlycaldt"].isna().sum()

    if null_permno != 0 or null_date != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"null_permno={null_permno}, "
            f"null_date={null_date}"
        )

    duplicate_count = df.duplicated(
        subset=["permno", "dlycaldt"]
    ).sum()

    if duplicate_count != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"found {duplicate_count} duplicate "
            "(permno, dlycaldt) keys"
        )

    # --------------------------------------------------
    # Date validation
    # --------------------------------------------------

    parsed_dates = pd.to_datetime(
        df["dlycaldt"],
        errors="coerce",
    )

    invalid_dates = parsed_dates.isna().sum()

    if invalid_dates != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"{invalid_dates} invalid dates"
        )

    actual_min_date = parsed_dates.min().date()
    actual_max_date = parsed_dates.max().date()

    if actual_min_date < query_start:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"min date {actual_min_date} "
            f"is before query start {query_start}"
        )

    if actual_max_date >= query_end:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"max date {actual_max_date} "
            f"is outside query range"
        )

    # --------------------------------------------------
    # Temporary parquet write
    # --------------------------------------------------

    df.to_parquet(
        temp_path,
        index=False,
        compression="zstd",
    )

    # Read parquet metadata back before commit.
    parquet_file = pq.ParquetFile(temp_path)
    parquet_rows = parquet_file.metadata.num_rows

    if parquet_rows != row_count:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"parquet rows={parquet_rows}, "
            f"expected={row_count}"
        )

    # --------------------------------------------------
    # Commit parquet
    # --------------------------------------------------

    os.replace(
        temp_path,
        final_path,
    )

    size_mb = (
        final_path.stat().st_size
        / 1024**2
    )

    # --------------------------------------------------
    # Commit manifest last
    # --------------------------------------------------

    manifest = {
        "source_schema": SOURCE_SCHEMA,
        "source_table": SOURCE_TABLE,
        "query_start": str(query_start),
        "query_end_exclusive": str(query_end),
        "rows": row_count,
        "min_date": str(actual_min_date),
        "max_date": str(actual_max_date),
        "file_size_bytes": final_path.stat().st_size,
        "downloaded_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    with open(
        temp_done_path,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            manifest,
            f,
            indent=2,
        )

    os.replace(
        temp_done_path,
        done_path,
    )

    print(
        f"[OK] {year}-{month:02d} "
        f"rows={row_count:,} "
        f"range={actual_min_date}..{actual_max_date} "
        f"size={size_mb:.1f} MB"
    )


def run(
    start_date: date,
    end_date: date | None = None,
    force: bool = False,
):
    print("Connecting to WRDS...")

    db = wrds.Connection()

    try:
        (
            source_min_date,
            source_max_date,
        ) = get_source_coverage(db)

        requested_end = (
            end_date
            if end_date is not None
            else source_max_date
        )

        effective_start = max(
            start_date,
            source_min_date,
        )

        effective_end = min(
            requested_end,
            source_max_date,
        )

        print("\n=== CRSP SOURCE COVERAGE ===")
        print(
            f"source          : "
            f"{SOURCE_SCHEMA}.{SOURCE_TABLE}"
        )
        print(
            f"source min date : {source_min_date}"
        )
        print(
            f"source max date : {source_max_date}"
        )
        print(
            f"requested start : {start_date}"
        )
        print(
            f"requested end   : "
            f"{end_date or '[source max]'}"
        )
        print(
            f"effective range : "
            f"{effective_start} -> {effective_end}"
        )

        if effective_start > effective_end:
            raise ValueError(
                "Requested range does not overlap "
                "CRSP source coverage."
            )

        current = date(
            effective_start.year,
            effective_start.month,
            1,
        )

        final_month = date(
            effective_end.year,
            effective_end.month,
            1,
        )

        print(
            f"months          : "
            f"{current:%Y-%m} -> "
            f"{final_month:%Y-%m}\n"
        )

        while current <= final_month:
            download_month(
                db=db,
                month_start=current,
                requested_start=effective_start,
                requested_end=effective_end,
                source_min_date=source_min_date,
                source_max_date=source_max_date,
                force=force,
            )

            current = first_day_next_month(
                current
            )

    finally:
        db.close()

    print(
        "\n[DONE] CRSP daily ingestion complete."
    )