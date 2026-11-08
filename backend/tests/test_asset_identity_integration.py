"""Real session HTTP and PostgreSQL identity completion; disposable DB only."""
from concurrent.futures import ThreadPoolExecutor
import os
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from app.main import app
from test_hmi_presence_integration import environment, signed_in


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated migrated database required")]


def post(client, path, data, key=None):
    response = client.post("/api/v1/setup/" + path,
        json={"reason": "Synthetic identity fixture", **data},
        headers={"Idempotency-Key": key or str(uuid4())})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def planned(client, *, family=None, number=1, model=None):
    family = family or "RP98." + str(int(uuid4().hex[:8], 16))
    data = {"asset_code": f"{family}-SYS-{number:03d}", "auto_number": False,
            "creation_scenario": "RND_TEST", "display_name": "Synthetic planned sample"}
    if model is not None:
        data["model"] = model
    return post(client, "assets", data)


def complete(client, sample, data, key=None):
    return client.patch(f"/api/v1/setup/assets/{sample['id']}/identity",
        json={"reason": "Synthetic verified identity", **data},
        headers={"Idempotency-Key": key or str(uuid4())})


def option(client, sample):
    response = client.get("/api/v1/setup/options")
    assert response.status_code == 200, response.text
    return next(row for row in response.json()["data"]["assets"] if row["id"] == sample["id"])


def identity(db, sample):
    return dict(db.execute(text("""SELECT a.serial_number,w.model,a.display_name,a.notes
        FROM test.asset a JOIN test.whole_machine_profile w ON w.asset_id=a.id
        WHERE a.public_id=:id"""), {"id": sample["id"]}).mappings().one())


def campaign(client):
    code = "IDENTITY-" + uuid4().hex
    program = post(client, "programs", {"program_code": code, "name": code})
    return post(client, "campaigns", {"program_id": program["id"], "campaign_code": code,
                                       "name": code, "asset_kind": "WHOLE_MACHINE"})


def test_unknown_registration_completes_once_with_receipt_and_audit_not_measurements(environment):
    engine, users = environment
    with signed_in(users["first"]) as owner:
        sample = planned(owner, model="24轴")
        before = option(owner, sample)
        assert before["serial_number"] is None and before["model"] == "24轴"
        assert before["can_complete_identity"] and before["blocked_reason"] is None
        with engine.connect() as db:
            original = identity(db, sample)
            configuration = db.execute(text("SELECT to_jsonb(c) FROM test.configuration_snapshot c WHERE public_id=:id"),
                {"id": sample["configuration_id"]}).scalar_one()
            counts = [db.execute(text("SELECT count(*) FROM " + name)).scalar_one()
                      for name in ("test.test_execution", "test.runtime_interval", "reliability.exposure_assessment")]
        key = str(uuid4())
        first = complete(owner, sample, {"serial_number": "SN-" + uuid4().hex}, key)
        assert first.status_code == 200, first.text
        receipt = first.json()["data"]
        assert receipt["identity_completed"] and receipt["model"] == "24轴" and not receipt["replayed"]
        replay = complete(owner, sample, {"serial_number": receipt["serial_number"]}, key)
        assert replay.status_code == 200 and replay.json()["data"]["replayed"]
        conflict = complete(owner, sample, {"serial_number": "DIFFERENT"}, key)
        assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
        assert complete(owner, sample, {"serial_number": receipt["serial_number"]}).status_code == 200
        after = option(owner, sample)
        assert not after["can_complete_identity"] and after["blocked_reason"]
    with engine.connect() as db:
        current = identity(db, sample)
        assert current == {**original, "serial_number": receipt["serial_number"]}
        assert db.execute(text("SELECT to_jsonb(c) FROM test.configuration_snapshot c WHERE public_id=:id"),
            {"id": sample["configuration_id"]}).scalar_one() == configuration
        assert [db.execute(text("SELECT count(*) FROM " + name)).scalar_one()
                for name in ("test.test_execution", "test.runtime_interval", "reliability.exposure_assessment")] == counts
        audit = db.execute(text("""SELECT before_data,after_data,actor_user_id,request_id
            FROM audit.change_log WHERE table_name='asset' AND operation='UPDATE'
            AND after_data->>'public_id'=:id AND change_reason='Synthetic verified identity'"""),
            {"id": sample["id"]}).mappings().all()
        assert len(audit) == 1 and audit[0]["actor_user_id"] and audit[0]["request_id"]
        assert audit[0]["before_data"]["serial_number"] is None
        assert audit[0]["after_data"]["serial_number"] == receipt["serial_number"]


