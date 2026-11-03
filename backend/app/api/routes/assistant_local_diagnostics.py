"""Authenticated outbound HMI transport, independent of archive submission."""
import json
from uuid import UUID

from fastapi import APIRouter, Request, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.auth import RequestActorDep
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.services.assistant_chat import runtime, require_runtime, actor_session, own_job
from app.services import assistant_local_diagnostics as local

router = APIRouter(prefix="/api/v1/assistant/jobs", tags=["assistant-local-diagnostics"])


class LocalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    installation_id: UUID
    window_id: str = Field(min_length=1, max_length=128)
    result: dict
    expected_request_sha256: str | None = Field(default=None,pattern=r'^[a-f0-9]{64}$')


@router.get("/{identifier}/local-requests")
def pending(identifier: UUID, actor: RequestActorDep, installation_id: UUID,
            window_id: str = Query(min_length=1, max_length=128)):
    require_runtime(actor)
    with actor_session(actor) as session:
        job = own_job(session, identifier)
    if 'resume_state' in job:
        from app.services.assistant_permissions import list_permissions
        return DataEnvelope(data=list_permissions(actor,identifier,executor=True,installation_id=installation_id,window_id=window_id))
    item = local.bound_item(runtime, actor, job, installation_id, window_id)
    return DataEnvelope(data={"job_id": str(identifier), "items": local.poll(item) if item else []})


@router.post("/{identifier}/local-results")
async def completed(identifier: UUID, request: Request, actor: RequestActorDep):
    require_runtime(actor)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > local.MAX_RESULT_BYTES + 2048:
            raise ApiError(413, "local_diagnostic_result_size", "本机读取回执过大，请分块。")
    try:
        body = LocalResult.model_validate(json.loads(raw))
    except (ValueError, TypeError, RecursionError, ValidationError) as exc:
        raise ApiError(422, "invalid_local_diagnostic_result", "本机读取回执格式无效。") from exc
    with actor_session(actor) as session:
        job = own_job(session, identifier)
    if 'resume_state' in job:
        from app.services.assistant_permissions import complete
        outcome=complete(actor,identifier,body.request_id,body.installation_id,body.window_id,body.result,body.expected_request_sha256)
        if outcome['state']=='completed':
            try:outcome['resume']=await runtime.resume(identifier,actor,request,body.request_id)
            except ApiError as exc:outcome['resume']={'accepted':False,'code':exc.code,'message':exc.message}
        return DataEnvelope(data=outcome)
    item = local.bound_item(runtime, actor, job, body.installation_id, body.window_id, for_result=True)
    return DataEnvelope(data=local.complete(item, body.request_id, body.result))
