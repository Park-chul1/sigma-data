"""CRSP CIZ event interpretation and fail-closed research return interfaces.

The normalized daily ``ret_total`` is CIZ DlyRet, including distributions and
the separate *daily row* carrying the delisting return. DelRet is corroborating
evidence, never an additional return component. ``ret_ex_div`` is DlyRetx,
which excludes ordinary dividends, not necessarily every distribution.

Official definitions (checked 2026-10-03):
* https://www.crsp.org/wp-content/uploads/DelistCode.html
* https://www.crsp.org/wp-content/uploads/appendix/FlagType_CI.html
* https://www.crsp.org/wp-content/uploads/appendix/FlagType_RM.html
* https://www.crsp.org/wp-content/uploads/appendix/FlagType_MU.html
* https://indexes.morningstar.com/docs/guide/crsp-us-stock-databases-guide-for-flat-file-format-2-0?isRdp=true

Event classification is an economic category, not proof of sufficient terms
for a cash/share ledger. The current extract omits distribution terms, payout
dates, announcement timestamps and revision vintages. No quantity, cash,
successor-position or strict availability reconstruction is performed here.
"""

from __future__ import annotations

import math
from numbers import Number
from typing import Any, Iterable, Mapping


EVENT_POLICY_VERSION = "crsp-ciz-embedded-v1"

# CIZ mnemonics, not guesses from legacy numeric DLSTCD ranges.
EVENT_ACTION_MAP = {
    "MER": "merger_or_stock_exchange",
    "GEX": "security_exchange",
    "GLI": "liquidation",
    "GDR": "delisting",
    "LOS": "lost_source",
}
SUPPORTED_TERMINAL_ACTIONS = frozenset({"MER", "GEX", "GLI", "GDR"})

# S1/S2 do not distinguish a split, reverse split and stock dividend. A daily
# impact flag does not establish the ratio, amount, ex-date or payment date.
DISTRIBUTION_EFFECT_MAP = {
    "C1": ("cash_dividend",),
    "C2": ("cash_dividend",),
    "CS": ("cash_dividend", "split_or_stock_dividend"),
    "D1": ("delisting",),
    "D2": ("delisting",),
    "F1": ("non_split_share_factor",),
    "M2": ("multiple_actions",),
    "MU": ("multiple_actions",),
    "N1": ("non_ordinary_distribution",),
    "NA": (),
    "NO": (),
    "O1": ("other_action",),
    "P1": ("non_split_price_factor",),
    "S1": ("split_or_stock_dividend",),
    "S2": ("split_or_stock_dividend",),
    "T1": ("tender_offer",),
}

# Common effect vocabulary for a later detailed event adapter. These types
# cannot be recovered specifically from the local daily impact flags.
UNSUPPORTED_DETAILED_EFFECTS = frozenset({
    "split", "reverse_split", "stock_dividend", "cash_dividend",
    "merger", "stock_exchange", "spin_off", "rights_distribution",
})


def validate_return(value: Any) -> str:
    """Return VALID, MISSING or a precise invalid status; zero is VALID.

    Numeric missing codes (e.g. legacy -66) are invalid, not economic losses.
    NaN and infinities remain distinguishable from a source SQL NULL.
    """
    if value is None:
        return "MISSING"
    if isinstance(value, bool) or not isinstance(value, Number):
        return "INVALID_TYPE"
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return "INVALID_TYPE"
    if not math.isfinite(numeric):
        return "NONFINITE"
    return "BELOW_MINUS_ONE" if numeric < -1 else "VALID"