def test_completion_requires_key_csrf_owner_and_editor_and_rejects_replacement(environment):
    _, users = environment
    with signed_in(users["first"]) as owner, signed_in(users["second"]) as stranger, signed_in(users["viewer"]) as viewer:
        sample = planned(owner, model="24轴")
        path = f"/api/v1/setup/assets/{sample['id']}/identity"
        payload = {"serial_number": "SN-" + uuid4().hex, "reason": "Synthetic verified identity"}
        assert owner.patch(path, json=payload).status_code == 422
        assert complete(stranger, sample, payload).status_code in {403, 404}
        assert complete(viewer, sample, payload).status_code == 403
        with TestClient(app) as anonymous:
            assert anonymous.patch(path, json=payload).status_code == 401
        csrf = owner.headers.pop("X-CSRF-Token")
        assert complete(owner, sample, payload).status_code == 403
        owner.headers["X-CSRF-Token"] = csrf
        assert complete(owner, sample, {**payload, "model": "28轴"}).status_code == 409
        for invalid in ({}, {"serial_number": None}, {"model": ""}, {"serial_number": "SN", "name": "forged"}):
            assert complete(owner, sample, invalid).status_code == 422
        assert option(owner, sample)["serial_number"] is None


def test_model_and_sn_can_be_filled_separately_but_old_values_never_replaced(environment):
    _, users = environment
    with signed_in(users["first"]) as owner:
        sample = planned(owner)
        first = complete(owner, sample, {"serial_number": "SN-" + uuid4().hex})
        assert first.status_code == 200 and not first.json()["data"]["identity_completed"]
        assert option(owner, sample)["can_complete_identity"]
        assert complete(owner, sample, {"serial_number": "different", "model": "24轴"}).status_code == 409
        response = complete(owner, sample, {"model": "24轴"})
        assert response.status_code == 200 and response.json()["data"]["identity_completed"]
        assert complete(owner, sample, {"model": "28轴"}).status_code == 409


def test_duplicate_sn_rolls_back_model_and_receipt_and_concurrent_completions_serialize(environment):
    engine, users = environment
    family = "RP98." + str(int(uuid4().hex[:8], 16))
    serial = "SN-" + uuid4().hex
    with signed_in(users["first"]) as owner:
        first, second = planned(owner, family=family), planned(owner, family=family, number=2)
        assert complete(owner, first, {"serial_number": serial, "model": "24轴"}).status_code == 200
        key = str(uuid4())
        assert complete(owner, second, {"serial_number": serial, "model": "28轴"}, key).status_code == 409
        with engine.connect() as db:
            assert identity(db, second)["serial_number"] is identity(db, second)["model"] is None
            assert db.execute(text("SELECT count(*) FROM integration.setup_receipt WHERE request_key=:key"), {"key": key}).scalar_one() == 0
        target = planned(owner)
    def send(serial):
        with signed_in(users["first"]) as client:
            return complete(client, target, {"serial_number": serial}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, ["SN-A-" + uuid4().hex, "SN-B-" + uuid4().hex])) == [200, 409]


def test_real_configuration_and_hidden_campaign_lock_identity_without_leaking_ids(environment):
    engine, users = environment
    with signed_in(users["first"]) as owner, signed_in(users["admin"]) as admin:
        configured = planned(owner)
        post(owner, f"assets/{configured['id']}/configurations", {"fingerprint": "ACTUAL-" + uuid4().hex})
        assert not option(owner, configured)["can_complete_identity"]
        assert complete(owner, configured, {"serial_number": "SN-" + uuid4().hex}).status_code == 409
        sample = planned(owner)
        plan = campaign(admin)
        with engine.begin() as db:
            db.execute(text("""INSERT INTO test.campaign_asset(campaign_id,asset_id)
                SELECT c.id,a.id FROM test.test_campaign c,test.asset a WHERE c.public_id=:campaign AND a.public_id=:asset"""),
                {"campaign": plan["id"], "asset": sample["id"]})
            db.execute(text("DELETE FROM iam.user_campaign_access WHERE campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:id) AND user_id=(SELECT id FROM iam.app_user WHERE username=:name)"),
                {"id": plan["id"], "name": users["first"]})
        assert all(row["id"] != plan["id"] for row in owner.get("/api/v1/setup/campaigns").json()["data"])
        item = option(owner, sample)
        assert not item["can_complete_identity"] and plan["id"] not in item["blocked_reason"]
        response = complete(owner, sample, {"serial_number": "SN-" + uuid4().hex})
        assert response.status_code == 409 and response.json()["error"]["code"] == "asset_identity_locked"
        assert plan["id"] not in response.text


