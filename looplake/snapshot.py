from __future__ import annotations

from .model import calendar_day
from .store import Store


class SnapshotAudit:
    def __init__(self, store: Store):
        self.store = store

    def releases_for_month(self, month: str):
        calendar_day(month + "-01", "month")
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT id,name,fingerprint,created_at FROM releases WHERE project_month=? ORDER BY id",
                                  (month,))
        return {"month": month, "releases": rows}

    def fact_usage(self, fact_id: int):
        fact = self.store.fact(fact_id)
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT id,name,project_month,content,fingerprint FROM releases ORDER BY id")
        import json
        hits = []
        for row in rows:
            content = json.loads(row["content"])
            used_in = [mart_row for mart_row in content["mart"]["rows"]
                       if fact_id in mart_row["fact_ids"]]
            if used_in:
                hits.append({"release_id": row["id"], "name": row["name"],
                             "month": row["project_month"], "fingerprint": row["fingerprint"],
                             "street_material": [{"street_id": x["street_id"],
                                                  "material_id": x["material_id"]} for x in used_in]})
        return {"fact_id": fact_id, "batch_id": fact["batch_id"], "releases": hits}

    def compare_month_releases(self, month: str):
        releases = self.releases_for_month(month)["releases"]
        if len(releases) < 2:
            return {"month": month, "changes": []}
        changes = []
        for before, after in zip(releases, releases[1:]):
            with self.store.connect() as conn:
                left = self.store.one(conn, "SELECT content FROM releases WHERE id=?", (before["id"],))
                right = self.store.one(conn, "SELECT content FROM releases WHERE id=?", (after["id"],))
            import json
            left_rows = json.loads(left["content"])["mart"]["rows"]
            right_rows = json.loads(right["content"])["mart"]["rows"]
            before_keys = {(x["street_id"], x["material_id"]): x for x in left_rows}
            after_keys = {(x["street_id"], x["material_id"]): x for x in right_rows}
            changed = sorted(key for key in set(before_keys) | set(after_keys)
                             if before_keys.get(key) != after_keys.get(key))
            changes.append({"from_release": before["id"], "to_release": after["id"],
                            "changed_keys": [{"street_id": key[0], "material_id": key[1]}
                                             for key in changed]})
        return {"month": month, "changes": changes}