def classify_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve event fields and add categories without inventing terms.

    Input is a normalized stkdelists row, or a future adapter's explicit
    ``event_type``. Delisting status/reason/payment codes are retained verbatim.
    A successor PERMNO is a relationship only: it never replaces ``permno``.
    The result is an audit record, not a feature available at ``event_date``.
    """
    result = dict(event)
    code = event.get("action_type")
    explicit_type = event.get("event_type")
    category = EVENT_ACTION_MAP.get(code, explicit_type or "unknown")
    if code is not None and code not in EVENT_ACTION_MAP:
        category = "unknown"
    result.update({
        "event_type": category,
        "event_status": "unsupported",
        "event_reason": "Detailed ratios, amounts and settlement dates are absent",
        "ledger_status": "unsupported",
        "event_terms_incomplete": True,
        "announcement_at": event.get("announcement_at"),
        "available_at": event.get("available_at"),
        "return_effect_policy": "never_add_to_ciz_daily_return",
        "successor_auto_linked": False,
    })
    if code in SUPPORTED_TERMINAL_ACTIONS:
        result["event_status"] = "requires_return_reconciliation"
        result["event_reason"] = (
            "Use only a matched CIZ daily terminal return for research; "
            "cash/share settlement remains unsupported"
        )
    elif category in {"ticker_change", "name_change"}:
        result.update({
            "event_status": "identity_only",
            "event_reason": "Keep PERMNO and effective-dated attributes; no economic posting",
            "ledger_status": "no_posting_required",
            "event_terms_incomplete": False,
        })
    elif category == "unknown":
        result["event_reason"] = "Unknown source action code; no economic terms inferred"
    elif category == "lost_source":
        result["event_reason"] = "Loss of source does not establish a final recovery value"
    elif category not in UNSUPPORTED_DETAILED_EFFECTS:
        result["event_type"] = "unknown"
        result["event_reason"] = "Unsupported event type; no economic terms inferred"
    return result


def classify_daily_distribution(row: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a daily impact flag; keep unidentified subtypes unidentified.

    Known complex distributions can be represented by a valid vendor total
    return for *research only*. They cannot be posted to a cash/share ledger.
    """
    flag = row.get("distribution_return_flag")
    known = flag in DISTRIBUTION_EFFECT_MAP
    effects = DISTRIBUTION_EFFECT_MAP.get(flag, ("unknown",))
    valid = (validate_return(row.get("ret_total")) == "VALID"
             and row.get("ret_missing_flag") == "NA")
    return {
        "distribution_return_flag": flag,
        "event_types": effects,
        "distribution_status": (
            "unknown" if not known else
            "none" if not effects else
            "vendor_return_only" if valid else "missing_return"
        ),
        "ledger_status": "unsupported" if effects else "no_posting_required",
        "event_terms_incomplete": bool(effects),
    }


