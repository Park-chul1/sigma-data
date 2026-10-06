"""The retired event builder cannot bypass the canonical research contract."""
from datetime import date
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb

from src.derive.crsp_corporate_events import build_corporate_event_audit, build_period
from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig
from tests.test_pipeline import CanonicalFixture


class CorporateEventPeriodTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = CanonicalFixture(self.root)
        self.config = ResearchConfig(
            requested_start="2024-01-17", requested_end="2024-01-24",
            factor_lookback_sessions=3, label_horizon_sessions=2,
        )

    def build(self, **kwargs):
        return build_corporate_event_audit(self.config, data_root=self.root, **kwargs)

    @staticmethod
    def query(folder, query):
        con = duckdb.connect()
        try:
            con.read_parquet(str(folder / "panel.parquet")).create_view("panel")
            return con.execute(query).fetchall()
        finally:
            con.close()

    def test_alias_returns_exact_canonical_run_and_manifest(self):
        folder, manifest = self.build()
        canonical_folder, canonical_manifest = build_dataset(self.config, data_root=self.root)
        self.assertEqual(folder, canonical_folder)
        self.assertEqual(manifest["run_id"], canonical_manifest["run_id"])
        self.assertEqual(manifest["artifacts"], canonical_manifest["artifacts"])
        self.assertTrue((folder / "events.parquet").is_file())
        self.assertTrue((folder / "evaluation_features.parquet").is_file())

    def test_normal_n_and_event_day_are_distinct_from_terminal_storage_day(self):
        folder, _ = self.build()
        rows = self.query(folder, """SELECT date,delist_flag,is_delisting_return,
            terminal_event_date,ret_total_backtest FROM panel
            WHERE permno=200 AND date IN (DATE '2024-01-18',DATE '2024-01-19')
            ORDER BY date""")
        self.assertEqual(rows, [
            (date(2024, 1, 18), "N", False, None, 0.01),
            (date(2024, 1, 19), "Y", True, date(2024, 1, 18), -1.0),
        ])

    def test_ciz_return_is_not_compounded_with_delisting_return(self):
        # A partial loss exposes double compounding; -100% alone would not.
        self.fixture.delistings[0] = (200, date(2024, 1, 18), -0.5,
                                      "GDR", "DEAD", "BANK", "NA", None, None)
        self.fixture.daily = [tuple(list(row[:4]) + [-0.5, -0.5] + list(row[6:11]) + ["D1"])
                              if row[0] == 200 and row[7] == "Y" else row
                              for row in self.fixture.daily]
        self.fixture.write()
        folder, _ = self.build()
        self.assertEqual(self.query(folder, """SELECT ret_total_backtest,delisting_return,
            return_source,return_was_reconstructed FROM panel
            WHERE permno=200 AND delist_flag='Y'"""),
            [(-0.5, -0.5, "CRSP_DELIST_EMBEDDED", False)])
        self.assertEqual(self.query(folder, """SELECT ret_total_backtest,return_quality
            FROM panel WHERE permno=201 AND delist_flag='Y'"""), [(None, "MISSING_RETURN")])

    def test_alias_inherits_strict_pit_rejection_and_artifact_integrity(self):
        with self.assertRaisesRegex(ValueError, "Strict PIT unavailable"):
            self.build(pit_mode="strict")
        folder, _ = self.build()
        with (folder / "events.parquet").open("ab") as stream:
            stream.write(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "changed/corrupt"):
            self.build()

    def test_old_api_fails_with_migration_without_overwriting(self):
        output = self.root / "existing.parquet"
        output.write_bytes(b"preserve prior output")
        with self.assertRaisesRegex(ValueError, "build_period was retired"):
            build_period(daily_pattern=Path("unused"), security_info_file=Path("unused"),
                delistings_file=Path("unused"), output_file=output,
                requested_start=date(2024, 1, 17), requested_end=date(2024, 1, 24), force=True)
        self.assertEqual(output.read_bytes(), b"preserve prior output")

    def test_compatibility_cli_exposes_canonical_controls(self):
        result = subprocess.run([sys.executable, "scripts/build_crsp_corporate_events.py", "--help"],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for flag in ("--lookback", "--training-sessions", "--availability-lag", "--pit-mode", "--quality-policy"):
            self.assertIn(flag, result.stdout)
        self.assertIn("audit artifacts", result.stderr)


if __name__ == "__main__":
    unittest.main()
