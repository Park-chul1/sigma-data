"""Trading-session planning and causal research utilities.

The calendar is XNYS, including exceptional closures and early closes.  Applying
it to CRSP securities on other exchanges is an explicit research assumption,
not evidence of that security's trading availability.  All timestamps returned
here are UTC-aware.  Vendor observation dates are not publication timestamps:
``available_at`` is a conservative *assumption*, not a historical-vintage claim.

Display rebasing is deliberately separate from returns and factor inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite, prod
from typing import Any, Iterable, Mapping, Sequence


DateLike = str | date
TEMPORAL_RULE_VERSION = "1.0"


def _date(value: DateLike) -> date:
    if isinstance(value, datetime):
        raise TypeError("Supply a session date, not a datetime")
    return date.fromisoformat(value) if isinstance(value, str) else value


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def _positive_integer(value: int, name: str, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f"{name} must be an integer >= {0 if zero else 1}")


@dataclass(frozen=True)
class ResearchConfig:
    requested_start: DateLike
    requested_end: DateLike
    factor_lookback_sessions: int = 252
    training_sessions: int = 0
    label_horizon_sessions: int = 1
    availability_lag_sessions: int = 1
    decision: str = "after_close"
    execution: str = "vendor_return_research"
    index_base_date: DateLike | None = None
    calendar_name: str = "XNYS"
    train_start: DateLike | None = None
    train_end: DateLike | None = None

    def __post_init__(self) -> None:
        for name in ("requested_start", "requested_end", "index_base_date", "train_start", "train_end"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _date(value))
        if self.requested_start > self.requested_end:
            raise ValueError("requested_start must not follow requested_end")
        _positive_integer(self.factor_lookback_sessions, "factor_lookback_sessions")
        _positive_integer(self.training_sessions, "training_sessions", zero=True)
        _positive_integer(self.label_horizon_sessions, "label_horizon_sessions")
        # Zero is an explicit, less conservative same-day-after-close policy.
        _positive_integer(self.availability_lag_sessions, "availability_lag_sessions", zero=True)
        if self.decision not in {"after_close", "preopen"}:
            raise ValueError("decision must be after_close or preopen")
        if self.execution not in {"vendor_return_research", "research_close_to_close"}:
            raise ValueError("Opening-price execution is unsupported; use vendor_return_research")
        if self.calendar_name != "XNYS":
            raise ValueError("Only the explicitly documented XNYS calendar is supported")
        if (self.train_start is None) != (self.train_end is None):
            raise ValueError("Explicit training needs both train_start and train_end")
        if self.train_start is not None:
            if self.training_sessions:
                raise ValueError("Use explicit training dates or training_sessions, not both")
            if self.train_start > self.train_end:
                raise ValueError("train_start must not follow train_end")
            if self.train_end >= self.requested_start:
                raise ValueError("Training decisions must precede evaluation")

    @property
    def observation_offset_sessions(self) -> int:
        return self.availability_lag_sessions + int(self.decision == "preopen")


class SessionCalendar:
    """Bounded exchange calendar with explicit errors instead of weekday guesses."""

    def __init__(self, start: DateLike, end: DateLike, name: str = "XNYS"):
        if name != "XNYS":
            raise ValueError("Only XNYS is currently supported")
        import exchange_calendars

        self.name = name
        start, end = _date(start), _date(end)
        if start > end:
            raise ValueError("Calendar start must not follow end")
        self._calendar = exchange_calendars.get_calendar(name, start=start.isoformat(), end=end.isoformat())
        self.sessions = [value.date() for value in self._calendar.sessions]
        self._positions = {value: index for index, value in enumerate(self.sessions)}

    def position(self, session: DateLike) -> int:
        session = _date(session)
        if session not in self._positions:
            raise ValueError(f"{session} is not a covered {self.name} trading session")
        return self._positions[session]

    def shift(self, session: DateLike, count: int) -> date:
        position = self.position(session) + count
        if not 0 <= position < len(self.sessions):
            raise ValueError(f"Insufficient calendar coverage to shift {session} by {count} sessions")
        return self.sessions[position]

    def open_at(self, session: DateLike) -> datetime:
        self.position(session)
        return self._calendar.session_open(_date(session).isoformat()).to_pydatetime()

    def close_at(self, session: DateLike) -> datetime:
        self.position(session)
        return self._calendar.session_close(_date(session).isoformat()).to_pydatetime()

    def decision_at(self, session: DateLike, decision: str = "after_close") -> datetime:
        if decision == "after_close":
            return self.close_at(session) + timedelta(minutes=60)
        if decision == "preopen":
            return self.open_at(session) - timedelta(minutes=1)
        raise ValueError("decision must be after_close or preopen")

    def available_at(self, observation_date: DateLike, lag: int = 1) -> datetime:
        _positive_integer(lag, "lag", zero=True)
        return self.close_at(self.shift(observation_date, lag)) + timedelta(minutes=30)

    def latest_observable_session(self, decision_date: DateLike, *, lag: int = 1,
                                  decision: str = "after_close") -> date:
        self.decision_at(decision_date, decision)
        _positive_integer(lag, "lag", zero=True)
        return self.shift(decision_date, -lag - int(decision == "preopen"))

    def rows(self, start: DateLike, end: DateLike, *, availability_lag_sessions: int = 1,
             decision: str = "after_close") -> list[dict[str, Any]]:
        start, end = _date(start), _date(end)
        if start > end:
            raise ValueError("Schedule start must not follow end")
        return [
            {"date": session, "session_index": position,
             "open_at": self.open_at(session), "close_at": self.close_at(session),
             "decision_time": self.decision_at(session, decision),
             "available_at": self.available_at(session, availability_lag_sessions)}
            for position, session in enumerate(self.sessions) if start <= session <= end
        ]


def calendar_schedule(start: DateLike, end: DateLike, *, availability_lag_sessions: int = 1,
                      decision: str = "after_close", calendar_name: str = "XNYS") -> list[dict[str, Any]]:
    """Return session rows; pad future calendar coverage for availability timestamps.

    The first returned session has ``session_index=0``.  Weekend boundaries are
    allowed here for file coverage queries; run evaluation endpoints are stricter.
    """
    start, end = _date(start), _date(end)
    _positive_integer(availability_lag_sessions, "availability_lag_sessions", zero=True)
    calendar = SessionCalendar(start, end + timedelta(days=4 * availability_lag_sessions + 30), calendar_name)
    rows = calendar.rows(start, end, availability_lag_sessions=availability_lag_sessions, decision=decision)
    for index, row in enumerate(rows):
        row["session_index"] = index
    return rows


@dataclass(frozen=True)
class ResearchPlan:
    config: ResearchConfig
    process_start: date
    train_start: date | None
    train_end: date | None
    eval_start: date
    eval_end: date
    model_train_cutoff: datetime
    index_base_date: date
    source_start: date
    source_end: date
    loaded_sessions: tuple[date, ...]
    evaluation_sessions: tuple[date, ...]
    training_decision_sessions: tuple[date, ...]
    earliest_feature_observation: date
    latest_feature_observation: date

    @property
    def requested_start(self) -> date:
        return self.eval_start

    @property
    def requested_end(self) -> date:
        return self.eval_end

    @property
    def model_training_cutoff(self) -> datetime:
        return self.model_train_cutoff

    def as_dict(self) -> dict[str, Any]:
        def encode(value: Any) -> Any:
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (tuple, list)):
                return [encode(item) for item in value]
            return value
        return encode(asdict(self))


def plan_research_window(config: ResearchConfig, source_start: DateLike, source_end: DateLike,
                         observed_sessions: Iterable[DateLike] | None = None) -> ResearchPlan:
    """Plan dependency-complete preparation, training, and evaluation intervals.

    Training dates denote *decision dates*.  A training label spans the next H
    sessions after its decision and must be available at the first evaluation
    decision.  Automatic planning reserves H plus the information lag before
    choosing the final training date, then reserves the entire factor lookback
    before the first training date.  It never substitutes calendar days for
    sessions.  Per-security gaps remain a separate factor-quality constraint.
    """
    source_start, source_end = _date(source_start), _date(source_end)
    if source_start > source_end:
        raise ValueError("Invalid source coverage")
    dependency_sessions = (config.factor_lookback_sessions + config.training_sessions
                           + config.label_horizon_sessions + 2 * config.observation_offset_sessions)
    earliest = min(source_start, config.requested_start, config.train_start or config.requested_start)
    latest = max(source_end, config.requested_end)
    calendar = SessionCalendar(earliest - timedelta(days=4 * dependency_sessions + 30),
                               latest + timedelta(days=4 * (config.label_horizon_sessions
                                                           + config.availability_lag_sessions) + 30),
                               config.calendar_name)
    evaluation_first = calendar.position(config.requested_start)
    evaluation_last = calendar.position(config.requested_end)
    offset = config.observation_offset_sessions
    maturity_distance = config.label_horizon_sessions + offset
    latest_train_position = evaluation_first - maturity_distance
    if config.train_start is not None:
        training_first = calendar.position(config.train_start)
        training_last = calendar.position(config.train_end)
        if training_last > latest_train_position:
            raise ValueError("Training label horizon and availability extend beyond model training cutoff")
    elif config.training_sessions:
        training_last = latest_train_position
        training_first = training_last - config.training_sessions + 1
    else:
        training_first = training_last = None
    earliest_decision = evaluation_first if training_first is None else training_first
    first_observation = earliest_decision - offset
    process_position = first_observation - config.factor_lookback_sessions + 1
    if process_position < 0:
        raise ValueError("Insufficient calendar history for preparation dependencies")
    process_start = calendar.sessions[process_position]
    if source_start > process_start:
        raise ValueError(f"Insufficient preparation history: need {process_start}, source starts {source_start}")
    if source_end < config.requested_end:
        raise ValueError(f"Insufficient evaluation coverage: need {config.requested_end}, source ends {source_end}")
    loaded = tuple(calendar.sessions[process_position:evaluation_last + 1])
    if observed_sessions is not None:
        observed = {_date(value) for value in observed_sessions}
        missing = [session for session in loaded if session not in observed]
        if missing:
            raise ValueError(f"Source is missing {len(missing)} required market sessions; first: {missing[:5]}")
    training = (() if training_first is None else
                tuple(calendar.sessions[training_first:training_last + 1]))
    base = config.index_base_date or config.requested_start
    calendar.position(base)
    if base < process_start or base > config.requested_end:
        raise ValueError("index_base_date must be in the loaded interval")
    return ResearchPlan(
        config=config, process_start=process_start,
        train_start=training[0] if training else None,
        train_end=training[-1] if training else None,
        eval_start=config.requested_start, eval_end=config.requested_end,
        model_train_cutoff=calendar.decision_at(config.requested_start, config.decision),
        index_base_date=base, source_start=source_start, source_end=source_end,
        loaded_sessions=loaded, evaluation_sessions=tuple(calendar.sessions[evaluation_first:evaluation_last + 1]),
        training_decision_sessions=training,
        earliest_feature_observation=calendar.sessions[first_observation],
        latest_feature_observation=calendar.sessions[evaluation_last - offset],
    )


def label_is_mature(label_end_time: datetime | None, available_at: datetime | None,
                    training_cutoff: datetime) -> bool:
    """An observation-complete label may still be unavailable; require both."""
    cutoff = _aware(training_cutoff)
    if label_end_time is None or available_at is None:
        return False
    return _aware(label_end_time) <= cutoff and _aware(available_at) <= cutoff


def matured_training_labels(rows: Iterable[Mapping[str, Any]],
                            training_cutoff: datetime) -> list[Mapping[str, Any]]:
    """Filter labels, using explicit ``label_end_time`` and ``available_at`` keys."""
    return [row for row in rows if label_is_mature(row.get("label_end_time"),
                                                 row.get("available_at"), training_cutoff)]


def _checked_return(value: float | None) -> float | None:
    if value is None:
        return None
    if not isfinite(value) or value < -1:
        raise ValueError(f"Invalid economic return: {value!r}")
    return float(value)


def compound_returns(returns: Iterable[float | None], initial: float = 1.0) -> list[float | None]:
    """Post-observation wealth; missing data invalidates the remaining path.

    A -100% outcome is absorbing.  Missing returns are never economic zeros,
    even after a terminal loss; no implicit restart follows a missing value.
    """
    if not isfinite(initial) or initial < 0:
        raise ValueError("initial wealth must be finite and nonnegative")
    wealth: float | None = float(initial)
    result: list[float | None] = []
    for value in returns:
        value = _checked_return(value)
        if value is None or wealth is None:
            wealth = None
        else:
            wealth *= 1 + value
            if not isfinite(wealth):
                raise ValueError("Compounded wealth overflowed")
        result.append(wealth)
    return result


def rolling_momentum(returns: Sequence[float | None], lookback_sessions: int) -> list[float | None]:
    """Causal complete-window momentum; a terminal loss ends future features.

    Rows must already occupy consecutive market sessions.  Missing observations
    must be represented by None instead of removing the session from this input.
    """
    _positive_integer(lookback_sessions, "lookback_sessions")
    checked = [_checked_return(value) for value in returns]
    result: list[float | None] = []
    terminal = False
    for index, value in enumerate(checked):
        if terminal or index + 1 < lookback_sessions:
            result.append(None)
        else:
            window = checked[index + 1 - lookback_sessions:index + 1]
            momentum = None if None in window else prod(1 + item for item in window) - 1
            if momentum is not None and not isfinite(momentum):
                raise ValueError("Momentum overflowed")
            result.append(momentum)
        terminal = terminal or value == -1
    return result


@dataclass(frozen=True)
class RebasedIndex:
    dates: tuple[date, ...]
    values: tuple[float | None, ...]
    requested_base_date: date
    actual_base_date: date | None
    status: str
    purpose: str = "display_only_individual_security_index"


def rebase_index(dates: Sequence[DateLike], index_values: Sequence[float | None],
                  base_date: DateLike, *, fallback: str = "none") -> RebasedIndex:
    """Compute 100 * J(t) / J(s) without inventing a common IPO baseline.

    Fallback ``first_valid_after_base`` is explicit and returns a distinct status
    and actual base date.  Preparation dates remain in output for display, but
    this return type must never be used as a training-feature input.
    """
    if fallback not in {"none", "first_valid_after_base"}:
        raise ValueError("Unknown rebase fallback")
    dates = tuple(_date(item) for item in dates)
    base = _date(base_date)
    if len(dates) != len(index_values) or list(dates) != sorted(set(dates)):
        raise ValueError("Index dates must be unique, increasing, and match the values")
    for value in index_values:
        if value is not None and (not isfinite(value) or value < 0):
            raise ValueError("Wealth index values must be finite and nonnegative")
    positions = {session: index for index, session in enumerate(dates)}
    position = positions.get(base)
    base_value = index_values[position] if position is not None else None
    actual, status = base, "exact_base"
    if base_value == 0:
        status, actual = "zero_base", None
    elif base_value is None:
        status, actual = "missing_base", None
        if fallback == "first_valid_after_base":
            for session, value in zip(dates, index_values):
                if session > base and value is not None and value > 0:
                    actual, base_value, status = session, value, "first_valid_after_base"
                    break
    if actual is None:
        values = tuple(None for _ in dates)
    else:
        values = tuple(None if value is None else 100 * (value / base_value) for value in index_values)
        if any(value is not None and not isfinite(value) for value in values):
            raise ValueError("Rebased index overflowed")
    return RebasedIndex(dates, values, base, actual, status)


def portfolio_index(evaluation_returns: Iterable[float | None]) -> list[float | None]:
    """Return [100, post-return wealth...]; input contains evaluation P&L only.

    Element zero is the initial portfolio mark *before* the first evaluated
    holding interval.  This differs from rebasing a security at an observed
    session's closing index, which excludes that base session's own return.
    """
    return [100.0, *compound_returns(evaluation_returns, initial=100.0)]
