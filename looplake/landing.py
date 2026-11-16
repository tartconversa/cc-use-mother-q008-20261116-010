from __future__ import annotations

from dataclasses import dataclass
import json

from .dimensions import Dimensions
from .model import LakeError, RawEvent, now
from .store import Store


@dataclass(frozen=True)
class Landed:
    ingestion_id: int
    event: RawEvent
    created: bool
    state: str
    normalized_id: int | None

    def as_dict(self):
        return {"ingestion_id": self.ingestion_id, "created": self.created,
                "state": self.state, "fact_id": self.normalized_id}


class Landing:
    def __init__(self, store: Store, dimensions: Dimensions):
        self.store, self.dimensions = store, dimensions

    def accept(self, data: dict) -> Landed:
        event = RawEvent.parse(data)
        with self.store.write() as conn:
            source = self.store.one(conn, "SELECT * FROM source_registry WHERE source=?", (event.source,))
            if source is None or not source["active"]:
                raise LakeError("source_inactive", "source must be registered and active", 403)
            if source["channel"] != event.channel:
                raise LakeError("source_channel", "source channel mismatch", 403)
            existing = self.store.one(conn, "SELECT * FROM ingestions WHERE source=? AND external_id=? AND content_hash=?",
                                      (event.source, event.external_id, event.content_hash))
            if existing:
                return Landed(existing["id"], event, False, existing["state"], existing["normalized_id"])
            prior = self.store.one(conn, "SELECT id FROM ingestions WHERE source=? AND external_id=?",
                                   (event.source, event.external_id))
            if prior:
                raise LakeError("event_conflict", "same source event ID has different content", 409)
            cursor = conn.execute("INSERT INTO ingestions(source,external_id,content_hash,raw_json,received_at,state) VALUES(?,?,?,?,?,?)",
                                  (event.source, event.external_id, event.content_hash,
                                   json.dumps(event.as_dict(), ensure_ascii=False, sort_keys=True), now(), "received"))
            ingestion_id = cursor.lastrowid
            self.store.audit(conn, "landed", str(ingestion_id), {"source": event.source,
                                                                   "external_id": event.external_id,
                                                                   "hash": event.content_hash})
        return Landed(ingestion_id, event, True, "received", None)

    def quarantine(self, ingestion_id: int, error: LakeError):
        with self.store.write() as conn:
            row = self.store.one(conn, "SELECT * FROM ingestions WHERE id=?", (ingestion_id,))
            if row is None:
                raise LakeError("unknown_ingestion", "ingestion not found", 404)
            if row["state"] == "normalized":
                raise LakeError("already_normalized", "normalized ingestion cannot be quarantined", 409)
            conn.execute("UPDATE ingestions SET state='quarantined',issue=? WHERE id=?",
                         (json.dumps({"code": error.code, "detail": error.detail}, ensure_ascii=False), ingestion_id))
            self.store.audit(conn, "quarantined", str(ingestion_id), {"code": error.code})
        return {"ingestion_id": ingestion_id, "state": "quarantined", "issue": error.code}

    def retry(self, ingestion_id: int):
        row = self.store.ingestion(ingestion_id)
        if row["state"] == "normalized":
            return {"ingestion_id": ingestion_id, "state": "normalized", "fact_id": row["normalized_id"]}
        return RawEvent.parse(row["raw"])

    def pending(self, state: str = "quarantined", limit: int = 100):
        if state not in {"received", "quarantined", "normalized"}:
            raise LakeError("invalid_state", "unsupported landing state")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise LakeError("invalid_limit", "limit must be 1..500")
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT id,source,external_id,received_at,state,issue,normalized_id FROM ingestions WHERE state=? ORDER BY id LIMIT ?",
                                  (state, limit))
        for row in rows:
            if row["issue"]:
                row["issue"] = json.loads(row["issue"])
        return rows
