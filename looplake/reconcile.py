from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .ledger import Ledger
from .model import LakeError, key


class Reconciliation:
    def __init__(self, ledger: Ledger, settlement=None):
        self.ledger = ledger
        self.settlement = settlement

    @staticmethod
    def _sum(facts, event_type: str, field: str) -> Decimal:
        return sum((Decimal(row[field] or "0") for row in facts if row["event_type"] == event_type), Decimal("0"))

    @staticmethod
    def _ids(facts, event_type: str):
        return [row["id"] for row in facts if row["event_type"] == event_type]

    def batch(self, batch_id: str, tolerance_kg: str = "0.5"):
        batch_id = key(batch_id, "batch_id")
        try:
            tolerance = Decimal(str(tolerance_kg))
        except Exception as exc:
            raise LakeError("invalid_tolerance", "tolerance must be numeric") from exc
        if not tolerance.is_finite() or tolerance < 0 or tolerance > 1000:
            raise LakeError("invalid_tolerance", "tolerance must be 0..1000 kg")
        facts = self.ledger.batch(batch_id)
        if not facts:
            raise LakeError("unknown_batch", "batch has no normalized facts", 404)
        deposit = self._sum(facts, "deposit", "weight_kg")
        weighing_in = self._sum(facts, "weigh_in", "weight_kg")
        weighing_out = self._sum(facts, "weigh_out", "weight_kg")
        transfer = self._sum(facts, "transfer", "weight_kg")
        payout = self._sum(facts, "payout", "payout_yuan")
        issues = []
        if not self._ids(facts, "deposit"):
            issues.append({"code": "missing_deposit", "fact_ids": []})
        if not self._ids(facts, "weigh_in"):
            issues.append({"code": "missing_weigh_in", "fact_ids": []})
        if self._ids(facts, "deposit") and self._ids(facts, "weigh_in"):
            if abs(deposit - weighing_in) > tolerance:
                issues.append({"code": "deposit_scale_gap", "fact_ids": self._ids(facts, "deposit") + self._ids(facts, "weigh_in"),
                               "gap_kg": format(deposit - weighing_in, "f")})
        if weighing_out and weighing_out > weighing_in + tolerance:
            issues.append({"code": "outbound_exceeds_inbound", "fact_ids": self._ids(facts, "weigh_out")})
        if transfer and weighing_out and abs(transfer - weighing_out) > tolerance:
            issues.append({"code": "transfer_scale_gap", "fact_ids": self._ids(facts, "transfer") + self._ids(facts, "weigh_out"),
                           "gap_kg": format(transfer - weighing_out, "f")})
        materials = {fact["material_id"] for fact in facts if fact["material_id"]}
        if len(materials) > 1:
            issues.append({"code": "mixed_material", "fact_ids": [fact["id"] for fact in facts if fact["material_id"]]})
        deposit_locations = {(fact["street_id"], fact["material_id"])
                             for fact in facts if fact["event_type"] == "deposit"}
        if self._ids(facts, "payout") and len(deposit_locations) > 1:
            issues.append({"code": "ambiguous_payout_allocation",
                           "fact_ids": self._ids(facts, "payout") + self._ids(facts, "deposit")})
        settled = self.settlement.batch(facts) if self.settlement else None
        if settled:
            issues.extend(settled["issues"])
        return {"batch_id": batch_id, "ready": not issues, "tolerance_kg": format(tolerance, "f"),
                "deposit_kg": format(deposit, "f"), "weigh_in_kg": format(weighing_in, "f"),
                "weigh_out_kg": format(weighing_out, "f"), "transfer_kg": format(transfer, "f"),
                "payout_yuan": format(payout, "f"), "fact_ids": [fact["id"] for fact in facts],
                "settlement": settled, "issues": issues}

    def month(self, month: str, tolerance_kg: str = "0.5"):
        rows = self.ledger.period(month)
        batch_ids = sorted({row["batch_id"] for row in rows})
        results = [self.batch(batch_id, tolerance_kg) for batch_id in batch_ids]
        return {"month": month, "ready": bool(results) and all(r["ready"] for r in results),
                "batches": results, "unresolved_batches": [r["batch_id"] for r in results if not r["ready"]]}

    def sources(self, month: str):
        facts = self.ledger.period(month)
        by_source = defaultdict(lambda: {"fact_count": 0, "weight_kg": Decimal("0"), "payout_yuan": Decimal("0")})
        for fact in facts:
            bucket = by_source[fact["source"]]
            bucket["fact_count"] += 1
            bucket["weight_kg"] += Decimal(fact["weight_kg"] or "0")
            bucket["payout_yuan"] += Decimal(fact["payout_yuan"] or "0")
        return [{"source": source, "fact_count": values["fact_count"],
                 "weight_kg": format(values["weight_kg"], "f"),
                 "payout_yuan": format(values["payout_yuan"], "f")}
                for source, values in sorted(by_source.items())]

    def fact_dependency(self, fact_id: int):
        fact = self.ledger.store.fact(fact_id)
        result = self.batch(fact["batch_id"])
        return {"fact_id": fact_id, "batch_id": fact["batch_id"],
                "related_fact_ids": result["fact_ids"], "batch_ready": result["ready"],
                "issues": [issue for issue in result["issues"] if fact_id in issue["fact_ids"]]}
