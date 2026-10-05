import argparse
from datetime import date
from pathlib import Path

from src.derive.crsp_corporate_events import build_period


ROOT = Path(__file__).resolve().parents[1]


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Date must be YYYY-MM-DD") from exc


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a date-bounded CRSP CIZ corporate-event PIT dataset."
    )
    parser.add_argument("--start", type=parse_date, required=True)
    parser.add_argument("--end", type=parse_date, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    output = args.output or (
        ROOT
        / "data/derived/crsp/corporate_events"
        / f"start={args.start}/end={args.end}/part-000.parquet"
    )
    manifest = build_period(
        daily_pattern=ROOT / "data/normalized/crsp/daily/year=*/month=*/part-000.parquet",
        security_info_file=ROOT / "data/normalized/crsp/security_info/part-000.parquet",
        delistings_file=ROOT / "data/normalized/crsp/delistings/part-000.parquet",
        output_file=output,
        requested_start=args.start,
        requested_end=args.end,
        force=args.force,
    )
    print(f"[OK] {output}")
    print(
        f"rows={manifest['rows']:,}, events={manifest['delisting_event_rows']:,}, "
        f"review={manifest['requires_review_rows']:,}"
    )


if __name__ == "__main__":
    main()
