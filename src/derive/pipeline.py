"""Row-preserving, approximate-PIT research exports from the canonical CIZ snapshot.

The common monthly panel contains outcomes and is NOT a strategy feature table.
Only features.parquet/evaluation_features.parquet are strategy inputs. No orders,
fills, portfolio simulation, reconstructed cash, or legacy RET/DLRET combination.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import duckdb

from .catalog import (ContentHashes, NormalizedCatalog, canonical_json, code_identity,
                      digest, parquet_sql, sql_literal as q, verify_artifacts, write_json)
from .temporal import SessionCalendar, calendar_schedule, plan_research_window
from .events import DISTRIBUTION_EFFECT_MAP, EVENT_ACTION_MAP, EVENT_POLICY_VERSION, SUPPORTED_TERMINAL_ACTIONS


RULES_VERSION = "sigma-ciz-research-1"
REPO = Path(__file__).resolve().parents[2]
DAILY_COLUMNS = {"permno", "date", "price_raw", "market_cap_kusd", "ret_total", "ret_ex_div",
                 "volume_raw_shares", "delist_flag", "price_flag", "market_cap_flag",
                 "ret_missing_flag", "distribution_return_flag"}
INFO_COLUMNS = {"permno", "valid_from", "valid_to", "ticker", "cusip", "primary_exchange",
                "share_type", "security_type", "security_subtype", "us_incorporated_flag",
                "issuer_type", "trading_status_flag", "conditional_type", "share_class"}
DELIST_COLUMNS = {"permno", "event_date", "delisting_return", "action_type", "status_type",
                  "reason_type", "payment_type", "successor_permno", "successor_permco"}
KNOWN_DIST = tuple(DISTRIBUTION_EFFECT_MAP)
EVENT_CAPABILITIES = {
    "split_reverse_split_stock_dividend": "Vendor-return-only; subtype/ratio and quantity posting unsupported",
    "cash_dividend": "Vendor-return-only; amount and payment-time cash posting unsupported",
    "ticker_change": "Effective-dated PERMNO continuity; approximate metadata availability",
    "name_change": "Unsupported: security name absent in normalized history",
    "merger_share_exchange": "Category from verified CIZ action code; no successor auto-link or share exchange",
    "spin_off_rights": "Unsupported: individual event type/terms/target security unavailable",
    "delisting_recovery": "Matched embedded vendor return once; unknown payout time excluded from features/labels",
}
LIMITATIONS = [
    "Approximate PIT only: modern vendor snapshot, no historical revision vintages/publication timestamps.",
    "Daily values and effective security metadata assumed available after configured session lag; not observed publication times.",
    "Terminal payouts have unknown information/cash availability and are excluded from features and training labels.",
    "Vendor daily return intervals; previous-price date/duration unavailable. No next-open, open-to-open, or open-to-close fills.",
    "Forward labels are statistical vendor-return outcomes, not executable after-close entry returns.",
    "No distribution ratios/payment dates/target securities: stock/cash ledger unsupported; numeric vendor total returns used once.",
    "XNYS is the shared session-calendar assumption for the CRSP US exchange universe; no vendor calendar ingested.",
    "No fundamentals, accounting-vintage joins, model training, portfolio positions, or strategy-performance engine.",
    "Mature usable-label training rows can have outcome-dependent missingness; training coverage is not certified unbiased.",
]


def _require_schema(con, relation, columns):
    found = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
    if not columns <= found:
        raise RuntimeError(f"Required CIZ schema fields missing: {sorted(columns - found)}")


def _count(con, query):
    return con.execute(query).fetchone()[0]


def _copy(con, query, path):
    con.execute(f"COPY ({query}) TO {q(path)} (FORMAT PARQUET, COMPRESSION ZSTD)")


def _publish(stage, final, manifest, hashes):
    manifest["artifacts"] = {p.name: hashes.file(p) for p in sorted(stage.glob("*.parquet"))}
    write_json(stage / "_SUCCESS.json", manifest)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        raise RuntimeError(f"Refusing to replace an existing result: {final}")
    os.rename(stage, final)
    hashes.save()


def _load_reference_tables(con, catalog, schedule):
    con.execute("CREATE TEMP TABLE calendar(date DATE, session_index BIGINT, open_at TIMESTAMPTZ, "
                "close_at TIMESTAMPTZ, decision_time TIMESTAMPTZ, available_at TIMESTAMPTZ)")
    con.executemany("INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?)", [
        (r["date"], r["session_index"], r["open_at"], r["close_at"], r["decision_time"], r["available_at"])
        for r in schedule])
    info_sql = parquet_sql([catalog.info])
    del_sql = parquet_sql([catalog.delistings])
    _require_schema(con, info_sql, INFO_COLUMNS)
    _require_schema(con, del_sql, DELIST_COLUMNS)
    con.execute(f"CREATE TEMP TABLE history AS SELECT * FROM {info_sql}")
    con.execute(f"CREATE TEMP TABLE delistings AS SELECT * FROM {del_sql}")
    if _count(con, "SELECT count(*) FROM history WHERE permno IS NULL OR valid_from IS NULL OR valid_to < valid_from"):
        raise RuntimeError("Invalid security-history keys/intervals")
    if _count(con, "SELECT count(*) FROM (SELECT permno,event_date FROM delistings GROUP BY ALL HAVING count(*)>1)"):
        raise RuntimeError("Duplicate delisting event keys")
    if _count(con, "SELECT count(*) FROM delistings WHERE permno IS NULL OR event_date IS NULL"):
        raise RuntimeError("NULL delisting keys")
    # This is an explicitly checked CIZ storage-date convention, not nearest-event matching.
    # The missing original DelDlyDt remains an evidence limitation in each record.
    earliest_event, latest_event = con.execute("SELECT min(event_date),max(event_date) FROM delistings").fetchone()
    con.execute("CREATE TEMP TABLE event_calendar(date DATE)")
    if earliest_event is not None:
        event_calendar = SessionCalendar(earliest_event-timedelta(days=10),latest_event+timedelta(days=30))
        con.executemany("INSERT INTO event_calendar VALUES (?)",[(d,) for d in event_calendar.sessions])
    con.execute("""CREATE TEMP TABLE terminal_events AS
        SELECT e.*, (SELECT min(date) FROM event_calendar WHERE date > e.event_date) AS return_storage_date
        FROM delistings e""")


def _build_month(con, catalog, key, folder, context):
    path, source_manifest = catalog.months[key]
    relation = parquet_sql([path])
    _require_schema(con, relation, DAILY_COLUMNS)
    con.execute(f"CREATE OR REPLACE TEMP TABLE daily AS SELECT * FROM {relation}")
    rows = _count(con, "SELECT count(*) FROM daily")
    if rows != source_manifest["rows"]:
        raise RuntimeError(f"Manifest row count mismatch: {path}")
    if _count(con, "SELECT count(*) FROM daily WHERE permno IS NULL OR date IS NULL"):
        raise RuntimeError(f"NULL daily key: {path}")
    if _count(con, "SELECT count(*) FROM (SELECT permno,date FROM daily GROUP BY ALL HAVING count(*)>1)"):
        raise RuntimeError(f"Duplicate daily key: {path}")
    year, month = key
    if _count(con, f"SELECT count(*) FROM daily WHERE year(date)!={year} OR month(date)!={month}"):
        raise RuntimeError(f"Wrong monthly partition: {path}")
    actual_bounds = con.execute("SELECT min(date)::VARCHAR,max(date)::VARCHAR FROM daily").fetchone()
    if actual_bounds != (source_manifest["min_date"], source_manifest["max_date"]):
        raise RuntimeError(f"Manifest coverage mismatch: {path}")
    missing = con.execute(f"""SELECT date FROM calendar WHERE year(date)={year} AND month(date)={month}
        AND date BETWEEN {q(catalog.source_start)} AND {q(catalog.source_end)}
        EXCEPT SELECT date FROM daily""").fetchall()
    extra = con.execute("SELECT DISTINCT date FROM daily EXCEPT SELECT date FROM calendar").fetchall()
    if missing or extra:
        raise RuntimeError(f"Calendar coverage mismatch for {key}: missing={missing}, extra={extra}")
    known_dist = ",".join(q(v) for v in KNOWN_DIST)
    supported_terminal = ",".join(q(v) for v in sorted(SUPPORTED_TERMINAL_ACTIONS))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE joined AS
      SELECT d.*, c.session_index, c.available_at,
        h.ticker AS ticker_asof, h.cusip AS cusip_asof, h.primary_exchange,
        h.share_type,h.security_type,h.security_subtype,h.us_incorporated_flag,
        h.issuer_type,h.trading_status_flag,h.conditional_type,h.share_class,
        h.valid_from AS metadata_effective_from,
        h.permno IS NOT NULL AS has_metadata,
        e.event_date AS terminal_event_date,e.delisting_return,
        e.action_type,e.status_type,e.reason_type,e.payment_type,e.successor_permno,
        CASE WHEN d.ret_total IS NULL THEN 'MISSING_RETURN'
             WHEN NOT isfinite(d.ret_total) OR d.ret_total < -1 THEN 'INVALID_RETURN'
             WHEN d.ret_missing_flag IS DISTINCT FROM 'NA' THEN 'INCOMPLETE_VENDOR_RETURN'
             WHEN d.distribution_return_flag IS NULL OR d.distribution_return_flag NOT IN ({known_dist})
                 THEN 'UNKNOWN_DISTRIBUTION_FLAG'
             WHEN d.delist_flag NOT IN ('Y','N') OR d.delist_flag IS NULL THEN 'UNKNOWN_DELIST_FLAG'
             WHEN d.delist_flag='Y' AND e.permno IS NULL THEN 'UNMATCHED_TERMINAL_EVENT'
             WHEN d.delist_flag='Y' AND d.ret_total IS DISTINCT FROM e.delisting_return THEN 'TERMINAL_RETURN_MISMATCH'
             WHEN d.delist_flag='Y' AND (e.action_type IS NULL OR e.action_type NOT IN ({supported_terminal}))
                 THEN 'UNSUPPORTED_TERMINAL_EVENT'
             ELSE 'VALID_VENDOR_RETURN' END AS return_quality,
        CASE WHEN h.permno IS NOT NULL THEN 'EFFECTIVE_INTERVAL_APPROXIMATE_PIT'
             WHEN d.delist_flag='Y' THEN 'TERMINAL_OUTSIDE_METADATA'
             ELSE 'MISSING_METADATA' END AS metadata_quality,
        coalesce(h.share_type='NS' AND h.security_type='EQTY' AND h.security_subtype='COM'
          AND h.us_incorporated_flag='Y' AND h.issuer_type IN ('ACOR','CORP')
          AND h.primary_exchange IN ('N','A','Q') AND h.trading_status_flag='A'
          AND h.conditional_type='RW',false) AS baseline_eligible_observed
      FROM daily d
      JOIN calendar c ON c.date=d.date
      LEFT JOIN history h ON d.permno=h.permno
        AND d.date >= h.valid_from AND d.date <= coalesce(h.valid_to, DATE '9999-12-31')
      LEFT JOIN terminal_events e ON d.delist_flag='Y' AND d.permno=e.permno
        AND d.date=e.return_storage_date
    """)
    if _count(con, "SELECT count(*) FROM joined") != rows:
        raise RuntimeError(f"Ambiguous history/event match changed daily row count: {key}")
    con.execute("""CREATE OR REPLACE TEMP TABLE panel AS
      SELECT *, ret_total AS ret_total_vendor,
        CASE WHEN return_quality='VALID_VENDOR_RETURN' THEN ret_total END AS ret_total_backtest,
        CASE WHEN return_quality='VALID_VENDOR_RETURN' AND delist_flag='N' THEN ret_total END AS ret_total_for_signal,
        CASE WHEN delist_flag='Y' THEN 'CRSP_DELIST_EMBEDDED' ELSE 'CRSP_DAILY' END AS return_source,
        false AS return_was_reconstructed,
        delist_flag='Y' AS is_delisting_return, delist_flag='Y' AS is_eventual_outcome,
        delist_flag='Y' OR distribution_return_flag NOT IN ('NO','NA') OR distribution_return_flag IS NULL AS event_terms_incomplete,
        CASE WHEN delist_flag='Y' OR distribution_return_flag NOT IN ('NO','NA') OR distribution_return_flag IS NULL
             THEN 'unsupported' ELSE 'no_posting_required' END AS ledger_status,
        'ASSUMED_SESSION_LAG_NOT_HISTORICAL_VINTAGE' AS availability_basis,
        CASE WHEN delist_flag='Y' THEN NULL::TIMESTAMPTZ ELSE available_at END AS return_available_at,
        return_quality!='VALID_VENDOR_RETURN' OR NOT has_metadata AS requires_review,
        'UNSUPPORTED_MISSING_OPEN_AND_EXECUTION_MODEL' AS execution_status
      FROM joined""")
    _copy(con, "SELECT * FROM panel ORDER BY permno,date", folder / "panel.parquet")
    # Event evidence is separate from strategy inputs. Codes describe economic categories,
    # but do not supply ratios, amounts, successor shares, or announcement timestamps.
    action_case = " ".join(f"WHEN action_type={q(code)} THEN {q(effect)}" for code,effect in EVENT_ACTION_MAP.items())
    distribution_case = " ".join(f"WHEN distribution_return_flag={q(code)} THEN {q('|'.join(effects) or 'none')}"
                                 for code,effects in DISTRIBUTION_EFFECT_MAP.items())
    _copy(con, f"""SELECT permno,date AS observed_date,
        terminal_event_date AS effective_date,
        CASE WHEN terminal_event_date IS NOT NULL THEN 'SOURCE_DELISTING_DATE'
             ELSE 'UNKNOWN_DAILY_IMPACT_DATE_ONLY' END AS effective_date_basis,
        NULL::TIMESTAMPTZ AS announced_at,
        CASE WHEN delist_flag='Y' THEN NULL::TIMESTAMPTZ ELSE available_at END AS available_at,
        'DAILY_CIZ_FLAGS' AS event_source, distribution_return_flag AS source_code,
        action_type,status_type,reason_type,payment_type,successor_permno,
        CASE {action_case}
             WHEN delist_flag='Y' THEN 'unknown_terminal_action'
             {distribution_case}
             ELSE 'unknown' END AS effect,
        CASE WHEN return_quality='VALID_VENDOR_RETURN' THEN 'VENDOR_RETURN_ONCE'
             ELSE 'INCOMPLETE' END AS research_status,
        'UNSUPPORTED' AS ledger_status,
        CASE WHEN delist_flag='Y' THEN 'Missing DelDlyDt, amount/payment dates and successor terms; next-session mapping checked against return'
             ELSE 'Missing event ratio, cash amount, ex/payment dates and target security; do not infer from price changes' END AS reason,
        ret_total_vendor,ret_total_backtest,return_quality
      FROM panel WHERE delist_flag='Y' OR distribution_return_flag NOT IN ('NO','NA')
        OR distribution_return_flag IS NULL
      ORDER BY permno,observed_date""", folder / "events.parquet")
    quality = dict(con.execute("SELECT return_quality,count(*) FROM panel GROUP BY 1").fetchall())
    metadata = dict(con.execute("SELECT metadata_quality,count(*) FROM panel GROUP BY 1").fetchall())
    return {**context, "rows": rows, "month": f"{year}-{month:02d}",
            "return_quality": quality, "metadata_quality": metadata,
            "pit_guarantee": "approximate", "source_path": str(path.relative_to(catalog.root))}


