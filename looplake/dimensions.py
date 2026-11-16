from __future__ import annotations

from datetime import date
import json

from .model import Dimension, LakeError, key
from .store import Store


class Dimensions:
    def __init__(self, store: Store):
        self.store = store

    def register_source(self, source: str, channel: str, expected_version: int | None = None):
        source = key(source, "source")
        if channel not in {"smart_box", "scale", "station", "payment"}:
            raise LakeError("invalid_channel", "source channel unsupported")
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
            actual = old["version"] if old else 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_source", "source version changed", 409)
            if old and old["channel"] != channel:
                raise LakeError("source_channel", "source channel cannot change", 409)
            version = actual + 1
            if old:
                conn.execute("UPDATE source_registry SET active=1,version=? WHERE source=?", (version, source))
            else:
                conn.execute("INSERT INTO source_registry VALUES(?,?,?,?,datetime('now'))",
                             (source, channel, 1, version))
            self.store.audit(conn, "source_registered", source, {"channel": channel, "version": version})
        return {"source": source, "channel": channel, "active": True, "version": version}

    def disable_source(self, source: str, expected_version: int):
        source = key(source, "source")
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
            if old is None:
                raise LakeError("unknown_source", "source is not registered", 404)
            if old["version"] != expected_version:
                raise LakeError("stale_source", "source version changed", 409)
            conn.execute("UPDATE source_registry SET active=0,version=version+1 WHERE source=?", (source,))
            self.store.audit(conn, "source_disabled", source, {"version": expected_version + 1})
        return {"source": source, "active": False, "version": expected_version + 1}

    def put(self, data: dict, expected_version: int | None = None):
        dimension = Dimension.parse(data)
        with self.store.write() as conn:
            old = self.store.one(conn, "SELECT MAX(version) AS v FROM dimension_versions WHERE kind=? AND external_id=?",
                                 (dimension.kind, dimension.external_id))
            actual = old["v"] or 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_dimension", "dimension version changed", 409)
            overlaps = self.store.all(conn, "SELECT id FROM dimension_versions WHERE kind=? AND external_id=? AND (valid_to IS NULL OR valid_to>?) AND (? IS NULL OR valid_from<?)",
                                      (dimension.kind, dimension.external_id, dimension.valid_from,
                                       dimension.valid_to, dimension.valid_to))
            if overlaps:
                raise LakeError("dimension_overlap", "effective periods overlap", 409)
            attrs = dimension.attributes
            if dimension.kind in {"community", "device"}:
                parent = "street_id" if dimension.kind == "community" else "community_id"
                if parent not in attrs:
                    raise LakeError("missing_parent", f"{parent} required")
                parent_kind = "street" if dimension.kind == "community" else "community"
                parent_row = self._resolve(conn, parent_kind, key(attrs[parent], parent), dimension.valid_from)
                if dimension.valid_to and parent_row["valid_to"] and parent_row["valid_to"] < dimension.valid_to:
                    raise LakeError("parent_range", "parent period ends before child")
                if parent_row["valid_from"] > dimension.valid_from:
                    raise LakeError("parent_range", "parent starts after child")
            version = actual + 1
            cursor = conn.execute("INSERT INTO dimension_versions(kind,external_id,name,valid_from,valid_to,attributes,version,created_at) VALUES(?,?,?,?,?,?,?,datetime('now'))",
                                  (dimension.kind, dimension.external_id, dimension.name, dimension.valid_from,
                                   dimension.valid_to, json.dumps(attrs, ensure_ascii=False, sort_keys=True), version))
            self.store.audit(conn, "dimension_added", f"{dimension.kind}:{dimension.external_id}",
                             {"id": cursor.lastrowid, "version": version, "valid_from": dimension.valid_from})
        return {"id": cursor.lastrowid, "kind": dimension.kind,
                "external_id": dimension.external_id, "version": version}

    def _resolve(self, conn, kind: str, external_id: str, day: str):
        rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
                              (kind, external_id, day, day))
        if len(rows) != 1:
            raise LakeError("unknown_dimension", f"{kind}:{external_id} not valid on {day}", 409)
        return rows[0]

    def resolve_event(self, conn, event) -> dict:
        day = event.happened_at[:10]
        result = {}
        for kind, external_id in (("material", event.material_id), ("device", event.device_id),
                                  ("station", event.station_id), ("actor", event.actor_id)):
            if external_id is not None:
                row = self._resolve(conn, kind, external_id, day)
                result[kind] = {"id": row["id"], "external_id": external_id,
                                "version": row["version"], "name": row["name"]}
                if kind == "device":
                    attrs = json.loads(row["attributes"])
                    community_id = key(attrs["community_id"], "community_id")
                    community = self._resolve(conn, "community", community_id, day)
                    street_id = key(json.loads(community["attributes"])["street_id"], "street_id")
                    street = self._resolve(conn, "street", street_id, day)
                    result["community"] = {"id": community["id"], "external_id": community_id,
                                           "version": community["version"], "name": community["name"]}
                    result["street"] = {"id": street["id"], "external_id": street_id,
                                        "version": street["version"], "name": street["name"]}
        return result

    def list_versions(self, kind: str, external_id: str):
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? ORDER BY version",
                                  (kind, external_id))
        for row in rows:
            row["attributes"] = json.loads(row["attributes"])
        return rows
