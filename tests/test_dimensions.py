import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import seed_dimensions, event
from looplake.model import LakeError
from looplake.service import LakeService


class DimensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def test_dimension_lookup_and_lineage(self):
        result = self.service.ingest(event("deposit", "d1"))
        self.assertEqual(result["state"], "normalized")
        fact = self.service.store.fact(result["fact_id"])
        self.assertEqual(fact["street_id"], "street-1")
        self.assertEqual(fact["community_id"], "community-1")
        self.assertEqual(fact["dimensions"]["device"]["version"], 1)
        trace = self.service.lineage.fact(result["fact_id"])
        self.assertEqual(trace["landing"]["external_id"], "d1")
        self.assertEqual(trace["dimension_versions"]["street"]["name"], "合成一街道")

    def test_unknown_device_quarantines_and_retry_succeeds(self):
        result = self.service.ingest(event("deposit", "d1", device_id="future-box"))
        self.assertEqual(result["state"], "quarantined")
        self.assertEqual(result["issue"]["code"], "unknown_dimension")
        self.service.register_dimension({"kind": "device", "external_id": "future-box", "name": "未来箱",
                                         "valid_from": "2026-01-01", "attributes": {"community_id": "community-1"}})
        retry = self.service.retry(result["ingestion_id"])
        self.assertEqual(retry["state"], "normalized")
        self.assertEqual(self.service.store.fact(retry["fact_id"])["device_id"], "future-box")
        self.assertFalse(self.service.retry(result["ingestion_id"])["created"])

    def test_effective_time_rejects_future_device(self):
        result = self.service.ingest(event("deposit", "d1", happened_at="2025-12-31T23:00:00Z"))
        self.assertEqual(result["state"], "quarantined")
        self.assertEqual(result["issue"]["code"], "unknown_dimension")

    def test_overlap_is_rejected(self):
        with self.assertRaises(LakeError) as failure:
            self.service.register_dimension({"kind": "material", "external_id": "foam", "name": "重复泡沫",
                                             "valid_from": "2026-06-01"})
        self.assertEqual(failure.exception.code, "dimension_overlap")

    def test_nonoverlap_version(self):
        self.service.register_dimension({"kind": "material", "external_id": "metal", "name": "金属一",
                                         "valid_from": "2026-01-01", "valid_to": "2026-06-01"})
        result = self.service.register_dimension({"kind": "material", "external_id": "metal", "name": "金属二",
                                                  "valid_from": "2026-06-01", "expected_version": 1})
        self.assertEqual(result["version"], 2)
        self.assertEqual(self.service.store.dimension("material", "metal", "2026-07-01")["name"], "金属二")
        self.assertEqual(self.service.store.dimension("material", "metal", "2026-05-01")["name"], "金属一")

    def test_stale_dimension_version(self):
        with self.assertRaises(LakeError) as failure:
            self.service.register_dimension({"kind": "material", "external_id": "new", "name": "新",
                                             "valid_from": "2026-01-01"}, expected_version=2)
        self.assertEqual(failure.exception.code, "stale_dimension")

    def test_missing_parent_is_rejected(self):
        with self.assertRaises(LakeError) as failure:
            self.service.register_dimension({"kind": "device", "external_id": "orphan", "name": "孤箱",
                                             "valid_from": "2026-01-01", "attributes": {"community_id": "missing"}})
        self.assertEqual(failure.exception.code, "unknown_dimension")

    def test_source_channel_and_disable(self):
        with self.assertRaises(LakeError) as failure:
            self.service.ingest(event("deposit", "d1", source="scale-feed"))
        self.assertEqual(failure.exception.code, "source_channel")
        self.service.dimensions.disable_source("box-feed", 1)
        with self.assertRaises(LakeError) as failure:
            self.service.ingest(event("deposit", "d1"))
        self.assertEqual(failure.exception.code, "source_inactive")

    def test_stale_source_update(self):
        with self.assertRaises(LakeError) as failure:
            self.service.dimensions.disable_source("box-feed", 0)
        self.assertEqual(failure.exception.code, "stale_source")

    def test_dimension_payload_constraints(self):
        for invalid in (None, "x", {}, {"kind": "unknown", "external_id": "x", "name": "x",
                                         "valid_from": "2026-01-01"}):
            with self.subTest(invalid=invalid), self.assertRaises(LakeError):
                self.service.register_dimension(invalid)

    def test_versions_are_auditable(self):
        rows = self.service.dimensions.list_versions("device", "box-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attributes"]["community_id"], "community-1")
        events = self.service.store.audits()
        self.assertTrue(any(item["action"] == "dimension_added" for item in events))
