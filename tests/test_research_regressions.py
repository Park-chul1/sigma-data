"""Independent causality/provenance regressions using synthetic CIZ code values.

Future-row invariance checks code causality. A separate revision test deliberately
shows why a modern historical snapshot still cannot certify strict PIT.
"""

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from src.derive.catalog import ContentHashes, verify_artifacts
from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig, calendar_schedule
from tests.test_pipeline import CanonicalFixture


class ResearchRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = CanonicalFixture(self.root)

    def build(self, **overrides):
        config = dict(requested_start="2024-01-22", requested_end="2024-01-31",
                      factor_lookback_sessions=3, training_sessions=3,
                      label_horizon_sessions=2, availability_lag_sessions=1)
        config.update(overrides)
        return build_dataset(ResearchConfig(**config), data_root=self.root)

    @staticmethod
    def query(folder, artifact, sql):
        with duckdb.connect() as con:
            con.execute("SET TimeZone='UTC'")
            con.read_parquet(str(folder / artifact)).create_view("result")
            return con.execute(sql).fetchall()

    def mutate_daily(self, permno, session, **values):
        fields = {"price_raw": 2, "ret_total": 4, "ret_ex_div": 5, "volume": 6,
                  "delist_flag": 7, "ret_missing_flag": 10, "distribution_flag": 11}
        changed = []
        for original in self.fixture.daily:
            row = list(original)
            if row[0] == permno and row[1] == date.fromisoformat(session):
                for field, value in values.items():
                    row[fields[field]] = value
            changed.append(tuple(row))
        self.fixture.daily = changed

    def test_future_terminal_event_and_survival_changes_preserve_prior_inputs(self):
        initial, first_manifest = self.build()
        past = "SELECT * FROM result WHERE decision_date<=DATE '2024-01-24' ORDER BY permno,decision_date"
        expected = self.query(initial, "features.parquet", past)
        universe = """SELECT permno,decision_date,eligible_for_research FROM result
            WHERE decision_date<=DATE '2024-01-24' ORDER BY permno,decision_date"""
        expected_universe = self.query(initial, "features.parquet", universe)

        # Change an actual future delisting ledger, storage row, and survival end.
        # Matching is exact: Jan 30 event -> Jan 31 return storage, not nearest.
        self.fixture.delistings.append((100, date(2024, 1, 30), -0.5,
                                        "GDR", "DEAD", "BANK", "NA", None, None))
        self.mutate_daily(100, "2024-01-31", ret_total=-0.5, ret_ex_div=-0.5,
                          delist_flag="Y", distribution_flag="D1")
        self.fixture.info = [tuple(list(row[:2]) + [date(2024, 1, 30)] + list(row[3:]))
                             if row[0] == 100 and row[1] == date(2024, 1, 16) else row
                             for row in self.fixture.info]
        self.fixture.write()
        changed, changed_manifest = self.build()
        self.assertNotEqual(first_manifest["snapshot_id"], changed_manifest["snapshot_id"])
        self.assertEqual(expected, self.query(changed, "features.parquet", past))
        self.assertEqual(expected_universe, self.query(changed, "features.parquet", universe))

        # Remove future source rows and the ledger record, then narrow coverage.
        self.fixture.daily = [row for row in self.fixture.daily if row[1] <= date(2024, 1, 24)]
        self.fixture.delistings = [row for row in self.fixture.delistings if row[0] != 100]
        self.fixture.write()
        truncated, _ = self.build(requested_end="2024-01-24")
        self.assertEqual(expected, self.query(truncated, "features.parquet", past))
        self.assertEqual(expected_universe, self.query(truncated, "features.parquet", universe))
        columns = {row[0] for row in self.query(truncated, "features.parquet", "DESCRIBE result")}
        self.assertTrue({"valid_to", "terminal_event_date", "delisting_return", "successor_permno",
                         "label_end_date", "forward_vendor_return"}.isdisjoint(columns))

    def test_historical_revision_changes_past_and_does_not_become_strict_pit(self):
        before, _ = self.build()
        query = "SELECT momentum FROM result WHERE permno=100 AND decision_date=DATE '2024-01-19'"
        original = self.query(before, "features.parquet", query)
        self.mutate_daily(100, "2024-01-18", ret_total=0.3, ret_ex_div=0.3)
        self.fixture.write()
        after, manifest = self.build()
        self.assertNotEqual(original, self.query(after, "features.parquet", query))
        self.assertEqual(manifest["pit_guarantee"], "approximate")
        with self.assertRaisesRegex(ValueError, "Strict PIT unavailable"):
            build_dataset(ResearchConfig("2024-01-22", "2024-01-24", factor_lookback_sessions=3),
                          data_root=self.root, pit_mode="strict")

    def test_unavailable_observation_is_withheld_even_with_planned_session_join(self):
        def delayed_schedule(*args, **kwargs):
            rows = calendar_schedule(*args, **kwargs)
            for row in rows:
                if row["date"] == date(2024, 1, 19):
                    row["available_at"] += timedelta(days=1)
            return rows

        with patch("src.derive.pipeline.calendar_schedule", delayed_schedule):
            folder, _ = self.build()
        self.assertEqual(self.query(folder, "features.parquet", """SELECT count(*) FROM result
            WHERE permno=100 AND decision_date=DATE '2024-01-22'"""), [(0,)])
        self.assertEqual(self.query(folder, "features.parquet", """SELECT count(*) FROM result
            WHERE available_at>decision_time"""), [(0,)])

    def test_preopen_price_volume_are_unchanged_by_same_day_close_revision(self):
        before, _ = self.build(decision="preopen", availability_lag_sessions=0)
        query = "SELECT * FROM result WHERE permno=100 AND decision_date=DATE '2024-01-22'"
        expected = self.query(before, "features.parquet", query)
        self.assertTrue(expected)
        self.mutate_daily(100, "2024-01-22", price_raw=999.0, volume=1_000_000.0,
                          ret_total=0.5, ret_ex_div=0.5)
        self.fixture.write()
        after, _ = self.build(decision="preopen", availability_lag_sessions=0)
        self.assertEqual(expected, self.query(after, "features.parquet", query))
        self.assertEqual(self.query(after, "features.parquet", """SELECT price_raw,volume_raw_shares
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-23'"""), [(999.0, 1_000_000.0)])

    def test_security_gap_and_ipo_keep_explicit_ineligible_rows(self):
        self.fixture.daily = [row for row in self.fixture.daily
                              if not (row[0] == 100 and row[1] == date(2024, 1, 18))]
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "features.parquet", """SELECT decision_date,has_required_history
            FROM result WHERE permno=100 AND decision_date BETWEEN DATE '2024-01-22' AND DATE '2024-01-24'
            ORDER BY decision_date"""), [(date(2024, 1, 22), False), (date(2024, 1, 23), False),
                                        (date(2024, 1, 24), True)])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT DISTINCT wealth_index,index_status
            FROM result WHERE permno=100 AND date>=DATE '2024-01-19'"""),
                         [(None, "MISSING_RETURN_OR_SESSION_GAP")])
        self.assertEqual(self.query(folder, "labels.parquet", """SELECT forward_vendor_return,label_status
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-17'"""), [(None, "SESSION_GAP")])
        self.assertEqual(self.query(folder, "features.parquet", """SELECT decision_date,eligible_for_research
            FROM result WHERE permno=400 AND decision_date<=DATE '2024-01-26' ORDER BY decision_date"""),
                         [(date(2024, 1, 24), False), (date(2024, 1, 25), False), (date(2024, 1, 26), True)])

    def test_configuration_changes_invalidate_run_and_availability_policy_cache(self):
        first, original = self.build()
        second, changed = self.build(availability_lag_sessions=0)
        self.assertNotEqual(first, second)
        self.assertNotEqual(original["cache_id"], changed["cache_id"])
        self.assertFalse(changed["cache_months_reused"])
        self.assertEqual(self.query(second, "evaluation_features.parquet", """SELECT observation_date
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-22'"""), [(date(2024, 1, 22),)])

    def test_missing_output_cannot_be_hidden_by_removing_its_checksum(self):
        folder, _ = self.build()
        manifest_path = folder / "_SUCCESS.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["artifacts"].pop("features.parquet")
        (folder / "features.parquet").unlink()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "inventory.*corrupt"):
            self.build()

    def test_training_is_mature_by_both_time_boundaries(self):
        folder, manifest = self.build(decision="preopen", availability_lag_sessions=0)
        cutoff = manifest["plan"]["model_train_cutoff"]
        with duckdb.connect() as con:
            con.read_parquet(str(folder / "training.parquet")).create_view("training")
            rows, violations = con.execute("""SELECT count(*),count(*) FILTER(
                WHERE label_available_at>?::TIMESTAMPTZ OR label_end_time>?::TIMESTAMPTZ
                OR available_at>decision_time OR forward_vendor_return IS NULL) FROM training""",
                [cutoff, cutoff]).fetchone()
        self.assertGreater(rows, 0)
        self.assertEqual(violations, 0)

    def test_ingestion_timestamp_is_provenance_not_historical_availability(self):
        raw = self.root / "raw/crsp/stkdelists"
        raw.mkdir(parents=True)
        (raw / "_SUCCESS.json").write_text(json.dumps({
            "source_schema": "crsp", "source_table": "stkdelists",
            "downloaded_at_utc": "2026-09-01T04:58:36+00:00"}))
        folder, manifest = self.build()
        snapshot = json.loads((folder.parents[1] / "snapshots" /
                               (manifest["snapshot_id"] + ".json")).read_text())
        provenance = snapshot["temporal_provenance"]
        self.assertIsNone(provenance["source_vintage"])
        self.assertEqual(provenance["ingested_at"][0]["timestamp"], "2026-09-01T04:58:36+00:00")
        self.assertEqual(self.query(folder, "features.parquet", "SELECT DISTINCT year(available_at) FROM result"),
                         [(2024,)])


class ArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hashes = ContentHashes(self.root / "hashes.json")

    def test_empty_inventory_does_not_certify_success(self):
        (self.root / "_SUCCESS.json").write_text(json.dumps({"artifacts": {}}))
        with self.assertRaisesRegex(RuntimeError, "inventory.*corrupt"):
            verify_artifacts(self.root, self.hashes)

    def test_same_size_and_restored_mtime_content_change_invalidates_hash(self):
        path = self.root / "source.bin"
        path.write_bytes(b"old-data")
        original_stat = path.stat()
        original_hash = self.hashes.file(path)
        path.write_bytes(b"new-data")
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertNotEqual(original_hash, self.hashes.file(path))


if __name__ == "__main__":
    unittest.main()
