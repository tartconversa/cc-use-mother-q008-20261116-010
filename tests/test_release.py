import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def put(self, rows):
        return [self.service.ingest(row) for row in rows]

    def test_preview_and_publish_freezes_lineage(self):
        results = self.put(balanced_batch())
        preview = self.service.preview("2026-07")
        self.assertTrue(preview["ready"])
        release = self.service.publish("july-1", "2026-07", preview["fingerprint"])
        self.assertTrue(release["created"])
        frozen = self.service.releases.get(release["release_id"])
        self.assertEqual(frozen["content"]["mart"]["rows"][0]["deposit_kg"], "10")
        lineage = self.service.lineage.release_row(release["release_id"], "street-1", "foam")
        self.assertIn(results[0]["fact_id"], lineage["row"]["fact_ids"])

    def test_unresolved_month_blocks_release(self):
        self.put([event("deposit", "d1")])
        preview = self.service.preview("2026-07")
        self.assertFalse(preview["ready"])
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-1", "2026-07", preview["fingerprint"])
        self.assertEqual(failure.exception.code, "unresolved_month")

    def test_stale_preview_blocks_release(self):
        self.put(balanced_batch())
        preview = self.service.preview("2026-07")
        self.put(balanced_batch("batch-2", "b"))
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-1", "2026-07", preview["fingerprint"])
        self.assertEqual(failure.exception.code, "stale_preview")

    def test_repeat_same_release_idempotent(self):
        self.put(balanced_batch())
        fingerprint = self.service.preview("2026-07")["fingerprint"]
        first = self.service.publish("july-1", "2026-07", fingerprint)
        second = self.service.publish("july-1", "2026-07", fingerprint)
        self.assertFalse(second["created"])
        self.assertEqual(first["release_id"], second["release_id"])

    def test_old_snapshot_survives_later_batch(self):
        self.put(balanced_batch())
        old_fp = self.service.preview("2026-07")["fingerprint"]
        old = self.service.publish("july-1", "2026-07", old_fp)
        self.put(balanced_batch("batch-2", "b"))
        new_fp = self.service.preview("2026-07")["fingerprint"]
        new = self.service.publish("july-2", "2026-07", new_fp)
        self.assertEqual(self.service.releases.get(old["release_id"])["content"]["mart"]["rows"][0]["deposit_kg"], "10")
        self.assertEqual(self.service.releases.get(new["release_id"])["content"]["mart"]["rows"][0]["deposit_kg"], "20")
        diff = self.service.releases.compare(old["release_id"], new["release_id"])
        self.assertEqual(len(diff["changes"]), 1)
        self.assertEqual(diff["changes"][0]["after"]["deposit_kg"], "20")

    def test_release_name_cannot_change_content(self):
        self.put(balanced_batch())
        first = self.service.preview("2026-07")["fingerprint"]
        self.service.publish("july-1", "2026-07", first)
        self.put(balanced_batch("batch-2", "b"))
        later = self.service.preview("2026-07")["fingerprint"]
        with self.assertRaises(LakeError) as failure:
            self.service.publish("july-1", "2026-07", later)
        self.assertEqual(failure.exception.code, "release_conflict")

    def test_row_lineage_current(self):
        results = self.put(balanced_batch())
        row = self.service.lineage.row("2026-07", "street-1", "foam")
        self.assertEqual(row["row"]["deposit_kg"], "10")
        self.assertEqual(row["traces"][0]["landing"]["external_id"], "a-deposit")
        self.assertEqual(row["reconciliations"][0]["batch_id"], "batch-1")

    def test_row_lineage_missing(self):
        self.put(balanced_batch())
        with self.assertRaises(LakeError) as failure:
            self.service.lineage.row("2026-07", "street-2", "foam")
        self.assertEqual(failure.exception.code, "unknown_mart_row")

    def test_street_and_material_filters(self):
        self.put(balanced_batch())
        self.put(balanced_batch("batch-2", "b", device_id="box-2"))
        rows = self.service.mart.street("street-1", "2026-07", "2026-07")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deposit_kg"], "10")
        self.assertEqual(self.service.mart.material("foam", "2026-07")["deposit_kg"], "20")

    def test_publish_empty_month_fails(self):
        preview = self.service.preview("2026-07")
        self.assertFalse(preview["ready"])
        with self.assertRaises(LakeError):
            self.service.publish("empty", "2026-07", preview["fingerprint"])

    def test_release_audit(self):
        self.put(balanced_batch())
        fingerprint = self.service.preview("2026-07")["fingerprint"]
        self.service.publish("july-1", "2026-07", fingerprint)
        events = self.service.lineage.audit_subject("july-1")
        self.assertEqual(events[-1]["action"], "release_published")
