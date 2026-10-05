import json
import unittest
from datetime import date, datetime, timedelta, timezone

from src.derive.temporal import (
    ResearchConfig, SessionCalendar, calendar_schedule, compound_returns,
    label_is_mature, matured_training_labels, plan_research_window,
    portfolio_index, rebase_index, rolling_momentum,
)


class CalendarTests(unittest.TestCase):
    def test_exceptional_closures_are_not_weekdays(self):
        rows = calendar_schedule("2001-09-10", "2001-09-18")
        self.assertEqual([row["date"].isoformat() for row in rows],
                         ["2001-09-10", "2001-09-17", "2001-09-18"])

    def test_early_close_and_lag_use_actual_sessions(self):
        rows = calendar_schedule("2024-11-27", "2024-12-02")
        before, friday, monday = rows
        self.assertEqual(friday["close_at"], datetime(2024, 11, 29, 18, tzinfo=timezone.utc))
        self.assertEqual(before["available_at"], friday["close_at"] + timedelta(minutes=30))
        self.assertLessEqual(before["available_at"], friday["decision_time"])
        self.assertEqual(friday["available_at"], monday["close_at"] + timedelta(minutes=30))
        self.assertEqual([row["session_index"] for row in rows], [0, 1, 2])

    def test_preopen_cannot_read_same_day_ohlcv_even_at_zero_lag(self):
        calendar = SessionCalendar("2024-03-01", "2024-03-20")
        monday = date(2024, 3, 11)
        self.assertEqual(calendar.latest_observable_session(monday, lag=0, decision="preopen"),
                         date(2024, 3, 8))
        self.assertGreater(calendar.available_at(monday, lag=0), calendar.decision_at(monday, "preopen"))
        self.assertEqual(calendar.open_at(monday).hour, 13)  # DST changed on Sunday.
        self.assertEqual(calendar.open_at("2024-03-08").hour, 14)

    def test_calendar_and_execution_guesses_rejected(self):
        with self.assertRaises(ValueError):
            ResearchConfig("2024-01-02", "2024-01-03", execution="next_open")
        with self.assertRaises(ValueError):
            ResearchConfig("2024-01-02", "2024-01-03", availability_lag_sessions=-1)
        with self.assertRaises(ValueError):
            ResearchConfig("2024-01-02", "2024-01-03", calendar_name="weekday")


class WindowPlanningTests(unittest.TestCase):
    def setUp(self):
        self.calendar = SessionCalendar("2019-01-01", "2025-12-31")

    def test_training_warmup_composes_entire_dependency_chain(self):
        config = ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=252,
                                training_sessions=504, label_horizon_sessions=5,
                                availability_lag_sessions=1)
        plan = plan_research_window(config, "2019-01-02", "2025-12-31", self.calendar.sessions)
        end_index = self.calendar.position(plan.eval_start)
        self.assertEqual(self.calendar.position(plan.train_end), end_index - 5 - 1)
        self.assertEqual(len(plan.training_decision_sessions), 504)
        self.assertEqual(self.calendar.position(plan.train_start), end_index - 5 - 1 - 503)
        self.assertEqual(self.calendar.position(plan.process_start),
                         self.calendar.position(plan.train_start) - 1 - 251)
        self.assertLess(plan.process_start, plan.train_start)
        self.assertLess(plan.train_end, plan.eval_start)
        self.assertIn("2024-01-02", json.dumps(plan.as_dict()))
        self.assertEqual(plan.model_training_cutoff, plan.model_train_cutoff)

    def test_preopen_adds_information_dependency(self):
        after = plan_research_window(ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=5),
                                     "2019-01-02", "2025-12-31")
        before = plan_research_window(ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=5,
                                                       decision="preopen"), "2019-01-02", "2025-12-31")
        self.assertEqual(before.process_start, self.calendar.shift(after.process_start, -1))
        self.assertEqual(before.latest_feature_observation, self.calendar.shift(after.latest_feature_observation, -1))

    def test_insufficient_history_and_missing_session_fail(self):
        config = ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=5)
        with self.assertRaisesRegex(ValueError, "Insufficient preparation history"):
            plan_research_window(config, "2024-01-02", "2025-12-31")
        with self.assertRaisesRegex(ValueError, "Insufficient evaluation coverage"):
            plan_research_window(config, "2019-01-02", "2024-01-05")
        missing = [session for session in self.calendar.sessions if session != date(2023, 12, 29)]
        with self.assertRaisesRegex(ValueError, "missing 1 required market sessions"):
            plan_research_window(config, "2019-01-02", "2025-12-31", missing)
        with self.assertRaisesRegex(ValueError, "trading session"):
            plan_research_window(ResearchConfig("2024-01-01", "2024-01-10"), "2019-01-02", "2025-12-31")

    def test_explicit_training_requires_mature_label_interval(self):
        config = ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=5,
                                train_start="2023-12-01", train_end="2023-12-29")
        with self.assertRaisesRegex(ValueError, "Training label horizon"):
            plan_research_window(config, "2019-01-02", "2025-12-31")
        valid = ResearchConfig("2024-01-02", "2024-01-10", factor_lookback_sessions=5,
                               train_start="2023-12-01", train_end="2023-12-28")
        plan = plan_research_window(valid, "2019-01-02", "2025-12-31")
        self.assertEqual(plan.train_end, date(2023, 12, 28))

    def test_warmup_supplies_first_factor_but_not_performance(self):
        plan = plan_research_window(ResearchConfig("2024-01-02", "2024-01-05", factor_lookback_sessions=3),
                                    "2019-01-02", "2025-12-31")
        returns = [0.01] * len(plan.loaded_sessions)
        factors = rolling_momentum(returns, 3)
        decision_index = plan.loaded_sessions.index(plan.eval_start)
        self.assertAlmostEqual(factors[decision_index - 1], 1.01 ** 3 - 1)
        evaluation_returns = [value for session, value in zip(plan.loaded_sessions, returns)
                              if session in plan.evaluation_sessions]
        self.assertEqual(len(evaluation_returns), 4)
        self.assertAlmostEqual(portfolio_index(evaluation_returns)[-1], 100 * 1.01 ** 4)
        self.assertNotEqual(portfolio_index(evaluation_returns)[-1], portfolio_index(returns)[-1])

    def test_future_cutoff_does_not_change_preparation_or_training(self):
        common = dict(factor_lookback_sessions=5, training_sessions=10, label_horizon_sessions=3)
        short = plan_research_window(ResearchConfig("2024-01-02", "2024-01-10", **common),
                                     "2019-01-02", "2024-01-10")
        long = plan_research_window(ResearchConfig("2024-01-02", "2024-03-28", **common),
                                    "2019-01-02", "2025-12-31")
        self.assertEqual(short.process_start, long.process_start)
        self.assertEqual(short.training_decision_sessions, long.training_decision_sessions)
        self.assertEqual(short.model_train_cutoff, long.model_train_cutoff)


