from typing import Annotated, Literal
from uuid import UUID
from pydantic import AwareDatetime

import anyio
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.exc import DBAPIError
from starlette.concurrency import run_in_threadpool

from app.core.auth import RequestActor, require_roles
from app.core.config import get_settings
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.archives import ArchiveRepository
from app.schemas.archives import RunManifest, ArchiveAnalysisCreate, ArchiveDomainClassification, ArchiveFactoryReview, ArchiveClassification, ArchiveCreate, ArchiveIntakeSubmit, ArchiveSubmit, ArchiveVerification
from app.schemas.common import DataEnvelope
from app.services.archive_storage import ArchiveStorage
from app.services.curve_analysis import CurveAnalysisService, MAX_TIME_SECONDS
from app.services.curve_query import CurveQueryService


factory_router = APIRouter(prefix="/api/v1/factory", tags=["factory-test-statistics"])
router = APIRouter(prefix="/api/v1/archives", tags=["central-test-archives"])
Operator = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]
Reader = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR", "VIEWER", "FACTORY_REVIEWER"))]
FactoryReviewer = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"))]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]
Reference = Annotated[str, Query(min_length=1,max_length=300)]


def operation(call):
    try:
        return DataEnvelope(data=call())
    except DBAPIError as exc:
        code = getattr(exc.orig,"sqlstate","") or ""
        if code=="42501":
            raise ApiError(403,"archive_forbidden","当前账号没有该归档的操作权限。") from exc
        if code=="P0002":
            raise ApiError(404,"archive_not_found","归档不存在或不可见。") from exc
        if code in {"P0001","23505","23P01","40P01","40001"}:
            raise ApiError(409,"archive_conflict","归档版本、上传状态或幂等键冲突，请刷新核对；本次变更已回滚。") from exc
        if code.startswith("22") or code in {"23514","23503","23502"}:
            raise ApiError(422,"invalid_archive","采集事实、核实信息或正式执行上下文不符合要求。") from exc
        raise


@router.get("")
def listing(_: Reader, session: DbSession, execution_uuid: UUID | None=None, execution_id: UUID | None=None,
            q: Annotated[str | None,Query(max_length=200)]=None,
            origin: Literal["HMI","HISTORY","OTHER"] | None=None,
            classification_state: Literal["UNCLASSIFIED","CLASSIFIED"] | None=None,
            processing_state: Literal["WAITING","QUEUED","PROCESSING","READY","FAILED"] | None=None,
            data_domain: Literal["RESEARCH","FACTORY","UNCLASSIFIED"] | None=None,
            sn: Annotated[str | None,Query(min_length=1,max_length=200)]=None,
            bench: Annotated[str | None,Query(min_length=1,max_length=100)]=None,
            started_from: AwareDatetime | None=None, started_to: AwareDatetime | None=None,
            submission_status: Literal["PENDING","APPROVED","REJECTED"] | None=None,
            before: Annotated[int | None,Query(ge=1)]=None,limit: Annotated[int,Query(ge=1,le=100)]=50):
    check_window(started_from, started_to)
    return DataEnvelope(data=ArchiveRepository(session).list(execution_uuid,execution_id,q,origin,classification_state,processing_state,before,limit,
        data_domain,sn,bench,started_from,started_to,submission_status))


@router.get("/{reference}")
def detail(reference: str, _: Reader, session: DbSession):
    return DataEnvelope(data=ArchiveRepository(session).get(reference,history=True))


@router.post("",status_code=201)
def create(payload: ArchiveCreate, _: Operator, session: DbSession, idempotency_key: Key):
    if payload.size_bytes>get_settings().archive_max_file_bytes:
        raise ApiError(413,"archive_too_large","文件超过中央归档允许的大小。")
    return operation(lambda: ArchiveRepository(session).create(payload,idempotency_key))


@router.put("/{reference}/content")
async def content(reference: str,request: Request, _: Operator,session: DbSession,
                  offset: Annotated[int,Query(ge=0)],idempotency_key: Key):
    settings = get_settings()
    if request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/octet-stream":
        raise ApiError(415,"invalid_content_type","原始上传块必须使用 application/octet-stream。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>settings.archive_max_chunk_bytes:
            raise ApiError(413,"archive_chunk_too_large","上传块超过允许大小，请按块继续上传。")
        body.extend(chunk)
    def append():
        repository = ArchiveRepository(session)
        initial = repository.get(reference)
        storage = ArchiveStorage(settings.archive_root)
        with storage.lock(initial["execution_uuid"]):
            archive = repository.lock_upload(initial["id"])
            if session.execute(__import__("sqlalchemy").text("SELECT EXISTS(SELECT 1 FROM integration.archive_run_manifest m JOIN integration.execution_archive a ON a.id=m.archive_id WHERE a.public_id=:id)"), {"id": archive["id"]}).scalar_one():
                raise ApiError(409,"segmented_upload_required","该执行须通过分段接口续传，不能替换为单文件上传")
            received = storage.append(archive["execution_uuid"],archive["received_bytes"],archive["size_bytes"],offset,bytes(body),archive["upload_state"]=="COMPLETE")
            return repository.update_upload(archive["id"],received,"COMPLETE" if archive["upload_state"]=="COMPLETE" else "UPLOADING")
    return await run_in_threadpool(operation,append)


@router.post("/{reference}/complete",status_code=202)
def complete(reference: str, _: Operator,session: DbSession,idempotency_key: Key):
    def queue():
        repository = ArchiveRepository(session)
        archive = repository.lock_upload(reference)
        if archive["received_bytes"]!=archive["size_bytes"]:
            raise ApiError(409,"incomplete_upload","原始文件尚未完整接收，继续上传后再完成归档。")
        # SHA256 validation and parsing happen in the recoverable archive worker.
        return repository.update_upload(archive["id"],archive["size_bytes"],"COMPLETE")
    return operation(queue)


@router.post("/{reference}/verification")
def verify(reference: str,payload: ArchiveVerification, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).edit(reference,"verification",payload,idempotency_key))


