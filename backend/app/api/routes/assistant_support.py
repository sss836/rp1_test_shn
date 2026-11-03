"""Authenticated diagnostic support; inherits the normal Cookie/CSRF boundary."""
from typing import Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError

from app.core.auth import RequestActorDep
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.services import assistant_support as service

router = APIRouter(prefix='/api/v1/assistant', tags=['assistant-support'])


def mutate(operation):
    try:
        return DataEnvelope(data=operation())
    except DBAPIError as exc:
        raise ApiError(409, 'assistant_support_conflict', '请求标识、权限或记录状态发生冲突，请核对原始回执。') from exc


class StrictPayload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Feedback(StrictPayload):
    request_key: UUID
    job_id: UUID
    rating: Literal['helpful', 'issue']
    note: str = Field(default='', max_length=4000)
    share_for_faq: bool = False


class FaqPublish(StrictPayload):
    request_key: UUID
    feedback_id: UUID
    faq_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=12000)


class RepairProposal(StrictPayload):
    request_key: UUID = Field(default_factory=uuid4)
    path: str = Field(min_length=1, max_length=4096)
    expected_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    original: str = Field(max_length=65536)
    replacement: str = Field(max_length=65536)
    reason: str = Field(min_length=1, max_length=2000)
    kind: Literal['config', 'code']


class RepairConfirm(StrictPayload):
    proposal_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class RepairReceipt(StrictPayload):
    installation_id: UUID
    window_id: str = Field(min_length=1, max_length=128)
    result: dict
    settlement_token: str | None = Field(default=None, pattern=r'^v1\.[a-f0-9]{64}$')


@router.post('/feedback', status_code=201)
def feedback_create(payload: Feedback, actor: RequestActorDep):
    return mutate(lambda: service.create_feedback(actor, payload))


@router.get('/feedback')
def feedback_list(actor: RequestActorDep, shared: bool = False):
    return DataEnvelope(data=service.list_feedback(actor, shared=shared))


@router.get('/faq')
def faq_list(actor: RequestActorDep, q: str = Query(default='', max_length=500)):
    return DataEnvelope(data=service.query_faq(actor, q))


@router.post('/faq', status_code=201)
def faq_publish(payload: FaqPublish, actor: RequestActorDep):
    return mutate(lambda: service.publish_faq(actor, payload))


@router.get('/jobs/{identifier}/report')
def report(identifier: UUID, actor: RequestActorDep, format: Literal['markdown', 'csv', 'json'] = 'markdown'):
    raw, mime, sha256 = service.download_report(actor, identifier, format)
    extension = {'markdown': 'md', 'csv': 'csv', 'json': 'json'}[format]
    return Response(raw, media_type=mime, headers={
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Content-SHA256': sha256,
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(f'rp1-diagnostic-{identifier}.{extension}', safe='')})


@router.post('/jobs/{identifier}/repairs', status_code=201)
def repair_create(identifier: UUID, payload: RepairProposal, actor: RequestActorDep):
    return mutate(lambda: service.propose_local_repair(actor, identifier, payload.model_dump(mode='json')))


@router.get('/jobs/{identifier}/repairs')
def repair_list(identifier: UUID, actor: RequestActorDep):
    return DataEnvelope(data=service.list_repairs(actor, identifier))


@router.post('/repairs/{identifier}/confirm')
def repair_confirm(identifier: UUID, payload: RepairConfirm, actor: RequestActorDep):
    return mutate(lambda: service.confirm_repair(actor, identifier, payload.proposal_sha256))


@router.get('/repairs/pending')
def repair_pending(actor: RequestActorDep, installation_id: UUID, window_id: str = Query(min_length=1, max_length=128)):
    return DataEnvelope(data=service.pending_repairs(actor, installation_id, window_id))


@router.post('/repairs/{identifier}/receipt')
def repair_receipt(identifier: UUID, payload: RepairReceipt, actor: RequestActorDep):
    return mutate(lambda: service.repair_receipt(actor, identifier, payload.installation_id, payload.window_id,
                                               payload.result, settlement_token=payload.settlement_token))
