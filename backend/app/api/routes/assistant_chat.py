from typing import Annotated
from uuid import UUID
from fastapi import APIRouter,Header,Request
from fastapi.responses import Response
from urllib.parse import unquote
from sqlalchemy.exc import DBAPIError
from app.core.auth import RequestActorDep
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.schemas.assistant_chat import ChatSubmit,ReadTool,MeterCall
from app.services.assistant_chat import runtime,list_threads,history,job_status,cancel_job,actor_session,require_runtime
from app.services.assistant_billing import budget,reliable_prices
from app.core.config import get_settings

router=APIRouter(prefix='/api/v1/assistant',tags=['assistant-runtime'])
Grant=Annotated[str,Header(alias='X-RP1-Tool-Grant',min_length=32,max_length=128)]


@router.post('/attachments',status_code=201)
async def attachment_upload(request:Request,actor:RequestActorDep):
    require_runtime(actor)
    from app.services.assistant_attachments import upload,MAX_BYTES
    from app.core.errors import ApiError
    from starlette.concurrency import run_in_threadpool
    raw=bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw)>MAX_BYTES:raise ApiError(413,'attachment_size','附件最大4MB。')
    return DataEnvelope(data=await run_in_threadpool(upload,actor,bytes(raw),unquote(request.headers.get('x-filename','attachment')),request.headers.get('content-type','').split(';')[0]))


@router.get('/attachments/{identifier}')
def attachment_get(identifier:UUID,actor:RequestActorDep):
    require_runtime(actor)
    from app.services.assistant_attachments import read
    meta,raw=read(actor,identifier)
    return Response(raw,media_type=meta['mime'],headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Disposition':'inline' if meta['kind']=='image' else 'attachment'})


@router.delete('/attachments/{identifier}',status_code=204)
def attachment_delete(identifier:UUID,actor:RequestActorDep):
    require_runtime(actor)
    from app.services.assistant_attachments import delete
    delete(actor,identifier)
    return Response(status_code=204)


@router.get('/threads')
def threads(actor:RequestActorDep):return DataEnvelope(data=list_threads(actor))


@router.get('/threads/{identifier}')
def thread(identifier:UUID,actor:RequestActorDep):return DataEnvelope(data=history(actor,identifier))


@router.post('/jobs',status_code=202)
async def submit(payload:ChatSubmit,request:Request,actor:RequestActorDep):
    try:return DataEnvelope(data=await runtime.submit(payload,request,actor))
    except DBAPIError as exc:raise ApiError(409,'assistant_conflict','请求标识或会话发生冲突，请核对原任务回执。') from exc


@router.get('/jobs/{identifier}')
def job(identifier:UUID,actor:RequestActorDep):return DataEnvelope(data=job_status(actor,identifier))


@router.post('/jobs/{identifier}/cancel')
def cancel(identifier:UUID,actor:RequestActorDep):return DataEnvelope(data=cancel_job(actor,identifier))


@router.get('/budget')
def site_budget(actor:RequestActorDep):
    require_runtime(actor)
    with actor_session(actor) as session:
        result=budget(session)
    try:reliable_prices(get_settings());result['pricing_verified']=True
    except ApiError:result['pricing_verified']=False
    return DataEnvelope(data=result)


@router.post('/tools/chat_read')
def tool_read(payload:ReadTool,grant:Grant):return DataEnvelope(data=runtime.read(grant,payload))


@router.post('/tools/model_call')
def model_call(payload:MeterCall,grant:Grant):
    try:return DataEnvelope(data=runtime.meter(grant,payload))
    except DBAPIError as exc:raise ApiError(409,'assistant_accounting_stopped','预算不足、用量待核对或任务达到轮次/时限，已停止新增模型调用。') from exc
