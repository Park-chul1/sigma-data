"""Independently validate a published research export without changing its artifacts.

The JSON report is stored separately under ignored research_validation/ by default.
Checks establish export consistency/causality, not historical vendor PIT vintages.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = {
    "panel": "panel.parquet", "features": "features.parquet",
    "evaluation": "evaluation_features.parquet", "training": "training.parquet",
    "labels": "labels.parquet", "events": "events.parquet",
    "display": "display_indices.parquet", "calendar": "calendar.parquet",
    "identity_events": "identity_events.parquet",
}
SOURCE_COLUMNS = (
    "permno", "date", "price_raw", "market_cap_kusd", "ret_total", "ret_ex_div",
    "volume_raw_shares", "delist_flag", "price_flag", "market_cap_flag",
    "ret_missing_flag", "distribution_return_flag",
)


def fingerprint(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(run_path: Path, data_root: Path | None = None) -> dict:
    run_path = run_path.resolve()
    manifest = json.loads((run_path / "_SUCCESS.json").read_text())
    plan, config = manifest["plan"], manifest["config"]
    cutoff = plan.get("model_train_cutoff", plan.get("model_training_cutoff"))
    if cutoff is None:
        raise ValueError("Run manifest has no model-training cutoff")
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "validator_version": 1, "run": str(run_path), "run_id": manifest["run_id"],
              "snapshot_id": manifest["snapshot_id"], "run_status": manifest["status"],
              "pit_guarantee": manifest["pit_guarantee"], "checks": {}, "counts": {},
              "unperformed": ["historical publication/vintage verification", "order fills and cash/share ledger accounting"]}
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    con.execute("SET threads=4")
    con.execute("SET memory_limit='2GB'")
    columns = {}

    def check(name, failures=0, detail=None):
        report["checks"][name] = {"passed": failures == 0, "violations": failures}
        if detail is not None:
            report["checks"][name]["detail"] = detail

    def query_check(name, sql, params=None):
        check(name, con.execute(sql, params or []).fetchone()[0])

    try:
        for table, name in ARTIFACTS.items():
            path = run_path / name
            if not path.is_file():
                raise FileNotFoundError(f"Required run artifact missing: {name}")
            expected = manifest.get("artifacts", {}).get(name)
            check(f"hash_{table}", int(expected is None or fingerprint(path) != expected))
            con.read_parquet(str(path), hive_partitioning=False).create_view(table)
            columns[table] = {row[0] for row in con.execute(f"describe {table}").fetchall()}
            n = con.execute(f"select count(*) from {table}").fetchone()[0]
            report["counts"][table] = n
            check(f"manifest_rows_{table}", int(manifest["rows"].get(name) != n))
        required = {
            "features": {"permno", "decision_date", "decision_time", "observation_date", "available_at", "price_flag", "observed_total_return"},
            "training": {"label_end_date", "label_end_time", "label_available_at", "forward_vendor_return"},
            "panel": set(SOURCE_COLUMNS) | {"ret_total_vendor", "ret_total_backtest", "ret_total_for_signal", "return_quality", "terminal_event_date", "delisting_return", "ledger_status", "event_terms_incomplete", "is_delisting_return"},
            "labels": {"permno", "decision_date", "label_end_time", "available_at", "forward_vendor_return"},
            "display": {"permno", "date", "wealth_index", "base_wealth", "index_base100", "index_base_date"},
        }
        schema_missing = {table: sorted(names-columns[table]) for table, names in required.items() if names-columns[table]}
        check("required_schema", len(schema_missing), schema_missing)
        if schema_missing:
            report["unperformed"].append("Dependent semantic checks: required export fields absent")
            report["passed"] = False
            return report
        for table, keys in [("panel", "permno,date"), ("features", "permno,decision_date"),
                            ("evaluation", "permno,decision_date"), ("training", "permno,decision_date"),
                            ("labels", "permno,decision_date"), ("display", "permno,date"),
                            ("events", "permno,observed_date"), ("calendar", "date")]:
            query_check(f"unique_keys_{table}", f"select count(*) from (select {keys} from {table} group by all having count(*)>1)")
            predicate = " or ".join(f"{key} is null" for key in keys.split(","))
            query_check(f"nonnull_keys_{table}", f"select count(*) from {table} where {predicate}")
        forbidden = {"forward_vendor_return", "label_end_date", "label_end_time", "label_available_at",
                     "index_base100", "wealth_index", "base_wealth", "index_base_date", "delisting_return",
                     "terminal_event_date", "successor_permno", "valid_to", "ret_total_backtest"}
        strategy_columns = columns["features"] | columns["evaluation"]
        leakage_columns = sorted(name for name in strategy_columns if name in forbidden
                                 or name.startswith(("forward_", "future_", "label_", "index_")))
        check("strategy_columns_exclude_outcomes_and_display", len(leakage_columns), leakage_columns)
        query_check("feature_information_available", "select count(*) from features where available_at is null or decision_time is null or available_at>decision_time")
        offset = int(config["availability_lag_sessions"]) + int(config["decision"] == "preopen")
        query_check("feature_observation_session_offset", """select count(*) from features f
            left join calendar obs on obs.date=f.observation_date
            left join calendar decision on decision.date=f.decision_date
            where obs.date is null or decision.date is null or decision.session_index-obs.session_index != ?
               or f.decision_time is distinct from decision.decision_time""", [offset])
        query_check("feature_source_rows_present", """select count(*) from features f left join panel p
            on p.permno=f.permno and p.date=f.observation_date where p.permno is null""")
        query_check("no_terminal_observations_in_features", """select count(*) from features f join panel p
            on p.permno=f.permno and p.date=f.observation_date where p.delist_flag is distinct from 'N'""")
        query_check("feature_values_preserved", """select count(*) from features f join panel p
            on p.permno=f.permno and p.date=f.observation_date
            where f.price_raw is distinct from p.price_raw or f.price_flag is distinct from p.price_flag
               or f.observed_total_return is distinct from p.ret_total_for_signal""")
        query_check("evaluation_dates_only", "select count(*) from evaluation where decision_date < ?::date or decision_date > ?::date", [plan["eval_start"], plan["eval_end"]])
        query_check("evaluation_exact_feature_subset", """select count(*) from (
            (select * from evaluation except all select * from features where decision_date between ?::date and ?::date)
            union all
            (select * from features where decision_date between ?::date and ?::date except all select * from evaluation))""",
            [plan["eval_start"], plan["eval_end"], plan["eval_start"], plan["eval_end"]])
        query_check("preparation_rows_retained", "select count(*) from panel where date < ?::date or date > ?::date", [plan["process_start"], plan["eval_end"]])
        query_check("training_labels_observed_and_available", """select count(*) from training
            where label_end_time is null or label_available_at is null
               or label_end_time > ?::timestamptz or label_available_at > ?::timestamptz
               or forward_vendor_return is null or not isfinite(forward_vendor_return)""", [cutoff, cutoff])
        if plan.get("train_start") is None:
            check("training_disabled_is_empty", report["counts"]["training"])
        else:
            query_check("training_decision_dates_only", "select count(*) from training where decision_date < ?::date or decision_date > ?::date", [plan["train_start"], plan["train_end"]])
        query_check("training_label_values_preserved", """select count(*) from training t left join labels l using(permno,decision_date)
            where l.permno is null or t.forward_vendor_return is distinct from l.forward_vendor_return
               or t.label_end_time is distinct from l.label_end_time or t.label_available_at is distinct from l.available_at""")
        query_check("vendor_return_preserved", "select count(*) from panel where ret_total_vendor is distinct from ret_total")
        query_check("valid_returns_match_vendor_once", """select count(*) from panel where return_quality='VALID_VENDOR_RETURN'
            and (ret_total_backtest is distinct from ret_total or ret_total is null or not isfinite(ret_total)
                 or ret_total < -1 or ret_missing_flag is distinct from 'NA')""")
        query_check("unresolved_returns_not_zero_filled", "select count(*) from panel where return_quality!='VALID_VENDOR_RETURN' and ret_total_backtest is not null")
        query_check("terminal_outcomes_not_signal_returns", "select count(*) from panel where delist_flag='Y' and ret_total_for_signal is not null")
        query_check("cash_share_event_interface_fails_closed", """select count(*) from panel
            where (delist_flag='Y' or distribution_return_flag not in ('NO','NA') or distribution_return_flag is null)
              and (ledger_status is distinct from 'unsupported' or event_terms_incomplete is distinct from true)""")
        query_check("terminal_event_return_equality", """select count(*) from panel where delist_flag='Y'
            and terminal_event_date is not null and ret_total is distinct from delisting_return""")
        query_check("terminal_event_one_storage_row", """select count(*) from (select permno,terminal_event_date from panel
            where delist_flag='Y' and terminal_event_date is not null group by all having count(*) != 1)""")
        query_check("valid_terminal_requires_event", """select count(*) from panel where delist_flag='Y'
            and return_quality='VALID_VENDOR_RETURN' and (terminal_event_date is null or terminal_event_date>=date)""")
        query_check("events_preserve_vendor_outcomes", """select count(*) from events e left join panel p
            on p.permno=e.permno and p.date=e.observed_date
            where p.permno is null or e.ret_total_vendor is distinct from p.ret_total_vendor
               or e.ret_total_backtest is distinct from p.ret_total_backtest""")
        query_check("display_base_date_matches_plan", "select count(*) from display where index_base_date is distinct from ?::date", [plan["index_base_date"]])
        query_check("display_zero_missing_base_is_null", """select count(*) from display
            where (base_wealth is null or base_wealth<=0 or not isfinite(base_wealth)) and index_base100 is not null""")
        query_check("display_formula", """select count(*) from display where base_wealth>0 and isfinite(base_wealth)
            and ((wealth_index is null and index_base100 is not null)
              or (wealth_index is not null and (index_base100 is null or not isfinite(index_base100)
                  or abs(index_base100 - 100*wealth_index/base_wealth)>1e-9*(1+abs(100*wealth_index/base_wealth)))))""")
        query_check("display_exact_base_is_100", """select count(*) from display where date=index_base_date
            and base_wealth>0 and isfinite(base_wealth) and (index_base100 is null or abs(index_base100-100)>1e-9)""")
        query_check("display_base_value_matches_observation", """select count(*) from display d left join display b
            on d.permno=b.permno and b.date=d.index_base_date where d.base_wealth is distinct from b.wealth_index""")
        report["counts"].update(dict(con.execute("""select 'terminal_rows',count(*) from panel where delist_flag='Y'
            union all select 'terminal_without_event',count(*) from panel where delist_flag='Y' and terminal_event_date is null
            union all select 'unresolved_return_rows',count(*) from panel where return_quality!='VALID_VENDOR_RETURN'
            union all select 'preparation_rows',count(*) from panel where date<?::date
            union all select 'first_evaluation_eligible',count(*) from evaluation where decision_date=?::date and eligible_for_research""", [plan["eval_start"], plan["eval_start"]]).fetchall()))
        check("manifest_unresolved_count", int(report["counts"]["unresolved_return_rows"] != manifest["unresolved_return_rows"]))
        if data_root is not None:
            data_root = data_root.resolve()
            start, end = datetime.fromisoformat(plan["process_start"]).date(), datetime.fromisoformat(plan["eval_end"]).date()
            paths = []
            year, month = start.year, start.month
            while (year, month) <= (end.year, end.month):
                path = data_root / f"normalized/crsp/daily/year={year}/month={month:02d}/part-000.parquet"
                if not path.is_file():
                    raise FileNotFoundError(f"Missing requested normalized input: {path}")
                paths.append(str(path))
                year, month = (year+1, 1) if month == 12 else (year, month+1)
            con.read_parquet(paths, hive_partitioning=False).create_view("source_daily")
            names = ",".join(SOURCE_COLUMNS)
            query_check("source_normalized_rows_and_fields_preserved", f"""select count(*) from (
                (select {names} from panel except all select {names} from source_daily where date between ?::date and ?::date)
                union all
                (select {names} from source_daily where date between ?::date and ?::date except all select {names} from panel))""",
                [start,end,start,end])
            source_events = data_root / "normalized/crsp/delistings/part-000.parquet"
            if not source_events.is_file():
                raise FileNotFoundError(f"Missing normalized event input: {source_events}")
            con.read_parquet(str(source_events), hive_partitioning=False).create_view("source_events")
            query_check("source_terminal_evidence_preserved", """select count(*) from panel p
                left join source_events e on p.permno=e.permno and p.terminal_event_date=e.event_date
                where p.delist_flag='Y' and p.terminal_event_date is not null
                  and (e.permno is null or p.delisting_return is distinct from e.delisting_return
                       or p.action_type is distinct from e.action_type or p.status_type is distinct from e.status_type
                       or p.reason_type is distinct from e.reason_type or p.payment_type is distinct from e.payment_type
                       or p.successor_permno is distinct from e.successor_permno)""")
        else:
            report["unperformed"].append("Current normalized-input reconciliation: supply --data-root")
        report["passed"] = all(item["passed"] for item in report["checks"].values())
        return report
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="Published run directory containing _SUCCESS.json")
    parser.add_argument("--data-root", type=Path, help="Also compare all preserved normalized fields for the loaded interval")
    parser.add_argument("--output", type=Path, help="Separate JSON validation report, never inside the published run")
    args = parser.parse_args()
    output = (args.output or ROOT / "data/derived/research_validation" / f"run_{args.run.name}.json").resolve()
    run_path = args.run.resolve()
    if output == run_path or run_path in output.parents:
        parser.error("Validation reports must be outside the published run directory")
    for protected in ((args.data_root or ROOT / "data").resolve() / "raw",
                      (args.data_root or ROOT / "data").resolve() / "normalized"):
        if output == protected or protected in output.parents:
            parser.error("Validation report must not overwrite RAW/NORMALIZED inputs")
    if output.suffix != ".json" or output.name == "_SUCCESS.json":
        parser.error("Use a separate .json report, not an artifact or _SUCCESS marker")
    try:
        report = validate(run_path, args.data_root)
    except (ValueError, RuntimeError, OSError, KeyError, duckdb.Error) as error:
        parser.exit(2, f"Run validation could not complete: {error}\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                      "failed": [name for name,item in report["checks"].items() if not item["passed"]],
                      "counts": report["counts"], "report": str(output)}, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
