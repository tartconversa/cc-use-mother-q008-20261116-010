from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from pathlib import Path

from .model import LakeError, now


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS dimension_versions (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, external_id TEXT NOT NULL,
 name TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT,
 attributes TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(kind,external_id,version)
);
CREATE INDEX IF NOT EXISTS ix_dim_time ON dimension_versions(kind,external_id,valid_from,valid_to);
CREATE TABLE IF NOT EXISTS ingestions (
 id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
 content_hash TEXT NOT NULL, raw_json TEXT NOT NULL, received_at TEXT NOT NULL,
 state TEXT NOT NULL, issue TEXT, normalized_id INTEGER,
 UNIQUE(source,external_id,content_hash)
);
CREATE INDEX IF NOT EXISTS ix_ingest_source ON ingestions(source,external_id,id);
CREATE TABLE IF NOT EXISTS facts (
 id INTEGER PRIMARY KEY, ingestion_id INTEGER NOT NULL UNIQUE REFERENCES ingestions(id),
 source TEXT NOT NULL, external_id TEXT NOT NULL, event_type TEXT NOT NULL,
 happened_at TEXT NOT NULL, day TEXT NOT NULL, batch_id TEXT NOT NULL,
 material_id TEXT, device_id TEXT, station_id TEXT, actor_id TEXT,
 street_id TEXT, community_id TEXT, weight_kg TEXT, payout_yuan TEXT,
 channel TEXT NOT NULL, dimensions_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS ix_fact_batch ON facts(batch_id,event_type);
CREATE INDEX IF NOT EXISTS ix_fact_day ON facts(day,street_id,material_id);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
 subject TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, project_month TEXT NOT NULL,
 fingerprint TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_registry (
 source TEXT PRIMARY KEY, channel TEXT NOT NULL, active INTEGER NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
 id INTEGER PRIMARY KEY, material_id TEXT NOT NULL, channel TEXT NOT NULL,
 valid_from TEXT NOT NULL, valid_to TEXT, rate_yuan_per_kg TEXT NOT NULL,
 version INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(material_id,channel,version)
);
CREATE INDEX IF NOT EXISTS ix_rate_period ON rate_cards(material_id,channel,valid_from,valid_to);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def write(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def one(conn, sql: str, args=()):
        row = conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    @staticmethod
    def all(conn, sql: str, args=()):
        return [dict(row) for row in conn.execute(sql, args)]

    @staticmethod
    def audit(conn, action: str, subject: str, payload: dict):
        conn.execute("INSERT INTO audit(at,action,subject,payload) VALUES(?,?,?,?)",
                     (now(), action, subject, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def source(self, source: str):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM source_registry WHERE source=?", (source,))
        if row is None:
            raise LakeError("unknown_source", "source is not registered", 404)
        return row

    def dimension(self, kind: str, external_id: str, day: str):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM dimension_versions WHERE kind=? AND external_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) ORDER BY version DESC",
                            (kind, external_id, day, day))
        if not rows:
            raise LakeError("unknown_dimension", f"{kind}:{external_id} is not valid on {day}", 409)
        if len(rows) > 1:
            raise LakeError("ambiguous_dimension", f"{kind}:{external_id} has overlapping versions", 409)
        row = rows[0]
        row["attributes"] = json.loads(row["attributes"])
        return row

    def fact(self, fact_id: int):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM facts WHERE id=?", (fact_id,))
        if row is None:
            raise LakeError("unknown_fact", "fact not found", 404)
        row["dimensions"] = json.loads(row.pop("dimensions_json"))
        return row

    def facts(self, where: str = "", args=()):
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM facts " + where + " ORDER BY id", args)
        for row in rows:
            row["dimensions"] = json.loads(row.pop("dimensions_json"))
        return rows

    def ingestion(self, ingestion_id: int):
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM ingestions WHERE id=?", (ingestion_id,))
        if row is None:
            raise LakeError("unknown_ingestion", "ingestion not found", 404)
        row["raw"] = json.loads(row.pop("raw_json"))
        return row

    def release(self, identifier: int | str):
        if isinstance(identifier, int):
            clause, arg = "id=?", identifier
        else:
            clause, arg = "name=?", identifier
        with self.connect() as conn:
            row = self.one(conn, "SELECT * FROM releases WHERE " + clause, (arg,))
        if row is None:
            raise LakeError("unknown_release", "release not found", 404)
        row["content"] = json.loads(row["content"])
        return row

    def audits(self, since: int = 0, limit: int = 100):
        if type(since) is not int or since < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise LakeError("invalid_page", "invalid audit pagination")
        with self.connect() as conn:
            rows = self.all(conn, "SELECT * FROM audit WHERE id>? ORDER BY id LIMIT ?", (since, limit))
        for row in rows:
            row["payload"] = json.loads(row["payload"])
        return rows
