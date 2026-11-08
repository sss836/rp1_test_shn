"""Planned registry maintenance uses the app role and normal audited setup service."""
import copy
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.repositories.setup import SetupRepository
from app.schemas.setup import AssetCreate
from test_hmi_presence_integration import PASSWORD, environment, signed_in


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("HMI_TEST_ADMIN_URL"), reason="disposable migrated database required")]
spec = importlib.util.spec_from_file_location("register_mtbf_samples", Path(__file__).resolve().parents[1] / "scripts/register_mtbf_samples.py")
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)
REASON = "Synthetic isolated plan-registration regression"


def manifest():
    value = copy.deepcopy(registry.load_manifest())
    family = "RP1." + str(int(uuid4().hex[:10], 16))
    value["product_family"] = family
    value["request_key"] = str(uuid4())
    suffix = uuid4().hex[:8]
    for number, sample in enumerate(value["samples"], 1):
        sample["asset_code"] = f"{family}-SYS-{number:03d}"
        sample["plan_label"] += "-" + suffix
    return value


def register(value, username, *, apply=False, reason=REASON, owner_username=None):
    with SessionLocal.begin() as session:
        if not apply:
            session.execute(text("SET TRANSACTION READ ONLY"))
        return registry.register_samples(session, value, username, reason, apply=apply,
                                         owner_username=owner_username)


def counts(engine):
    with engine.connect() as db:
        return {name: db.execute(text("SELECT count(*) FROM " + name)).scalar_one() for name in (
            "test.asset", "test.configuration_snapshot", "test.test_execution", "test.test_cycle",
            "test.campaign_coverage_item", "test.runtime_interval", "reliability.exposure_assessment",
            "integration.setup_receipt")}


def test_readonly_preview_atomic_registration_replay_and_explicit_maintenance_evidence(environment):
    engine, users = environment
    value = manifest()
    before = counts(engine)
    dry = register(value, users["admin"])
    assert dry["status"] == "READY" and len(dry["samples"]) == 5
    assert counts(engine) == before
    result = register(value, users["admin"], apply=True)
    assert result["status"] == "REGISTERED" and result["replayed"] is False
    assert set(result["objects"]) == {"m1", "m2", "m3", "m4", "test_rd"}
    after = counts(engine)
    assert after["test.asset"] == before["test.asset"] + 5
    assert after["test.configuration_snapshot"] == before["test.configuration_snapshot"] + 5
    assert after["integration.setup_receipt"] == before["integration.setup_receipt"] + 1
    assert all(after[name] == before[name] for name in before if name not in (
        "test.asset", "test.configuration_snapshot", "integration.setup_receipt"))
    with engine.connect() as db:
        rows = db.execute(text("""SELECT a.display_name,a.serial_number,a.creation_scenario,w.model,c.reliability_impact,
            c.fingerprint,c.hardware_manifest,c.software_manifest,c.parameter_manifest,a.notes
            FROM test.asset a JOIN test.whole_machine_profile w ON w.asset_id=a.id
            JOIN test.configuration_snapshot c ON c.asset_id=a.id WHERE a.product_family=:family ORDER BY a.asset_code"""),
            {"family": value["product_family"]}).mappings().all()
        assert [r["display_name"] for r in rows] == [sample["plan_label"] for sample in value["samples"]]
        assert [r["model"] for r in rows] == ["24轴", "24轴", "24轴", "28轴", None]
        assert all(r["serial_number"] is None and r["creation_scenario"] == "RND_TEST" for r in rows)
        assert all(r["reliability_impact"] == "UNKNOWN" and r["fingerprint"].startswith("registration:") for r in rows)
        assert all(not r[field] for r in rows for field in ("hardware_manifest", "software_manifest", "parameter_manifest"))
        assert '"planned_hours":480' in rows[0]["notes"] and '"planned_hours":null' in rows[-1]["notes"]
        evidence = db.execute(text("SELECT authorization_evidence FROM integration.setup_receipt WHERE request_key=:key"), {"key": value["request_key"]}).scalar_one()
        assert evidence["channel"] == "local_maintenance" and evidence["source_document_sha256"] == registry.SOURCE_SHA256
        assert evidence["maintenance_actor_username"] == users["admin"]
        assert evidence["owner_selection"] == "maintenance_admin_default"
        assert db.execute(text("SELECT count(*) FROM audit.change_log WHERE change_reason=:reason AND request_id=:request AND actor_user_id IS NOT NULL"),
                          {"reason": REASON, "request": "mtbf-planned-samples:" + value["request_key"]}).scalar_one() >= 10
    assert register(value, users["admin"])["status"] == "WOULD_REPLAY"
    replay = register(value, users["admin"], apply=True)
    assert replay["status"] == "REPLAYED" and replay["objects"] == result["objects"]
    assert counts(engine) == after
    with pytest.raises(ApiError, match="固定登记请求") as failure:
        register(value, users["admin"], apply=True, reason=REASON + " changed")
    assert failure.value.code == "idempotency_conflict"


