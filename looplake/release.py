from __future__ import annotations

import json

from .mart import Mart
from .model import LakeError, digest, key, now
from .reconcile import Reconciliation
from .store import Store


class Release:
    def __init__(self, store: Store, mart: Mart, reconciliation: Reconciliation, inventory=None):
        self.store, self.mart, self.reconciliation = store, mart, reconciliation
        self.inventory = inventory

    def preview(self, month: str):
        report = self.mart.month(month)
        checked = self.reconciliation.month(month)
        inventory = self.inventory.month(month) if self.inventory else None
        with self.store.connect() as conn:
            quarantined = self.store.all(conn, "SELECT id FROM ingestions WHERE state='quarantined' AND substr(json_extract(raw_json,'$.happened_at'),1,7)=?",
                                         (month,))
        return {"month": month, "ready": report["ready"] and checked["ready"]
                and (inventory is None or inventory["ready"]) and not quarantined,
                "mart": report, "reconciliation": checked, "inventory": inventory,
                "quarantined_ingestion_ids": [row["id"] for row in quarantined],
                "fingerprint": digest({"mart": report, "reconciliation": checked,
                                       "inventory": inventory, "quarantined": quarantined})}

    def publish(self, name: str, month: str, expected_fingerprint: str):
        name = key(name, "name")
        if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
            raise LakeError("invalid_fingerprint", "expected_fingerprint must be SHA-256")
        preview = self.preview(month)
        if not preview["ready"]:
            raise LakeError("unresolved_month", "month has unresolved material batches", 409)
        if expected_fingerprint != preview["fingerprint"]:
            raise LakeError("stale_preview", "month changed since preview", 409)
        with self.store.write() as conn:
            if self.preview(month)["fingerprint"] != expected_fingerprint:
                raise LakeError("stale_preview", "month changed during release", 409)
            previous = self.store.one(conn, "SELECT * FROM releases WHERE name=?", (name,))
            if previous:
                if previous["fingerprint"] != preview["fingerprint"]:
                    raise LakeError("release_conflict", "release name already has different content", 409)
                return {"release_id": previous["id"], "name": name,
                        "fingerprint": previous["fingerprint"], "created": False}
            cursor = conn.execute("INSERT INTO releases(name,project_month,fingerprint,content,created_at) VALUES(?,?,?,?,?)",
                                  (name, month, preview["fingerprint"],
                                   json.dumps(preview, ensure_ascii=False, sort_keys=True), now()))
            self.store.audit(conn, "release_published", name, {"id": cursor.lastrowid,
                                                                  "fingerprint": preview["fingerprint"]})
        return {"release_id": cursor.lastrowid, "name": name,
                "fingerprint": preview["fingerprint"], "created": True}

    def get(self, identifier: int | str):
        return self.store.release(identifier)

    def compare(self, left: int | str, right: int | str):
        first, second = self.get(left), self.get(right)
        before = {(row["street_id"], row["material_id"]): row
                  for row in first["content"]["mart"]["rows"]}
        after = {(row["street_id"], row["material_id"]): row
                 for row in second["content"]["mart"]["rows"]}
        changes = []
        for key_tuple in sorted(set(before) | set(after)):
            a, b = before.get(key_tuple), after.get(key_tuple)
            if a != b:
                changes.append({"street_id": key_tuple[0], "material_id": key_tuple[1],
                                "before": a, "after": b})
        return {"left_id": first["id"], "right_id": second["id"],
                "left_fingerprint": first["fingerprint"], "right_fingerprint": second["fingerprint"],
                "changes": changes}
