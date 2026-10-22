"""One transactional setup service for ordinary pages and scoped Pi tools."""
import hashlib
import json
import math
import re
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import text

from app.core.errors import ApiError
from app.repositories.setup import SetupRepository
from app.schemas.archives import AcceptanceRules
from app.schemas.setup import (AssetCreate, CampaignCreate, CaseCreate, CaseDefinition, CaseRevision, Change,
    ConfigurationCreate, ContextCreate, MetadataChange, ProgramCreate, SetupBundle, StationCreate)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=str)


def validate_definition(value: CaseDefinition):
    if not value.publish:
        return
    def content(value):
        if isinstance(value,str): return bool(value.strip())
        if isinstance(value,dict): return any(content(child) for child in value.values())
        if isinstance(value,list): return any(content(child) for child in value)
        return value is not None
    missing = [name for name in ("equipment_requirements", "sampling_requirements", "termination_rules") if not content(getattr(value, name))]
    if not content(value.procedure_spec) and not content(value.stage_definitions):
        missing.append("procedure_spec/stage_definitions")
    rules = value.decision_rules
    mode = rules.get("mode")
    if mode not in {"TEXT_ONLY", "RECORD_ONLY", "AUTOMATIC"}:
        missing.append("decision_rules.mode")
    if mode == "TEXT_ONLY" and not str(rules.get("text", "")).strip():
        missing.append("decision_rules.text")
    if missing:
        raise ApiError(422, "case_incomplete", "发布前请补齐步骤/阶段、设备、采样、终止条件及判定说明。", missing)
    if mode in {"TEXT_ONLY", "RECORD_ONLY"} and "archive_acceptance" in rules:
        raise ApiError(422, "case_rules_conflict", "文字或仅记录模式不能包含自动判据。")
    if mode == "AUTOMATIC":
        candidate = rules.get("archive_acceptance")
        if not isinstance(candidate, dict) or any(k != "criteria_version" and v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)) for k, v in candidate.items()):
            raise ApiError(422, "case_rules_invalid", "自动判据必须使用后端支持的数值字段。")
        parsed = AcceptanceRules.model_validate(candidate)
        if not any(v is not None for k, v in parsed.model_dump().items() if k not in {"criteria_version", "record_rate_hz"}):
            raise ApiError(422, "case_rules_empty", "自动判据至少需要一个明确判定条件。")


def resolve_refs(value, results, identifier_field=False):
    if identifier_field and isinstance(value, str) and value.startswith("$"):
        match = re.fullmatch(r"\$([a-z][a-z0-9_]{0,31})\.(id|configuration_id|version_id)", value)
        if not match or match[1] not in results or match[2] not in results[match[1]]:
            raise ApiError(422, "invalid_reference", "组内引用必须指向前面成功创建的对象和实际返回字段。")
        return results[match[1]][match[2]]
    if isinstance(value, dict): return {k: resolve_refs(v, results, k=="id" or k.endswith("_id")) for k, v in value.items()}
    if isinstance(value, list): return [resolve_refs(v, results) for v in value]
    return value


def operation(repo, op, data):
    data = dict(data)
    identifier = data.pop("id", None)
    campaign = data.pop("campaign_id", None) if op.kind == "context" else None
    if op.kind in {"asset", "program", "campaign", "station", "context"}:
        schema, method = {"asset": (AssetCreate, repo.create_asset), "program": (ProgramCreate, repo.create_program),
            "campaign": (CampaignCreate, repo.create_campaign), "station": (StationCreate, repo.create_station),
            "context": (ContextCreate, repo.create_context)}[op.kind]
        if identifier is not None: raise ApiError(422, "invalid_setup", "新对象不能指定已有对象id。")
        value = schema.model_validate(data)
        return method(UUID(str(campaign)), value) if op.kind == "context" else method(value)
    if op.kind == "case":
        if identifier is not None: raise ApiError(422, "invalid_setup", "新用例不能指定已有id。")
        value = CaseCreate.model_validate(data)
        validate_definition(value)
        return repo.create_case(value)
    if identifier is None:
        raise ApiError(422, "missing_identifier", "请明确要管理的对象id。")
    identifier = UUID(str(identifier))
    if op.kind == "case_revision":
        value = CaseRevision.model_validate(data)
        validate_definition(value)
        return repo.revise_case(identifier, value)
    if op.kind == "case_publish":
        value = Change.model_validate(data)
        return repo.publish_case(identifier, value.reason, validate_definition)
    if op.kind == "configuration":
        return repo.append_configuration(identifier, ConfigurationCreate.model_validate(data))
    if op.kind == "campaign_activate":
        return repo.activate_campaign(identifier, Change.model_validate(data).reason)
    return repo.update_metadata(op.kind.removesuffix("_update"), identifier, MetadataChange.model_validate(data))


