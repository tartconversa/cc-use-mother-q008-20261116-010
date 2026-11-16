from __future__ import annotations

from .ledger import Ledger
from .mart import Mart
from .reconcile import Reconciliation
from .release import Release
from .store import Store


class Lineage:
    def __init__(self, store: Store, ledger: Ledger, reconciliation: Reconciliation,
                 mart: Mart, releases: Release):
        self.store, self.ledger = store, ledger
        self.reconciliation, self.mart, self.releases = reconciliation, mart, releases

    def fact(self, fact_id: int):
        trace = self.ledger.trace(fact_id)
        dependency = self.reconciliation.fact_dependency(fact_id)
        return {**trace, "reconciliation": dependency}

    def row(self, month: str, street_id: str, material_id: str):
        report = self.mart.month(month)
        rows = [row for row in report["rows"] if row["street_id"] == street_id
                and row["material_id"] == material_id]
        if not rows:
            from .model import LakeError
            raise LakeError("unknown_mart_row", "mart row not found", 404)
        row = rows[0]
        traces = [self.ledger.trace(fact_id) for fact_id in row["fact_ids"]]
        batch_ids = sorted({trace["fact"]["batch_id"] for trace in traces})
        return {"row": row, "traces": traces,
                "reconciliations": [self.reconciliation.batch(batch_id) for batch_id in batch_ids]}

    def release_row(self, release_id: int, street_id: str, material_id: str):
        release = self.releases.get(release_id)
        rows = [row for row in release["content"]["mart"]["rows"]
                if row["street_id"] == street_id and row["material_id"] == material_id]
        if not rows:
            from .model import LakeError
            raise LakeError("unknown_release_row", "release row not found", 404)
        return {"release_id": release_id, "fingerprint": release["fingerprint"],
                "row": rows[0], "frozen_reconciliation": release["content"]["reconciliation"]}

    def audit_subject(self, subject: str):
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM audit WHERE subject=? ORDER BY id", (subject,))
        import json
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows
