import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, seed_dimensions
from looplake.service import LakeService


class QuerySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def put(self, rows):
        return [self.service.ingest(row) for row in rows]

    def test_community_and_device_views(self):
        ids = self.put(balanced_batch())
        community = self.service.query.community_month("community-1", "2026-07")
        self.assertEqual(community["deposit_event_count"], 1)
        self.assertEqual(community["rows"][0]["deposit_kg"], "10")
        device = self.service.query.device_history("box-1", "2026-07-01", "2026-07-31")
        self.assertEqual(device["rows"][0]["fact_id"], ids[0]["fact_id"])
        self.assertEqual(device["rows"][0]["dimension_version"], 1)

    def test_source_view_preserves_event_types(self):
        self.put(balanced_batch())
        report = self.service.query.source_month("scale-feed", "2026-07")
        self.assertEqual(report["events"]["weigh_in"]["count"], 1)
        self.assertEqual(report["events"]["weigh_in"]["weight_kg"], "10")
        self.assertEqual(report["events"]["weigh_in"]["payout_yuan"], "0")

    def test_snapshot_usage(self):
        ids = self.put(balanced_batch())
        first = self.service.publish("july-1", "2026-07", self.service.preview("2026-07")["fingerprint"])
        usage = self.service.snapshots.fact_usage(ids[0]["fact_id"])
        self.assertEqual(usage["releases"][0]["release_id"], first["release_id"])
        self.assertEqual(usage["releases"][0]["street_material"][0]["street_id"], "street-1")

    def test_month_release_comparison(self):
        self.put(balanced_batch())
        self.service.publish("july-1", "2026-07", self.service.preview("2026-07")["fingerprint"])
        self.put(balanced_batch("batch-2", "b"))
        self.service.publish("july-2", "2026-07", self.service.preview("2026-07")["fingerprint"])
        listing = self.service.snapshots.releases_for_month("2026-07")
        self.assertEqual([r["name"] for r in listing["releases"]], ["july-1", "july-2"])
        diff = self.service.snapshots.compare_month_releases("2026-07")
        self.assertEqual(len(diff["changes"]), 1)
        self.assertEqual(diff["changes"][0]["changed_keys"][0]["material_id"], "foam")

    def test_empty_month_release_listing(self):
        self.assertEqual(self.service.snapshots.releases_for_month("2026-08")["releases"], [])
        self.assertEqual(self.service.snapshots.compare_month_releases("2026-08")["changes"], [])
