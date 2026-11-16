import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def test_rate_effective_period(self):
        rate = self.service.settlement.rate_at("foam", "smart_box", "2026-07-15")
        self.assertEqual(rate["rate_yuan_per_kg"], "0.08")
        self.assertEqual(rate["version"], 1)
        self.assertIsNone(self.service.settlement.rate_at("foam", "smart_box", "2025-07-15"))

    def test_rate_overlap_rejected(self):
        with self.assertRaises(LakeError) as failure:
            self.service.settlement.put_rate("foam", "smart_box", "2026-06-01", "0.09")
        self.assertEqual(failure.exception.code, "rate_overlap")

    def test_rate_invalid_period(self):
        with self.assertRaises(LakeError) as failure:
            self.service.settlement.put_rate("foam", "smart_box", "2026-06-01", "0.09",
                                             valid_to="2026-05-01")
        self.assertEqual(failure.exception.code, "invalid_rate_period")

    def test_rate_unregistered_material(self):
        with self.assertRaises(LakeError) as failure:
            self.service.settlement.put_rate("unknown", "smart_box", "2026-01-01", "0.08")
        self.assertEqual(failure.exception.code, "unknown_material")

    def test_rate_version_conflict(self):
        with self.assertRaises(LakeError) as failure:
            self.service.settlement.put_rate("foam", "smart_box", "2027-01-01", "0.09",
                                             expected_version=0)
        self.assertEqual(failure.exception.code, "stale_rate")

    def test_payment_matches_rate(self):
        for row in balanced_batch():
            self.service.ingest(row)
        result = self.service.batch_status("batch-1")
        self.assertTrue(result["ready"])
        self.assertEqual(result["settlement"]["pricing"][0]["rate_version"], 1)
        self.assertEqual(result["settlement"]["expected_yuan"], "0.80")

    def test_payment_mismatch_explains_fact_ids(self):
        results = [self.service.ingest(row) for row in balanced_batch(payout="2")]
        batch = self.service.batch_status("batch-1")
        issue = next(x for x in batch["issues"] if x["code"] == "payout_gap")
        self.assertEqual(set(issue["fact_ids"]), {results[0]["fact_id"], results[2]["fact_id"]})
        self.assertEqual(issue["gap_yuan"], "1.20")

    def test_no_payout_is_not_fabricated(self):
        for row in balanced_batch(payout=None):
            self.service.ingest(row)
        batch = self.service.batch_status("batch-1")
        self.assertEqual(batch["settlement"]["actual_yuan"], "0")
        self.assertTrue(batch["ready"])

    def test_ambiguous_allocation_blocks(self):
        rows = balanced_batch()
        rows.append(event("deposit", "d2", device_id="box-2", weight="1"))
        for row in rows:
            self.service.ingest(row)
        issues = [x["code"] for x in self.service.batch_status("batch-1")["issues"]]
        self.assertIn("ambiguous_payout_allocation", issues)

    def test_street_payout_follows_deposit(self):
        for row in balanced_batch():
            self.service.ingest(row)
        report = self.service.month_report("2026-07")
        self.assertEqual(len(report["rows"]), 1)
        self.assertEqual(report["rows"][0]["street_id"], "street-1")
        self.assertEqual(report["rows"][0]["payout_yuan"], "0.8")

    def test_money_tolerance_validation(self):
        for row in balanced_batch():
            self.service.ingest(row)
        for invalid in ("NaN", "-1", "101", "bad"):
            with self.subTest(invalid=invalid), self.assertRaises(LakeError):
                self.service.settlement.batch(self.service.ledger.batch("batch-1"), invalid)
