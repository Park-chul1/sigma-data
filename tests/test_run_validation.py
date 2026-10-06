"""Independent export validation, including correctly rehashed semantic damage."""

import json
from pathlib import Path
import tempfile
import unittest

import duckdb

from scripts.validate_research_run import fingerprint, validate
from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig
from tests.test_pipeline import CanonicalFixture


class RunValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        CanonicalFixture(self.root)
        self.folder, self.manifest = build_dataset(
            ResearchConfig("2024-01-22", "2024-01-26", factor_lookback_sessions=3,
                           training_sessions=3, label_horizon_sessions=2), data_root=self.root)

    def rewrite_and_rehash(self, artifact, query):
        """Update checksums/counts so the semantic checker, not hashing, detects damage."""
        path = self.folder / artifact
        replacement = self.folder / "replacement.parquet"
        with duckdb.connect() as con:
            con.read_parquet(str(path)).create_view("original")
            con.execute(f"CREATE TABLE changed AS {query}")
            count = con.execute("SELECT count(*) FROM changed").fetchone()[0]
            con.execute("COPY changed TO ? (FORMAT PARQUET)", [str(replacement)])
        replacement.replace(path)
        self.manifest["artifacts"][artifact] = fingerprint(path)
        self.manifest["rows"][artifact] = count
        (self.folder / "_SUCCESS.json").write_text(json.dumps(self.manifest, default=str))

    def assert_integrity_passed(self, report, table):
        self.assertTrue(report["checks"][f"hash_{table}"]["passed"])
        self.assertTrue(report["checks"][f"manifest_rows_{table}"]["passed"])

    def test_synthetic_run_passes_independent_export_and_source_checks(self):
        report = validate(self.folder, self.root)
        failures = {name: check for name, check in report["checks"].items() if not check["passed"]}
        self.assertTrue(report["passed"], failures)
        self.assertGreaterEqual(len(report["checks"]), 80)
        self.assertEqual(report["checks"]["source_normalized_rows_and_fields_preserved"]["violations"], 0)
        for name in ("audit_preserves_all_feature_candidates", "audit_included_equals_training",
                     "audit_summary_totals", "complete_labels_have_exact_horizon",
                     "terminal_reconciliation_no_silent_loss", "source_terminal_evidence_preserved"):
            self.assertTrue(report["checks"][name]["passed"], name)
        # Passing validation must retain the public-availability/vintage limitation.
        self.assertEqual(report["pit_guarantee"], "approximate")
        self.assertIn("historical publication/vintage verification", report["unperformed"])

    def test_dropped_training_candidate_fails_after_legitimate_rehash(self):
        self.rewrite_and_rehash("training_audit.parquet", """SELECT * FROM original
            WHERE NOT (permno=100 AND decision_date=DATE '2024-01-16')""")
        report = validate(self.folder, self.root)
        self.assert_integrity_passed(report, "training_audit")
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"]["audit_preserves_all_feature_candidates"]["violations"], 1)
        self.assertEqual(report["checks"]["audit_manifest_candidates"]["violations"], 1)

    def test_terminal_outcome_cannot_be_marked_included_in_training(self):
        self.rewrite_and_rehash("training_audit.parquet", """SELECT * REPLACE(
            CASE WHEN training_candidate AND outcome_disposition='TERMINAL_OUTCOME'
                 THEN true ELSE training_included END AS training_included) FROM original""")
        report = validate(self.folder, self.root)
        self.assert_integrity_passed(report, "training_audit")
        self.assertFalse(report["passed"])
        self.assertGreater(report["checks"]["audit_no_invalid_inclusion"]["violations"], 0)
        self.assertGreater(report["checks"]["audit_included_equals_training"]["violations"], 0)

    def test_dropped_terminal_reconciliation_is_not_hidden_by_valid_checksum(self):
        self.rewrite_and_rehash("terminal_reconciliation.parquet",
                                "SELECT * FROM original WHERE permno!=200")
        report = validate(self.folder, self.root)
        self.assert_integrity_passed(report, "terminal_reconciliation")
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"]["terminal_reconciliation_no_silent_loss"]["violations"], 1)


if __name__ == "__main__":
    unittest.main()
