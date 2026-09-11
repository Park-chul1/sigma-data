import argparse
from datetime import date

from src.ingest.crsp_security_info import run


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Date must be YYYY-MM-DD"
        ) from exc


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        type=parse_date,
        required=True,
    )

    parser.add_argument(
        "--end",
        type=parse_date,
        required=True,
    )

    parser.add_argument(
        "--force",
        action="store_true",
    )

    args = parser.parse_args()

    run(
        start_date=args.start,
        end_date=args.end,
        force=args.force,
    )


if __name__ == "__main__":
    main()
