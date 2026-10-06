"""CIZ source-code and exact event/storage reconciliation regressions."""

from datetime import date
from pathlib import Path
import tempfile
import unittest

from tests import test_pipeline as fixture_support


class EventReconciliationTests(unittest.TestCase):
    config = fixture_support.PipelineTests.config
    build = fixture_support.PipelineTests.build
    query = staticmethod(fixture_support.PipelineTests.query)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = fixture_support.CanonicalFixture(self.root)

    def change_daily(self, permno, day, **changes):
        indices = {"date": 1, "ret_total": 4, "delist_flag": 7,
                   "ret_missing_flag": 10, "distribution_return_flag": 11}
        for index, row in enumerate(self.fixture.daily):
            if row[0] == permno and row[1] == date.fromisoformat(day):
                revised = list(row)
                for key, value in changes.items():
                    revised[indices[key]] = value
                self.fixture.daily[index] = tuple(revised)
                return
        self.fail("Synthetic daily key absent")

    def event_source_fields(self, name, values):
        self.fixture.write_table("delistings", "stkdelists", fixture_support.DELIST_DDL + ", " + name,
                                 [tuple(row) + (value,) for row, value in zip(self.fixture.delistings, values)])

    def test_n_means_regular_and_unknown_flags_never_pass(self):
        self.change_daily(100, "2024-01-16", delist_flag=None)
        self.change_daily(100, "2024-01-17", delist_flag="UNSEEN")
        self.fixture.write()
        folder, _ = self.build()
        normal = self.query(folder, "panel.parquet", """SELECT is_delisting_return,daily_record_type,
            ret_total_backtest FROM result WHERE permno=100 AND date=DATE '2024-01-18'""")
        self.assertEqual(normal, [(False, "ORDINARY_DAILY", 0.01)])
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT return_quality,ret_total_backtest
            FROM result WHERE permno=100 AND date IN (DATE '2024-01-16',DATE '2024-01-17')"""),
                         [("UNKNOWN_DELISTING_FLAG", None)] * 2)

    def test_explicit_source_storage_date_overrides_next_session_fallback(self):
        self.change_daily(200, "2024-01-19", date=date(2024, 1, 22))
        self.fixture.write()
        self.event_source_fields("deldlydt DATE", [date(2024, 1, 22), date(2024, 1, 19)])
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT date,terminal_event_date,
            return_storage_date,storage_date_basis,ret_total_backtest,terminal_reconciliation
            FROM result WHERE permno=200 AND delist_flag='Y'"""),
            [(date(2024, 1, 22), date(2024, 1, 18), date(2024, 1, 22), "SOURCE_DELDLYDT", -1.0, "MATCHED_VALUE")])

    def test_absent_source_storage_is_explicit_fallback_and_null_is_unknown(self):
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT terminal_event_date,
            return_storage_date,source_return_storage_date,storage_date_basis FROM result
            WHERE permno=200 AND delist_flag='Y'"""),
            [(date(2024, 1, 18), date(2024, 1, 19), None, "NEXT_SESSION_FALLBACK_NOT_SOURCE_DATE")])
        self.event_source_fields("return_storage_date DATE", [None, date(2024, 1, 19)])
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT return_quality,ret_total_backtest
            FROM result WHERE permno=200 AND delist_flag='Y'"""), [("UNMATCHED_TERMINAL_EVENT", None)])
        self.assertEqual(self.query(folder, "terminal_reconciliation.parquet", """SELECT reconciliation_status
            FROM result WHERE permno=200 ORDER BY observed_date"""),
            [("MISSING_SOURCE_STORAGE_DATE",), ("UNMATCHED_TERMINAL_EVENT",)])

    def test_wrong_event_key_is_not_nearest_matched_or_applied_to_regular_row(self):
        event = list(self.fixture.delistings[0])
        event[1] = date(2024, 1, 17)
        self.fixture.delistings[0] = tuple(event)
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT date,return_quality,ret_total_backtest
            FROM result WHERE permno=200 AND date IN (DATE '2024-01-18',DATE '2024-01-19') ORDER BY date"""),
            [(date(2024, 1, 18), "VALID_VENDOR_RETURN", 0.01),
             (date(2024, 1, 19), "UNMATCHED_TERMINAL_EVENT", None)])
        self.assertEqual(self.query(folder, "terminal_reconciliation.parquet", """SELECT reconciliation_status
            FROM result WHERE permno=200 ORDER BY observed_date"""),
            [("EXPECTED_TERMINAL_FLAG",), ("UNMATCHED_TERMINAL_EVENT",)])

    def test_missing_terminal_daily_row_is_preserved_in_reverse_audit(self):
        self.fixture.daily = [row for row in self.fixture.daily
                              if not (row[0] == 200 and row[7] == "Y")]
        self.fixture.write()
        folder, manifest = self.build()
        self.assertEqual(self.query(folder, "terminal_reconciliation.parquet", """SELECT effective_date,
            return_storage_date,actual_daily_date,reconciliation_status FROM result WHERE permno=200"""),
            [(date(2024, 1, 18), date(2024, 1, 19), None, "MISSING_TERMINAL_DAILY_ROW")])
        self.assertEqual(manifest["unresolved_terminal_events"], 1)
        self.assertEqual(manifest["status"], "INCOMPLETE_RETURNS")

    def test_nonfinite_and_below_minus_one_blocked_zero_and_total_loss_retained(self):
        for day, value in (("2024-01-16", float("nan")), ("2024-01-17", float("inf")),
                           ("2024-01-18", -1.01), ("2024-01-19", -1.0), ("2024-01-22", 0.0)):
            self.change_daily(100, day, ret_total=value)
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT return_quality,ret_total_backtest
            FROM result WHERE permno=100 AND date BETWEEN DATE '2024-01-16' AND DATE '2024-01-22'
            ORDER BY date"""), [("INVALID_RETURN", None)] * 3 +
                         [("VALID_VENDOR_RETURN", -1.0), ("VALID_VENDOR_RETURN", 0.0)])

    def test_numeric_and_null_terminal_mismatches_are_independent_from_daily_quality(self):
        for index in (0, 1):
            event = list(self.fixture.delistings[index])
            event[2] = -0.5
            self.fixture.delistings[index] = tuple(event)
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT return_quality,ret_total_backtest,
            terminal_reconciliation FROM result WHERE delist_flag='Y' ORDER BY permno"""),
            [("TERMINAL_RETURN_MISMATCH", None, "TERMINAL_RETURN_MISMATCH")] * 2)

    def test_numeric_mv_and_event_missing_flag_disagreement_fail_closed(self):
        self.change_daily(100, "2024-01-17", ret_missing_flag="MV")
        self.fixture.write()
        self.event_source_fields("event_ret_missing_flag VARCHAR", ["MV", "NA"])
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT ret_total_vendor,ret_total_backtest,
            return_quality FROM result WHERE permno=100 AND date=DATE '2024-01-17'"""),
            [(0.01, None, "INCOMPLETE_VENDOR_RETURN")])
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT ret_total_vendor,ret_total_backtest,
            return_quality FROM result WHERE permno=200 AND delist_flag='Y'"""),
            [(-1.0, None, "TERMINAL_MISSING_FLAG_MISMATCH")])

    def test_future_terminal_events_mutation_and_removal_preserve_past_features(self):
        folder, _ = self.build()
        query = "SELECT * FROM result WHERE decision_date<=DATE '2024-01-18' ORDER BY permno,decision_date"
        expected = self.query(folder, "features.parquet", query)
        self.fixture.delistings = [(100, date(2024, 1, 30), -0.9, "UNKNOWN", "FPAY", "UNAV", "CASH", None, None)]
        self.fixture.write()
        changed, _ = self.build()
        self.assertEqual(expected, self.query(changed, "features.parquet", query))
        self.fixture.delistings = []
        self.fixture.write()
        removed, _ = self.build()
        self.assertEqual(expected, self.query(removed, "features.parquet", query))


if __name__ == "__main__":
    unittest.main()