def _factor_table(con, sql, folder, context, end, hashes, validate):
    """Reuse a causally calculated feature history covering this endpoint.

    The dependency anchor is part of the key; display base/evaluation end are
    not. Longer exports or an earlier display base do not contaminate inputs.
    """
    folder.mkdir(parents=True,exist_ok=True)
    candidates = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name>=str(end))
    if candidates:
        candidate = candidates[0]
        saved = verify_artifacts(candidate,hashes)
        if saved["context"] != context:
            raise RuntimeError("Factor cache identity mismatch")
        con.execute(f"CREATE TEMP TABLE factor_observations AS SELECT * FROM {parquet_sql([candidate/'factors.parquet'])} WHERE date<={q(end)}")
        return True
    con.execute(sql)
    stage = Path(tempfile.mkdtemp(prefix=".stage-",dir=folder))
    try:
        _copy(con,"SELECT * FROM factor_observations ORDER BY permno,date",stage / "factors.parquet")
        validate()
        _publish(stage,folder / str(end),{"context":context,"coverage_end":str(end)},hashes)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return False


def _run_tables(con, config, plan, panel_paths, factor_folder, factor_context, hashes, validate):
    con.execute(f"""CREATE TEMP TABLE run_panel AS SELECT * FROM {parquet_sql(panel_paths)}
        WHERE date BETWEEN {q(plan.process_start)} AND {q(plan.eval_end)}""")
    if not _count(con, "SELECT count(*) FROM run_panel"):
        raise RuntimeError("Empty requested research range")
    lookback = config.factor_lookback_sessions
    horizon = config.label_horizon_sessions
    # Rolling products use log sums plus explicit NULL/zero tracking. NULLs are never
    # silently skipped and windows cannot bridge absent security sessions.
    factor_sql = f"""CREATE TEMP TABLE factor_observations AS
      WITH windows AS (
        SELECT *, count(*) OVER w AS window_rows,
          coalesce(bool_or(ret_total_backtest=-1 OR delist_flag='Y') OVER(
            PARTITION BY permno ORDER BY date ROWS UNBOUNDED PRECEDING),false) AS economic_series_ended,
          count(ret_total_for_signal) OVER w AS valid_rows,
          min(session_index) OVER w AS first_session,
          count(*) FILTER(WHERE ret_total_for_signal=-1) OVER w AS zero_count,
          sum(CASE WHEN ret_total_for_signal > -1 THEN ln(1+ret_total_for_signal) ELSE 0 END) OVER w AS log_product
        FROM run_panel
        WINDOW w AS (PARTITION BY permno ORDER BY date ROWS BETWEEN {lookback-1} PRECEDING AND CURRENT ROW)
      )
      SELECT *, CASE WHEN NOT economic_series_ended AND window_rows={lookback} AND valid_rows={lookback}
                     AND session_index-first_session={lookback-1}
                THEN CASE WHEN zero_count>0 THEN -1.0 ELSE exp(log_product)-1 END END AS momentum,
        NOT economic_series_ended AND window_rows={lookback} AND valid_rows={lookback} AND session_index-first_session={lookback-1} AS has_required_history
      FROM windows"""
    factors_reused = _factor_table(con,factor_sql,factor_folder,factor_context,plan.eval_end,hashes,validate)
    # Availability joins, not output-date filters, control what the strategy can see.
    # Delisting/event payload and future validity end are deliberately absent.
    con.execute(f"""CREATE TEMP TABLE features AS
      SELECT f.permno,c.date AS decision_date,c.decision_time,f.date AS observation_date,
        f.available_at, f.price_raw,f.price_flag,f.market_cap_kusd,f.market_cap_flag,f.volume_raw_shares,
        f.ret_missing_flag,f.return_quality,f.execution_status,
        f.ticker_asof,f.cusip_asof,f.primary_exchange,f.security_type,f.security_subtype,
        f.share_type,f.share_class,f.us_incorporated_flag,
        f.trading_status_flag,f.conditional_type,
        f.ret_total_for_signal AS observed_total_return,f.momentum,f.has_required_history,
        f.baseline_eligible_observed,
        f.baseline_eligible_observed AND f.has_required_history AS eligible_for_research,
        f.metadata_quality,f.availability_basis,
        'APPROXIMATE_PIT' AS pit_status
      FROM factor_observations f JOIN calendar c
        ON c.session_index=f.session_index+{config.observation_offset_sessions}
        AND c.decision_time >= f.available_at
      WHERE f.delist_flag='N'
    """)
    con.execute(f"DELETE FROM features WHERE decision_date>{q(plan.eval_end)}")
    if _count(con, "SELECT count(*) FROM features WHERE available_at>decision_time"):
        raise RuntimeError("Feature availability violation")
    # Labels are stored in a separate artifact. Terminal payouts are NOT assigned a
    # made-up availability timestamp; they cannot become model training targets.
    con.execute(f"""CREATE TEMP TABLE labels AS
      WITH windows AS (
        SELECT permno,date,session_index,
          count(*) OVER w AS n,count(ret_total_for_signal) OVER w AS n_valid,
          max(session_index) OVER w AS last_index,max(date) OVER w AS label_end_date,
          max(return_available_at) OVER w AS available_at,
          count(*) FILTER(WHERE ret_total_for_signal=-1) OVER w AS zeros,
          sum(CASE WHEN ret_total_for_signal > -1 THEN ln(1+ret_total_for_signal) ELSE 0 END) OVER w AS log_product
        FROM run_panel WINDOW w AS (PARTITION BY permno ORDER BY date ROWS BETWEEN 1 FOLLOWING AND {horizon} FOLLOWING)
      ) SELECT permno,w.date AS decision_date,label_end_date,c.close_at AS label_end_time,w.available_at,
          CASE WHEN n={horizon} AND n_valid={horizon} AND last_index-w.session_index={horizon}
               THEN CASE WHEN zeros>0 THEN -1.0 ELSE exp(log_product)-1 END END AS forward_vendor_return,
          CASE WHEN n<{horizon} THEN 'UNREALIZED_OR_TRUNCATED'
               WHEN n_valid<{horizon} THEN 'MISSING_OR_UNKNOWN_AVAILABILITY'
               WHEN last_index-w.session_index!={horizon} THEN 'SESSION_GAP'
               ELSE 'OBSERVED_APPROXIMATE_AVAILABILITY' END AS label_status
      FROM windows w LEFT JOIN calendar c ON c.date=w.label_end_date""")
    # Display-only index. The initial observed positive price anchors the interval;
    # its incoming return is not part of the subsequent wealth path.
    con.execute("""CREATE TEMP TABLE wealth AS
      WITH seed AS (
        SELECT *,min(CASE WHEN price_raw>0 AND isfinite(price_raw) AND delist_flag='N' THEN date END)
            OVER(PARTITION BY permno) AS seed_date,
          lag(session_index) OVER(PARTITION BY permno ORDER BY date) AS prev_session
        FROM run_panel
      ), paths AS (
        SELECT *,count(*) FILTER(WHERE date>seed_date AND
            (ret_total_backtest IS NULL OR session_index-prev_session!=1)) OVER w AS missing_count,
          count(*) FILTER(WHERE date>seed_date AND ret_total_backtest=-1) OVER w AS zero_count,
          sum(CASE WHEN date>seed_date AND ret_total_backtest > -1
                   THEN ln(1+ret_total_backtest) ELSE 0 END) OVER w AS log_product
        FROM seed WINDOW w AS(PARTITION BY permno ORDER BY date ROWS UNBOUNDED PRECEDING)
      ) SELECT permno,date,CASE WHEN date>=seed_date THEN seed_date END AS index_anchor_date,
          CASE WHEN date<seed_date OR seed_date IS NULL THEN NULL
               WHEN missing_count>0 THEN NULL
               WHEN zero_count>0 THEN 0.0 ELSE exp(log_product) END AS wealth_index,
          CASE WHEN date<seed_date OR seed_date IS NULL THEN 'NO_VALID_ANCHOR'
               WHEN missing_count>0 THEN 'MISSING_RETURN_OR_SESSION_GAP'
               WHEN zero_count>0 THEN 'ZERO_WEALTH' ELSE 'VALID' END AS index_status
      FROM paths""")
    return factors_reused