@router.post("/{reference}/classification")
def classify(reference: str,payload: ArchiveClassification, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).edit(reference,"classification",payload,idempotency_key))


@router.post("/{reference}/submit")
def submit(reference: str,payload: ArchiveSubmit, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).submit(reference,payload,idempotency_key))


@router.post("/{reference}/verify-and-submit")
def intake_submit(reference: str, payload: ArchiveIntakeSubmit, _: Operator, session: DbSession, idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).intake_submit(reference, payload, idempotency_key))


@router.get("/{reference}/analyses")
def analyses(reference: str, _: Reader,session: DbSession):
    return DataEnvelope(data=ArchiveRepository(session).analyses(reference))


@router.post("/{reference}/analyses",status_code=201)
def analyze(reference: str,payload: ArchiveAnalysisCreate, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).add_analysis(reference,payload,idempotency_key))


@router.get("/{reference}/curves")
def curves(reference: str, _: Reader,session: DbSession,
           metrics: Annotated[str,Query(max_length=4000)]="",subjects: Annotated[str,Query(max_length=4000)]="",
           start: float | None=None,end: float | None=None,
           max_points: Annotated[int,Query(ge=50,le=20000)]=4000,
           stage: Annotated[str | None,Query(max_length=100)]=None,
           cycle: Annotated[str | None,Query(max_length=100)]=None):
    archive = ArchiveRepository(session).get(reference)
    settings = get_settings()
    query = CurveQueryService(ArchiveStorage(settings.archive_root),settings.archive_query_memory_mb)
    return DataEnvelope(data=query.query(archive,[m.strip() for m in metrics.split(",") if m.strip()],
        [s.strip() for s in subjects.split(",") if s.strip()],start,end,max_points,stage,cycle))


def raw_request(reference: str, _: Reader, session: DbSession,
                metrics: Annotated[str, Query(max_length=4000)]="",
                subjects: Annotated[str, Query(max_length=4000)]="",
                start: Annotated[float | None, Query(ge=0, le=MAX_TIME_SECONDS, allow_inf_nan=False)]=None,
                end: Annotated[float | None, Query(ge=0, le=MAX_TIME_SECONDS, allow_inf_nan=False)]=None,
                stage: Annotated[str | None, Query(max_length=100)]=None,
                cycle: Annotated[str | None, Query(max_length=100)]=None):
    # Use the same per-archive RLS gate as /curves on every request, including
    # paginated reads and downloads; a shared URL confers no new permission.
    archive = ArchiveRepository(session).get(reference)
    settings = get_settings()
    service = CurveAnalysisService(ArchiveStorage(settings.archive_root), settings.archive_query_memory_mb)
    selection = service.select(archive, [m.strip() for m in metrics.split(",") if m.strip()],
                               [s.strip() for s in subjects.split(",") if s.strip()], start, end, stage, cycle)
    return service, selection


RawRequest = Annotated[tuple, Depends(raw_request)]


@router.get("/{reference}/statistics")
def raw_statistics(query: RawRequest):
    service, selection = query
    return DataEnvelope(data=service.statistics(selection))


@router.get("/{reference}/samples")
def raw_samples(query: RawRequest,
                offset: Annotated[int, Query(ge=0, le=9223372036854775807)]=0,
                limit: Annotated[int, Query(ge=1, le=500)]=100):
    service, selection = query
    return DataEnvelope(data=service.samples(selection, offset, limit))


