from __future__ import annotations

from pathlib import Path
import json

from .ledger import Ledger
from .model import LakeError, digest


class BatchImport:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    @staticmethod
    def load(path: str | Path):
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LakeError("manifest_read", "manifest cannot be read") from exc
        if len(raw) > 10_000_000:
            raise LakeError("manifest_size", "manifest is too large")
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise LakeError("manifest_json", "manifest is not valid JSON") from exc
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise LakeError("manifest_shape", "manifest requires events array")
        if len(document["events"]) > 10000:
            raise LakeError("manifest_size", "manifest has too many events")
        return document

    def run(self, document: dict, offset: int = 0, limit: int | None = None,
            stop_on_error: bool = False):
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise LakeError("manifest_shape", "manifest requires events array")
        events = document["events"]
        if type(offset) is not int or offset < 0 or offset > len(events):
            raise LakeError("invalid_offset", "offset is invalid")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise LakeError("invalid_limit", "limit must be 1..10000")
        end = min(len(events), offset + limit) if limit else len(events)
        outcomes = []
        for index in range(offset, end):
            data = events[index]
            try:
                result = self.ledger.ingest(data)
                outcomes.append({"index": index, "source": data.get("source") if isinstance(data, dict) else None,
                                 "external_id": data.get("external_id") if isinstance(data, dict) else None,
                                 **result})
            except LakeError as exc:
                outcomes.append({"index": index, "state": "error", "issue": {"code": exc.code,
                                                                               "detail": exc.detail}})
                if stop_on_error:
                    break
        next_offset = outcomes[-1]["index"] + 1 if outcomes else offset
        return {"manifest_digest": digest(document), "total": len(events),
                "offset": offset, "next_offset": next_offset,
                "complete": next_offset == len(events), "outcomes": outcomes}

    @staticmethod
    def save_checkpoint(path: str | Path, result: dict):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temp.replace(path)
        return str(path)

    def resume(self, manifest_path: str | Path, checkpoint_path: str | Path,
               limit: int | None = None, stop_on_error: bool = False):
        document = self.load(manifest_path)
        checkpoint = Path(checkpoint_path)
        if checkpoint.exists():
            try:
                previous = json.loads(checkpoint.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise LakeError("checkpoint_json", "checkpoint is not JSON") from exc
            if previous.get("manifest_digest") != digest(document):
                raise LakeError("checkpoint_changed", "manifest changed since checkpoint", 409)
            offset = previous.get("next_offset")
        else:
            offset = 0
        result = self.run(document, offset, limit, stop_on_error)
        self.save_checkpoint(checkpoint, result)
        return result
