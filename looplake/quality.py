from __future__ import annotations

from collections import Counter
import json

from .landing import Landing
from .ledger import Ledger
from .mart import Mart
from .reconcile import Reconciliation
from .store import Store


class Quality:
    def __init__(self, store: Store, landing: Landing, ledger: Ledger,
                 reconciliation: Reconciliation, mart: Mart):
        self.store, self.landing, self.ledger = store, landing, ledger
        self.reconciliation, self.mart = reconciliation, mart

    def ingestion_health(self):
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT source,state,issue FROM ingestions ORDER BY id")
        states = Counter(row["state"] for row in rows)
        per_source = {}
        issues = Counter()
        for row in rows:
            per_source.setdefault(row["source"], Counter())[row["state"]] += 1
            if row["issue"]:
                issues[json.loads(row["issue"])["code"]] += 1
        return {"total": len(rows), "states": dict(states),
                "by_source": {key: dict(value) for key, value in sorted(per_source.items())},
                "issues": dict(issues)}

    def month(self, month: str):
        checked = self.reconciliation.month(month)
        report = self.mart.month(month)
        with self.store.connect() as conn:
            quarantined = self.store.all(conn, "SELECT id,source,external_id,issue FROM ingestions WHERE state='quarantined' AND substr(json_extract(raw_json,'$.happened_at'),1,7)=? ORDER BY id",
                                         (month,))
        for row in quarantined:
            row["issue"] = json.loads(row["issue"])
        return {"month": month, "ready": checked["ready"] and not quarantined,
                "unresolved_batches": checked["unresolved_batches"],
                "quarantined": quarantined, "excluded_fact_ids": report["excluded_fact_ids"],
                "rows": len(report["rows"])}

    def batch(self, batch_id: str):
        result = self.reconciliation.batch(batch_id)
        facts = self.ledger.batch(batch_id)
        return {"batch_id": batch_id, "ready": result["ready"],
                "issues": result["issues"], "fact_count": len(facts),
                "sources": sorted({row["source"] for row in facts})}

    def source(self, source_id: str):
        source = self.store.source(source_id)
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT id,state,issue FROM ingestions WHERE source=? ORDER BY id", (source_id,))
        return {"source": source_id, "active": bool(source["active"]), "channel": source["channel"],
                "version": source["version"], "ingestions": len(rows),
                "quarantined_ids": [row["id"] for row in rows if row["state"] == "quarantined"]}
