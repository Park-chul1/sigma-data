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
    / "stksecurityinfohist"
    / "part-000.parquet"
)

RAW_SUCCESS = RAW_FILE.parent / "_SUCCESS.json"

OUT_ROOT = (
    ROOT
    / "data"
    / "normalized"
    / "crsp"
    / "security_info"
)

OUT_FILE = OUT_ROOT / "part-000.parquet"
OUT_SUCCESS = OUT_ROOT / "_SUCCESS.json"

SCHEMA_VERSION = 1


def run(force: bool = False):
    if not RAW_FILE.exists() or not RAW_SUCCESS.exists():
        raise RuntimeError(
            "Committed raw security history not found."
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
        print("[SKIP] normalized security history exists")
        return

    temp_file = OUT_FILE.with_name(
        OUT_FILE.name + ".tmp"
    )

    temp_success = OUT_SUCCESS.with_name(
        OUT_SUCCESS.name + ".tmp"
    )

    for path in [
        temp_file,
        temp_success,
    ]:
        if path.exists():
            path.unlink()

    if force:
        for path in [
            OUT_FILE,
            OUT_SUCCESS,
        ]:
            if path.exists():
                path.unlink()

    con = duckdb.connect()

    try:
        # ----------------------------------------------
        # Raw validation
        # ----------------------------------------------

        raw_rows = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_parquet('{RAW_FILE}')
            """
        ).fetchone()[0]

        invalid = con.execute(
            f"""
            SELECT
                COUNT(*) FILTER (
                    WHERE permno IS NULL
                ) AS null_permno,

                COUNT(*) FILTER (
                    WHERE secinfostartdt IS NULL
                ) AS null_start,

                COUNT(*) FILTER (
                    WHERE TRY_CAST(
                        secinfostartdt AS DATE
                    ) IS NULL
                ) AS bad_start,

                COUNT(*) FILTER (
                    WHERE secinfoenddt IS NOT NULL
                      AND TRY_CAST(
                        secinfoenddt AS DATE
                      ) IS NULL
                ) AS bad_end

            FROM read_parquet('{RAW_FILE}')
            """
        ).fetchone()

        if any(x != 0 for x in invalid):
            raise RuntimeError(
                f"Invalid raw security history: {invalid}"
            )

        # ----------------------------------------------
        # Strict normalization
        # ----------------------------------------------

        con.execute(
            f"""
            COPY (
                SELECT
                    CAST(permno AS BIGINT)
                        AS permno,

                    CAST(secinfostartdt AS DATE)
                        AS valid_from,

                    CAST(secinfoenddt AS DATE)
                        AS valid_to,

                    CAST(ticker AS VARCHAR)
                        AS ticker,

                    CAST(cusip AS VARCHAR)
                        AS cusip,

                    CAST(primaryexch AS VARCHAR)
                        AS primary_exchange,

                    CAST(sharetype AS VARCHAR)
                        AS share_type,

                    CAST(securitytype AS VARCHAR)
                        AS security_type,

                    CAST(securitysubtype AS VARCHAR)
                        AS security_subtype,

                    CAST(usincflg AS VARCHAR)
                        AS us_incorporated_flag,

                    CAST(issuertype AS VARCHAR)
                        AS issuer_type,

                    CAST(tradingstatusflg AS VARCHAR)
                        AS trading_status_flag,

                    CAST(conditionaltype AS VARCHAR)
                        AS conditional_type,

                    CAST(shareclass AS VARCHAR)
                        AS share_class

                FROM read_parquet('{RAW_FILE}')
            )

            TO '{temp_file}'
            (
                FORMAT PARQUET,
                COMPRESSION ZSTD
            )
            """
        )

        # ----------------------------------------------
        # Output validation
        # ----------------------------------------------

        normalized_rows = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_parquet('{temp_file}')
            """
        ).fetchone()[0]

        if normalized_rows != raw_rows:
            raise RuntimeError(
                f"Row mismatch: "
                f"raw={raw_rows}, "
                f"normalized={normalized_rows}"
            )

        bad_intervals = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_parquet('{temp_file}')
            WHERE valid_to IS NOT NULL
              AND valid_to < valid_from
            """
        ).fetchone()[0]

        if bad_intervals != 0:
            raise RuntimeError(
                f"{bad_intervals} invalid validity intervals"
            )

        # ----------------------------------------------
        # CRITICAL PIT invariant:
        # no overlapping intervals per PERMNO
        # ----------------------------------------------

        overlapping = con.execute(
            f"""
            WITH x AS (
                SELECT
                    permno,
                    valid_from,
                    valid_to,

                    LAG(
                        COALESCE(
                            valid_to,
                            DATE '9999-12-31'
                        )
                    ) OVER (
                        PARTITION BY permno
                        ORDER BY
                            valid_from,
                            COALESCE(
                                valid_to,
                                DATE '9999-12-31'
                            )
                    ) AS previous_valid_to

                FROM read_parquet('{temp_file}')
            )

            SELECT COUNT(*)
            FROM x
            WHERE previous_valid_to >= valid_from
            """
        ).fetchone()[0]

        if overlapping != 0:
            raise RuntimeError(
                f"{overlapping} overlapping "
                f"security-info intervals found"
            )

    finally:
        con.close()

    os.replace(
        temp_file,
        OUT_FILE,
    )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source": str(RAW_FILE),
        "rows": normalized_rows,
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
        f"[OK] normalized security history "
        f"rows={normalized_rows:,}"
    )