def test_conflicting_candidate_refuses_the_entire_group(environment):
    engine, users = environment
    value = manifest()
    # A conflicting later candidate must prevent the first two from appearing.
    bundle = registry.build_bundle(value, REASON)
    with SessionLocal.begin() as session:
        registry.establish_maintenance_actor(session, users["admin"], REASON, "isolated-register-conflict")
        SetupRepository(session).create_asset(AssetCreate.model_validate(bundle.operations[2].data))
    before = counts(engine)
    with pytest.raises(ApiError) as failure:
        register(value, users["admin"], apply=True)
    assert failure.value.code == "planned_sample_conflict"
    assert counts(engine) == before


def test_non_admin_disabled_locked_and_privileged_database_context_are_rejected(environment):
    engine, users = environment
    value = manifest()
    for username in (users["first"], users["viewer"]):
        with pytest.raises(ApiError) as failure:
            register(value, username, apply=True)
        assert failure.value.code == "maintenance_actor_invalid"
    with engine.begin() as db:
        db.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:name"), {"name": users["admin"]})
    with pytest.raises(ApiError) as failure:
        register(value, users["admin"], apply=True)
    assert failure.value.code == "maintenance_actor_invalid"
    with engine.begin() as db:
        db.execute(text("UPDATE iam.app_user SET enabled=true WHERE username=:name"), {"name": users["admin"]})
        db.execute(text("UPDATE iam.user_credential SET locked_until=now()+interval '5 minutes' WHERE user_id=(SELECT id FROM iam.app_user WHERE username=:name)"), {"name": users["admin"]})
    with pytest.raises(ApiError) as failure:
        register(value, users["admin"], apply=True)
    assert failure.value.code == "maintenance_actor_invalid"
    with Session(engine) as session, session.begin():
        with pytest.raises(ApiError) as failure:
            registry.register_samples(session, value, users["admin"], REASON, apply=True)
        assert failure.value.code == "application_role_required"


def test_late_setup_failure_rolls_back_all_five_samples_and_receipt(environment, monkeypatch):
    engine, users = environment
    value = manifest()
    before = counts(engine)
    original = SetupRepository.create_asset

    def fail_last(self, asset):
        if asset.display_name.startswith("test_RD-"):
            raise ApiError(422, "synthetic_late_failure", "isolated failure injection")
        return original(self, asset)

    monkeypatch.setattr(SetupRepository, "create_asset", fail_last)
    with pytest.raises(ApiError) as failure:
        register(value, users["admin"], apply=True)
    assert failure.value.code == "synthetic_late_failure"
    assert counts(engine) == before