class ReturnsAndDisplayTests(unittest.TestCase):
    def test_missing_is_not_zero_and_terminal_loss_does_not_recover(self):
        self.assertEqual(compound_returns([0.0, None, 0.1]), [1.0, None, None])
        self.assertEqual(compound_returns([-1, 0.5, 0.0]), [0.0, 0.0, 0.0])
        self.assertEqual(compound_returns([-1, None]), [0.0, None])
        self.assertEqual(rolling_momentum([0.1, -1, 0.2, 0.3], 2), [None, -1, None, None])
        for invalid in (-1.01, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                compound_returns([invalid])

    def test_rebase_preserves_returns_momentum_and_preparation(self):
        dates = [date(2024, 1, day) for day in (2, 3, 4, 5, 8, 9)]
        returns = [0.01, -0.02, 0.03, 0.02, 0.0, 0.01]
        original = compound_returns(returns)
        first = rebase_index(dates, original, dates[1])
        second = rebase_index(dates, original, dates[3])
        self.assertEqual(first.values[1], 100)
        self.assertEqual(second.values[3], 100)
        self.assertEqual(first.dates, tuple(dates))
        self.assertEqual(second.purpose, "display_only_individual_security_index")
        derived_returns = []
        for index in range(1, len(dates)):
            one = first.values[index] / first.values[index - 1] - 1
            two = second.values[index] / second.values[index - 1] - 1
            self.assertAlmostEqual(one, two)
            self.assertAlmostEqual(one, returns[index])
            derived_returns.append(two)
        for left, right in zip(rolling_momentum(returns[1:], 2), rolling_momentum(derived_returns, 2)):
            if left is None:
                self.assertIsNone(right)
            else:
                self.assertAlmostEqual(left, right)

    def test_ipo_missing_and_zero_base_are_explicit(self):
        dates = [date(2024, 1, day) for day in (2, 3, 4)]
        result = rebase_index(dates, [None, 1.0, 1.1], dates[0])
        self.assertEqual(result.status, "missing_base")
        self.assertEqual(result.values, (None, None, None))
        fallback = rebase_index(dates, [None, 1.0, 1.1], dates[0], fallback="first_valid_after_base")
        self.assertEqual(fallback.status, "first_valid_after_base")
        self.assertEqual(fallback.actual_base_date, dates[1])
        self.assertEqual(fallback.values[1], 100)
        self.assertIsNone(fallback.values[0])
        zero = rebase_index(dates, [0, 0, 0], dates[0], fallback="first_valid_after_base")
        self.assertEqual(zero.status, "zero_base")
        self.assertEqual(zero.values, (None, None, None))

    def test_future_rows_and_future_event_returns_cannot_change_past(self):
        historical = [0.01, 0.0, -0.02, 0.03, 0.01]
        for future in ([0.04, -1], [0.8, -0.2], [None, None], []):
            complete = historical + future
            self.assertEqual(compound_returns(complete)[:len(historical)], compound_returns(historical))
            self.assertEqual(rolling_momentum(complete, 3)[:len(historical)], rolling_momentum(historical, 3))


class LabelTests(unittest.TestCase):
    def test_labels_must_be_realized_and_available(self):
        cutoff = datetime(2024, 1, 5, 22, tzinfo=timezone.utc)
        before, after = cutoff - timedelta(minutes=1), cutoff + timedelta(minutes=1)
        rows = [
            {"id": "mature", "label_end_time": before, "available_at": before},
            {"id": "not_realized", "label_end_time": after, "available_at": before},
            {"id": "not_available", "label_end_time": before, "available_at": after},
            {"id": "missing", "label_end_time": None, "available_at": before},
        ]
        self.assertEqual([row["id"] for row in matured_training_labels(rows, cutoff)], ["mature"])
        self.assertTrue(label_is_mature(cutoff, cutoff, cutoff))
        with self.assertRaisesRegex(ValueError, "timezone"):
            label_is_mature(datetime(2024, 1, 5), before, cutoff)


if __name__ == "__main__":
    unittest.main()
