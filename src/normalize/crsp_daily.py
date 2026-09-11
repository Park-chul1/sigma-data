from pathlib import Path
from datetime import date, datetime, timezone
import json
import os

import duckdb


ROOT = Path(__file__).resolve().parents[2]

RAW_ROOT = (
    ROOT
    / "data"
    / "raw"
    / "crsp"
    / "stkdlysecurityprimarydata"
)

NORMALIZED_ROOT = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "daily"
)

SCHEMA_VERSION = 1


def normalized_path(year: int, month: int) -> Path:
    return (
        NORMALIZED_ROOT
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "part-000.parquet"
    )


def success_path(year: int, month: int) -> Path:
    return (
        NORMALIZED_ROOT
        / f"year={year:04d}"
        / f"month={month:02d}"
        / "_SUCCESS.json"
    )


def raw_partition(year: int, month: int):
    base = (
        RAW_ROOT
        / f"year={year:04d}"
        / f"month={month:02d}"
    )

    return (
        base / "part-000.parquet",
        base / "_SUCCESS.json",
    )


def normalize_month(
    con,
    year: int,
    month: int,
    force: bool = False,
):
    raw_file, raw_success = raw_partition(
        year,
        month,
    )

    # Only normalize committed raw partitions.
    if not raw_file.exists() or not raw_success.exists():
        return False

    out_file = normalized_path(year, month)
    out_success = success_path(year, month)

    out_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        out_file.exists()
        and out_success.exists()
        and not force
    ):
        print(
            f"[SKIP] {year}-{month:02d} "
            "(normalized)"
        )
        return True

    temp_file = out_file.with_name(
        out_file.name + ".tmp"
    )

    temp_success = out_success.with_name(
        out_success.name + ".tmp"
    )

    for path in [
        temp_file,
        temp_success,
    ]:
        if path.exists():
            path.unlink()

    # Incomplete previous normalized partition.
    if not force:
        if out_file.exists() != out_success.exists():
            print(
                f"[REBUILD] incomplete normalized "
                f"{year}-{month:02d}"
            )

            if out_file.exists():
                out_file.unlink()

            if out_success.exists():
                out_success.unlink()

    elif force:
        for path in [out_file, out_success]:
            if path.exists():
                path.unlink()

    print(
        f"[NORMALIZE] {year}-{month:02d}"
    )

    # --------------------------------------------------
    # 1. Validate raw keys first
    # --------------------------------------------------

    raw_stats = con.execute(
        f"""
        SELECT
            COUNT(*) AS rows,

            COUNT(*) FILTER (
                WHERE permno IS NULL
            ) AS null_permno,

            COUNT(*) FILTER (
                WHERE dlycaldt IS NULL
            ) AS null_date,

            COUNT(*) FILTER (
                WHERE TRY_CAST(dlycaldt AS DATE) IS NULL
            ) AS invalid_date

        FROM read_parquet('{raw_file}')
        """
    ).fetchone()

    (
        raw_rows,
        null_permno,
        null_date,
        invalid_date,
    ) = raw_stats

    if null_permno != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"{null_permno} null PERMNO"
        )

    if null_date != 0 or invalid_date != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"null_date={null_date}, "
            f"invalid_date={invalid_date}"
        )

    duplicate_keys = con.execute(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT
                permno,
                dlycaldt,
                COUNT(*) AS n

            FROM read_parquet('{raw_file}')

            GROUP BY
                permno,
                dlycaldt

            HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]

    if duplicate_keys != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"{duplicate_keys} duplicate keys"
        )

    # --------------------------------------------------
    # 2. Normalize with strict CAST
    # --------------------------------------------------

    con.execute(
        f"""
        COPY (
            SELECT
                CAST(permno AS BIGINT)
                    AS permno,

                CAST(dlycaldt AS DATE)
                    AS date,

                CAST(dlyprc AS DOUBLE)
                    AS price_raw,

                CAST(dlycap AS DOUBLE)
                    AS market_cap_kusd,

                CAST(dlyret AS DOUBLE)
                    AS ret_total,

                CAST(dlyretx AS DOUBLE)
                    AS ret_ex_div,

                CAST(dlyvol AS DOUBLE)
                    AS volume_raw_shares,

                CAST(dlydelflg AS VARCHAR)
                    AS delist_flag,

                CAST(dlyprcflg AS VARCHAR)
                    AS price_flag,

                CAST(dlycapflg AS VARCHAR)
                    AS market_cap_flag,

                CAST(dlyretmissflg AS VARCHAR)
                    AS ret_missing_flag,

                CAST(dlydistretflg AS VARCHAR)
                    AS distribution_return_flag

            FROM read_parquet('{raw_file}')
        )

        TO '{temp_file}'
        (
            FORMAT PARQUET,
            COMPRESSION ZSTD
        )
        """
    )

    # --------------------------------------------------
    # 3. Validate normalized output
    # --------------------------------------------------

    result = con.execute(
        f"""
        SELECT
            COUNT(*) AS rows,
            MIN(date) AS min_date,
            MAX(date) AS max_date,

            COUNT(*) FILTER (
                WHERE permno IS NULL
            ) AS null_permno,

            COUNT(*) FILTER (
                WHERE date IS NULL
            ) AS null_date

        FROM read_parquet('{temp_file}')
        """
    ).fetchone()

    (
        normalized_rows,
        min_date,
        max_date,
        out_null_permno,
        out_null_date,
    ) = result

    if normalized_rows != raw_rows:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"raw rows={raw_rows}, "
            f"normalized rows={normalized_rows}"
        )

    if out_null_permno != 0 or out_null_date != 0:
        raise RuntimeError(
            f"{year}-{month:02d}: "
            "normalized key contains NULL"
        )

    if (
        min_date.year != year
        or min_date.month != month
        or max_date.year != year
        or max_date.month != month
    ):
        raise RuntimeError(
            f"{year}-{month:02d}: "
            f"unexpected date range "
            f"{min_date}..{max_date}"
        )

    # --------------------------------------------------
    # 4. Atomic commit parquet
    # --------------------------------------------------

    os.replace(
        temp_file,
        out_file,
    )

    # --------------------------------------------------
    # 5. Write normalized manifest
    # --------------------------------------------------

    manifest = {
        "schema_version": SCHEMA_VERSION,

        "source": str(raw_file),

        "rows": normalized_rows,

        "min_date": str(min_date),
        "max_date": str(max_date),

        "columns": {
            "permno": "BIGINT",
            "date": "DATE",
            "price_raw": "DOUBLE",
            "market_cap_kusd": "DOUBLE",
            "ret_total": "DOUBLE",
            "ret_ex_div": "DOUBLE",
            "volume_raw_shares": "DOUBLE",
            "delist_flag": "VARCHAR",
            "price_flag": "VARCHAR",
            "market_cap_flag": "VARCHAR",
            "ret_missing_flag": "VARCHAR",
            "distribution_return_flag": "VARCHAR",
        },

        "normalized_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    }

    with open(
        temp_success,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            manifest,
            f,
            indent=2,
        )

    os.replace(
        temp_success,
        out_success,
    )

    size_mb = (
        out_file.stat().st_size
        / 1024**2
    )

    print(
        f"[OK] {year}-{month:02d} "
        f"rows={normalized_rows:,} "
        f"range={min_date}..{max_date} "
        f"size={size_mb:.1f} MB"
    )

    return True


def run(
    start_date: date | None = None,
    end_date: date | None = None,
    force: bool = False,
):
    con = duckdb.connect()

    try:
        success_files = sorted(
            RAW_ROOT.glob(
                "year=*/month=*/_SUCCESS.json"
            )
        )

        if not success_files:
            print(
                "[INFO] No committed raw partitions found."
            )
            return

        processed = 0

        for raw_success in success_files:
            month_dir = raw_success.parent
            year_dir = month_dir.parent

            year = int(
                year_dir.name.split("=")[1]
            )

            month = int(
                month_dir.name.split("=")[1]
            )

            partition_date = date(
                year,
                month,
                1,
            )

            if start_date is not None:
                start_month = date(
                    start_date.year,
                    start_date.month,
                    1,
                )

                if partition_date < start_month:
                    continue

            if end_date is not None:
                end_month = date(
                    end_date.year,
                    end_date.month,
                    1,
                )

                if partition_date > end_month:
                    continue

            ok = normalize_month(
                con=con,
                year=year,
                month=month,
                force=force,
            )

            if ok:
                processed += 1

        print(
            f"\n[DONE] normalized partitions "
            f"processed/available: {processed}"
        )

    finally:
        con.close()
