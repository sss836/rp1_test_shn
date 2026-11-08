#!/usr/bin/env python3
"""Register the approved five planned samples through the normal setup service.

Run only as an explicitly authorized local platform maintainer in the configured
application environment. This does not authenticate a browser, change credentials,
start equipment or declare any observed exposure. Default mode is read-only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.schemas.setup import SetupBundle, SetupOperation
from app.services.setup import apply_bundle, canonical, validate_bundle_shape


DEFAULT_MANIFEST = Path(__file__).with_name("mtbf_samples_v1_1.json")
SOURCE_SHA256 = "b4736a5c50383495f4c33521a70b448caf93f6e7e321ddcfc4360f0dbd4b05b8"
TASK_HOURS = {"WALK_UNLOADED": 162, "DYNAMIC_FAST": 60, "STAND_IDLE": 222, "DANCE": 33, "STUNT_ACTION": 3}
SCENE_HOURS = {"科研运动演示": 240, "展演展示": 120, "短距离移动展示": 120}


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    expected = {"schema", "source_document", "request_key", "product_family", "per_sample_hours",
                "group_total_hours", "task_hours", "scene_hours", "samples"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("The planned-sample manifest has unexpected fields")
    source = value["source_document"]
    if value["schema"] != "rp1.mtbf-planned-samples.v1" or source != {
        "title": "RP1人形机器人整机MTBF测试用例详细方案", "version": "V1.1", "sha256": SOURCE_SHA256,
    }:
        raise ValueError("The approved source document or manifest version changed")
    UUID(value["request_key"])
    family = value["product_family"]
    if not isinstance(family, str) or not re.fullmatch(r"RP[0-9]+\.[0-9]+", family):
        raise ValueError("A standard platform product family is required")
    if (value["per_sample_hours"] != 480 or value["group_total_hours"] != 1920
            or value["task_hours"] != TASK_HOURS or value["scene_hours"] != SCENE_HOURS):
        raise ValueError("The approved planned quotas changed")
    samples = value["samples"]
    if not isinstance(samples, list) or len(samples) != 5:
        raise ValueError("Exactly four plan samples and one separate validation sample are required")
    for number, sample in enumerate(samples, 1):
        label = f"M{number}" if number < 5 else "test_RD"
        expected_sample = {"alias": f"m{number}" if number < 5 else "test_rd", "plan_label": label,
            "asset_code": f"{family}-SYS-{number:03d}",
            "model": ("24轴" if number < 4 else "28轴") if number < 5 else None,
            "included_in_mtbf_group": number < 5}
        if sample != expected_sample:
            raise ValueError("The planned sample identities changed or real identity fields were supplied")
    return value


def build_bundle(manifest: dict, reason: str) -> SetupBundle:
    if not 1 <= len(reason.strip()) <= 300:
        raise ValueError("A concise explicit maintenance authorization reason is required")
    operations = []
    for sample in manifest["samples"]:
        member = sample["included_in_mtbf_group"]
        notes = {"registration_state": "PLANNED_UNBOUND", "plan_label": sample["plan_label"],
                 "source_document": manifest["source_document"], "included_in_four_sample_mtbf_group": member,
                 "identity_status": "SN待绑定；实际设备配置未采集；本次仅计划建档",
                 "planned_hours": manifest["per_sample_hours"] if member else None,
                 "planned_task_hours": manifest["task_hours"] if member else {},
                 "planned_scene_hours": manifest["scene_hours"] if member else {},
                 "purpose": "整机MTBF V1.1计划样品" if member else "独立研发功能验证样品；不属于本次四台1920台时配额",
                 "exposure_status": "未产生执行或有效暴露；计划目标不是实测结果"}
        operations.append(SetupOperation(kind="asset", alias=sample["alias"], data={
            "asset_code": sample["asset_code"], "auto_number": False, "creation_scenario": "RND_TEST",
            "display_name": sample["plan_label"], "serial_number": None, "model": sample["model"],
            "notes": canonical(notes), "reason": reason.strip(),
        }))
    bundle = SetupBundle(request_key=UUID(manifest["request_key"]), operations=operations)
    validate_bundle_shape(bundle)
    return bundle


def establish_maintenance_actor(session, username: str, reason: str, request_id: str):
    role = session.execute(text("""SELECT r.rolsuper,r.rolbypassrls,
        pg_has_role(current_user,c.relowner,'USAGE') AS owns_asset
        FROM pg_roles r CROSS JOIN pg_class c
        WHERE r.rolname=current_user AND c.oid='test.asset'::regclass""")).mappings().one()
    if role["rolsuper"] or role["rolbypassrls"] or role["owns_asset"]:
        raise ApiError(403, "application_role_required", "请在受RLS约束的应用角色环境执行维护登记，不能使用数据库超级用户或表所有者。")
    # Do not select password hashes, create a session or change credentials.
    actor = session.execute(text("""SELECT user_public_id,normalized_username,role,principal_kind,
        enabled,must_change_password,locked_until FROM iam.auth_lookup_login_credential(:username)"""),
        {"username": username.strip().lower()}).mappings().first()
    if (actor is None or not actor["enabled"] or actor["role"] != "SYSTEM_ADMIN"
            or actor["principal_kind"] != "HUMAN" or actor["must_change_password"]
            or actor["locked_until"] is not None and actor["locked_until"] > datetime.now(timezone.utc)):
        raise ApiError(403, "maintenance_actor_invalid", "维护上下文必须明确指定当前启用、未锁定且无需改密的人工系统管理员。")
    session.execute(text("SELECT iam.set_request_context(:actor,:request,:reason)"),
                    {"actor": str(actor["user_public_id"]), "request": request_id, "reason": reason})
    return actor


def register_samples(session, manifest: dict, username: str, reason: str, *, apply: bool = False):
    """Caller owns the short transaction; dry-run caller must set it read-only."""
    bundle = build_bundle(manifest, reason)
    request_id = "mtbf-planned-samples:" + str(bundle.request_key)
    actor = establish_maintenance_actor(session, username, reason.strip(), request_id)
    payload = bundle.model_dump(mode="json", exclude={"request_key"})
    digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
    if apply:
        # Same lock order as apply_bundle: actor/key first, sorted number prefix
        # second. This prevents concurrent setup requests from racing preflight.
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text || :key,0))"),
                        {"key": str(bundle.request_key)})
    previous = session.execute(text("""SELECT payload_hash,result FROM integration.setup_receipt
        WHERE user_id=iam.current_user_id() AND request_key=:key"""), {"key": str(bundle.request_key)}).mappings().first()
    if previous and previous["payload_hash"] != digest:
        raise ApiError(409, "idempotency_conflict", "固定登记请求内容已变化，不能覆盖已登记回执。")
    if not previous:
        if apply:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('asset-number:' || :prefix,0))"),
                            {"prefix": manifest["product_family"] + "-SYS"})
        codes = [sample["asset_code"] for sample in manifest["samples"]]
        names = [sample["plan_label"] for sample in manifest["samples"]]
        conflicts = session.execute(text("""SELECT asset_code,display_name FROM test.asset
            WHERE asset_code=ANY(CAST(:codes AS text[])) OR display_name=ANY(CAST(:names AS text[]))
            ORDER BY asset_code"""), {"codes": codes, "names": names}).mappings().all()
        if conflicts:
            raise ApiError(409, "planned_sample_conflict", "计划编号或显示名已存在；未覆盖、跳过或自动改号，请先核对现有名录。")
    if not apply:
        return {"mode": "DRY_RUN", "status": "WOULD_REPLAY" if previous else "READY",
                "request_key": str(bundle.request_key), "actor_username": actor["normalized_username"],
                "samples": [{**sample, "serial_number": None} for sample in manifest["samples"]],
                "creates_execution": False, "creates_measured_exposure": False,
                "configuration_state": "UNKNOWN registration only"}
    result = apply_bundle(session, bundle, authorization={
        "channel": "local_maintenance", "user_requested": True,
        "maintenance_actor_username": actor["normalized_username"],
        "source_document_sha256": manifest["source_document"]["sha256"],
        "purpose": "register five planned assets only; no browser login or observed test facts",
    })
    return {"mode": "APPLY", "status": "REPLAYED" if result["replayed"] else "REGISTERED", **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-username", required=True, help="Existing enabled HUMAN SYSTEM_ADMIN; explicitly selected maintenance context")
    parser.add_argument("--reason", required=True, help="Explicit user authorization reason, 1..300 characters")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--apply", action="store_true", help="Commit the atomic five-sample registration; default is read-only")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest)
        with SessionLocal.begin() as session:
            if not args.apply:
                session.execute(text("SET TRANSACTION READ ONLY"))
            result = register_samples(session, manifest, args.actor_username, args.reason, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, default=str))
    except ApiError as exc:
        print(json.dumps({"status": "REFUSED", "code": exc.code, "message": exc.message}, ensure_ascii=False))
        raise SystemExit(1) from None
    except DBAPIError as exc:
        print(json.dumps({"status": "REFUSED", "code": "database_conflict" if getattr(exc.orig, "sqlstate", "") == "23505" else "database_operation_failed",
                          "message": "登记未确认；请保留固定request_key查询回执。未自动覆盖编号、重置账号或重试写入。"}, ensure_ascii=False))
        raise SystemExit(1) from None
    except (ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