def build_dataset(config, *, data_root=None, output_root=None, pit_mode="approximate", quality_policy="mark"):
    """Build one reproducible research run; strict historical PIT fails explicitly."""
    if pit_mode != "approximate":
        raise ValueError("Strict PIT unavailable: source publication timestamps and historical vintages are absent")
    if quality_policy not in {"mark", "fail"}:
        raise ValueError("quality_policy must be mark or fail")
    data_root = Path(data_root or REPO / "data").resolve()
    output_root = Path(output_root or data_root / "derived/crsp/research").resolve()
    for protected in (data_root / "raw", data_root / "normalized"):
        if output_root == protected or protected in output_root.parents:
            raise ValueError("Derived output must not overwrite RAW/NORMALIZED")
    output_root.mkdir(parents=True, exist_ok=True)
    hashes = ContentHashes(output_root / ".hash_index.json")
    catalog = NormalizedCatalog(data_root, hashes)
    plan = plan_research_window(config, source_start=date.fromisoformat(catalog.source_start),
                                source_end=date.fromisoformat(catalog.source_end))
    code_id = code_identity(REPO)
    def validate_build_identity():
        catalog.assert_unchanged()
        if code_identity(REPO) != code_id:
            raise RuntimeError("Processing code changed during build; rerun with a stable code version")
    versions = {name: importlib.metadata.version(name) for name in ("duckdb", "exchange_calendars", "pandas", "numpy")}
    policy = {"rules_version": RULES_VERSION, "event_policy_version": EVENT_POLICY_VERSION,
              "code_id": code_id, "dependencies": versions,
              "availability_lag_sessions": config.availability_lag_sessions,
              "calendar": config.calendar_name, "return_policy": "CIZ_VENDOR_ONCE_FLAG_NA_NO_RECONSTRUCTION"}
    cache_id = digest({"source": catalog.snapshot_id, "policy": policy})
    run_identity = {"source": catalog.snapshot_id, "policy": policy, "plan": plan.as_dict(),
                    "config": config.__dict__, "quality_policy": quality_policy}
    run_id = digest(run_identity)
    final = output_root / "runs" / run_id[:24]
    if final.exists():
        result = verify_artifacts(final, hashes)
        if result["run_id"] != run_id:
            raise RuntimeError("Run identity collision")
        hashes.save()
        catalog.assert_unchanged()
        return final, result
    snapshot_path = output_root / "snapshots" / (catalog.snapshot_id + ".json")
    if not snapshot_path.exists():
        write_json(snapshot_path, catalog.snapshot)
    schedule = calendar_schedule(date.fromisoformat(catalog.source_start),date.fromisoformat(catalog.source_end),
                                 availability_lag_sessions=config.availability_lag_sessions,
                                 decision=config.decision,calendar_name=config.calendar_name)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='2GB'")
    con.execute("SET TimeZone='UTC'")
    stage = None
    try:
        _load_reference_tables(con, catalog, schedule)
        panels, event_files, cache_reused, month_manifests = [], [], [], []
        for key in catalog.selected_months(plan.process_start,plan.eval_end):
            catalog.assert_unchanged()
            target = output_root / "cache" / cache_id[:24] / f"year={key[0]}" / f"month={key[1]:02d}"
            if target.exists():
                cached = verify_artifacts(target, hashes)
                if cached["cache_id"] != cache_id:
                    raise RuntimeError("Cache identity collision")
                cache_reused.append(cached["month"])
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                month_stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=target.parent))
                try:
                    cached = _build_month(con,catalog,key,month_stage,
                                          {"cache_id":cache_id,"snapshot_id":catalog.snapshot_id,"policy":policy})
                    catalog.assert_unchanged()
                    _publish(month_stage,target,cached,hashes)
                finally:
                    if month_stage.exists():
                        shutil.rmtree(month_stage)
            month_manifests.append(cached)
            panels.append(target / "panel.parquet")
            event_files.append(target / "events.parquet")
        factor_context = {"cache_id":cache_id,"process_start":str(plan.process_start),
                          "factor_lookback_sessions":config.factor_lookback_sessions}
        factors_reused = _run_tables(con,config,plan,panels,
            output_root/"factor_cache"/digest(factor_context)[:24],factor_context,hashes,validate_build_identity)
        counts = dict(con.execute("SELECT return_quality,count(*) FROM run_panel GROUP BY 1").fetchall())
        metadata_counts = dict(con.execute("SELECT metadata_quality,count(*) FROM run_panel GROUP BY 1").fetchall())
        unresolved = sum(n for k,n in counts.items() if k != "VALID_VENDOR_RETURN")
        review_rows = _count(con,"SELECT count(*) FROM run_panel WHERE requires_review")
        if quality_policy == "fail" and review_rows:
            raise RuntimeError(f"Strict quality policy: {review_rows} return/metadata rows require review; no run published")
        stage_parent = output_root / "runs"
        stage_parent.mkdir(parents=True,exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".stage-",dir=stage_parent))
        _copy(con,"SELECT * FROM run_panel ORDER BY permno,date",stage / "panel.parquet")
        _copy(con,"SELECT * FROM features ORDER BY permno,decision_date",stage / "features.parquet")
        _copy(con,f"SELECT * FROM features WHERE decision_date BETWEEN {q(plan.eval_start)} AND {q(plan.eval_end)} ORDER BY permno,decision_date",stage / "evaluation_features.parquet")
        _copy(con,"SELECT * FROM labels ORDER BY permno,decision_date",stage / "labels.parquet")
        cutoff = plan.model_training_cutoff
        training_where = "false" if plan.train_start is None else (
            f"f.decision_date BETWEEN {q(plan.train_start)} AND {q(plan.train_end)} "
            f"AND l.available_at <= TIMESTAMPTZ {q(cutoff.isoformat())} "
            f"AND l.label_end_time <= TIMESTAMPTZ {q(cutoff.isoformat())}")
        _copy(con,f"""SELECT f.*,l.label_end_date,l.label_end_time,l.available_at AS label_available_at,l.forward_vendor_return
          FROM features f JOIN labels l USING(permno,decision_date)
          WHERE {training_where} AND f.eligible_for_research AND l.forward_vendor_return IS NOT NULL
          ORDER BY permno,decision_date""",stage / "training.parquet")
        base = plan.index_base_date
        _copy(con,f"""SELECT w.*,b.wealth_index AS base_wealth,
          CASE WHEN b.wealth_index>0 AND isfinite(b.wealth_index) THEN 100*w.wealth_index/b.wealth_index END AS index_base100,
          CASE WHEN b.date IS NULL THEN 'BASE_OBSERVATION_ABSENT'
               WHEN b.wealth_index IS NULL THEN 'BASE_VALUE_MISSING'
               WHEN b.wealth_index=0 THEN 'BASE_VALUE_ZERO'
               WHEN w.wealth_index IS NULL THEN 'PATH_INCOMPLETE' ELSE 'VALID' END AS rebase_status,
          {q(base)}::DATE AS index_base_date,
          CASE WHEN b.date IS NULL AND w.index_anchor_date>{q(base)}::DATE THEN 100*w.wealth_index END AS later_first_valid_base100,
          'DISPLAY_ONLY_NEVER_A_FEATURE' AS usage
          FROM wealth w LEFT JOIN wealth b ON b.permno=w.permno AND b.date={q(base)}::DATE
          ORDER BY permno,date""",stage / "display_indices.parquet")
        _copy(con,f"SELECT * FROM {parquet_sql(event_files)} WHERE observed_date BETWEEN {q(plan.process_start)} AND {q(plan.eval_end)} ORDER BY permno,observed_date",stage / "events.parquet")
        # Identity changes use historical intervals, never ticker as a join key.
        _copy(con,f"""WITH changes AS (
          SELECT permno,valid_from,ticker,lag(ticker) OVER(PARTITION BY permno ORDER BY valid_from) AS previous_ticker
          FROM history
        ) SELECT permno,valid_from AS effective_date,previous_ticker,ticker,
          NULL::TIMESTAMPTZ AS announced_at,'TICKER_CHANGE_SAME_PERMNO' AS effect,
          'IDENTITY_ONLY_NO_VALUE_ADJUSTMENT' AS policy
          FROM changes WHERE previous_ticker IS NOT NULL AND ticker IS DISTINCT FROM previous_ticker
          AND valid_from BETWEEN {q(plan.process_start)} AND {q(plan.eval_end)}
          ORDER BY permno,effective_date""",stage / "identity_events.parquet")
        _copy(con,f"SELECT * FROM calendar WHERE date BETWEEN {q(plan.process_start)} AND {q(plan.eval_end)} ORDER BY date",stage / "calendar.parquet")
        artifact_rows = {p.name:_count(con,f"SELECT count(*) FROM {parquet_sql([p])}") for p in stage.glob("*.parquet")}
        if plan.train_start is not None and artifact_rows["training.parquet"] == 0:
            raise RuntimeError("No eligible mature training examples; training data cannot be silently empty")
        event_counts = [dict(zip(("research_status","ledger_status","rows"),row)) for row in con.execute(
            f"SELECT research_status,ledger_status,count(*) FROM {parquet_sql([stage/'events.parquet'])} GROUP BY 1,2 ORDER BY 1,2").fetchall()]
        factor_counts = dict(con.execute("SELECT coalesce(has_required_history,false),count(*) FROM features GROUP BY 1").fetchall())
        label_counts = dict(con.execute("SELECT label_status,count(*) FROM labels GROUP BY 1").fetchall())
        revision = subprocess.run(["git","rev-parse","HEAD"],cwd=REPO,capture_output=True,text=True,check=False).stdout.strip()
        result = {"run_id":run_id,"snapshot_id":catalog.snapshot_id,"cache_id":cache_id,
                  "created_at_utc":datetime.now(timezone.utc).isoformat(),"git_commit":revision,
                  "policy":policy,"config":config.__dict__,"plan":plan.as_dict(),"rows":artifact_rows,
                  "pit_guarantee":"approximate","limitations":LIMITATIONS,"event_capabilities":EVENT_CAPABILITIES,
                  "status":"INCOMPLETE_RETURNS" if unresolved else "INCOMPLETE_METADATA" if review_rows else "RESEARCH_ONLY",
                  "quality_policy":quality_policy,"return_quality":counts,"unresolved_return_rows":unresolved,
                  "metadata_quality":metadata_counts,"requires_review_rows":review_rows,"event_status_counts":event_counts,
                  "factor_history_counts":{"ready":factor_counts.get(True,0),"unready":factor_counts.get(False,0)},
                  "label_status_counts":label_counts,
                  "held_outcome_validation_required":True,"cash_share_ledger_status":"UNSUPPORTED",
                  "cache_months_reused":cache_reused,"factor_cache_reused":factors_reused,
                  "factor_cache_context":factor_context,"months":month_manifests,
                  "artifact_roles":{"features.parquet":"as-of strategy input including preparation",
                      "evaluation_features.parquet":"evaluation-period as-of inputs only",
                      "training.parquet":"matured labels joined to eligible past inputs at fixed training cutoff",
                      "labels.parquet":"future outcome targets; do not join without maturity filter",
                      "panel.parquet":"audit/outcomes; NOT strategy input",
                      "events.parquet":"event evidence/unsupported ledger interface; NOT strategy input",
                      "display_indices.parquet":"per-security display; NOT training input or portfolio performance"}}
        catalog.assert_unchanged()
        if code_identity(REPO) != code_id:
            raise RuntimeError("Processing code changed during build; rerun with a stable code version")
        _publish(stage,final,result,hashes)
        stage = None
        return final,result
    finally:
        con.close()
        if stage is not None and stage.exists():
            shutil.rmtree(stage)