def resolve_daily_return(
    daily: Mapping[str, Any],
    event: Mapping[str, Any] | None = None,
    *,
    reconciled: bool = False,
    source_format: str = "CRSP_CIZ",
) -> dict[str, Any]:
    """Add validated return fields to one unchanged normalized daily record.

    ``reconciled=True`` certifies the caller checked the event key against the
    *next exchange session* (or source DelDlyDt), not a nearest-date join. The
    method additionally checks the PERMNO, date order and return equality.
    A numeric return requires ``ret_missing_flag='NA'``: for example an MV
    flag denotes missing corporate-action value even if a number is stored.
    A valid terminal return without reconciliation is preserved as vendor
    evidence but is not released as ``ret_total_backtest``.

    A CIZ terminal storage date is not an announcement/payment/availability
    timestamp. Its return is an eventual-outcome accounting record, excluded
    from trading features until a separately evidenced available_at exists.
    Earlier ordinary rows deliberately ignore an attached future event.
    """
    if source_format != "CRSP_CIZ":
        raise ValueError("Only verified CRSP CIZ returns are supported; no legacy RET/DLRET merge")
    result = dict(daily)
    raw = daily.get("ret_total")
    quality = validate_return(raw)
    terminal = daily.get("delist_flag") == "Y"
    distribution = classify_daily_distribution(daily)
    reason = None if quality == "VALID" else quality
    if quality == "VALID" and daily.get("ret_missing_flag") != "NA":
        reason = "INCOMPLETE_VENDOR_RETURN"
    if quality == "VALID" and distribution["distribution_status"] == "unknown":
        reason = "UNKNOWN_DISTRIBUTION_FLAG"
    if daily.get("delist_flag") not in {"N", "Y"}:
        reason = "UNKNOWN_DELISTING_FLAG"
    if terminal:
        if not reconciled or event is None:
            reason = "UNRECONCILED_TERMINAL_RETURN"
        else:
            if daily.get("permno") != event.get("permno"):
                raise ValueError("Terminal event PERMNO does not match daily record")
            if daily.get("date") is None or event.get("event_date") is None:
                raise ValueError("Terminal reconciliation requires daily and event dates")
            if event["event_date"] >= daily["date"]:
                raise ValueError("CIZ terminal storage date must follow delisting event date")
            event_quality = validate_return(event.get("delisting_return"))
            if quality == "VALID" and event_quality == "VALID":
                if not math.isclose(float(raw), float(event["delisting_return"]), rel_tol=1e-12, abs_tol=1e-12):
                    raise ValueError("Embedded daily and event delisting returns disagree")
            elif quality != event_quality:
                raise ValueError("Embedded daily and event return missingness/validity disagree")
            classified = classify_event(event)
            if classified["event_status"] == "unsupported":
                reason = "UNSUPPORTED_TERMINAL_EVENT"
            elif quality != "VALID":
                reason = "MISSING_TERMINAL_RETURN" if quality == "MISSING" else quality
    result.update(distribution)
    result.update({
        "ret_total_vendor": raw,
        "ret_total_backtest": float(raw) if reason is None else None,
        "return_quality": quality,
        "vendor_return_complete": quality == "VALID" and daily.get("ret_missing_flag") == "NA",
        "return_source": (
            "CRSP_DELIST_EMBEDDED" if terminal and quality == "VALID" else
            "CRSP_DAILY" if quality == "VALID" else "MISSING_OR_INVALID"
        ),
        "return_was_reconstructed": False,
        "requires_review": reason is not None,
        "review_reason": reason,
        "is_delisting_return": terminal,
        "is_eventual_outcome": terminal,
        "usable_as_feature": not terminal and reason is None,
        "cash_available_at": None,
        "event_policy_version": EVENT_POLICY_VERSION,
    })
    return result


class HeldOutcomeError(RuntimeError):
    """A held security lacks a supported, finite economic outcome."""


