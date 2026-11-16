import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import seed_dimensions, event
from looplake.model import LakeError, RawEvent
from looplake.service import LakeService


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "lake.sqlite3"
        self.service = LakeService(self.db)
        seed_dimensions(self.service)

    def test_exact_replay_is_idempotent(self):
        raw = event("deposit", "d1")
        first = self.service.ingest(raw)
        second = self.service.ingest(raw)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["fact_id"], second["fact_id"])
        self.assertEqual(len(self.service.ledger.batch("batch-1")), 1)

    def test_same_external_id_changed_content_is_conflict(self):
        self.service.ingest(event("deposit", "d1"))
        with self.assertRaises(LakeError) as failure:
            self.service.ingest(event("deposit", "d1", weight="20"))
        self.assertEqual(failure.exception.code, "event_conflict")

    def test_same_external_id_other_source_allowed(self):
        self.service.register_source("box-2-feed", "smart_box")
        first = self.service.ingest(event("deposit", "d1"))
        second = self.service.ingest(event("deposit", "d1", source="box-2-feed"))
        self.assertNotEqual(first["fact_id"], second["fact_id"])

    def test_weight_invalid_before_landing(self):
        for weight in ("0", "-1", "NaN", "Infinity", "1.1234567", "abc", None):
            with self.subTest(weight=weight), self.assertRaises(LakeError):
                self.service.ingest(event("deposit", "d1", weight=weight))
        self.assertEqual(self.service.quality.ingestion_health()["total"], 0)

    def test_invalid_time_and_keys(self):
        with self.assertRaises(LakeError) as failure:
            self.service.ingest(event("deposit", "d1", happened_at="2026-07-01T10:00:00"))
        self.assertEqual(failure.exception.code, "invalid_happened_at")
        with self.assertRaises(LakeError):
            self.service.ingest(event("deposit", "bad id"))

    def test_quarantine_preserves_raw(self):
        raw = event("deposit", "d1", material_id="unknown")
        result = self.service.ingest(raw)
        self.assertEqual(result["state"], "quarantined")
        landed = self.service.store.ingestion(result["ingestion_id"])
        self.assertEqual(landed["raw"]["material_id"], "unknown")
        self.assertEqual(landed["state"], "quarantined")
        self.assertEqual(self.service.landing.pending()[0]["issue"]["code"], "unknown_dimension")

    def test_quarantine_duplicate_still_retryable(self):
        raw = event("deposit", "d1", material_id="future")
        first = self.service.ingest(raw)
        second = self.service.ingest(raw)
        self.assertEqual(first["ingestion_id"], second["ingestion_id"])
        self.assertEqual(second["state"], "quarantined")
        self.service.register_dimension({"kind": "material", "external_id": "future", "name": "未来物料",
                                         "valid_from": "2026-01-01"})
        recovered = self.service.retry(first["ingestion_id"])
        self.assertEqual(recovered["state"], "normalized")

    def test_reopen_store_keeps_facts(self):
        first = self.service.ingest(event("deposit", "d1"))
        reopened = LakeService(self.db)
        self.assertEqual(reopened.store.fact(first["fact_id"])["external_id"], "d1")
        self.assertFalse(reopened.ingest(event("deposit", "d1"))["created"])

    def test_malformed_event_types(self):
        base = event("deposit", "d1")
        for field, value in (("event_type", "unknown"), ("channel", "unknown"),
                             ("material_id", None), ("device_id", None), ("weight_kg", "-3")):
            changed = {**base, field: value}
            with self.subTest(field=field), self.assertRaises(LakeError):
                RawEvent.parse(changed)

    def test_outbound_requires_station(self):
        raw = event("weigh_out", "w1", station_id=None)
        result = self.service.ingest(raw)
        self.assertEqual(result["issue"]["code"], "missing_station")

    def test_landing_health_counts(self):
        self.service.ingest(event("deposit", "d1"))
        self.service.ingest(event("deposit", "d2", material_id="unknown"))
        report = self.service.quality.ingestion_health()
        self.assertEqual(report["states"], {"normalized": 1, "quarantined": 1})
        self.assertEqual(report["issues"], {"unknown_dimension": 1})

    def test_audit_links_landing_to_fact(self):
        result = self.service.ingest(event("deposit", "d1"))
        entries = self.service.store.audits()
        self.assertTrue(any(x["action"] == "landed" for x in entries))
        self.assertTrue(any(x["action"] == "normalized" and x["subject"] == str(result["fact_id"])
                            for x in entries))
