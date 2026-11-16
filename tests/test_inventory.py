import tempfile
import unittest
from pathlib import Path

from looplake.fixtures import balanced_batch, event, seed_dimensions
from looplake.model import LakeError
from looplake.service import LakeService


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = LakeService(Path(self.temp.name) / "lake.sqlite3")
        seed_dimensions(self.service)

    def put(self, rows):
        return [self.service.ingest(row) for row in rows]

    def test_inbound_without_outbound_is_stock(self):
        self.put(balanced_batch())
        result = self.service.inventory.station_day("station-1", "foam", "2026-07-15")
        self.assertEqual(result["inbound_to_date_kg"], "10")
        self.assertEqual(result["outbound_to_date_kg"], "0")
        self.assertEqual(result["closing_kg"], "10")
        self.assertTrue(result["ready"])

    def test_outbound_after_inbound(self):
        self.put(balanced_batch())
        self.put([event("weigh_out", "out-1", weight="6", happened_at="2026-07-15T11:00:00+08:00")])
        result = self.service.inventory.station_day("station-1", "foam", "2026-07-15")
        self.assertEqual(result["closing_kg"], "4")
        self.assertTrue(result["ready"])
        month = self.service.inventory.month("2026-07")
        self.assertTrue(month["ready"])
        self.assertEqual(month["station_materials"][0]["closing_kg"], "4")

    def test_outbound_first_is_negative(self):
        self.put([event("weigh_out", "out-1", weight="10")])
        inventory = self.service.inventory.month("2026-07")
        self.assertFalse(inventory["ready"])
        self.assertEqual(len(inventory["negative_fact_ids"]), 1)
        preview = self.service.preview("2026-07")
        self.assertFalse(preview["ready"])

    def test_arrival_after_departure_still_flags_order(self):
        self.put([event("weigh_out", "out-1", weight="10", happened_at="2026-07-15T10:00:00+08:00")])
        self.put([event("weigh_in", "in-1", weight="10", happened_at="2026-07-15T11:00:00+08:00")])
        inventory = self.service.inventory.station_day("station-1", "foam", "2026-07-15")
        self.assertEqual(inventory["closing_kg"], "0")
        self.assertFalse(inventory["ready"])

    def test_different_materials_are_separate(self):
        self.put([event("weigh_in", "foam-in", material_id="foam")])
        self.put([event("weigh_in", "glass-in", material_id="glass", weight="7")])
        foam = self.service.inventory.station_day("station-1", "foam", "2026-07-15")
        glass = self.service.inventory.station_day("station-1", "glass", "2026-07-15")
        self.assertEqual(foam["closing_kg"], "10")
        self.assertEqual(glass["closing_kg"], "7")

    def test_batch_station_trace(self):
        self.put(balanced_batch())
        result = self.service.inventory.batch_station_trace("batch-1")
        self.assertEqual(result["stations"][0]["inbound_kg"], "10")
        self.assertEqual(result["stations"][0]["outbound_kg"], "0")

    def test_movements_order(self):
        self.put([event("weigh_out", "out-1", weight="2", happened_at="2026-07-15T11:00:00+08:00")])
        self.put([event("weigh_in", "in-1", weight="10", happened_at="2026-07-15T10:00:00+08:00")])
        movement = self.service.inventory.movements("station-1", "foam", "2026-07-15", "2026-07-15")
        self.assertEqual([r["event_type"] for r in movement["movements"]], ["weigh_in", "weigh_out"])

    def test_invalid_movement_range(self):
        with self.assertRaises(LakeError) as failure:
            self.service.inventory.movements("station-1", "foam", "2026-07-16", "2026-07-15")
        self.assertEqual(failure.exception.code, "invalid_range")

    def test_month_end_boundary(self):
        self.put([event("weigh_in", "dec-in", happened_at="2026-12-31T12:00:00+08:00")])
        result = self.service.inventory.month("2026-12")
        self.assertEqual(result["station_materials"][0]["closing_kg"], "10")

    def test_empty_month_is_neutral(self):
        self.assertEqual(self.service.inventory.month("2026-07")["station_materials"], [])
        self.assertTrue(self.service.inventory.month("2026-07")["ready"])