def validate_held_outcomes(
    rows: Iterable[Mapping[str, Any]],
    held_permnos: Iterable[int],
    *,
    mode: str = "raise",
    accounting_mode: str = "total_return",
) -> dict[str, Any]:
    """Guard one holding interval before aggregation (never sum-skip NULL).

    Pass all held securities' observations for the interval, including halted
    or ineligible rows. ``held_permnos`` is the *pre-interval* set; entry filters
    must not determine this set. Missing securities, duplicate observations,
    unknown events, and unresolved returns fail or mark the result incomplete.
    Use one call per holding interval: this interface cannot detect a missing
    daily row inside a multi-day stream without the caller's expected calendar.
    Both ``resolve_daily_return`` records and exported ``panel.parquet`` rows
    are accepted; source flags are checked even if optional effect fields are
    absent. Field values used for disposition are case-insensitive.
    """
    if mode not in {"raise", "incomplete"}:
        raise ValueError("mode must be raise or incomplete")
    if accounting_mode not in {"total_return", "cash_shares"}:
        raise ValueError("accounting_mode must be total_return or cash_shares")
    held = set(held_permnos)
    seen: set[int] = set()
    interval_date = None
    issues = []
    for row in rows:
        permno = row.get("permno")
        if permno not in held:
            continue
        reason = None
        observation_date = row.get("date")
        if observation_date is None:
            reason = "MISSING_HOLDING_INTERVAL_DATE"
        elif interval_date is None:
            interval_date = observation_date
        elif observation_date != interval_date:
            reason = "MIXED_HOLDING_INTERVAL_DATES"
        if permno in seen:
            reason = "DUPLICATE_HELD_OBSERVATION"
        seen.add(permno)
        quality = row.get("return_quality")
        event_status = str(row.get("event_status", "")).lower()
        research_status = str(row.get("research_status", "")).lower()
        ledger_status = str(row.get("ledger_status", "")).lower()
        distribution_status = str(row.get("distribution_status", "")).lower()
        source_dist = row.get("distribution_return_flag")
        terminal = bool(row.get("is_delisting_return")) or row.get("delist_flag") == "Y"
        if validate_return(row.get("ret_total_backtest")) != "VALID":
            reason = row.get("review_reason") or quality or "MISSING_OR_INVALID_HELD_RETURN"
        elif quality is not None and quality not in {"VALID", "VALID_VENDOR_RETURN"}:
            reason = quality
        elif row.get("requires_review"):
            reason = row.get("review_reason") or "HELD_OUTCOME_REQUIRES_REVIEW"
        elif event_status in {"unknown", "unsupported"}:
            reason = "UNSUPPORTED_HELD_EVENT"
        elif research_status in {"unknown", "unsupported", "incomplete"}:
            reason = "INCOMPLETE_HELD_EVENT"
        elif distribution_status == "unknown" or (
            "distribution_return_flag" in row and source_dist not in DISTRIBUTION_EFFECT_MAP
        ):
            reason = "UNKNOWN_HELD_DISTRIBUTION"
        elif (terminal and row.get("action_type") is not None
              and row["action_type"] not in SUPPORTED_TERMINAL_ACTIONS):
            reason = "UNSUPPORTED_TERMINAL_EVENT"
        if accounting_mode == "cash_shares" and (
            ledger_status == "unsupported"
            or row.get("event_terms_incomplete")
            or terminal
            or bool(DISTRIBUTION_EFFECT_MAP.get(source_dist))
        ):
            reason = "UNSUPPORTED_CASH_SHARE_SETTLEMENT"
        if reason:
            issues.append({"permno": permno, "date": row.get("date"), "reason": reason})
    issues.extend(
        {"permno": permno, "date": None, "reason": "MISSING_HELD_SECURITY"}
        for permno in sorted(held - seen)
    )
    report = {"status": "incomplete" if issues else "complete", "issues": issues,
              "accounting_mode": accounting_mode, "held_count": len(held)}
    if issues and mode == "raise":
        raise HeldOutcomeError(f"Unresolved held outcomes: {issues}")
    return report


def apply_verified_split(shares: float, new_shares_per_old: float) -> float:
    """Unit-conversion interface for externally verified effective split terms.

    This helper neither infers a ratio from prices nor posts an event. It is
    called by a future ledger only at the effective instant, once. Fractional
    quantities are mathematical entitlements; cash-in-lieu is not implemented.
    """
    if not math.isfinite(shares) or not math.isfinite(new_shares_per_old) or new_shares_per_old <= 0:
        raise ValueError("Split requires finite shares and a positive verified ratio")
    quantity = shares * new_shares_per_old
    if not math.isfinite(quantity):
        raise ValueError("Split quantity overflow")
    return quantity


def research_wealth_step(
    wealth: float, ret_total: float, *, cash_distribution: float = 0.0,
) -> float:
    """Compound research total returns exactly once; disallow cash postings.

    The cash argument is an explicit misuse guard for callers migrating from
    a cash/share ledger. It must be zero because dividends are in ret_total.
    """
    if not math.isfinite(wealth) or wealth < 0:
        raise ValueError("Research wealth must be finite and nonnegative")
    if cash_distribution != 0:
        raise ValueError("Do not add cash distributions to a total-return wealth path")
    if validate_return(ret_total) != "VALID":
        raise HeldOutcomeError("Cannot compound a missing or invalid return")
    result = wealth * (1.0 + ret_total)
    if not math.isfinite(result):
        raise ValueError("Research wealth overflow")
    return result
