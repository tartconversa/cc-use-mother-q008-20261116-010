from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .ledger import Ledger
from .model import LakeError, calendar_day, key


class Inventory:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def station_day(self, station_id: str, material_id: str, day: str):
        station_id = key(station_id, "station_id")
        material_id = key(material_id, "material_id")
        day = calendar_day(day, "day")
        facts = self.ledger.store.facts("WHERE station_id=? AND material_id=? AND day<=?",
                                       (station_id, material_id, day))
        incoming = Decimal("0")
        outgoing = Decimal("0")
        arrivals = []
        departures = []
        for fact in facts:
            if fact["event_type"] == "weigh_in":
                incoming += Decimal(fact["weight_kg"])
                arrivals.append(fact)
            elif fact["event_type"] == "weigh_out":
                outgoing += Decimal(fact["weight_kg"])
                departures.append(fact)
        running = Decimal("0")
        negative_at = []
        movements = sorted(arrivals + departures, key=lambda row: (row["happened_at"], row["id"]))
        for fact in movements:
            delta = Decimal(fact["weight_kg"])
            running += delta if fact["event_type"] == "weigh_in" else -delta
            if running < 0:
                negative_at.append(fact["id"])
        same_day = [fact for fact in movements if fact["day"] == day]
        return {"station_id": station_id, "material_id": material_id, "day": day,
                "inbound_to_date_kg": format(incoming, "f"),
                "outbound_to_date_kg": format(outgoing, "f"),
                "closing_kg": format(incoming - outgoing, "f"),
                "negative_fact_ids": negative_at,
                "movement_ids": [fact["id"] for fact in same_day],
                "ready": not negative_at and incoming >= outgoing}

    def month(self, month: str):
        calendar_day(month + "-01", "month")
        facts = self.ledger.period(month)
        keys = sorted({(fact["station_id"], fact["material_id"])
                       for fact in facts if fact["station_id"] and fact["material_id"]
                       and fact["event_type"] in {"weigh_in", "weigh_out"}})
        last = month + "-31"
        from datetime import date, timedelta
        if month.endswith("12"):
            year = int(month[:4]) + 1
            end = date(year, 1, 1) - timedelta(days=1)
        else:
            end = date(int(month[:4]), int(month[5:7]) + 1, 1) - timedelta(days=1)
        rows = [self.station_day(station, material, end.isoformat()) for station, material in keys]
        return {"month": month, "ready": all(row["ready"] for row in rows),
                "station_materials": rows,
                "negative_fact_ids": sorted({fact_id for row in rows for fact_id in row["negative_fact_ids"]})}

    def batch_station_trace(self, batch_id: str):
        batch_id = key(batch_id, "batch_id")
        facts = self.ledger.batch(batch_id)
        stations = sorted({(fact["station_id"], fact["material_id"])
                           for fact in facts if fact["station_id"] and fact["material_id"]})
        results = []
        for station, material in stations:
            selected = [fact for fact in facts if fact["station_id"] == station
                        and fact["material_id"] == material]
            incoming = sum((Decimal(fact["weight_kg"] or "0") for fact in selected
                            if fact["event_type"] == "weigh_in"), Decimal("0"))
            outgoing = sum((Decimal(fact["weight_kg"] or "0") for fact in selected
                            if fact["event_type"] == "weigh_out"), Decimal("0"))
            results.append({"station_id": station, "material_id": material,
                            "inbound_kg": format(incoming, "f"), "outbound_kg": format(outgoing, "f"),
                            "net_kg": format(incoming - outgoing, "f"),
                            "fact_ids": [fact["id"] for fact in selected]})
        return {"batch_id": batch_id, "stations": results}

    def movements(self, station_id: str, material_id: str, start: str, end: str):
        station_id = key(station_id, "station_id")
        material_id = key(material_id, "material_id")
        start, end = calendar_day(start, "start"), calendar_day(end, "end")
        if end < start:
            raise LakeError("invalid_range", "end precedes start")
        facts = self.ledger.store.facts("WHERE station_id=? AND material_id=? AND day>=? AND day<=?",
                                       (station_id, material_id, start, end))
        rows = []
        for fact in facts:
            if fact["event_type"] not in {"weigh_in", "weigh_out"}:
                continue
            rows.append({"fact_id": fact["id"], "batch_id": fact["batch_id"],
                         "happened_at": fact["happened_at"], "event_type": fact["event_type"],
                         "weight_kg": fact["weight_kg"]})
        rows.sort(key=lambda row: (row["happened_at"], row["fact_id"]))
        return {"station_id": station_id, "material_id": material_id,
                "start": start, "end": end, "movements": rows}
