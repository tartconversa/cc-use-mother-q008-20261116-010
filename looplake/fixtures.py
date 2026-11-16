from __future__ import annotations

from .service import LakeService


def seed_dimensions(service: LakeService, start: str = "2026-01-01"):
    for source, channel in (("box-feed", "smart_box"), ("scale-feed", "scale"),
                            ("station-feed", "station"), ("pay-feed", "payment")):
        service.register_source(source, channel)
    for row in (
        {"kind": "street", "external_id": "street-1", "name": "合成一街道", "valid_from": start},
        {"kind": "street", "external_id": "street-2", "name": "合成二街道", "valid_from": start},
        {"kind": "community", "external_id": "community-1", "name": "合成一社区",
         "valid_from": start, "attributes": {"street_id": "street-1"}},
        {"kind": "community", "external_id": "community-2", "name": "合成二社区",
         "valid_from": start, "attributes": {"street_id": "street-2"}},
        {"kind": "device", "external_id": "box-1", "name": "合成箱一",
         "valid_from": start, "attributes": {"community_id": "community-1"}},
        {"kind": "device", "external_id": "box-2", "name": "合成箱二",
         "valid_from": start, "attributes": {"community_id": "community-2"}},
        {"kind": "station", "external_id": "station-1", "name": "合成站一", "valid_from": start},
        {"kind": "material", "external_id": "foam", "name": "泡沫", "valid_from": start},
        {"kind": "material", "external_id": "glass", "name": "玻璃", "valid_from": start},
        {"kind": "actor", "external_id": "resident-1", "name": "合成用户", "valid_from": start},
    ):
        service.register_dimension(row)
    service.settlement.put_rate("foam", "smart_box", start, "0.08")
    service.settlement.put_rate("glass", "smart_box", start, "0.04")


def event(event_type: str, external_id: str, batch_id: str = "batch-1",
          material_id: str = "foam", weight: str = "10",
          happened_at: str = "2026-07-15T10:00:00+08:00", **changes):
    details = {"deposit": ("box-feed", "smart_box", "device_id", "box-1"),
               "weigh_in": ("scale-feed", "scale", "station_id", "station-1"),
               "weigh_out": ("scale-feed", "scale", "station_id", "station-1"),
               "transfer": ("station-feed", "station", "station_id", "station-1"),
               "payout": ("pay-feed", "payment", "actor_id", "resident-1")}
    source, channel, location_key, location_value = details[event_type]
    row = {"source": source, "external_id": external_id, "event_type": event_type,
           "happened_at": happened_at, "batch_id": batch_id, "channel": channel,
           location_key: location_value}
    if event_type == "payout":
        row["payout_yuan"] = changes.pop("payout_yuan", "0.8")
    else:
        row["material_id"] = material_id
        row["weight_kg"] = weight
    row.update(changes)
    return row


def balanced_batch(batch_id: str = "batch-1", prefix: str = "a", material_id: str = "foam",
                   weight: str = "10", payout: str | None = "0.8", **changes):
    result = [event("deposit", prefix + "-deposit", batch_id, material_id, weight, **changes),
              event("weigh_in", prefix + "-scale", batch_id, material_id, weight, **changes)]
    if payout is not None:
        result.append(event("payout", prefix + "-payout", batch_id, material_id, weight,
                            payout_yuan=payout, **changes))
    return result
