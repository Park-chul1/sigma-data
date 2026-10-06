"""Compatibility CLI for the canonical research builder's event audit artifacts.

Use the same flags and immutable run layout as build_backtest_dataset.py.
The old --output/--force single-file export is intentionally unsupported.
"""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_backtest_dataset import main as build_research_main


def main() -> None:
    print(
        "Compatibility alias: build_backtest_dataset.py performs all validation. "
        "panel.parquet/events.parquet are audit artifacts; strategy inputs are "
        "features.parquet/evaluation_features.parquet.",
        file=sys.stderr,
    )
    build_research_main()


if __name__ == "__main__":
    main()
