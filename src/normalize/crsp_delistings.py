from pathlib import Path
from datetime import datetime, timezone
import json
import os

import duckdb


ROOT = Path(__file__).resolve().parents[2]

RAW_FILE = (
    ROOT
    / "data"
    / "raw"
    / "crsp"
    / "stkdelists"
    / "part-000.parquet"
)

RAW_SUCCESS = RAW_FILE.parent / "_SUCCESS.json"

OUT_ROOT = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "delistings"
)

OUT_FILE = OUT_ROOT / "part-000.parquet"
OUT_SUCCESS = OUT_ROOT / "_SUCCESS.json"

SCHEMA_VERSION = 1


def run(force: bool = False):
    if not RAW_FILE.exists() or not RAW_SUCCESS.exists():
        raise RuntimeError(
            "Committed raw delisting dataset not found."
        )

    OUT_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        OUT_FILE.exists()
        and OUT_SUCCESS.exists()
        and not force
    ):
        print("[SKIP] normalized delistings already committed")
        return

    temp_file = OUT_FILE.with_name(
        OUT_FILE.name + ".tmp"
    )

    temp_success = OUT_SUCCESS.with_name(
        OUT_SUCCESS.name + ".tmp"
    )

    for path in [temp_file, temp_success]:
        if path.exists():
            path.unlink()

    if force:
        for path in [OUT_FILE, OUT_SUCCESS]:
            if path.exists():
                path.unlink()

    con = duckdb.connect()

    try:
        # ------------------------------------------
        # RAW validation
        # ------------------------------------------

        raw_stats = con.execute(
            f"""
            SELECT
                COUNT(*) AS rows,

                COUNT(*) FILTER (
                    WHERE permno IS NULL
                ) AS null_permno,

                COUNT(*) FILTER (
                    WHERE delistingdt IS NULL
                ) AS null_event_date,

                COUNT(*) FILTER (
                    WHERE delistingdt IS NOT NULL
                      AND TRY_CAST(
                            delistingdt AS DATE
                          ) IS NULL
                ) AS invalid_event_date

            FROM read_parquet('{RAW_FILE}')
            """
        ).fetchone()

        (
            raw_rows,
            null_permno,
            null_event_date,
            invalid_event_date,
        ) = raw_stats

        if null_permno != 0:
            raise RuntimeError(
                f"NULL PERMNO rows: {null_permno}"
            )

        if null_event_date != 0 or invalid_event_date != 0:
            raise RuntimeError(
                "Invalid delisting dates: "
                f"null={null_event_date}, "
                f"invalid={invalid_event_date}"
            )

        # ------------------------------------------
        # Strict normalization
        # ------------------------------------------

        con.execute(
            f"""
            COPY (
                SELECT
                    CAST(permno AS BIGINT)
                        AS permno,

                    CAST(delistingdt AS DATE)
                        AS event_date,

                    CAST(delret AS DOUBLE)
                        AS delisting_return,

                    CAST(delactiontype AS VARCHAR)
                        AS action_type,

                    CAST(delstatustype AS VARCHAR)
                        AS status_type,

                    CAST(delreasontype AS VARCHAR)
                        AS reason_type,

                    CAST(delpaymenttype AS VARCHAR)
                        AS payment_type,

                    CAST(delpermno AS BIGINT)
                        AS successor_permno,

                    CAST(delpermco AS BIGINT)
                        AS successor_permco

                FROM read_parquet('{RAW_FILE}')
            )

            TO '{temp_file}'
            (
                FORMAT PARQUET,
                COMPRESSION ZSTD
            )
            """
        )

        # ------------------------------------------
        # NORMALIZED validation
        # ------------------------------------------

        normalized_stats = con.execute(
            f"""
            SELECT
                COUNT(*) AS rows,
                MIN(event_date) AS min_date,
                MAX(event_date) AS max_date,

                COUNT(*) FILTER (
                    WHERE permno IS NULL
                ) AS null_permno,

                COUNT(*) FILTER (
                    WHERE event_date IS NULL
                ) AS null_event_date

            FROM read_parquet('{temp_file}')
            """
        ).fetchone()

        (
            normalized_rows,
            min_date,
            max_date,
            out_null_permno,
            out_null_event_date,
        ) = normalized_stats

        if normalized_rows != raw_rows:
            raise RuntimeError(
                "Row count changed during normalization: "
                f"raw={raw_rows}, "
                f"normalized={normalized_rows}"
            )

        if out_null_permno != 0 or out_null_event_date != 0:
            raise RuntimeError(
                "Normalized delisting key contains NULL."
            )

        # 아직 unique invariant로 확정하지 않고
        # 실제 분포를 보고한다.
        duplicate_keys = con.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT
                    permno,
                    event_date,
                    COUNT(*) AS n

                FROM read_parquet('{temp_file}')

                GROUP BY
                    permno,
                    event_date

                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]

    finally:
        con.close()

    # ----------------------------------------------
    # Atomic commit
    # ----------------------------------------------

    os.replace(
        temp_file,
        OUT_FILE,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source": str(RAW_FILE),
        "rows": normalized_rows,
        "min_date": str(min_date),
        "max_date": str(max_date),
        "duplicate_permno_event_date_keys": duplicate_keys,
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
        OUT_SUCCESS,
    )

    print(
        f"[OK] normalized delistings "
        f"rows={normalized_rows:,} "
        f"range={min_date}..{max_date}"
    )

    print(
        f"[INFO] duplicate "
        f"(permno, event_date) keys={duplicate_keys:,}"
    )