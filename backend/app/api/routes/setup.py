from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Header
from sqlalchemy.exc import DBAPIError

from app.core.auth import RequestActor, RequestActorDep, require_roles
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.setup import SetupRepository
from app.schemas.common import DataEnvelope
from app.schemas.setup import (AssetCreate, CampaignCreate, Change, ContextCreate, ProgramCreate, StationCreate,
    CaseCreate, CaseRevision, ConfigurationCreate, MetadataChange, SetupBundle, SetupOperation)
from app.services.setup import apply_bundle
from sqlalchemy import text
from app.schemas.stations import RegisteredStationList

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])
Admin = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN"))]
Editor = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]


def write(operation):
    try:
        return DataEnvelope(data=operation())
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise ApiError(409, "duplicate", "编号、序列号或关联已存在，请检查后重试。") from exc
        if code in {"23514", "23503", "P0001", "22001"}:
            raise ApiError(422, "invalid_setup", "字段或关联不符合业务规则。") from exc
        if code == "42501":
            raise ApiError(403, "forbidden", "当前账户没有此对象的操作权限。") from exc
        raise


@router.get("/catalog", response_model=DataEnvelope[list[dict[str, Any]]])
def catalog(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).catalog())


@router.get("/options", response_model=DataEnvelope[dict[str, Any]])
def options(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).options())


@router.get("/campaigns", response_model=DataEnvelope[list[dict[str, Any]]])
def campaigns(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).campaigns())


@router.get("/stations", response_model=DataEnvelope[RegisteredStationList])
def list_stations(
    actor: RequestActorDep,
    session: DbSession,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
):
    if actor.principal_kind != "HUMAN":
        raise ApiError(403, "forbidden", "台架总览需要登录账号。")
    return DataEnvelope(data=SetupRepository(session).stations(cursor=cursor, limit=limit))


@router.get("/campaigns/{campaign_id}/contexts", response_model=DataEnvelope[list[dict[str, Any]]])
def contexts(campaign_id: UUID, session: DbSession):
    return DataEnvelope(data=SetupRepository(session).contexts(campaign_id))


RequestKey = Annotated[UUID | None, Header(alias="Idempotency-Key")]


def single(session, kind, value, key, **identifiers):
    if kind in {"case_revision","configuration"} and key is None:
        raise ApiError(422,"idempotency_key_required","追加版本或配置必须提供Idempotency-Key，以便重试取得原回执。")
    bundle = SetupBundle(request_key=key or uuid4(), operations=[SetupOperation(kind=kind, alias="object", data={**value.model_dump(mode="json"), **identifiers})])
    result = apply_bundle(session, bundle)
    return DataEnvelope(data={**result["objects"]["object"], "request_key": result["request_key"], "replayed": result["replayed"]})


@router.post("/bundle", status_code=201)
def bundle(payload: SetupBundle, _: Editor, session: DbSession):
    return write(lambda: apply_bundle(session, payload))


@router.get("/receipts/{request_key}")
def receipt(request_key: UUID, _: RequestActorDep, session: DbSession):
    row = session.execute(text("SELECT result FROM integration.setup_receipt WHERE user_id=iam.current_user_id() AND request_key=:key"), {"key":request_key}).scalar_one_or_none()
    if row is None: raise ApiError(404,"receipt_not_found","没有此账号的已提交回执。")
    return DataEnvelope(data=row)


@router.post("/assets", status_code=201)
def asset(payload: AssetCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"asset",payload,key)


def single_write(session, kind, payload, key, **identifiers):
    return write(lambda: single(session,kind,payload,key,**identifiers).data)


@router.post("/programs", status_code=201)
def program(payload: ProgramCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"program",payload,key)


@router.post("/campaigns", status_code=201)
def campaign(payload: CampaignCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"campaign",payload,key)


@router.post("/campaigns/{campaign_id}/activate")
def activate(campaign_id: UUID, payload: Change, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"campaign_activate",payload,key,id=str(campaign_id))


@router.post("/stations", status_code=201)
def station(payload: StationCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"station",payload,key)


@router.post("/campaigns/{campaign_id}/contexts", status_code=201)
def configure(campaign_id: UUID, payload: ContextCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"context",payload,key,campaign_id=str(campaign_id))


@router.post("/cases", status_code=201)
def case(payload: CaseCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"case",payload,key)


@router.get("/cases/{case_id}/versions")
def case_versions(case_id: UUID, _: RequestActorDep, session: DbSession):
    return DataEnvelope(data=SetupRepository(session).case_versions(case_id))


@router.post("/cases/{case_id}/versions", status_code=201)
def revise_case(case_id: UUID, payload: CaseRevision, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"case_revision",payload,key,id=str(case_id))


@router.post("/case-versions/{version_id}/publish")
def publish_case(version_id: UUID, payload: Change, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"case_publish",payload,key,id=str(version_id))


@router.post("/assets/{asset_id}/configurations", status_code=201)
def configuration(asset_id: UUID, payload: ConfigurationCreate, _: Editor, session: DbSession, key: RequestKey = None):
    return single_write(session,"configuration",payload,key,id=str(asset_id))


@router.patch("/{kind}/{identifier}")
def update_metadata(kind: str, identifier: UUID, payload: MetadataChange, _: Editor, session: DbSession, key: RequestKey = None):
    kinds = {"assets":"asset", "programs":"program", "campaigns":"campaign", "stations":"station"}
    if kind not in kinds: raise ApiError(404,"not_found","不支持此管理对象。")
    return single_write(session,kinds[kind]+"_update",payload,key,id=str(identifier))
