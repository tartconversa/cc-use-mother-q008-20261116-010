from __future__ import annotations

from pathlib import Path
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .model import LakeError
from .service import LakeService


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInput(Strict):
    source: str
    channel: str
    expected_version: int | None = None


class DimensionInput(Strict):
    kind: str
    external_id: str
    name: str
    valid_from: str
    valid_to: str | None = None
    attributes: dict = {}
    expected_version: int | None = None


class EventInput(Strict):
    source: str
    external_id: str
    event_type: str
    happened_at: str
    batch_id: str
    channel: str
    material_id: str | None = None
    device_id: str | None = None
    station_id: str | None = None
    actor_id: str | None = None
    weight_kg: str | None = None
    payout_yuan: str | None = None
    metadata: dict = {}


class ReleaseInput(Strict):
    name: str
    month: str
    expected_fingerprint: str


class RateInput(Strict):
    material_id: str
    channel: str
    valid_from: str
    valid_to: str | None = None
    rate_yuan_per_kg: str
    expected_version: int | None = None


class SourceDisableInput(Strict):
    expected_version: int


def create_app(db_path: str | Path | None = None):
    path = db_path or os.environ.get("LOOPLAKE_DB", "looplake.sqlite3")
    service = LakeService(path)
    app = FastAPI(title="LoopLake 回收物料数据台", version="1.0.0")
    app.state.service = service

    @app.exception_handler(LakeError)
    async def lake_error(request: Request, exc: LakeError):
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "detail": exc.detail})

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/sources")
    def register_source(body: SourceInput):
        return service.register_source(body.source, body.channel, body.expected_version)

    @app.post("/sources/{source_id}/disable")
    def disable_source(source_id: str, body: SourceDisableInput):
        return service.dimensions.disable_source(source_id, body.expected_version)

    @app.get("/sources/{source_id}/quality")
    def source_quality(source_id: str):
        return service.quality.source(source_id)

    @app.post("/dimensions")
    def register_dimension(body: DimensionInput):
        data = body.model_dump(exclude={"expected_version"})
        return service.register_dimension(data, body.expected_version)

    @app.get("/dimensions/{kind}/{external_id}")
    def dimension_versions(kind: str, external_id: str):
        return service.dimensions.list_versions(kind, external_id)

    @app.post("/events")
    def ingest(body: EventInput):
        return service.ingest(body.model_dump(exclude_none=True))

    @app.post("/rates")
    def put_rate(body: RateInput):
        return service.settlement.put_rate(body.material_id, body.channel, body.valid_from,
                                           body.rate_yuan_per_kg, body.valid_to, body.expected_version)

    @app.get("/rates/{material_id}")
    def rates(material_id: str):
        return service.settlement.rates(material_id)

    @app.post("/ingestions/{ingestion_id}/retry")
    def retry(ingestion_id: int):
        return service.retry(ingestion_id)

    @app.get("/ingestions/{ingestion_id}")
    def ingestion(ingestion_id: int):
        return service.store.ingestion(ingestion_id)

    @app.get("/ingestions")
    def ingestions(state: str = "quarantined", limit: int = 100):
        return service.landing.pending(state, limit)

    @app.get("/facts/{fact_id}")
    def fact(fact_id: int):
        return service.store.fact(fact_id)

    @app.get("/lineage/facts/{fact_id}")
    def fact_lineage(fact_id: int):
        return service.lineage.fact(fact_id)

    @app.get("/lineage/rows/{month}/{street_id}/{material_id}")
    def row_lineage(month: str, street_id: str, material_id: str):
        return service.lineage.row(month, street_id, material_id)

    @app.get("/batches/{batch_id}")
    def batch(batch_id: str):
        return service.batch_status(batch_id)

    @app.get("/batches/{batch_id}/quality")
    def batch_quality(batch_id: str):
        return service.quality.batch(batch_id)

    @app.get("/batches/{batch_id}/inventory")
    def batch_inventory(batch_id: str):
        return service.inventory.batch_station_trace(batch_id)

    @app.get("/months/{month}/quality")
    def month_quality(month: str):
        return service.month_status(month)

    @app.get("/months/{month}/mart")
    def month_mart(month: str):
        return service.month_report(month)

    @app.get("/months/{month}/inventory")
    def inventory(month: str):
        return service.inventory.month(month)

    @app.get("/communities/{community_id}/months/{month}")
    def community_month(community_id: str, month: str):
        return service.query.community_month(community_id, month)

    @app.get("/devices/{device_id}/history")
    def device_history(device_id: str, start: str, end: str):
        return service.query.device_history(device_id, start, end)

    @app.get("/sources/{source_id}/months/{month}")
    def source_month(source_id: str, month: str):
        return service.query.source_month(source_id, month)

    @app.get("/months/{month}/preview")
    def preview(month: str):
        return service.preview(month)

    @app.post("/releases")
    def release(body: ReleaseInput):
        return service.publish(body.name, body.month, body.expected_fingerprint)

    @app.get("/releases/{release_id}")
    def get_release(release_id: int):
        return service.releases.get(release_id)

    @app.get("/releases/{left_id}/compare/{right_id}")
    def compare_releases(left_id: int, right_id: int):
        return service.releases.compare(left_id, right_id)

    @app.get("/months/{month}/releases")
    def month_releases(month: str):
        return service.snapshots.releases_for_month(month)

    @app.get("/facts/{fact_id}/releases")
    def fact_releases(fact_id: int):
        return service.snapshots.fact_usage(fact_id)

    @app.get("/releases/{release_id}/lineage/{street_id}/{material_id}")
    def release_lineage(release_id: int, street_id: str, material_id: str):
        return service.lineage.release_row(release_id, street_id, material_id)

    @app.get("/audits")
    def audits(since: int = 0, limit: int = 100):
        return service.store.audits(since, limit)

    return app


app = create_app()
