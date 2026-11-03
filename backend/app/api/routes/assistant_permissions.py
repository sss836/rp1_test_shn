from typing import Literal
from uuid import UUID
from fastapi import APIRouter,Request
from pydantic import BaseModel,ConfigDict,Field
from sqlalchemy.exc import DBAPIError
from app.core.auth import RequestActorDep
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.services import assistant_permissions as service

router=APIRouter(prefix='/api/v1/assistant/jobs',tags=['assistant-read-permissions'])
class Identity(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_key:UUID
class PermissionIdentity(Identity):
    expected_request_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
class Decision(PermissionIdentity):
    scope:Literal['file','directory','all','deny']
def mutate(call):
    try:return DataEnvelope(data=call())
    except DBAPIError as exc:raise ApiError(409,'permission_conflict','读取申请状态发生并发冲突，请核对原申请回执。') from exc
@router.get('/{identifier}/permissions')
def permissions(identifier:UUID,actor:RequestActorDep):return DataEnvelope(data=service.list_permissions(actor,identifier))
@router.post('/{identifier}/permissions/{rid}/decision')
def decision(identifier:UUID,rid:UUID,payload:Decision,actor:RequestActorDep):
    return mutate(lambda:service.decision(actor,identifier,rid,payload))
@router.post('/{identifier}/permissions/{rid}/retry')
def retry(identifier:UUID,rid:UUID,payload:PermissionIdentity,actor:RequestActorDep):
    return mutate(lambda:service.decision(actor,identifier,rid,payload,retry=True))
@router.post('/{identifier}/resume',status_code=202)
async def resume(identifier:UUID,payload:Identity,request:Request,actor:RequestActorDep):
    from app.services.assistant_chat import runtime
    return DataEnvelope(data=await runtime.resume(identifier,actor,request,payload.request_key))