def test_uncommitted_fk_reference_blocks_then_completion_sees_committed_usage(environment):
    engine, users = environment
    with signed_in(users["first"]) as owner, signed_in(users["admin"]) as admin:
        sample, plan = planned(owner), campaign(admin)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.begin() as db:
            db.execute(text("""INSERT INTO test.campaign_asset(campaign_id,asset_id)
                SELECT c.id,a.id FROM test.test_campaign c,test.asset a WHERE c.public_id=:campaign AND a.public_id=:asset"""),
                {"campaign": plan["id"], "asset": sample["id"]})
            def bind():
                with signed_in(users["first"]) as client:
                    return complete(client, sample, {"serial_number": "SN-" + uuid4().hex})
            future = pool.submit(bind)
            time.sleep(.2)
            assert not future.done(), "identity write must wait for the asset FK lock"
        response = future.result(timeout=15)
        assert response.status_code == 409, response.text
    with engine.connect() as db:
        assert identity(db, sample)["serial_number"] is None


def test_classified_archive_uses_real_context_and_locks_sample_identity(environment):
    from test_archive_imports import import_payload
    from test_archives_integration import upload
    engine, users = environment
    with signed_in(users["admin"]) as admin:
        sample, plan = planned(admin), campaign(admin)
        code = "IDENTITY-" + uuid4().hex
        station = post(admin, "stations", {"site_code": code, "site_name": code,
            "lab_code": code, "lab_name": code, "station_code": code, "name": code,
            "station_type": "WHOLE_MACHINE"})
        catalog = admin.get("/api/v1/setup/catalog").json()["data"]
        case = next(row for row in catalog if row["asset_kind"] == "WHOLE_MACHINE" and row["enabled"] and row["version_id"])
        linked = post(admin, f"campaigns/{plan['id']}/contexts", {"asset_id": sample["id"],
            "configuration_id": sample["configuration_id"], "test_case_version_id": case["version_id"],
            "station_id": station["id"], "cycle_code": code})
        raw = f"time_s,j_{uuid4().hex}_pos_rad\n0,0\n1,1\n".encode()
        response = admin.post("/api/v1/archive-imports", json={**import_payload(station["id"], raw),
            "classification": linked["context"]}, headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 201, response.text
        archive = upload(admin, response.json()["data"]["archive"], raw)
        assert archive["processing_state"] == "READY" and archive["classification_state"] == "CLASSIFIED"
        assert complete(admin, sample, {"serial_number": "SN-" + uuid4().hex}).status_code == 409
        with engine.connect() as db:
            assert db.execute(text("""SELECT payload->>'asset_id' FROM integration.archive_classification
                WHERE archive_id=(SELECT id FROM integration.execution_archive WHERE public_id=:id)"""),
                {"id": archive["id"]}).scalar_one() == sample["id"]
            assert db.execute(text("SELECT count(*) FROM test.test_execution WHERE asset_id=(SELECT id FROM test.asset WHERE public_id=:id)"),
                {"id": sample["id"]}).scalar_one() == 0


def test_identity_helper_is_read_only_scope_checked_and_not_public(environment):
    engine, _ = environment
    with engine.connect() as db:
        row = db.execute(text("""SELECT p.prosecdef,p.proconfig,
            (SELECT count(*) FROM aclexplode(p.proacl) x WHERE x.grantee=0 AND x.privilege_type='EXECUTE') AS public_execute
            FROM pg_proc p WHERE p.oid='test.asset_identity_blocked_reason(bigint)'::regprocedure""")).mappings().one()
        assert row["prosecdef"] and row["public_execute"] == 0 and row["proconfig"] == ["search_path=pg_catalog"]
