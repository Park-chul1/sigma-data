"""Audit denominators must not depend on the availability of future labels."""
from datetime import date
from pathlib import Path
import tempfile
import unittest

from src.derive.pipeline import build_dataset
from src.derive.temporal import ResearchConfig
from tests import test_pipeline as fixtures


class TrainingAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixture = fixtures.CanonicalFixture(self.root)

    def build(self):
        return build_dataset(ResearchConfig("2024-01-22", "2024-01-26",
            factor_lookback_sessions=3,training_sessions=3,label_horizon_sessions=2),data_root=self.root)

    query = staticmethod(fixtures.PipelineTests.query)

    def test_candidates_are_preserved_with_exclusive_exclusion_reasons(self):
        folder, manifest = self.build()
        audit = manifest["training_sample_audit"]
        candidates = self.query(folder,"features.parquet","""SELECT count(*) FROM result
            WHERE eligible_for_research AND decision_date BETWEEN DATE '2024-01-12' AND DATE '2024-01-17'""")[0][0]
        self.assertEqual(audit["feature_based_candidates"],candidates)
        self.assertEqual(audit["included"],manifest["rows"]["training.parquet"])
        self.assertEqual(audit["included"]+audit["excluded"],candidates)
        self.assertEqual(sum(audit["disposition_counts"].values()),candidates)
        self.assertGreater(audit["disposition_counts"]["TERMINAL_OUTCOME"],0)
        self.assertGreater(audit["disposition_counts"]["MISSING_RETURN"],0)
        self.assertTrue(audit["warnings"])
        totals = self.query(folder,"training_audit_summary.parquet","""SELECT sum(training_candidates),
            sum(training_included),sum(training_excluded) FROM result""")[0]
        self.assertEqual(totals,(candidates,audit["included"],audit["excluded"]))
        # Complete but not yet usable and unobservable tail are different states.
        statuses = self.query(folder,"training_audit.parquet","""SELECT decision_date,outcome_disposition,
            immature_at_cutoff FROM result WHERE permno=100 AND decision_date IN
            (DATE '2024-01-22',DATE '2024-01-26') ORDER BY decision_date""")
        self.assertEqual(statuses,[(date(2024,1,22),'LABEL_IMMATURE_CUTOFF',True),
                                   (date(2024,1,26),'PERIOD_END_TRUNCATED',True)])

    def test_missing_decision_and_future_session_remain_candidates(self):
        self.fixture.daily = [row for row in self.fixture.daily
            if not (row[0]==100 and row[1]==date(2024,1,16))]
        self.fixture.write()
        folder, manifest = self.build()
        statuses = self.query(folder,"training_audit.parquet","""SELECT decision_date,training_disposition
            FROM result WHERE permno=100 AND training_candidate ORDER BY decision_date""")
        self.assertIn((date(2024,1,12),'SESSION_GAP'),statuses)
        self.assertIn((date(2024,1,16),'MISSING_DECISION_OBSERVATION'),statuses)
        self.assertEqual(self.query(folder,"labels.parquet","""SELECT observed_sessions,forward_vendor_return
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-12'"""),[(1,None)])
        self.assertGreater(manifest["training_sample_audit"]["excluded"],0)

    def test_numeric_incomplete_label_is_audited_without_removing_past_input(self):
        original,_ = self.build()
        expected=self.query(original,"features.parquet","SELECT * FROM result WHERE decision_date<=DATE '2024-01-17' ORDER BY permno,decision_date")
        rows=[]
        for row in self.fixture.daily:
            if row[0]==100 and row[1]==date(2024,1,18):
                row=list(row); row[10]='MV'; row=tuple(row)
            rows.append(row)
        self.fixture.daily=rows
        self.fixture.write()
        folder,manifest=self.build()
        self.assertEqual(expected,self.query(folder,"features.parquet","SELECT * FROM result WHERE decision_date<=DATE '2024-01-17' ORDER BY permno,decision_date"))
        self.assertGreater(manifest["training_sample_audit"]["disposition_counts"]["INCOMPLETE_RETURN"],0)
        self.assertEqual(self.query(folder,"labels.parquet","""SELECT forward_vendor_return,label_status
            FROM result WHERE permno=100 AND decision_date=DATE '2024-01-17'"""),[(None,'INCOMPLETE_RETURN')])
