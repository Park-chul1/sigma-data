"""Read-only CRSP input audit; local vendor samples stay under ignored data/.

No WRDS connection, normalization, or source mutation is performed. The default
reads every partition's footer/manifest, plus three bounded monthly samples.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]


def records(con, sql, params=None):
    result = con.execute(sql, params or [])
    names = [item[0] for item in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def inspect(data_root: Path, calendar_check: bool = False,
            full_terminal_check: bool = False) -> dict:
    con = duckdb.connect()
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(),
              "audit_version": 1, "duckdb_version": duckdb.__version__,
              "scope": "all canonical manifests/footers; bounded monthly value checks",
              "checks": {}, "datasets": {}}
    checks = report["checks"]
    paths = {}
    for layer, name, relative in [
        ("normalized", "daily", "crsp/daily/year=*/month=*/part-000.parquet"),
        ("normalized", "security_info", "crsp/security_info/part-000.parquet"),
        ("normalized", "delistings", "crsp/delistings/part-000.parquet"),
        ("raw", "daily", "crsp/stkdlysecurityprimarydata/year=*/month=*/part-000.parquet"),
        ("raw", "security_info", "crsp/stksecurityinfohist/part-000.parquet"),
        ("raw", "delistings", "crsp/stkdelists/part-000.parquet"),
    ]:
        key = f"{layer}_{name}"
        files = sorted((data_root / layer).glob(relative))
        if not files:
            raise FileNotFoundError(f"No canonical {key} files under {data_root}")
        paths[key] = files
        manifests = []
        identity = hashlib.sha256()
        metadata_rows = 0
        mismatch = []
        for path in files:
            marker = path.with_name("_SUCCESS.json")
            blob = marker.read_bytes()
            manifest = json.loads(blob)
            manifests.append(manifest)
            row_count = con.execute("select num_rows from parquet_file_metadata(?)", [str(path)]).fetchone()[0]
            metadata_rows += row_count
            stat = path.stat()
            identity.update(str(path.relative_to(data_root)).encode())
            identity.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
            identity.update(blob)
            if manifest["rows"] != row_count:
                mismatch.append(str(path.relative_to(data_root)))
        columns = records(con, "describe select * from read_parquet(?, hive_partitioning=false)", [str(files[0])])
        report["datasets"][key] = {
            "partitions": len(files), "footer_rows": metadata_rows,
            "manifest_rows": sum(m["rows"] for m in manifests),
            "min_date": min((m["min_date"] for m in manifests if "min_date" in m), default=None),
            "max_date": max((m["max_date"] for m in manifests if "max_date" in m), default=None),
            "source_tables": sorted(set(m["source_table"] for m in manifests if "source_table" in m)),
            "source_version": "vendor release not recorded",
            "metadata_identity_sha256": identity.hexdigest(),
            "identity_scope": "paths, sizes, mtimes and manifests; not full content hash",
            "columns": [{"name": row["column_name"], "type": row["column_type"]} for row in columns],
            "manifest_footer_mismatch": mismatch,
        }
        checks[f"{key}_manifest_footer_counts"] = not mismatch
    for name in ["security_info", "delistings"]:
        con.read_parquet(str(paths[f"normalized_{name}"][0]), hive_partitioning=False).create_view(name)
    report["delistings"] = records(con, """
        select count(*) as row_count, count(distinct permno) securities,
               min(event_date) min_date, max(event_date) max_date,
               count(*) - count(distinct (permno,event_date)) duplicate_keys,
               count(*) filter(where delisting_return is null) missing_returns,
               count(*) filter(where delisting_return = 0) zero_returns,
               count(*) filter(where delisting_return = -1) total_losses,
               count(*) filter(where delisting_return < -1 or not isfinite(delisting_return)) invalid_returns
        from delistings""")[0]
    checks["delisting_keys_unique"] = report["delistings"]["duplicate_keys"] == 0
    report["history"] = records(con, """
        select count(*) as row_count, count(distinct permno) securities,
               min(valid_from) min_effective_date, max(valid_to) max_effective_date,
               count(*) filter(where valid_to < valid_from) invalid_intervals
        from security_info""")[0]
    overlap = con.execute("""with h as (
        select *, lag(coalesce(valid_to,date '9999-12-31')) over(partition by permno order by valid_from) previous_end
        from security_info)
        select count(*) from h where previous_end >= valid_from""").fetchone()[0]
    checks["history_nonoverlap"] = overlap == 0
    report["history"]["overlapping_intervals"] = overlap
    report["samples"] = {}
    cast_columns = """cast(permno as bigint) permno, cast(dlycaldt as date) date,
        cast(dlyprc as double) price_raw, cast(dlycap as double) market_cap_kusd,
        cast(dlyret as double) ret_total, cast(dlyretx as double) ret_ex_div,
        cast(dlyvol as double) volume_raw_shares, cast(dlydelflg as varchar) delist_flag,
        cast(dlyprcflg as varchar) price_flag, cast(dlycapflg as varchar) market_cap_flag,
        cast(dlyretmissflg as varchar) ret_missing_flag,
        cast(dlydistretflg as varchar) distribution_return_flag"""
    for year, month in [(2019, 1), (2020, 8), (2022, 6)]:
        partition = f"year={year}/month={month:02d}/part-000.parquet"
        norm = data_root / "normalized/crsp/daily" / partition
        raw = data_root / "raw/crsp/stkdlysecurityprimarydata" / partition
        key = f"{year}-{month:02d}"
        if not norm.exists() or not raw.exists():
            report["samples"][key] = {"status": "unavailable", "reason": "fixture month absent"}
            continue
        table = f"daily_{year}_{month:02d}"
        con.read_parquet(str(norm), hive_partitioning=False).create_view(table)
        con.read_parquet(str(raw), hive_partitioning=False).create_view("raw_sample", replace=True)
        stat = records(con, f"""
            select count(*) as row_count, count(distinct permno) securities,
                   min(date) min_date, max(date) max_date,
                   count(*) - count(distinct (permno,date)) duplicate_keys,
                   count(*) filter(where permno is null or date is null) null_keys,
                   count(*) filter(where ret_total is null) missing_returns,
                   count(*) filter(where ret_total = 0) zero_returns,
                   count(*) filter(where ret_total < -1 or not isfinite(ret_total)) invalid_returns,
                   count(*) filter(where ret_total is not null and ret_missing_flag != 'NA') numeric_return_with_warning
            from {table}""")[0]
        diff = con.execute(f"""select count(*) from (
            (select * from {table} except all select {cast_columns} from raw_sample)
            union all
            (select {cast_columns} from raw_sample except all select * from {table})
        )""").fetchone()[0]
        stat["raw_normalized_differences"] = diff
        stat["return_missing_flags"] = records(con, f"select ret_missing_flag, count(*) as row_count from {table} group by all order by 1")
        stat["distribution_flags"] = records(con, f"select distribution_return_flag, count(*) as row_count from {table} group by all order by 1")
        report["samples"][key] = stat
        checks[f"{key}_keys_valid"] = stat["duplicate_keys"] == stat["null_keys"] == 0
        checks[f"{key}_raw_preserved"] = diff == 0
        checks[f"{key}_return_domain"] = stat["invalid_returns"] == 0
    cases = report["event_cases"] = {}
    if "raw_normalized_differences" in report["samples"]["2020-08"]:
        # Terms are independently documented in Apple's 2020-07-30 release;
        # they are validation evidence, not inferred ingestion/event inputs.
        cases["apple"] = records(con, """select permno,date,price_raw,ret_total,ret_ex_div,distribution_return_flag
            from daily_2020_08 where permno=14593 and date in ('2020-08-06','2020-08-07','2020-08-28','2020-08-31') order by date""")
        apple = {str(x["date"]): x for x in cases["apple"]}
        if len(apple) == 4:
            split_expected = 4 * apple["2020-08-31"]["price_raw"] / apple["2020-08-28"]["price_raw"] - 1
            dividend_expected = (apple["2020-08-07"]["price_raw"] + .82) / apple["2020-08-06"]["price_raw"] - 1
            checks["apple_split_in_vendor_return"] = abs(split_expected-apple["2020-08-31"]["ret_total"]) <= 0.00000051
            checks["apple_dividend_once_in_vendor_return"] = abs(dividend_expected-apple["2020-08-07"]["ret_total"]) <= 0.00000051
            cases["apple_validation_terms"] = {"split_ratio": 4, "cash_dividend": .82,
                "source": "https://www.apple.com/uk/newsroom/2020/07/apple-reports-third-quarter-results/",
                "note": "Contemporaneous issuer terms used for this check only; terms absent from normalized extract."}
        cases["august_terminal_reconciliation"] = records(con, """select count(*) terminal_rows,
            count(*) filter(where e.permno is null) without_event,
            count(*) filter(where d.ret_total is distinct from e.delisting_return) value_mismatches
            from daily_2020_08 d left join delistings e using(permno) where d.delist_flag='Y'""")[0]
        checks["august_terminal_return_embedded"] = cases["august_terminal_reconciliation"]["value_mismatches"] == cases["august_terminal_reconciliation"]["without_event"] == 0
        cases["numeric_terminal_warning"] = records(con, """select permno,date,ret_total,ret_missing_flag,distribution_return_flag
            from daily_2020_08 where delist_flag='Y' and ret_total is not null and ret_missing_flag != 'NA'""")
    if "raw_normalized_differences" in report["samples"]["2019-01"]:
        cases["missing_terminal"] = records(con, """select d.permno,d.date,d.ret_total,d.ret_missing_flag,e.event_date,e.delisting_return
            from daily_2019_01 d join delistings e using(permno) where d.permno=16925 and d.delist_flag='Y'""")
        checks["missing_terminal_stays_missing"] = bool(cases["missing_terminal"]) and all(x["ret_total"] is None and x["delisting_return"] is None for x in cases["missing_terminal"])
    if "raw_normalized_differences" in report["samples"]["2022-06"]:
        cases["ticker_change"] = records(con, """select d.permno,d.date,h.ticker,d.ret_total
            from daily_2022_06 d left join security_info h
            on d.permno=h.permno and d.date between h.valid_from and coalesce(h.valid_to,date '9999-12-31')
            where d.permno=13407 and d.date between '2022-06-08' and '2022-06-10' order by d.date""")
        checks["ticker_change_stable_id"] = [x["ticker"] for x in cases["ticker_change"]] == ["FB", "META", "META"]
    if calendar_check or full_terminal_check:
        import exchange_calendars as xc
        con.read_parquet([str(p) for p in paths["normalized_daily"]], hive_partitioning=False).create_view("all_daily")
        observed = [row[0] for row in con.execute("select distinct date from all_daily order by date").fetchall()]
        cal = xc.get_calendar("XNYS", start=str(observed[0]), end=str(observed[-1]))
        sessions = [d.date() for d in cal.sessions_in_range(str(observed[0]), str(observed[-1]))]
        report["calendar"] = {"library": "exchange_calendars", "version": xc.__version__,
            "name": "XNYS", "observed_sessions": len(observed), "expected_sessions": len(sessions),
            "missing_sessions": sorted(set(sessions)-set(observed)),
            "unexpected_sessions": sorted(set(observed)-set(sessions)),
            "carter_funeral_2025_01_09_observed": date(2025,1,9) in observed,
            "scope": "session presence, not per-security membership/source completeness"}
        checks["calendar_sessions_match"] = set(observed) == set(sessions)
        if full_terminal_check:
            con.execute("create temp table sessions(date date)")
            con.executemany("insert into sessions values (?)", [(d,) for d in sessions])
            report["full_terminal_reconciliation"] = records(con, """with expected as(
                select e.*, (select min(date) from sessions where date > e.event_date) return_date from delistings e
                ), actual as(select * from all_daily where delist_flag='Y')
                select count(*) terminal_rows,
                    count(*) filter(where e.permno is null) without_event,
                    count(*) filter(where e.permno is not null and d.ret_total is distinct from e.delisting_return) value_mismatches,
                    count(*) filter(where e.permno is not null and d.date != e.return_date) storage_date_mismatches,
                    count(*) filter(where d.ret_total is null) missing_returns,
                    count(*) filter(where d.ret_total is not null and d.ret_missing_flag != 'NA') numeric_returns_with_warning
                from actual d left join expected e using(permno)""")[0]
            x = report["full_terminal_reconciliation"]
            checks["full_matched_terminal_value_and_date"] = x["value_mismatches"] == x["storage_date_mismatches"] == 0
    report["unperformed"] = ["vendor release reconciliation and live WRDS checks",
        "historical availability/vintage verification", "share/cash ledger payoff reconstruction",
        "complete distribution-term verification beyond issuer-grounded Apple cases"]
    report["passed"] = all(checks.values())
    con.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "data")
    parser.add_argument("--output", type=Path, default=ROOT / "data/derived/research_validation/input_audit.json")
    parser.add_argument("--calendar", action="store_true", help="Scan date column across all daily partitions")
    parser.add_argument("--full-terminal-check", action="store_true", help="Also scan terminal rows across all partitions")
    args = parser.parse_args()
    report = inspect(args.data_root.resolve(), args.calendar, args.full_terminal_check)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                      "failed": [k for k, v in report["checks"].items() if not v], "report": str(args.output)}, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
