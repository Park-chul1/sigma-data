"""Small canonical-Parquet integration fixtures; no licensed data in Git."""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig, calendar_schedule


DAILY_DDL = """permno BIGINT, date DATE, price_raw DOUBLE, market_cap_kusd DOUBLE,
    ret_total DOUBLE, ret_ex_div DOUBLE, volume_raw_shares DOUBLE, delist_flag VARCHAR,
    price_flag VARCHAR, market_cap_flag VARCHAR, ret_missing_flag VARCHAR,
    distribution_return_flag VARCHAR"""
INFO_DDL = """permno BIGINT, valid_from DATE, valid_to DATE, ticker VARCHAR, cusip VARCHAR,
    primary_exchange VARCHAR, share_type VARCHAR, security_type VARCHAR,
    security_subtype VARCHAR, us_incorporated_flag VARCHAR, issuer_type VARCHAR,
    trading_status_flag VARCHAR, conditional_type VARCHAR, share_class VARCHAR"""
DELIST_DDL = """permno BIGINT, event_date DATE, delisting_return DOUBLE,
    action_type VARCHAR, status_type VARCHAR, reason_type VARCHAR, payment_type VARCHAR,
    successor_permno BIGINT, successor_permco BIGINT"""


class CanonicalFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.sessions = [r["date"] for r in calendar_schedule("2024-01-02", "2024-01-31")]
        self.daily = []
        self.info = []
        self.delistings = [
            (200, date(2024, 1, 18), -1.0, "GDR", "DEAD", "BANK", "NA", None, None),
            (201, date(2024, 1, 18), None, "GDR", "DEAD", "BANK", "NA", None, None),
        ]
        for session in self.sessions:
            for permno in (100, 200, 201, 300, 400, 500):
                if permno in (200, 201) and session > date(2024, 1, 19):
                    continue
                if permno == 400 and session < date(2024, 1, 23):
                    continue
                terminal = permno in (200, 201) and session == date(2024, 1, 19)
                missing = (permno == 300 and session == date(2024, 1, 17)) or (permno == 201 and terminal)
                ret = None if missing else -1.0 if terminal else 0.0 if permno == 500 else 0.01
                price = 100.0
                flag, retx = "D1" if terminal else "NO", ret
                if permno == 500:
                    if session >= date(2024, 1, 16):
                        price = 50.0
                    if session == date(2024, 1, 16):
                        flag = "S1"
                    if session >= date(2024, 1, 18):
                        price = 49.0
                    if session == date(2024, 1, 18):
                        flag, retx = "C1", -0.02
                self.daily.append((permno, session, price, 1_000_000.0, ret, retx,
                                   10_000.0, "Y" if terminal else "N", "A", "A", "NA", flag))
        for permno in (100, 200, 201, 300, 400, 500):
            if permno == 100:
                self.info.extend([self.history(100, "OLD", "2024-01-02", "2024-01-15"),
                                  self.history(100, "NEW", "2024-01-16", "2024-01-31")])
            else:
                self.info.append(self.history(permno, f"S{permno}",
                                              "2024-01-23" if permno == 400 else "2024-01-02",
                                              "2024-01-18" if permno in (200, 201) else "2024-01-31"))
        self.write()

    @staticmethod
    def history(permno, ticker, start, end):
        return (permno, date.fromisoformat(start), date.fromisoformat(end), ticker, "12345678",
                "N", "NS", "EQTY", "COM", "Y", "CORP", "A", "RW", "A")

    def write_table(self, relative, source_table, ddl, rows):
        path = self.root / "normalized/crsp" / relative / "part-000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE fixture({ddl})")
            if rows:
                con.executemany("INSERT INTO fixture VALUES (" + ",".join("?" for _ in rows[0]) + ")", rows)
            path.unlink(missing_ok=True)
            con.execute("COPY fixture TO ? (FORMAT PARQUET)", [str(path)])
        finally:
            con.close()
        manifest = {"schema_version": 1, "source": f"data/raw/crsp/{source_table}/part-000.parquet", "rows": len(rows)}
        if source_table == "stkdlysecurityprimarydata":
            manifest.update(min_date=min(row[1] for row in rows).isoformat(),
                            max_date=max(row[1] for row in rows).isoformat())
        (path.parent / "_SUCCESS.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")

    def write(self):
        self.write_table("daily/year=2024/month=01", "stkdlysecurityprimarydata", DAILY_DDL, self.daily)
        self.write_table("security_info", "stksecurityinfohist", INFO_DDL, self.info)
        self.write_table("delistings", "stkdelists", DELIST_DDL, self.delistings)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = CanonicalFixture(self.root)

    def config(self, **overrides):
        fields = dict(requested_start="2024-01-22", requested_end="2024-01-26",
                      factor_lookback_sessions=3, training_sessions=3,
                      label_horizon_sessions=2, availability_lag_sessions=1)
        fields.update(overrides)
        return ResearchConfig(**fields)

    def build(self, **overrides):
        return build_dataset(self.config(**overrides), data_root=self.root)

    @staticmethod
    def query(folder, artifact, sql="SELECT * FROM result"):
        con = duckdb.connect()
        try:
            con.execute("SET TimeZone='UTC'")
            con.read_parquet(str(folder / artifact)).create_view("result")
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_preparation_first_factor_and_mature_training(self):
        folder, manifest = self.build()
        first = self.query(folder, "evaluation_features.parquet", """SELECT decision_date,observation_date,momentum,
            available_at<=decision_time FROM result WHERE permno=100 ORDER BY decision_date LIMIT 1""")[0]
        self.assertEqual(first[:2], (date(2024, 1, 22), date(2024, 1, 19)))
        self.assertAlmostEqual(first[2], 1.01 ** 3 - 1)
        self.assertTrue(first[3])
        self.assertEqual(self.query(folder, "training.parquet", "SELECT count(*) FROM result WHERE permno=100"), [(3,)])
        self.assertEqual(self.query(folder, "training.parquet", """SELECT count(*) FROM result
            WHERE label_available_at>TIMESTAMPTZ '2024-01-22 22:00:00+00'
               OR label_end_date>DATE '2024-01-22'"""), [(0,)])
        dates = self.query(folder, "evaluation_features.parquet", "SELECT min(decision_date),max(decision_date) FROM result")[0]
        self.assertEqual(dates, (date(2024, 1, 22), date(2024, 1, 26)))
        self.assertLess(manifest["plan"]["process_start"], manifest["plan"]["train_start"])
        self.assertEqual(manifest["rows"]["calendar.parquet"], len(manifest["plan"]["loaded_sessions"]))
        self.assertEqual(self.query(folder, "labels.parquet", """SELECT forward_vendor_return,label_status
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-26'"""),
            [(None, "PERIOD_END_TRUNCATED")])

    def test_preopen_and_zero_lag_still_exclude_same_day_ohlcv(self):
        folder, _ = self.build(decision="preopen", availability_lag_sessions=0)
        self.assertEqual(self.query(folder, "evaluation_features.parquet", """SELECT observation_date
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-22'"""), [(date(2024, 1, 19),)])
        self.assertEqual(self.query(folder, "features.parquet", """SELECT count(*) FROM result
            WHERE available_at>decision_time OR observation_date>=decision_date"""), [(0,)])

    def test_future_cutoff_and_future_event_mutation_preserve_past_features(self):
        short, short_manifest = self.build(requested_end="2024-01-24")
        long, long_manifest = self.build(requested_end="2024-01-31")
        select = "SELECT * FROM result WHERE decision_date<=DATE '2024-01-24' ORDER BY permno,decision_date"
        expected = self.query(short, "features.parquet", select)
        self.assertEqual(expected, self.query(long, "features.parquet", select))
        self.assertEqual(short_manifest["plan"]["process_start"], long_manifest["plan"]["process_start"])
        self.assertEqual(short_manifest["plan"]["model_train_cutoff"], long_manifest["plan"]["model_train_cutoff"])
        revised = []
        for row in self.fixture.daily:
            row = list(row)
            if row[0] == 100 and row[1] >= date(2024, 1, 25):
                row[4], row[5], row[11] = 0.75, -0.5, "CS"
            revised.append(tuple(row))
        self.fixture.daily = revised
        self.fixture.write()
        changed, changed_manifest = self.build(requested_end="2024-01-31")
        self.assertNotEqual(changed_manifest["snapshot_id"], long_manifest["snapshot_id"])
        self.assertEqual(expected, self.query(changed, "features.parquet", select))
        wealth = "SELECT * FROM result WHERE date<=DATE '2024-01-24' ORDER BY permno,date"
        self.assertEqual(self.query(short, "display_indices.parquet", wealth),
                         self.query(changed, "display_indices.parquet", wealth))
        # Physically remove future source rows as well as selecting an earlier
        # endpoint. The snapshot ID changes, but anchor/preparation/cutoff stay
        # fixed and the observable historical values must still be identical.
        self.fixture.daily = [row for row in self.fixture.daily if row[1] <= date(2024, 1, 24)]
        self.fixture.write()
        truncated, truncated_manifest = self.build(requested_end="2024-01-24")
        self.assertEqual(short_manifest["plan"]["process_start"], truncated_manifest["plan"]["process_start"])
        self.assertEqual(short_manifest["plan"]["model_train_cutoff"], truncated_manifest["plan"]["model_train_cutoff"])
        self.assertEqual(expected, self.query(truncated, "features.parquet", select))
        self.assertEqual(self.query(short, "display_indices.parquet", wealth),
                         self.query(truncated, "display_indices.parquet", wealth))

    def test_rebase_changes_display_only_and_preserves_preparation(self):
        first, _ = self.build(index_base_date="2024-01-22")
        second, manifest = self.build(index_base_date="2024-01-24")
        for artifact in ("features.parquet", "evaluation_features.parquet", "training.parquet", "labels.parquet"):
            self.assertEqual(self.query(first, artifact), self.query(second, artifact))
        self.assertEqual(manifest["cache_months_reused"], ["2024-01"])
        self.assertTrue(manifest["factor_cache_reused"])
        self.assertEqual(self.query(second, "display_indices.parquet", """SELECT index_base100
            FROM result WHERE permno=100 AND date=DATE '2024-01-24'"""), [(100.0,)])
        rows = self.query(second, "display_indices.parquet", "SELECT min(date) FROM result")[0]
        self.assertLess(rows[0], date(2024, 1, 22))
        compare = "SELECT date,wealth_index,index_anchor_date,index_status FROM result ORDER BY permno,date"
        self.assertEqual(self.query(first, "display_indices.parquet", compare),
                         self.query(second, "display_indices.parquet", compare))

    def test_ticker_identity_and_event_economic_effects_once(self):
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "identity_events.parquet", "SELECT permno,previous_ticker,ticker FROM result"),
                         [(100, "OLD", "NEW")])
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT count(*) FROM
            (SELECT permno,date FROM result GROUP BY ALL HAVING count(*)>1)"""), [(0,)])
        self.assertEqual(self.query(folder, "features.parquet", """SELECT ticker_asof FROM result
            WHERE permno=100 AND decision_date=DATE '2024-01-17'"""), [("NEW",)])
        values = self.query(folder, "panel.parquet", """SELECT date,price_raw,ret_total_backtest,ret_ex_div
            FROM result WHERE permno=500 AND date IN (DATE '2024-01-16',DATE '2024-01-18') ORDER BY date""")
        self.assertEqual(values, [(date(2024, 1, 16), 50.0, 0.0, 0.0), (date(2024, 1, 18), 49.0, 0.0, -0.02)])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT DISTINCT wealth_index
            FROM result WHERE permno=500"""), [(1.0,)])

    def test_delisting_missing_and_ipo_paths_stay_explicit(self):
        folder, manifest = self.build()
        self.assertEqual(manifest["status"], "INCOMPLETE_RETURNS")
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT ret_total_backtest,return_source,has_metadata
            FROM result WHERE permno=200 AND date=DATE '2024-01-19'"""), [(-1.0, "CRSP_DELIST_EMBEDDED", False)])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT wealth_index,index_status
            FROM result WHERE permno=200 AND date=DATE '2024-01-19'"""), [(0.0, "ZERO_WEALTH")])
        self.assertEqual(self.query(folder, "panel.parquet", """SELECT ret_total_backtest,return_quality
            FROM result WHERE permno=201 AND date=DATE '2024-01-19'"""), [(None, "MISSING_RETURN")])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT DISTINCT wealth_index,index_status
            FROM result WHERE permno=300 AND date>=DATE '2024-01-17'"""), [(None, "MISSING_RETURN_OR_SESSION_GAP")])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT index_base100,rebase_status,
            later_first_valid_base100 FROM result WHERE permno=400 AND date=DATE '2024-01-23'"""),
            [(None, "BASE_OBSERVATION_ABSENT", 100.0)])
        self.assertEqual(self.query(folder, "training.parquet", """SELECT count(*) FROM result
            WHERE permno=200 AND label_end_date>=DATE '2024-01-19'"""), [(0,)])
        with self.assertRaisesRegex(RuntimeError, "Strict quality policy"):
            build_dataset(self.config(), data_root=self.root, quality_policy="fail")

    def test_identical_run_reuses_and_source_change_invalidates(self):
        first, manifest = self.build()
        before = (first / "features.parquet").stat().st_mtime_ns
        again, again_manifest = self.build()
        self.assertEqual(first, again)
        self.assertEqual(manifest["run_id"], again_manifest["run_id"])
        self.assertEqual(before, (again / "features.parquet").stat().st_mtime_ns)
        row = list(self.fixture.daily[0])
        row[2] = 111.0
        self.fixture.daily[0] = tuple(row)
        self.fixture.write()
        updated, updated_manifest = self.build()
        self.assertNotEqual(first, updated)
        self.assertNotEqual(manifest["cache_id"], updated_manifest["cache_id"])
        self.assertEqual(updated_manifest["cache_months_reused"], [])

    def test_artifact_corruption_is_not_reused(self):
        folder, _ = self.build()
        with (folder / "features.parquet").open("ab") as handle:
            handle.write(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "changed/corrupt"):
            self.build()

    def test_processing_rule_version_invalidates_cached_outputs(self):
        first, old = self.build()
        with patch("src.derive.pipeline.RULES_VERSION", "fixture-new-rule-version"):
            second, new = self.build()
        self.assertNotEqual(first, second)
        self.assertNotEqual(old["cache_id"], new["cache_id"])
        self.assertEqual(new["cache_months_reused"], [])
        self.assertFalse(new["factor_cache_reused"])

    def test_shorter_endpoint_reuses_covering_factors_without_future_rows(self):
        longer, _ = self.build(requested_end="2024-01-31")
        shorter, manifest = self.build(requested_end="2024-01-24")
        self.assertTrue(manifest["factor_cache_reused"])
        self.assertEqual(manifest["cache_months_reused"], ["2024-01"])
        query = "SELECT * FROM result WHERE decision_date<=DATE '2024-01-24' ORDER BY permno,decision_date"
        self.assertEqual(self.query(longer, "features.parquet", query), self.query(shorter, "features.parquet", query))
        self.assertEqual(self.query(shorter, "features.parquet", "SELECT max(decision_date) FROM result"),
                         [(date(2024, 1, 24),)])
        self.assertEqual(self.query(shorter, "labels.parquet", """SELECT forward_vendor_return,label_status
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-24'"""),
                         [(None, "PERIOD_END_TRUNCATED")])

    def test_total_loss_cannot_resurrect_factor_eligibility(self):
        revised = []
        for row in self.fixture.daily:
            row = list(row)
            if row[0] == 100 and row[1] == date(2024, 1, 12):
                row[4] = row[5] = -1.0
            revised.append(tuple(row))
        self.fixture.daily = revised
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, "evaluation_features.parquet", """SELECT DISTINCT momentum,eligible_for_research
            FROM result WHERE permno=100"""), [(None, False)])
        self.assertEqual(self.query(folder, "display_indices.parquet", """SELECT DISTINCT wealth_index
            FROM result WHERE permno=100 AND date>=DATE '2024-01-12'"""), [(0.0,)])

    def test_duplicate_key_and_overlapping_history_fail(self):
        self.fixture.daily.append(self.fixture.daily[0])
        self.fixture.write()
        with self.assertRaisesRegex(RuntimeError, "Duplicate daily key"):
            self.build()
        self.fixture.daily.pop()
        self.fixture.info.append(CanonicalFixture.history(100, "OVERLAP", "2024-01-12", "2024-01-19"))
        self.fixture.write()
        with self.assertRaisesRegex(RuntimeError, "Ambiguous history/event match"):
            self.build()

    def test_missing_market_session_fails(self):
        self.fixture.daily = [row for row in self.fixture.daily if row[1] != date(2024, 1, 11)]
        self.fixture.write()
        with self.assertRaisesRegex(RuntimeError, "Calendar coverage mismatch"):
            self.build()

    def test_strict_pit_and_protected_destinations_fail(self):
        with self.assertRaisesRegex(ValueError, "Strict PIT unavailable"):
            build_dataset(self.config(), data_root=self.root, pit_mode="strict")
        with self.assertRaisesRegex(ValueError, "must not overwrite"):
            build_dataset(self.config(), data_root=self.root, output_root=self.root / "normalized/results")


if __name__ == "__main__":
    unittest.main()
