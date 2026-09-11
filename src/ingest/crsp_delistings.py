from pathlib import Path
from datetime import date, datetime, timezone
import json
import os

import wrds


ROOT = Path(__file__).resolve().parents[2]

SOURCE_SCHEMA = "crsp"
SOURCE_TABLE = "stkdelists"

RAW_ROOT = (
    ROOT
    / "data"
    / "raw"
    / "crsp"
    / SOURCE_TABLE
)

RAW_FILE = RAW_ROOT / "part-000.parquet"
SUCCESS_FILE = RAW_ROOT / "_SUCCESS.json"


def run(
    start_date: date,
    end_date: date,
    force: bool = False,
):
    RAW_ROOT.mkdir(
        parents=True,
        exist_ok=True,
    )

    if (
        RAW_FILE.exists()
        and SUCCESS_FILE.exists()
        and not force
    ):
        print("[SKIP] delistings already committed")
        return

    temp_file = RAW_FILE.with_name(
        RAW_FILE.name + ".tmp"
    )

    temp_success = SUCCESS_FILE.with_name(
        SUCCESS_FILE.name + ".tmp"
    )

    for path in [temp_file, temp_success]:
        if path.exists():
            path.unlink()

    if force:
        for path in [RAW_FILE, SUCCESS_FILE]:
            if path.exists():
                path.unlink()

    print("Connecting to WRDS...")

    db = wrds.Connection()

    try:
        sql = f"""
        SELECT
            permno,
            delistingdt,
            delret,

            delactiontype,
            delstatustype,
            delreasontype,
            delpaymenttype,

            delpermno,
            delpermco

        FROM {SOURCE_SCHEMA}.{SOURCE_TABLE}

        WHERE delistingdt >= '{start_date}'
          AND delistingdt <= '{end_date}'
        """

        print(
            f"[DOWNLOAD] delistings "
            f"{start_date} -> {end_date}"
        )

        df = db.raw_sql(sql)

    finally:
        db.close()

    if len(df) == 0:
        raise RuntimeError(
            "Delisting query returned zero rows."
        )

    if df["permno"].isna().any():
        raise RuntimeError(
            "NULL PERMNO in delisting data."
        )

    if df["delistingdt"].isna().any():
        raise RuntimeError(
            "NULL delisting date."
        )

    df.to_parquet(
        temp_file,
        index=False,
        compression="zstd",
    )

    os.replace(
        temp_file,
        RAW_FILE,
    )

    manifest = {
        "source_schema": SOURCE_SCHEMA,
        "source_table": SOURCE_TABLE,
        "requested_start": str(start_date),
        "requested_end": str(end_date),
        "rows": len(df),
        "downloaded_at_utc": datetime.now(
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
        SUCCESS_FILE,
    )

    print(
        f"[OK] delistings rows={len(df):,}"
    )