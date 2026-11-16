from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


class LakeError(Exception):
    def __init__(self, code: str, detail: str, status: int = 400):
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


def required(value, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LakeError("invalid_" + name, f"{name} requires 1..{maximum} characters")
    return value.strip()


def key(value, name: str) -> str:
    value = required(value, name, 80)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", value):
        raise LakeError("invalid_" + name, f"{name} must be a stable token")
    return value


def calendar_day(value, name: str) -> str:
    if not isinstance(value, str):
        raise LakeError("invalid_" + name, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise LakeError("invalid_" + name, f"{name} must be YYYY-MM-DD") from exc


def instant(value, name: str) -> str:
    if not isinstance(value, str):
        raise LakeError("invalid_" + name, f"{name} must include a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LakeError("invalid_" + name, f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise LakeError("invalid_" + name, f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def amount(value, name: str, allow_zero: bool = False) -> str:
    if isinstance(value, bool) or value is None:
        raise LakeError("invalid_" + name, f"{name} must be numeric")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise LakeError("invalid_" + name, f"{name} must be numeric") from exc
    if not number.is_finite() or number < 0 or (not allow_zero and number == 0):
        raise LakeError("invalid_" + name, f"{name} must be positive")
    if number > Decimal("1000000000000") or abs(number.as_tuple().exponent) > 6:
        raise LakeError("invalid_" + name, f"{name} exceeds supported precision")
    return format(number.normalize(), "f")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Dimension:
    kind: str
    external_id: str
    name: str
    valid_from: str
    valid_to: str | None
    attributes: dict

    @classmethod
    def parse(cls, data: dict) -> "Dimension":
        if not isinstance(data, dict):
            raise LakeError("invalid_dimension", "dimension must be an object")
        kind = data.get("kind")
        if kind not in {"street", "community", "device", "station", "material", "actor"}:
            raise LakeError("invalid_kind", "unsupported dimension kind")
        external_id = key(data.get("external_id"), "external_id")
        name = required(data.get("name"), "name", 200)
        start = calendar_day(data.get("valid_from"), "valid_from")
        end = data.get("valid_to")
        if end is not None:
            end = calendar_day(end, "valid_to")
            if end <= start:
                raise LakeError("invalid_range", "valid_to must follow valid_from")
        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict) or len(json.dumps(attrs, ensure_ascii=False)) > 3000:
            raise LakeError("invalid_attributes", "attributes must be a small object")
        return cls(kind, external_id, name, start, end, attrs)


EVENT_TYPES = {"deposit", "weigh_in", "weigh_out", "transfer", "payout"}
WEIGHT_EVENTS = {"deposit", "weigh_in", "weigh_out", "transfer"}
CHANNELS = {"smart_box", "scale", "station", "payment"}


@dataclass(frozen=True)
class RawEvent:
    source: str
    external_id: str
    event_type: str
    happened_at: str
    batch_id: str
    material_id: str | None
    device_id: str | None
    station_id: str | None
    actor_id: str | None
    weight_kg: str | None
    payout_yuan: str | None
    channel: str
    metadata: dict

    @classmethod
    def parse(cls, data: dict) -> "RawEvent":
        if not isinstance(data, dict):
            raise LakeError("invalid_event", "event must be an object")
        source, external_id = key(data.get("source"), "source"), key(data.get("external_id"), "external_id")
        event_type = data.get("event_type")
        if event_type not in EVENT_TYPES:
            raise LakeError("invalid_event_type", "unsupported event type")
        happened_at = instant(data.get("happened_at"), "happened_at")
        batch_id = key(data.get("batch_id"), "batch_id")
        channel = data.get("channel")
        if channel not in CHANNELS:
            raise LakeError("invalid_channel", "unsupported channel")
        ids = {}
        for field in ("material_id", "device_id", "station_id", "actor_id"):
            ids[field] = key(data[field], field) if data.get(field) is not None else None
        if event_type in WEIGHT_EVENTS and ids["material_id"] is None:
            raise LakeError("missing_material", "weight events require material_id")
        if channel == "smart_box" and ids["device_id"] is None:
            raise LakeError("missing_device", "smart_box events require device_id")
        if channel == "station" and ids["station_id"] is None:
            raise LakeError("missing_station", "station events require station_id")
        weight = amount(data.get("weight_kg"), "weight_kg") if data.get("weight_kg") is not None else None
        payout = amount(data.get("payout_yuan"), "payout_yuan", True) if data.get("payout_yuan") is not None else None
        if event_type in WEIGHT_EVENTS and weight is None:
            raise LakeError("missing_weight", "weight events require weight_kg")
        if event_type == "payout" and (payout is None or ids["actor_id"] is None):
            raise LakeError("missing_payout", "payout events require actor_id and payout_yuan")
        if event_type == "payout" and channel != "payment":
            raise LakeError("invalid_channel", "payout channel must be payment")
        if event_type == "deposit" and channel != "smart_box":
            raise LakeError("invalid_channel", "deposit channel must be smart_box")
        if event_type in {"weigh_in", "weigh_out"} and channel != "scale":
            raise LakeError("invalid_channel", "weighing channel must be scale")
        metadata = data.get("metadata", {})
        if not isinstance(metadata, dict) or len(json.dumps(metadata, ensure_ascii=False)) > 3000:
            raise LakeError("invalid_metadata", "metadata must be a small object")
        return cls(source, external_id, event_type, happened_at, batch_id,
                   ids["material_id"], ids["device_id"], ids["station_id"],
                   ids["actor_id"], weight, payout, channel, metadata)

    def as_dict(self) -> dict:
        return self.__dict__.copy()

    @property
    def content_hash(self) -> str:
        return digest(self.as_dict())
