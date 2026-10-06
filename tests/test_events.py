"""Synthetic economic invariants; fixtures contain no licensed observations."""

from datetime import date
import unittest

from src.derive.events import (
    HeldOutcomeError,
    apply_verified_split,
    classify_daily_distribution,
    classify_event,
    research_wealth_step,
    resolve_daily_return,
    validate_held_outcomes,
    validate_return,
)


def daily(**changes):
    row = {"permno": 10001, "date": date(2020, 1, 3), "price_raw": 100.0,
           "ret_total": 0.0, "ret_ex_div": 0.0, "delist_flag": "N",
           "distribution_return_flag": "NO", "ret_missing_flag": "NA"}
    row.update(changes)
    return row


def terminal(**changes):
    row = {"permno": 10001, "event_date": date(2020, 1, 2),
           "action_type": "MER", "status_type": "FPAY", "reason_type": "UNAV",
           "payment_type": "CASH", "delisting_return": -0.5, "successor_permno": 20002}
    row.update(changes)
    return row


class EventTests(unittest.TestCase):
    def test_split_preserves_value_without_modifying_raw_prices(self):
        shares = apply_verified_split(7.0, 2.0)
        self.assertEqual(7 * 100.0, shares * 50.0)
        row = daily(price_raw=50.0, distribution_return_flag="S1")
        result = resolve_daily_return(row)
        self.assertEqual(result["price_raw"], 50)
        self.assertEqual(research_wealth_step(700, result["ret_total_backtest"]), 700)
        self.assertEqual(result["event_types"], ("split_or_stock_dividend",))
        self.assertTrue(result["event_terms_incomplete"])

    def test_reverse_split_is_not_a_return(self):
        self.assertEqual(apply_verified_split(20, 0.5) * 100, 20 * 50)
        with self.assertRaises(ValueError):
            apply_verified_split(20, 0)

    def test_dividend_is_included_exactly_once(self):
        # Price falls 100 -> 98 and holder receives 2: value stays 100.
        result = resolve_daily_return(daily(price_raw=98, ret_ex_div=-0.02,
                                            distribution_return_flag="C1"))
        self.assertEqual(research_wealth_step(100, result["ret_total_backtest"]), 100)
        with self.assertRaisesRegex(ValueError, "Do not add cash"):
            research_wealth_step(100, result["ret_total_backtest"], cash_distribution=2)

    def test_ticker_change_retains_identity_and_is_not_a_merger_link(self):
        before = resolve_daily_return(daily(ticker="OLD", ret_total=0.1))
        after = resolve_daily_return(daily(ticker="NEW", ret_total=-0.1))
        self.assertEqual(before["permno"], after["permno"])
        self.assertAlmostEqual(research_wealth_step(research_wealth_step(100, 0.1), -0.1), 99)
        event = classify_event(terminal())
        self.assertEqual(event["permno"], 10001)
        self.assertEqual(event["successor_permno"], 20002)
        self.assertFalse(event["successor_auto_linked"])
        self.assertEqual(event["payment_type"], "CASH")

    def test_embedded_delisting_not_compounded_again_or_filtered_by_eligibility(self):
        result = resolve_daily_return(
            daily(ret_total=-0.5, delist_flag="Y", is_investable=False,
                  distribution_return_flag="D1"), terminal(), reconciled=True)
        self.assertEqual(result["ret_total_backtest"], -0.5)
        self.assertEqual(research_wealth_step(100, result["ret_total_backtest"]), 50)
        self.assertEqual(result["return_source"], "CRSP_DELIST_EMBEDDED")
        self.assertFalse(result["usable_as_feature"])
        self.assertIsNone(result["cash_available_at"])
        self.assertEqual(validate_held_outcomes([result], [10001])["status"], "complete")
        with self.assertRaises(HeldOutcomeError):
            validate_held_outcomes([result], [10001], accounting_mode="cash_shares")

    def test_missing_terminal_return_is_never_zero(self):
        result = resolve_daily_return(
            daily(ret_total=None, delist_flag="Y", distribution_return_flag="D1"),
            terminal(delisting_return=None), reconciled=True)
        self.assertIsNone(result["ret_total_backtest"])
        self.assertEqual(result["review_reason"], "MISSING_TERMINAL_RETURN")
        with self.assertRaises(HeldOutcomeError):
            validate_held_outcomes([result], [10001])
        self.assertEqual(validate_held_outcomes([result], [10001], mode="incomplete")["status"], "incomplete")

    def test_optional_absent_event_flag_and_fallback_provenance_match_pipeline(self):
        result = resolve_daily_return(
            daily(ret_total=-0.5, delist_flag="Y", distribution_return_flag="D1"),
            terminal(return_storage_date=date(2020, 1, 3), source_return_storage_date=None,
                     storage_date_basis="NEXT_SESSION_FALLBACK_NOT_SOURCE_DATE",
                     event_ret_missing_flag=None, event_missing_flag_present=False), reconciled=True)
        self.assertEqual(result["ret_total_backtest"], -0.5)
        self.assertIsNone(result["source_return_storage_date"])
        self.assertEqual(result["storage_date_basis"], "NEXT_SESSION_FALLBACK_NOT_SOURCE_DATE")

    def test_bad_terminal_reconciliation_is_rejected(self):
        row = daily(ret_total=-0.5, delist_flag="Y", distribution_return_flag="D1")
        self.assertIsNone(resolve_daily_return(row)["ret_total_backtest"])
        for bad in (terminal(permno=9), terminal(delisting_return=-0.4),
                    terminal(delisting_return=None), terminal(event_date=row["date"]),
                    terminal(return_storage_date=date(2020, 1, 6)),
                    terminal(return_storage_date=None),
                    terminal(event_ret_missing_flag="MV")):
            with self.subTest(event=bad), self.assertRaises(ValueError):
                resolve_daily_return(row, bad, reconciled=True)
        with self.assertRaisesRegex(ValueError, "storage date"):
            resolve_daily_return(dict(row, date=date(2020, 1, 6)), terminal(), reconciled=True)
        result = resolve_daily_return(dict(row, date=date(2020, 1, 6)),
                                      terminal(deldlydt=date(2020, 1, 6)), reconciled=True)
        self.assertEqual(result["ret_total_backtest"], -0.5)
        self.assertEqual(result["storage_date_basis"], "SOURCE_DELDLYDT")

    def test_numeric_partial_vendor_return_is_not_released(self):
        for flag in ("MV", None, "UNSEEN"):
            row = daily(ret_total=-0.2, ret_missing_flag=flag, delist_flag="Y",
                        distribution_return_flag="MU")
            result = resolve_daily_return(row, terminal(delisting_return=-0.2), reconciled=True)
            self.assertEqual(result["ret_total_vendor"], -0.2)
            self.assertIsNone(result["ret_total_backtest"])
            self.assertEqual(result["review_reason"], "INCOMPLETE_VENDOR_RETURN")
            self.assertFalse(result["vendor_return_complete"])
            with self.assertRaises(HeldOutcomeError):
                validate_held_outcomes([result], [10001])

    def test_unknown_codes_and_missing_held_observation_fail_closed(self):
        result = resolve_daily_return(daily(distribution_return_flag="UNSEEN"))
        self.assertIsNone(result["ret_total_backtest"])
        self.assertEqual(result["distribution_status"], "unknown")
        self.assertEqual(classify_event(terminal(action_type="UNSEEN"))["event_status"], "unsupported")
        self.assertEqual(resolve_daily_return(daily(delist_flag="UNSEEN"))["review_reason"],
                         "UNKNOWN_DELISTING_FLAG")
        for rows in ([result], [], [dict(daily(), ret_total_backtest=0, event_status="unsupported")]):
            with self.subTest(rows=rows), self.assertRaises(HeldOutcomeError):
                validate_held_outcomes(rows, [10001])

    def test_distribution_terms_are_not_inferred(self):
        for effect in ("spin_off", "rights_distribution", "stock_exchange", "split"):
            result = classify_event({"permno": 1, "event_type": effect})
            self.assertEqual(result["event_status"], "unsupported")
        result = classify_daily_distribution(daily(distribution_return_flag="CS"))
        self.assertEqual(result["event_types"], ("cash_dividend", "split_or_stock_dividend"))
        self.assertEqual(result["ledger_status"], "unsupported")

    def test_future_event_mutation_does_not_change_an_earlier_return(self):
        row = daily(ret_total=0.1)
        a = resolve_daily_return(row)
        b = resolve_daily_return(row, terminal(event_date=date(2021, 1, 1)))
        c = resolve_daily_return(row, terminal(event_date=date(2021, 1, 1),
                                               delisting_return=-1.0, action_type="UNKNOWN"))
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_return_quality_zero_and_total_loss_are_distinct_from_missing(self):
        for value, expected in [(None, "MISSING"), (0, "VALID"), (-1, "VALID"),
                                (-66, "BELOW_MINUS_ONE"), (float("nan"), "NONFINITE"),
                                (float("inf"), "NONFINITE"), ("0", "INVALID_TYPE")]:
            with self.subTest(value=value):
                self.assertEqual(validate_return(value), expected)
        self.assertEqual(research_wealth_step(100, -1), 0)
        self.assertEqual(research_wealth_step(0, 100), 0)
        with self.assertRaises(HeldOutcomeError):
            research_wealth_step(100, None)

    def test_no_legacy_fallback_and_no_duplicate_held_rows(self):
        with self.assertRaises(ValueError):
            resolve_daily_return(daily(), source_format="CRSP_LEGACY")
        row = resolve_daily_return(daily())
        with self.assertRaises(HeldOutcomeError):
            validate_held_outcomes([row, row], [10001])

    def test_pipeline_panel_guard_compatibility(self):
        row = dict(daily(), ret_total_backtest=0.0, requires_review=False,
                   return_quality="VALID_VENDOR_RETURN")
        self.assertEqual(validate_held_outcomes([row], [10001])["status"], "complete")
        for changes in ({"delist_flag": "Y", "action_type": "MER"},
                        {"distribution_return_flag": "S1"},
                        {"distribution_return_flag": "C1"},
                        {"ledger_status": "UNSUPPORTED"}):
            with self.subTest(changes=changes), self.assertRaises(HeldOutcomeError):
                validate_held_outcomes([dict(row, **changes)], [10001], accounting_mode="cash_shares")
        for changes in ({"distribution_return_flag": "UNKNOWN"},
                        {"ret_missing_flag": "MV"},
                        {"delist_flag": None},
                        {"delist_flag": "UNKNOWN"},
                        {"delist_flag": "Y", "action_type": "UNKNOWN"},
                        {"return_quality": "INCOMPLETE_VENDOR_RETURN"},
                        {"event_status": "UNSUPPORTED"},
                        {"research_status": "INCOMPLETE"}):
            with self.subTest(changes=changes), self.assertRaises(HeldOutcomeError):
                validate_held_outcomes([dict(row, **changes)], [10001])

    def test_held_outcomes_require_one_complete_observation_interval(self):
        first = resolve_daily_return(daily())
        other_day = resolve_daily_return(daily(permno=10002, date=date(2020, 1, 6)))
        report = validate_held_outcomes([first, other_day], [10001, 10002], mode="incomplete")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["issues"][0]["reason"], "MIXED_HOLDING_INTERVAL_DATES")
        with self.assertRaises(HeldOutcomeError):
            validate_held_outcomes([first, other_day], [10001, 10002])
        with self.assertRaises(HeldOutcomeError):
            validate_held_outcomes([dict(first, date=None)], [10001])
        same_day = dict(other_day, date=first["date"])
        self.assertEqual(validate_held_outcomes([first, same_day], [10001, 10002])["status"], "complete")


if __name__ == "__main__":
    unittest.main()