class ArchiveCsvResponse(StreamingResponse):
    def __init__(self, export):
        self.export = export
        super().__init__(export.chunks(), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{export.filename}"',
            "Cache-Control": "no-store", "X-Archive-Resolution": "raw",
            "X-Archive-Row-Count": str(export.count),
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # A generator's finally is insufficient if streaming never begins or
            # the client disconnects while sending a chunk. Always release the
            # DuckDB connection and its bounded spill directory, even on cancel.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.export.close)


@router.get("/{reference}/export.csv")
def raw_export_csv(query: RawRequest):
    service, selection = query
    return ArchiveCsvResponse(service.export_csv(selection))


def check_window(started_from, started_to):
    if started_from is not None and started_to is not None and started_from >= started_to:
        raise ApiError(422,"invalid_time_window","开始时间必须早于结束时间。")


@factory_router.get("/statistics")
def factory_statistics(_: Reader, session: DbSession,
        sn: Annotated[str | None,Query(min_length=1,max_length=200)]=None,
        bench: Annotated[str | None,Query(min_length=1,max_length=100)]=None,
        started_from: AwareDatetime | None=None, started_to: AwareDatetime | None=None):
    check_window(started_from, started_to)
    return DataEnvelope(data=ArchiveRepository(session).factory_statistics(sn,bench,started_from,started_to))


@router.post("/{reference}/factory-review")
def factory_review(reference: str, payload: ArchiveFactoryReview, _: FactoryReviewer, session: DbSession, idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).factory_review(reference,payload,idempotency_key))


@router.post("/{reference}/domain-classification")
def classify_domain(reference: str, payload: ArchiveDomainClassification, _: Operator, session: DbSession, idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).classify_domain(reference,payload,idempotency_key))

@router.post('/{reference}/segments')
def register_segments(reference: str, payload: RunManifest, _: Operator, session: DbSession, idempotency_key: Key):
    from app.services.run_segments import RunSegments
    def register():
        archive=ArchiveRepository(session).lock_upload(reference)
        return RunSegments(session,ArchiveStorage(get_settings().archive_root)).register(archive,payload.model_dump(mode='json'))
    return operation(register)

@router.get('/{reference}/segments')
def segment_status(reference: str, _: Reader, session: DbSession):
    from app.services.run_segments import RunSegments
    archive=ArchiveRepository(session).get(reference)
    return DataEnvelope(data=RunSegments(session,ArchiveStorage(get_settings().archive_root)).status(archive))

@router.put('/{reference}/segments/{sequence}')
async def segment_content(reference: str, sequence: int, request: Request, _: Operator, session: DbSession, idempotency_key: Key, offset: Annotated[int,Query(ge=0)]):
    from app.services.run_segments import RunSegments
    settings=get_settings();body=bytearray()
    if request.headers.get('content-type','').split(';')[0]!='application/octet-stream':raise ApiError(415,'invalid_content_type','分段须使用二进制上传')
    async for chunk in request.stream():
        if len(body)+len(chunk)>settings.archive_max_chunk_bytes:raise ApiError(413,'archive_chunk_too_large','上传块过大')
        body.extend(chunk)
    def append():
        repository=ArchiveRepository(session);initial=repository.get(reference);storage=ArchiveStorage(settings.archive_root)
        with storage.lock(initial['execution_uuid']):
            archive=repository.lock_upload(reference)
            return RunSegments(session,storage).append(archive,sequence,offset,bytes(body))
    return await run_in_threadpool(operation,append)

@router.post('/{reference}/segments-complete',status_code=202)
def complete_segments(reference: str, _: Operator, session: DbSession, idempotency_key: Key):
    from app.services.run_segments import RunSegments
    def complete():
        repository=ArchiveRepository(session);initial=repository.get(reference);storage=ArchiveStorage(get_settings().archive_root)
        with storage.lock(initial['execution_uuid']):
            archive=repository.lock_upload(reference);RunSegments(session,storage).materialize(archive)
            return repository.update_upload(archive['id'],archive['size_bytes'],'COMPLETE')
    return operation(complete)

from pydantic import BaseModel,Field
from sqlalchemy import text
class FactoryHandoff(BaseModel):
    kind: Literal['REWORK','HANDOFF','ACKNOWLEDGED']
    note: str=Field(min_length=1,max_length=1000)
    destination: str=Field(default='',max_length=100)
    expected_revision:int=Field(ge=0)
@router.get('/{reference}/handoffs')
def handoffs(reference:str,_:Reader,session:DbSession):
    archive=ArchiveRepository(session).get(reference)
    rows=session.execute(text("SELECT h.revision,h.kind,h.note,h.destination,h.created_at,integration.archive_actor_name(h.archive_id,h.actor_id) AS actor FROM integration.factory_handoff h JOIN integration.execution_archive a ON a.id=h.archive_id WHERE a.public_id=:id ORDER BY h.revision DESC LIMIT 100"),{'id':archive['id']}).mappings()
    return DataEnvelope(data={'items':[dict(row) for row in rows]})
@router.post('/{reference}/handoffs')
def add_handoff(reference:str,payload:FactoryHandoff,_:Operator,session:DbSession,idempotency_key:Key):
    def save():
        archive=ArchiveRepository(session).get(reference)
        revision=session.execute(text('SELECT integration.factory_handoff(:id,:kind,:note,:destination,:expected_revision,:key)'),{'id':archive['id'],**payload.model_dump(),'key':idempotency_key}).scalar_one()
        return {'revision':revision,'recorded':True,'formal_verdict_unchanged':True}
    return operation(save)
