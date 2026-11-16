from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .ledger import Ledger
from .model import LakeError, calendar_day, key


class Query:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def community_month(self, community_id: str, month: str):
        community_id = key(community_id, "community_id")
        calendar_day(month + "-01", "month")
        facts = self.ledger.store.facts("WHERE community_id=? AND substr(day,1,7)=?",
                                       (community_id, month))
        deposits = [fact for fact in facts if fact["event_type"] == "deposit"]
        grouped = defaultdict(lambda: {"kg": Decimal("0"), "ids": []})
        for fact in deposits:
            entry = grouped[fact["material_id"]]
            entry["kg"] += Decimal(fact["weight_kg"])
            entry["ids"].append(fact["id"])
        rows = [{"material_id": material, "deposit_kg": format(values["kg"], "f"),
                 "fact_ids": sorted(values["ids"])} for material, values in sorted(grouped.items())]
        return {"community_id": community_id, "month": month, "rows": rows,
                "deposit_event_count": len(deposits)}

    def device_history(self, device_id: str, start: str, end: str):
        device_id = key(device_id, "device_id")
        start, end = calendar_day(start, "start"), calendar_day(end, "end")
        if start > end:
            raise LakeError("invalid_range", "start follows end")
        facts = self.ledger.store.facts("WHERE device_id=? AND day>=? AND day<=?",
                                       (device_id, start, end))
        rows = [{"fact_id": fact["id"], "day": fact["day"],
                 "street_id": fact["street_id"], "community_id": fact["community_id"],
                 "material_id": fact["material_id"], "weight_kg": fact["weight_kg"],
                 "dimension_version": fact["dimensions"].get("device", {}).get("version")}
                for fact in facts if fact["event_type"] == "deposit"]
        return {"device_id": device_id, "start": start, "end": end, "rows": rows}

    def source_month(self, source: str, month: str):
        source = key(source, "source")
        calendar_day(month + "-01", "month")
        facts = self.ledger.store.facts("WHERE source=? AND substr(day,1,7)=?", (source, month))
        by_type = defaultdict(lambda: {"count": 0, "weight": Decimal("0"), "payout": Decimal("0")})
        for fact in facts:
            row = by_type[fact["event_type"]]
            row["count"] += 1
            row["weight"] += Decimal(fact["weight_kg"] or "0")
            row["payout"] += Decimal(fact["payout_yuan"] or "0")
        return {"source": source, "month": month,
                "events": {kind: {"count": values["count"],
                                  "weight_kg": format(values["weight"], "f"),
                                  "payout_yuan": format(values["payout"], "f")}
                           for kind, values in sorted(by_type.items())}}
