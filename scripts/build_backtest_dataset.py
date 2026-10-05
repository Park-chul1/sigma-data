"""Build local CIZ research data; no WRDS access, strategy execution, or source writes."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start",type=date.fromisoformat,required=True,help="Evaluation start (exchange session)")
    parser.add_argument("--end",type=date.fromisoformat,required=True,help="Evaluation end (exchange session)")
    parser.add_argument("--lookback",type=int,default=252,help="Trailing total-return momentum sessions")
    parser.add_argument("--training-sessions",type=int,default=0)
    parser.add_argument("--train-start",type=date.fromisoformat)
    parser.add_argument("--train-end",type=date.fromisoformat)
    parser.add_argument("--label-horizon",type=int,default=1)
    parser.add_argument("--availability-lag",type=int,default=1,help="Assumed additional exchange sessions before daily information available")
    parser.add_argument("--decision",choices=["after_close","preopen"],default="after_close")
    parser.add_argument("--execution",default="vendor_return_research",help="Only vendor_return_research/research_close_to_close supported; no fills")
    parser.add_argument("--index-base",type=date.fromisoformat)
    parser.add_argument("--pit-mode",choices=["approximate","strict"],default="approximate")
    parser.add_argument("--quality-policy",choices=["mark","fail"],default="mark")
    parser.add_argument("--data-root",type=Path)
    parser.add_argument("--output-root",type=Path)
    args = parser.parse_args()
    try:
        config = ResearchConfig(requested_start=args.start,requested_end=args.end,
            factor_lookback_sessions=args.lookback,training_sessions=args.training_sessions,
            label_horizon_sessions=args.label_horizon,availability_lag_sessions=args.availability_lag,
            decision=args.decision,execution=args.execution,index_base_date=args.index_base,
            train_start=args.train_start,train_end=args.train_end)
        path,manifest = build_dataset(config,data_root=args.data_root,output_root=args.output_root,
                                     pit_mode=args.pit_mode,quality_policy=args.quality_policy)
    except (ValueError,RuntimeError,FileNotFoundError) as error:
        parser.exit(2,f"Research dataset failed: {error}\n")
    print(f"Run: {path}")
    print(f"Status: {manifest['status']}; PIT: {manifest['pit_guarantee']}")
    print(f"Unresolved return rows: {manifest['unresolved_return_rows']}")
    print(f"Evaluation inputs: {manifest['rows']['evaluation_features.parquet']}; training examples: {manifest['rows']['training.parquet']}")
    print("No next-open fills or portfolio performance are simulated. See _SUCCESS.json for policies and limitations.")


if __name__ == "__main__":
    main()
