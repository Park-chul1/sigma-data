import argparse
from datetime import date

from src.ingest.crsp_daily import run


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Date must be YYYY-MM-DD"
        ) from exc


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Ingest CRSP daily security data "
            "from WRDS into monthly Parquet partitions."
        )
    )

    parser.add_argument(
        "--start",
        type=parse_date,
        required=True,
        help="Start date in YYYY-MM-DD format.",
    )

    parser.add_argument(
        "--end",
        type=parse_date,
        default=None,
        help=(
            "End date in YYYY-MM-DD format. "
            "Defaults to WRDS source max date."
        ),
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download committed partitions.",
    )

    args = parser.parse_args()

    run(
        start_date=args.start,
        end_date=args.end,
        force=args.force,
    )


if __name__ == "__main__":
    main()