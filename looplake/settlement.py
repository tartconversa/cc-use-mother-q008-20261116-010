from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .model import LakeError, amount, calendar_day, key
from .store import Store


class Settlement:
    def __init__(self, store: Store):
        self.store = store

    def put_rate(self, material_id: str, channel: str, valid_from: str,
                 rate_yuan_per_kg, valid_to: str | None = None,
                 expected_version: int | None = None):
        material_id = key(material_id, "material_id")
        if channel != "smart_box":
            raise LakeError("invalid_rate_channel", "only smart_box deposits are priced")
        valid_from = calendar_day(valid_from, "valid_from")
        if valid_to is not None:
            valid_to = calendar_day(valid_to, "valid_to")
            if valid_to <= valid_from:
                raise LakeError("invalid_rate_period", "rate end must follow start")
        rate = amount(rate_yuan_per_kg, "rate_yuan_per_kg", True)
        with self.store.write() as conn:
            material = self.store.one(conn, "SELECT id FROM dimension_versions WHERE kind='material' AND external_id=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
                                      (material_id, valid_from, valid_from))
            if material is None:
                raise LakeError("unknown_material", "rate material must be registered", 409)
            old = self.store.one(conn, "SELECT MAX(version) AS version FROM rate_cards WHERE material_id=? AND channel=?",
                                 (material_id, channel))
            actual = old["version"] or 0
            if expected_version is not None and expected_version != actual:
                raise LakeError("stale_rate", "rate version changed", 409)
            overlap = self.store.one(conn, "SELECT id FROM rate_cards WHERE material_id=? AND channel=? AND (valid_to IS NULL OR valid_to>?) AND (? IS NULL OR valid_from<?)",
                                     (material_id, channel, valid_from, valid_to, valid_to))
            if overlap:
                raise LakeError("rate_overlap", "effective rate periods overlap", 409)
            version = actual + 1
            cursor = conn.execute("INSERT INTO rate_cards(material_id,channel,valid_from,valid_to,rate_yuan_per_kg,version,created_at) VALUES(?,?,?,?,?,?,datetime('now'))",
                                  (material_id, channel, valid_from, valid_to, rate, version))
            self.store.audit(conn, "rate_added", f"{material_id}:{channel}",
                             {"rate_id": cursor.lastrowid, "rate": rate, "version": version})
        return {"rate_id": cursor.lastrowid, "material_id": material_id,
                "channel": channel, "rate": rate, "version": version}

    def rate_at(self, material_id: str, channel: str, day: str):
        with self.store.connect() as conn:
            rows = self.store.all(conn, "SELECT * FROM rate_cards WHERE material_id=? AND channel=? AND valid_from<=? AND (valid_to IS NULL OR valid_to>?)",
                                  (material_id, channel, day, day))
        if len(rows) > 1:
            raise LakeError("ambiguous_rate", "multiple rates effective for date", 409)
        return rows[0] if rows else None

    def batch(self, facts: list[dict], tolerance_yuan: str = "0.01"):
        try:
            tolerance = Decimal(str(tolerance_yuan))
        except Exception as exc:
            raise LakeError("invalid_money_tolerance", "money tolerance must be numeric") from exc
        if not tolerance.is_finite() or tolerance < 0 or tolerance > 100:
            raise LakeError("invalid_money_tolerance", "money tolerance out of range")
        deposits = [fact for fact in facts if fact["event_type"] == "deposit"]
        payouts = [fact for fact in facts if fact["event_type"] == "payout"]
        expected = Decimal("0")
        actual = sum((Decimal(fact["payout_yuan"] or "0") for fact in payouts), Decimal("0"))
        pricing = []
        missing_rates = []
        for fact in deposits:
            rate = self.rate_at(fact["material_id"], fact["channel"], fact["day"])
            if rate is None:
                missing_rates.append(fact["id"])
                continue
            value = Decimal(fact["weight_kg"]) * Decimal(rate["rate_yuan_per_kg"])
            expected += value
            pricing.append({"fact_id": fact["id"], "rate_id": rate["id"],
                            "rate_version": rate["version"], "expected_yuan": format(value, "f")})
        issues = []
        if payouts and missing_rates:
            issues.append({"code": "unpriced_deposit", "fact_ids": missing_rates})
        if payouts and not deposits:
            issues.append({"code": "orphan_payout", "fact_ids": [row["id"] for row in payouts]})
        if payouts and not missing_rates and abs(expected - actual) > tolerance:
            issues.append({"code": "payout_gap", "fact_ids": [row["id"] for row in payouts + deposits],
                           "gap_yuan": format(actual - expected, "f")})
        return {"expected_yuan": format(expected, "f"), "actual_yuan": format(actual, "f"),
                "pricing": pricing, "issues": issues,
                "tolerance_yuan": format(tolerance, "f")}

    def rates(self, material_id: str):
        material_id = key(material_id, "material_id")
        with self.store.connect() as conn:
            return self.store.all(conn, "SELECT * FROM rate_cards WHERE material_id=? ORDER BY version", (material_id,))
