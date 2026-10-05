from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path

import duckdb


SCHEMA_VERSION = 1
RETURN_POLICY = "CRSP_CIZ_DAILY"


def _sql_path(path: Path) -> str:
    """Quote a filesystem path for use inside a DuckDB string literal."""
    return str(path).replace("'", "''")


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
    """Build the first corporate-event-safe CRSP PIT dataset.

    CRSP's CIZ ``DlyRet`` already includes delisting returns.  Consequently
    this phase treats the normalized daily return as the economic return and
    joins ``stkdelists`` only as an event/audit ledger.  It deliberately does
    not compound ``delisting_return`` into the daily return a second time.
    """
    if requested_start > requested_end:
        raise ValueError("requested_start must be on or before requested_end")

    inputs = (daily_pattern, security_info_file, delistings_file)
    for path in inputs:
        if "*" not in str(path) and not path.exists():
            raise FileNotFoundError(path)

    success_file = output_file.with_name("_SUCCESS.json")
    if output_file.exists() and success_file.exists() and not force:
        with success_file.open(encoding="utf-8") as stream:
            return json.load(stream)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = output_file.with_name(output_file.name + ".tmp")
    temp_success = success_file.with_name(success_file.name + ".tmp")
    for path in (temp_file, temp_success):
        path.unlink(missing_ok=True)

    daily = _sql_path(daily_pattern)
    info = _sql_path(security_info_file)
    delist = _sql_path(delistings_file)
    out = _sql_path(temp_file)

    con = duckdb.connect()
    try:
        duplicate_events = con.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT permno, event_date
                FROM read_parquet('{delist}')
                WHERE event_date BETWEEN ? AND ?
                GROUP BY permno, event_date
                HAVING COUNT(*) > 1
            )
            """,
            [requested_start, requested_end],
        ).fetchone()[0]
        if duplicate_events:
            raise RuntimeError(
                f"Ambiguous delisting ledger: {duplicate_events} duplicate "
                "(permno, event_date) keys"
            )

        source_rows = con.execute(
            f"""
            SELECT COUNT(*)
            FROM read_parquet('{daily}')
            WHERE date BETWEEN ? AND ?
            """,
            [requested_start, requested_end],
        ).fetchone()[0]
        if source_rows == 0:
            raise RuntimeError("No normalized daily rows in requested period")

        con.execute(
            f"""
            COPY (
                SELECT
                    d.permno,
                    d.date,
                    d.price_raw,
                    d.market_cap_kusd,
                    d.volume_raw_shares,
                    d.ret_total AS ret_total_vendor,
                    d.ret_ex_div,
                    d.delist_flag,
                    d.ret_missing_flag,
                    d.distribution_return_flag,
                    h.ticker AS ticker_asof,
                    h.primary_exchange AS exchange_asof,
                    h.security_type AS security_type_asof,
                    h.trading_status_flag AS trading_status_asof,
                    e.event_date AS delisting_event_date,
                    e.delisting_return,
                    e.action_type AS delisting_action_type,
                    e.status_type AS delisting_status_type,
                    e.reason_type AS delisting_reason_type,
                    e.payment_type AS delisting_payment_type,
                    e.successor_permno,
                    (e.permno IS NOT NULL OR NULLIF(TRIM(d.delist_flag), '') IS NOT NULL)
                        AS is_delisting_day,
                    d.ret_total AS ret_total_backtest,
                    CASE
                        WHEN d.ret_total IS NOT NULL THEN 'CRSP_CIZ_DAILY'
                        ELSE 'MISSING'
                    END AS return_source,
                    FALSE AS return_was_reconstructed,
                    (h.permno IS NOT NULL) AS has_security_info,
                    (
                        (e.permno IS NOT NULL OR NULLIF(TRIM(d.delist_flag), '') IS NOT NULL)
                        AND d.ret_total IS NULL
                    ) AS requires_review
                FROM read_parquet('{daily}') d
                LEFT JOIN read_parquet('{info}') h
                  ON d.permno = h.permno
                 AND d.date >= h.valid_from
                 AND d.date <= COALESCE(h.valid_to, DATE '9999-12-31')
                LEFT JOIN read_parquet('{delist}') e
                  ON d.permno = e.permno
                 AND d.date = e.event_date
                WHERE d.date BETWEEN ? AND ?
            ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """,
            [requested_start, requested_end],
        )

        stats = con.execute(
            f"""
            SELECT COUNT(*), MIN(date), MAX(date),
                   COUNT(*) FILTER (WHERE is_delisting_day),
                   COUNT(*) FILTER (WHERE requires_review),
                   COUNT(*) FILTER (WHERE NOT has_security_info)
            FROM read_parquet('{out}')
            """
        ).fetchone()
        output_rows, min_date, max_date, event_rows, review_rows, unmatched_rows = stats
        if output_rows != source_rows:
            raise RuntimeError(
                "PIT joins changed row count: "
                f"daily={source_rows}, output={output_rows}"
            )
    finally:
        con.close()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "return_policy": RETURN_POLICY,
        "requested_start": str(requested_start),
        "requested_end": str(requested_end),
        "rows": output_rows,
        "min_date": str(min_date),
        "max_date": str(max_date),
        "delisting_event_rows": event_rows,
        "requires_review_rows": review_rows,
        "unmatched_security_info_rows": unmatched_rows,
        "sources": {
            "daily": str(daily_pattern),
            "security_info": str(security_info_file),
            "delistings": str(delistings_file),
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    with temp_success.open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)

    if force:
        output_file.unlink(missing_ok=True)
        success_file.unlink(missing_ok=True)
    os.replace(temp_file, output_file)
    os.replace(temp_success, success_file)
    return manifest
