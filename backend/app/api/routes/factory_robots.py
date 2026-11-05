"""Explicit, human-verified factory CSV summaries; never waveform ingestion."""
from typing import Annotated, Literal
import hashlib
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from sqlalchemy.exc import DBAPIError

from app.api.routes.archives import operation
from app.core.auth import RequestActor, require_roles
from app.core.config import get_settings
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.factory_robots import FactoryRobotRepository
from app.schemas.common import DataEnvelope
from app.schemas.factory_robots import FactoryRobotHeartbeat, FactoryRobotReview, FactoryRobotUpload
from app.services.factory_robot_results import FactoryRobotResultStorage, MAX_CSV_BYTES, parse_result_csv


router = APIRouter(prefix="/api/v1/factory", tags=["factory-robot-result-summaries"])
Reader = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER", "VIEWER"))]
Operator = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]
Reviewer = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"))]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def storage():
    return FactoryRobotResultStorage(get_settings().archive_root)


@router.get("/robots")
def robots(_: Reader, session: DbSession, source_id: str | None = Query(default=None, max_length=100),
           robot_id: str | None = Query(default=None, max_length=128),
           serial_number: str | None = Query(default=None, max_length=200),
           limit: int = Query(default=200, ge=1, le=200)):
    try:
        return DataEnvelope(data=FactoryRobotRepository(session).robots(source_id, robot_id, serial_number, limit))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", "") == "57014":
            raise ApiError(503, "factory_robot_query_busy", "整机记录查询超过预算，请缩小范围或稍后刷新。") from exc
        raise


@router.post("/robots/presence")
def presence(payload: FactoryRobotHeartbeat, _: Operator, session: DbSession):
    return operation(lambda: FactoryRobotRepository(session).presence(payload.model_dump(mode="json")))


@router.get("/robot-result-archives")
def listing(_: Reader, session: DbSession, source_id: str | None = Query(default=None, max_length=100),
            robot_id: str | None = Query(default=None, max_length=128),
            serial_number: str | None = Query(default=None, max_length=200),
            status: Literal["PENDING", "APPROVED", "REJECTED"] | None = None,
            before: int | None = Query(default=None, ge=1), limit: int = Query(default=50, ge=1, le=100)):
    return DataEnvelope(data=FactoryRobotRepository(session).listing(source_id, robot_id, serial_number, status, before, limit))


@router.post("/robot-result-archives", status_code=201)
async def upload(request: Request, payload: Annotated[FactoryRobotUpload, Query()], _: Operator,
                 session: DbSession, idempotency_key: Key):
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() not in {"text/csv", "application/octet-stream"}:
        raise ApiError(415, "factory_result_content_type", "专用工厂汇总原件须使用text/csv。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CSV_BYTES:
            raise ApiError(413, "factory_result_too_large", "工厂汇总CSV超过4MiB，未接收。")
        body.extend(chunk)
    raw = bytes(body)
    if len(raw) != payload.size_bytes or hashlib.sha256(raw).hexdigest() != payload.sha256:
        raise ApiError(422, "factory_result_checksum_mismatch", "CSV大小或SHA256与核实提交的原件不一致。")

    def save():
        parsed = parse_result_csv(raw, payload.source_id, payload.source_kind)
        metadata = {**payload.model_dump(mode="json"), **parsed, "protocol": "reliability_v1", "public_id": str(uuid4())}
        archive = FactoryRobotRepository(session).create(metadata, idempotency_key)
        storage().put(archive["id"], raw, payload.sha256)
        # Keep upload receipts bounded. Detail rows are retrieved separately.
        return {key: archive[key] for key in ("id", "source_id", "robot_id", "serial_number", "source_kind", "sha256", "size_bytes", "status", "revision", "formal_statistics_eligible")}
    return await run_in_threadpool(operation, save)


@router.get("/robot-result-archives/{archive_id}")
def detail(archive_id: UUID, _: Reader, session: DbSession):
    return DataEnvelope(data=FactoryRobotRepository(session).get(archive_id))


@router.get("/robot-result-archives/{archive_id}/file")
def original(archive_id: UUID, _: Reader, session: DbSession):
    archive = FactoryRobotRepository(session).get(archive_id, history=False)
    data = storage().read(archive["id"], archive["size_bytes"], archive["sha256"])
    return Response(data, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="factory-result-{archive_id}.csv"',
        "Cache-Control": "no-store", "X-Content-SHA256": archive["sha256"],
        "X-RP1-CSV-Schema": "factory-robot-result-summary-v1"})


@router.post("/robot-result-archives/{archive_id}/review")
def review(archive_id: UUID, payload: FactoryRobotReview, _: Reviewer, session: DbSession, idempotency_key: Key):
    def save():
        repository = FactoryRobotRepository(session)
        archive = repository.get(archive_id, history=False)
        if payload.decision == "APPROVE":
            storage().read(archive["id"], archive["size_bytes"], archive["sha256"])
        return repository.review(archive_id, payload.model_dump(mode="json"), idempotency_key, archive["sha256"])
    return operation(save)
