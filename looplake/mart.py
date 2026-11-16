from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .ledger import Ledger
from .model import LakeError
from .reconcile import Reconciliation


class Mart:
    def __init__(self, ledger: Ledger, reconciliation: Reconciliation):
        self.ledger, self.reconciliation = ledger, reconciliation

    @staticmethod
    def _number(value: Decimal):
        return format(value, "f")

    def month(self, month: str, include_unresolved: bool = False):
        facts = self.ledger.period(month)
        statuses = {batch: self.reconciliation.batch(batch) for batch in sorted({f["batch_id"] for f in facts})}
        selected_batches = {batch for batch, status in statuses.items() if status["ready"] or include_unresolved}
        buckets = defaultdict(lambda: {"weight_kg": Decimal("0"), "payout_yuan": Decimal("0"),
                                       "deposit_events": 0, "payout_events": 0, "fact_ids": []})
        batch_locations = {}
        for fact in facts:
            if fact["event_type"] == "deposit":
                batch_locations.setdefault(fact["batch_id"], set()).add((fact["street_id"], fact["material_id"]))
        excluded = []
        for fact in facts:
            if fact["batch_id"] not in selected_batches:
                excluded.append(fact["id"])
                continue
            if fact["event_type"] not in {"deposit", "payout"}:
                continue
            if fact["event_type"] == "payout":
                destinations = batch_locations.get(fact["batch_id"], set())
                if len(destinations) != 1:
                    excluded.append(fact["id"])
                    continue
                location, material = next(iter(destinations))
            else:
                location, material = fact["street_id"], fact["material_id"]
            location = location or "unassigned"
            material = material or "unassigned"
            bucket = buckets[(location, material)]
            if fact["event_type"] == "deposit":
                bucket["weight_kg"] += Decimal(fact["weight_kg"] or "0")
                bucket["deposit_events"] += 1
            else:
                bucket["payout_yuan"] += Decimal(fact["payout_yuan"] or "0")
                bucket["payout_events"] += 1
            bucket["fact_ids"].append(fact["id"])
        rows = [{"month": month, "street_id": key[0], "material_id": key[1],
                 "deposit_kg": self._number(value["weight_kg"]),
                 "payout_yuan": self._number(value["payout_yuan"]),
                 "deposit_events": value["deposit_events"], "payout_events": value["payout_events"],
                 "fact_ids": sorted(value["fact_ids"])} for key, value in sorted(buckets.items())]
        return {"month": month, "ready": bool(statuses) and all(r["ready"] for r in statuses.values()),
                "rows": rows, "excluded_fact_ids": excluded,
                "unresolved_batches": [batch for batch, result in statuses.items() if not result["ready"]],
                "batch_count": len(statuses)}

    def street(self, street_id: str, start_month: str, end_month: str):
        from .model import key
        street_id = key(street_id, "street_id")
        if start_month > end_month:
            raise LakeError("invalid_range", "start_month follows end_month")
        facts = [row for row in self.ledger.store.facts("WHERE street_id=?", (street_id,))
                 if start_month <= row["day"][:7] <= end_month]
        months = sorted({row["day"][:7] for row in facts})
        result = []
        for month in months:
            report = self.month(month)
            result.extend(row for row in report["rows"] if row["street_id"] == street_id)
        return {"street_id": street_id, "start_month": start_month, "end_month": end_month,
                "rows": result}

    def material(self, material_id: str, month: str):
        from .model import key
        material_id = key(material_id, "material_id")
        report = self.month(month)
        rows = [row for row in report["rows"] if row["material_id"] == material_id]
        total = sum((Decimal(row["deposit_kg"]) for row in rows), Decimal("0"))
        return {"material_id": material_id, "month": month, "deposit_kg": self._number(total),
                "rows": rows, "ready": report["ready"]}
