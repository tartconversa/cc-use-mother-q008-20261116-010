import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def ingest(self, rows):
        return [self.service.ingest(row) for row in rows]

    def test_balanced_batch_and_mart(self):
        ids = self.ingest(balanced_batch())
        batch = self.service.batch_status("batch-1")
        self.assertTrue(batch["ready"])
        self.assertEqual(batch["deposit_kg"], "10")
        self.assertEqual(batch["weigh_in_kg"], "10")
        self.assertEqual(batch["settlement"]["actual_yuan"], "0.8")
        mart = self.service.month_report("2026-07")
        self.assertEqual(mart["rows"][0]["deposit_kg"], "10")
        self.assertEqual(mart["rows"][0]["payout_yuan"], "0.8")
        self.assertEqual(len(ids), 3)

    def test_weight_gap_blocks_batch(self):
        rows = balanced_batch()
        rows[1]["weight_kg"] = "7"
        self.ingest(rows)
        batch = self.service.batch_status("batch-1")
        self.assertFalse(batch["ready"])
        self.assertIn("deposit_scale_gap", [issue["code"] for issue in batch["issues"]])
        mart = self.service.month_report("2026-07")
        self.assertEqual(mart["rows"], [])
        self.assertEqual(len(mart["excluded_fact_ids"]), 3)

    def test_payout_gap_blocks_batch(self):
        rows = balanced_batch(payout="1.2")
        self.ingest(rows)
        batch = self.service.batch_status("batch-1")
        self.assertFalse(batch["ready"])
        self.assertIn("payout_gap", [issue["code"] for issue in batch["issues"]])
        self.assertEqual(batch["settlement"]["expected_yuan"], "0.80")

    def test_unpriced_deposit_with_payout(self):
        self.service.register_dimension({"kind": "material", "external_id": "paper", "name": "纸",
                                         "valid_from": "2026-01-01"})
        self.ingest(balanced_batch(material_id="paper"))
        self.assertIn("unpriced_deposit", [x["code"] for x in self.service.batch_status("batch-1")["issues"]])

    def test_new_rate_releases_batch_without_reingest(self):
        self.service.register_dimension({"kind": "material", "external_id": "paper", "name": "纸",
                                         "valid_from": "2026-01-01"})
        self.ingest(balanced_batch(material_id="paper"))
        self.assertFalse(self.service.batch_status("batch-1")["ready"])
        self.service.settlement.put_rate("paper", "smart_box", "2026-01-01", "0.08")
        self.assertTrue(self.service.batch_status("batch-1")["ready"])

    def test_material_mismatch(self):
        rows = balanced_batch()
        rows[1]["material_id"] = "glass"
        self.ingest(rows)
        self.assertIn("mixed_material", [x["code"] for x in self.service.batch_status("batch-1")["issues"]])

    def test_outbound_over_inbound(self):
        rows = balanced_batch()
        rows.append(event("weigh_out", "out-1", weight="15"))
        self.ingest(rows)
        self.assertIn("outbound_exceeds_inbound", [x["code"] for x in self.service.batch_status("batch-1")["issues"]])

    def test_transfer_gap(self):
        rows = balanced_batch()
        rows.append(event("weigh_out", "out-1", weight="9"))
        rows.append(event("transfer", "transfer-1", weight="7"))
        self.ingest(rows)
        self.assertIn("transfer_scale_gap", [x["code"] for x in self.service.batch_status("batch-1")["issues"]])

    def test_missing_weight_source(self):
        self.ingest([event("deposit", "d1")])
        self.assertIn("missing_weigh_in", [x["code"] for x in self.service.batch_status("batch-1")["issues"]])

    def test_orphan_payout(self):
        self.ingest([event("payout", "p1")])
        issues = [x["code"] for x in self.service.batch_status("batch-1")["issues"]]
        self.assertIn("orphan_payout", issues)

    def test_two_batches_one_month(self):
        self.ingest(balanced_batch("batch-1", "a"))
        self.ingest(balanced_batch("batch-2", "b"))
        month = self.service.reconciliation.month("2026-07")
        self.assertTrue(month["ready"])
        self.assertEqual(len(month["batches"]), 2)
        self.assertEqual(self.service.mart.material("foam", "2026-07")["deposit_kg"], "20")

    def test_tolerance_validation(self):
        self.ingest(balanced_batch())
        for invalid in ("x", "NaN", "-1", "1001"):
            with self.subTest(invalid=invalid), self.assertRaises(LakeError):
                self.service.reconciliation.batch("batch-1", invalid)

    def test_fact_dependency(self):
        ids = self.ingest(balanced_batch())
        result = self.service.reconciliation.fact_dependency(ids[0]["fact_id"])
        self.assertEqual(len(result["related_fact_ids"]), 3)
        self.assertTrue(result["batch_ready"])

    def test_quality_explains_unresolved(self):
        rows = balanced_batch()
        rows[1]["weight_kg"] = "6"
        self.ingest(rows)
        quality = self.service.month_status("2026-07")
        self.assertFalse(quality["ready"])
        self.assertEqual(quality["unresolved_batches"], ["batch-1"])

    def test_source_summary(self):
        self.ingest(balanced_batch())
        sources = self.service.reconciliation.sources("2026-07")
        self.assertEqual({x["source"] for x in sources}, {"box-feed", "scale-feed", "pay-feed"})
