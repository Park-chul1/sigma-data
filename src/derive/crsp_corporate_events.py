"""Compatibility entry point for corporate-event audits of canonical runs.

There is one research dataset builder: :mod:`src.derive.pipeline`. Its panel
and event artifacts contain eventual outcomes and must never be used directly
as strategy features. This module has no SQL, return policy, or output cache of
its own. Arbitrary-Parquet ``build_period`` exports have been retired because
they bypassed provenance, availability, and return-quality checks.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from .pipeline import build_dataset
from .temporal import ResearchConfig


def build_corporate_event_audit(
    config: ResearchConfig,
    *,
    data_root: Path | None = None,
    output_root: Path | None = None,
    pit_mode: str = "approximate",
    quality_policy: str = "mark",
) -> tuple[Path, dict]:
    """Return the canonical run and manifest, including audit-only events.

    This is a compatibility alias with precisely the canonical configuration,
    preparation window, source validation, and content/artifact hash checks.
    Inspect ``panel.parquet``/``events.parquet`` for corporate-event evidence;
    consume only the documented feature artifacts as strategy inputs.
    """
    return build_dataset(
        config,
        data_root=data_root,
        output_root=output_root,
        pit_mode=pit_mode,
        quality_policy=quality_policy,
    )


def build_period(
    *,
    daily_pattern: Path,
    security_info_file: Path,
    delistings_file: Path,
    output_file: Path,
    requested_start: date,
    requested_end: date,
    force: bool = False,
) -> dict:
    """Reject the retired independent export contract without touching files.

    The former arbitrary input paths and fixed output filename cannot preserve
    canonical source lineage or preparation coverage. In particular, ``force``
    must not restore an unchecked overwrite path.
    """
    raise ValueError(
        "build_period was retired: its arbitrary-input/fixed-file export bypassed "
        "research validation. Use build_corporate_event_audit(ResearchConfig("
        "requested_start=..., requested_end=..., factor_lookback_sessions=...), "
        "data_root=..., output_root=...) or scripts/build_backtest_dataset.py. "
        "Outputs are immutable runs/<run-id>/; panel.parquet and events.parquet "
        "are audit artifacts, not strategy inputs. --output and --force are no "
        "longer supported."
    )
