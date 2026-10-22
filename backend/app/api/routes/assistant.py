from typing import Annotated

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict

from app.core.auth import RequestActorDep
from app.core.config import get_settings
from app.schemas.common import DataEnvelope
from app.services.assistant import TOOL_NAME, read_stations, require_pilot, run_probe
from app.services.setup_assistant import SetupCommand, AdoptProposal, adopt_proposal, run_command, apply_tool
from app.schemas.setup import SetupOperation
from app.core.db import DbSession
from app.api.routes.setup import write
from pydantic import Field
from app.core.errors import ApiError

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant-isolation"])


class EmptyProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.get("/capabilities")
def capabilities(actor: RequestActorDep):
    require_pilot(get_settings(), actor.public_id, actor.role, actor.principal_kind)
    if get_settings().assistant_runtime_enabled:
        return DataEnvelope(data={'mode':'assistant_runtime','tools':['get_curve_statistics','get_test_progress','list_hmi_stations','apply_setup_bundle',*__import__('app.services.assistant_business',fromlist=['TOOLS']).TOOLS],'writes_enabled':True,'limits':{'daily_cny':100,'concurrency':2,'rounds':6,'seconds':120,'chat_days':30}})
    return DataEnvelope(data={"mode": "isolation_probe", "tools": [TOOL_NAME], "writes_enabled": False})


@router.post("/probe")
async def probe(_: EmptyProbe, request: Request, actor: RequestActorDep):
    settings = get_settings()
    if settings.assistant_runtime_enabled or not settings.assistant_legacy_probes or settings.app_env!='test':raise ApiError(410,'assistant_legacy_closed','请使用统一助手任务入口。')
    return DataEnvelope(data=await run_probe(request.cookies.get(settings.auth_cookie_name, ""), actor, settings))


@router.post("/tools/list_hmi_stations")
def tool(_: EmptyProbe, grant: Annotated[str, Header(alias="X-RP1-Tool-Grant", min_length=32, max_length=128)]):
    # Scoped capability is required; neither a browser cookie nor an ingestion
    # Bearer authenticates this callback. Its original browser session is rechecked.
    return DataEnvelope(data=read_stations(grant, get_settings()))


class SetupToolInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    operations: list[SetupOperation] = Field(min_length=1,max_length=20)


@router.post("/setup/commands")
async def setup_command(payload: SetupCommand, request: Request, actor: RequestActorDep):
    settings=get_settings()
    if settings.assistant_runtime_enabled or not settings.assistant_legacy_probes or settings.app_env!='test':raise ApiError(410,'assistant_legacy_closed','请使用统一助手任务入口。')
    return DataEnvelope(data=await run_command(payload,request,actor))


@router.post("/setup/adopt")
def adopt(payload: AdoptProposal, actor: RequestActorDep, session: DbSession):
    return write(lambda: adopt_proposal(payload,actor,session))


@router.post("/tools/apply_setup_bundle")
def setup_tool(payload: SetupToolInput, grant: Annotated[str, Header(alias="X-RP1-Tool-Grant",min_length=32,max_length=128)]):
    from app.services.assistant_chat import runtime
    if grants_key_in_runtime(grant):runtime.allow_write(grant)
    elif get_settings().assistant_runtime_enabled or not get_settings().assistant_legacy_probes or get_settings().app_env!='test':raise ApiError(403,'assistant_grant_invalid','需要统一任务授权。')
    return DataEnvelope(data=apply_tool(grant,[op.model_dump(mode="json") for op in payload.operations]))


def grants_key_in_runtime(grant):
    from app.services.assistant import grants
    from app.services.assistant_chat import runtime
    return grants.key(grant) in runtime.tokens