def apply_bundle(session, bundle: SetupBundle, authorization=None):
    """Caller owns a short transaction. No model/network waits occur inside it."""
    payload = bundle.model_dump(mode="json", exclude={"request_key"})
    try:
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
    except ValueError as exc:
        raise ApiError(422,"invalid_setup","参数不允许非有限数值。") from exc
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text || :key,0))"), {"key": str(bundle.request_key)})
    previous = session.execute(text("SELECT payload_hash,result FROM integration.setup_receipt WHERE user_id=iam.current_user_id() AND request_key=:key"), {"key": str(bundle.request_key)}).mappings().first()
    if previous:
        if previous["payload_hash"] != digest:
            raise ApiError(409, "idempotency_conflict", "同一请求标识的内容已变化，请使用新请求标识。")
        return {**previous["result"], "replayed": True}
    repo = SetupRepository(session)
    results = {}
    try:
        validate_bundle_shape(bundle)
        for op in bundle.operations:
            results[op.alias] = operation(repo, op, resolve_refs(op.data, results))
    except (ValidationError, ValueError, TypeError) as exc:
        details = [{"field": ".".join(map(str, error["loc"])), "message": error["msg"]} for error in exc.errors()] if isinstance(exc, ValidationError) else []
        raise ApiError(422, "invalid_setup", "参数缺失或格式不正确，请核对整组字段后重试。", details) from exc
    result = json.loads(canonical({"request_key": str(bundle.request_key), "objects": results, "replayed": False}))
    session.execute(text("INSERT INTO integration.setup_receipt(user_id,request_key,payload_hash,request_payload,authorization_evidence,result) VALUES(iam.current_user_id(),:key,:hash,CAST(:payload AS jsonb),CAST(:evidence AS jsonb),CAST(:result AS jsonb))"), {
        "key": str(bundle.request_key), "hash": digest, "payload": canonical(payload),
        "evidence": canonical(authorization or {"channel": "authenticated_api"}), "result": canonical(result),
    })
    return result


def validate_bundle_shape(bundle):
    """Validate every operation before the first business mutation."""
    schemas = {"asset":AssetCreate,"program":ProgramCreate,"campaign":CampaignCreate,"station":StationCreate,
        "case":CaseCreate,"context":ContextCreate,"case_revision":CaseRevision,"case_publish":Change,
        "configuration":ConfigurationCreate,"campaign_activate":Change}
    previous = {}
    for op in bundle.operations:
        data = resolve_refs(op.data, previous)
        identifier = data.pop("id", None)
        if op.kind == "context": UUID(str(data.pop("campaign_id", None)))
        if op.kind not in {"asset","program","campaign","station","case","context"}: UUID(str(identifier))
        elif identifier is not None: raise ValueError("新建对象不能指定id")
        value = schemas.get(op.kind, MetadataChange).model_validate(data)
        if isinstance(value,CaseDefinition): validate_definition(value)
        fields = [] if op.kind=="case_publish" else ["id"]
        if op.kind in {"case","case_revision","case_publish"}: fields += ["version_id"]
        if op.kind in {"asset","configuration"}: fields += ["configuration_id"]
        previous[op.alias] = {name:"00000000-0000-4000-8000-000000000001" for name in fields}
