from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.core.auth import RequestActor, RequestActorDep
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.hmi_presence import HmiPresenceRepository
from app.schemas.common import DataEnvelope
from app.schemas.hmi_presence import HmiHeartbeat, HmiStationList


router = APIRouter(prefix="/api/v1/hmi", tags=["hmi-presence"])


def require_hmi_operator(actor: RequestActorDep) -> RequestActor:
    if actor.principal_kind != "HUMAN" or actor.role not in {"TEST_EXECUTOR", "SYSTEM_ADMIN"} or actor.session_public_id is None:
        raise ApiError(403, "hmi_operator_required", "上位机需要测试执行员或管理员的有效登录会话。")
    return actor


HmiOperator = Annotated[RequestActor, Depends(require_hmi_operator)]


@router.put("/presence/{connection_id}")
def heartbeat(connection_id: UUID, payload: HmiHeartbeat, actor: HmiOperator, session: DbSession) -> DataEnvelope[dict]:
    return DataEnvelope(data=HmiPresenceRepository(session).heartbeat(connection_id, payload, actor))


@router.delete("/presence/{connection_id}")
def disconnect(connection_id: UUID, actor: HmiOperator, session: DbSession) -> DataEnvelope[dict]:
    return DataEnvelope(data=HmiPresenceRepository(session).disconnect(connection_id, actor))


@router.get("/stations")
def stations(
    actor: RequestActorDep,
    session: DbSession,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DataEnvelope[HmiStationList]:
    if actor.principal_kind != "HUMAN":
        raise ApiError(403, "forbidden", "工位监控需要登录账号。")
    return DataEnvelope(data=HmiPresenceRepository(session).list_stations(cursor=cursor, limit=limit))
