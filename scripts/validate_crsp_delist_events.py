"""Compatibility CLI for validating terminal evidence in a canonical research run.

The historical nearest-event diagnostic is retired. Supply the published run
folder; the common validator checks the pipeline's exact event/storage keys,
return evidence, artifact integrity, and temporal boundaries. No nearest-date
match or independent event dataset is computed by this command.
"""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_research_run import main as validate_research_main


def main() -> None:
    print(
        "Compatibility alias: validate_research_run.py validates the supplied "
        "run directory. Nearest-event matching has been retired.",
        file=sys.stderr,
    )
    validate_research_main()


if __name__ == "__main__":
    main()
