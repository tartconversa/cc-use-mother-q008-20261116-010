import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from looplake.api import create_app
from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.service = LakeService(self.folder / "lake.sqlite3")
        seed_dimensions(self.service)

    def test_manifest_resume_and_idempotence(self):
        manifest = self.folder / "events.json"
        checkpoint = self.folder / "check.json"
        manifest.write_text(json.dumps({"events": balanced_batch()}), encoding="utf-8")
        first = self.service.batches.resume(manifest, checkpoint, limit=1)
        self.assertEqual(first["next_offset"], 1)
        self.assertFalse(first["complete"])
        second = self.service.batches.resume(manifest, checkpoint)
        self.assertTrue(second["complete"])
        self.assertEqual(second["offset"], 1)
        self.assertTrue(self.service.batch_status("batch-1")["ready"])
        repeat = self.service.batches.run(self.service.batches.load(manifest))
        self.assertTrue(all(not item["created"] for item in repeat["outcomes"]))

    def test_manifest_mutation_rejected(self):
        manifest = self.folder / "events.json"
        checkpoint = self.folder / "check.json"
        rows = balanced_batch()
        manifest.write_text(json.dumps({"events": rows}), encoding="utf-8")
        self.service.batches.resume(manifest, checkpoint, limit=1)
        rows[0]["weight_kg"] = "11"
        manifest.write_text(json.dumps({"events": rows}), encoding="utf-8")
        with self.assertRaises(LakeError) as failure:
            self.service.batches.resume(manifest, checkpoint)
        self.assertEqual(failure.exception.code, "checkpoint_changed")

    def test_bad_event_stops_on_error(self):
        rows = [event("deposit", "d1"), event("deposit", "bad id"), event("weigh_in", "w1")]
        result = self.service.batches.run({"events": rows}, stop_on_error=True)
        self.assertEqual(result["next_offset"], 2)
        self.assertEqual(result["outcomes"][-1]["state"], "error")
        self.assertEqual(len(self.service.ledger.batch("batch-1")), 1)

    def test_replay_after_process_restart(self):
        manifest = self.folder / "events.json"
        checkpoint = self.folder / "check.json"
        manifest.write_text(json.dumps({"events": balanced_batch()}), encoding="utf-8")
        self.service.batches.resume(manifest, checkpoint, limit=1)
        restarted = LakeService(self.folder / "lake.sqlite3")
        result = restarted.batches.resume(manifest, checkpoint)
        self.assertTrue(result["complete"])
        self.assertTrue(restarted.batch_status("batch-1")["ready"])

    def test_missing_manifest(self):
        with self.assertRaises(LakeError) as failure:
            self.service.batches.load(self.folder / "missing.json")
        self.assertEqual(failure.exception.code, "manifest_read")

    def test_invalid_json(self):
        path = self.folder / "bad.json"
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(LakeError) as failure:
            self.service.batches.load(path)
        self.assertEqual(failure.exception.code, "manifest_json")

    def test_invalid_checkpoint(self):
        manifest = self.folder / "events.json"
        checkpoint = self.folder / "check.json"
        manifest.write_text(json.dumps({"events": balanced_batch()}), encoding="utf-8")
        checkpoint.write_text("{broken", encoding="utf-8")
        with self.assertRaises(LakeError) as failure:
            self.service.batches.resume(manifest, checkpoint)
        self.assertEqual(failure.exception.code, "checkpoint_json")

    def test_manifest_shape(self):
        with self.assertRaises(LakeError):
            self.service.batches.run({"rows": []})
        with self.assertRaises(LakeError):
            self.service.batches.run({"events": []}, offset=-1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = TestClient(create_app(Path(self.temp.name) / "api.sqlite3"))

    def test_source_dimension_event_to_release(self):
        self.assertEqual(self.client.get("/health").json()["status"], "ok")
        source = self.client.post("/sources", json={"source": "box-feed", "channel": "smart_box"})
        self.assertEqual(source.status_code, 200)
        for row in (
            {"kind": "street", "external_id": "street-1", "name": "街道", "valid_from": "2026-01-01"},
            {"kind": "community", "external_id": "community-1", "name": "社区", "valid_from": "2026-01-01",
             "attributes": {"street_id": "street-1"}},
            {"kind": "device", "external_id": "box-1", "name": "箱", "valid_from": "2026-01-01",
             "attributes": {"community_id": "community-1"}},
            {"kind": "material", "external_id": "foam", "name": "泡沫", "valid_from": "2026-01-01"},
        ):
            self.assertEqual(self.client.post("/dimensions", json=row).status_code, 200)
        self.assertEqual(self.client.post("/rates", json={"material_id": "foam", "channel": "smart_box",
                                                         "valid_from": "2026-01-01", "rate_yuan_per_kg": "0.08"}).status_code, 200)
        self.assertEqual(self.client.get("/rates/foam").json()[0]["rate_yuan_per_kg"], "0.08")
        posted = self.client.post("/events", json=event("deposit", "d1"))
        self.assertEqual(posted.status_code, 200)
        self.assertEqual(posted.json()["state"], "normalized")
        fact_id = posted.json()["fact_id"]
        self.assertEqual(self.client.get(f"/facts/{fact_id}").json()["street_id"], "street-1")
        self.assertEqual(self.client.get(f"/lineage/facts/{fact_id}").json()["fact"]["id"], fact_id)
        self.assertFalse(self.client.get("/months/2026-07/quality").json()["ready"])

    def test_api_error_semantics(self):
        response = self.client.post("/events", json=event("deposit", "d1"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "source_inactive")
        response = self.client.get("/facts/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "unknown_fact")

    def test_api_rejects_extra_fields(self):
        payload = event("deposit", "d1")
        payload["unknown"] = "value"
        response = self.client.post("/events", json=payload)
        self.assertEqual(response.status_code, 422)

    def test_api_source_version(self):
        first = self.client.post("/sources", json={"source": "box-feed", "channel": "smart_box"})
        self.assertEqual(first.json()["version"], 1)
        stale = self.client.post("/sources", json={"source": "box-feed", "channel": "smart_box",
                                                         "expected_version": 0})
        self.assertEqual(stale.status_code, 409)

    def test_api_quality_and_audit(self):
        self.client.post("/sources", json={"source": "box-feed", "channel": "smart_box"})
        audit = self.client.get("/audits").json()
        self.assertTrue(any(row["action"] == "source_registered" for row in audit))
        self.assertEqual(self.client.get("/ingestions?state=quarantined").json(), [])