def test_verified_admin_can_delegate_owner_without_grants_or_browser_impersonation(environment):
    engine, users = environment
    value = manifest()
    before = counts(engine)
    preview = register(value, users["admin"], owner_username=users["first"])
    assert preview["status"] == "READY" and preview["delegated"] is True
    assert preview["maintenance_admin_username"] == users["admin"]
    assert preview["owner_username"] == users["first"]
    assert preview["owner_user_public_id"] != preview["maintenance_admin_user_public_id"]
    assert preview["owner_selection"] == "explicit_maintenance_cli_argument"
    assert counts(engine) == before
    with pytest.raises(ApiError) as refused:
        register(value, users["first"], apply=True, owner_username=users["first"])
    assert refused.value.code == "maintenance_actor_invalid"
    with pytest.raises(ApiError) as refused:
        register(value, users["admin"], apply=True, owner_username=users["viewer"])
    assert refused.value.code == "registration_owner_invalid"
    result = register(value, users["admin"], apply=True, owner_username=users["first"])
    assert result["status"] == "REGISTERED" and result["delegated"] is True
    identifiers = {entry["id"] for entry in result["objects"].values()}
    with engine.connect() as db:
        evidence = db.execute(text("SELECT authorization_evidence FROM integration.setup_receipt WHERE request_key=:key"),
                              {"key": value["request_key"]}).scalar_one()
        assert evidence["channel"] == "local_maintenance" and evidence["delegated"] is True
        assert evidence["owner_selection"] == "explicit_maintenance_cli_argument"
        assert evidence["maintenance_admin_username"] == users["admin"] and evidence["owner_username"] == users["first"]
        assert evidence["maintenance_admin_user_public_id"] == preview["maintenance_admin_user_public_id"]
        assert evidence["owner_user_public_id"] == preview["owner_user_public_id"]
        assert evidence["audit_request_id"] == "mtbf-planned-samples:" + value["request_key"]
        owners = db.execute(text("""SELECT DISTINCT u.username FROM test.asset a JOIN iam.app_user u ON u.id=a.created_by
            WHERE a.product_family=:family"""), {"family": value["product_family"]}).scalars().all()
        assert owners == [users["first"]]
        assert db.execute(text("""SELECT count(*) FROM audit.change_log WHERE request_id=:request
            AND actor_user_id=(SELECT id FROM iam.app_user WHERE username=:owner)"""),
            {"request": evidence["audit_request_id"], "owner": users["first"]}).scalar_one() >= 10
    # Actual authenticated readers see the normal owner/admin scopes; there is
    # no grant broadening and a stranger cannot edit any of these samples.
    with signed_in(users["first"]) as owner, signed_in(users["second"]) as stranger, signed_in(users["admin"]) as admin:
        own_assets = owner.get("/api/v1/setup/options").json()["data"]["assets"]
        assert identifiers.issubset({item["id"] for item in own_assets if item["can_edit"]})
        assert identifiers.isdisjoint({item["id"] for item in stranger.get("/api/v1/setup/options").json()["data"]["assets"]})
        assert identifiers.issubset({item["id"] for item in admin.get("/api/v1/setup/options").json()["data"]["assets"]})
        target = result["objects"]["m1"]["id"]
        update = {"serial_number": "SYNTHETIC-" + uuid4().hex, "reason": "isolated owner edit check"}
        assert stranger.patch(f"/api/v1/setup/assets/{target}/identity",json=update,
            headers={"Idempotency-Key":str(uuid4())}).status_code in {403,404}
        response = owner.patch(f"/api/v1/setup/assets/{target}/identity",json=update,
            headers={"Idempotency-Key":str(uuid4())})
        assert response.status_code == 200, response.text
    with engine.begin() as db:
        alternate = "registry-maintainer-" + uuid4().hex[:12]
        uid = db.execute(text("""INSERT INTO iam.app_user(username,display_name,role,principal_kind,must_change_password)
            VALUES(:name,:name,'SYSTEM_ADMIN','HUMAN',false) RETURNING id"""), {"name":alternate}).scalar_one()
        db.execute(text("INSERT INTO iam.user_credential(user_id,password_hash) VALUES(:id,:hash)"),
                   {"id":uid,"hash":PasswordHasher().hash(PASSWORD)})
    # The actor/key receipt belongs to the actual owner. A different verified
    # administrator can recover it but cannot replace original authorization.
    replay = register(value, alternate, apply=True, owner_username=users["first"])
    assert replay["status"] == "REPLAYED" and replay["objects"] == result["objects"]
    with engine.connect() as db:
        assert db.execute(text("SELECT authorization_evidence FROM integration.setup_receipt WHERE request_key=:key"),
                          {"key":value["request_key"]}).scalar_one() == evidence
    after = counts(engine)
    with pytest.raises(ApiError) as refused:
        register(value, users["admin"], apply=True, owner_username=users["second"])
    assert refused.value.code == "planned_sample_conflict"
    assert counts(engine) == after
    with engine.begin() as db:
        db.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:name"), {"name":users["first"]})
    with pytest.raises(ApiError) as refused:
        register(value, users["admin"], apply=True, owner_username=users["first"])
    assert refused.value.code == "registration_owner_invalid"
