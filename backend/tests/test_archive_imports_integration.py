"""Real HTTP/SQL intake, digest concurrency and publication boundaries; isolated DB only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.schemas.archives import ArchiveCreate
from app.services.archive_storage import ArchiveStorage
from test_archive_imports import import_payload
from test_archives import archive_payload
from test_archives_integration import create, headers, upload, verify, submit, classify
from test_hmi_presence_integration import environment, signed_in
from test_submissions_integration import context, review

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated CSV import database required")]


def sample_bytes():
    return f"time_s,j{uuid4().hex}_pos_rad\n0,0\n1,9\n2,-4\n".encode()


def intake(client, payload, key=None):
    response = client.post("/api/v1/archive-imports", json=payload, headers=headers(key))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def quick_payload(item, ctx):
    return {"verification": {"expected_revision": item["revision"], "tester": "真实测试人", "station_id": ctx["station_id"],
            "verdict": "passed", "notes": "核对现场记录，3kg负载测试完成", "declared_completed_cycles": 82, "declared_exception_count": 0},
            "context": {key: ctx[key] for key in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")},
            "expected_submission_revision": item["submission_revision"] or 0}


def test_compact_intake_is_atomic_idempotent_owner_only_and_supports_rejection(environment):
    engine, users = environment
    with signed_in(users["admin"]) as author, signed_in(users["first"]) as other, signed_in(users["viewer"]) as viewer:
        ctx = context(author)
        raw = sample_bytes()
        original = upload(author, intake(author, {**import_payload(ctx["station_id"], raw), "filename": f"LOCAL_PERF-ARM-001_260101120000123456_{uuid4().hex}.csv"})["archive"], raw)
        route = f"/api/v1/archives/{original['id']}/verify-and-submit"
        payload = quick_payload(original, ctx)
        assert other.post(route, json=payload, headers=headers()).status_code == 403
        assert viewer.post(route, json=payload, headers=headers()).status_code == 403
        for invalid in ({**payload, "context": {**payload["context"], "asset_id": str(uuid4())}},
                        {**payload, "verification": {**payload["verification"], "notes": ""}},
                        {**payload, "verification": {**payload["verification"], "declared_completed_cycles": None}}):
            response = author.post(route, json=invalid, headers=headers())
            assert response.status_code == 422, response.text
            unchanged = author.get(f"/api/v1/archives/{original['id']}").json()["data"]
            assert unchanged["revision"] == original["revision"] and not unchanged["verification_history"]
            assert not unchanged["classification_history"] and unchanged["submission_id"] is None
        key = uuid4().hex
        response = author.post(route, json=payload, headers=headers(key))
        assert response.status_code == 200, response.text
        pending = response.json()["data"]
        assert pending["submission_status"] == "PENDING" and pending["revision"] == original["revision"] + 2
        assert len(pending["verification_history"]) == len(pending["classification_history"]) == 1
        assert pending["current_verification"]["discrepancy_reason"] == payload["verification"]["notes"]
        assert pending["current_verification"]["timing_evidence"] == original["detected_timing"]
        assert author.post(route, json=payload, headers=headers(key)).json()["data"]["revision"] == pending["revision"]
        assert author.post(route, json={**payload, "context": {**payload["context"], "asset_id": str(uuid4())}}, headers=headers(key)).status_code == 409
        assert author.post(route, json=payload, headers=headers()).status_code == 409
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:id"), {"id": original["execution_uuid"]}).scalar_one() == 0
        assert review(author, {"id": pending["submission_id"], "revision": 1}, "REJECTED").status_code == 200
        rejected = author.get(f"/api/v1/archives/{original['id']}").json()["data"]
        corrected = quick_payload(rejected, ctx)
        corrected["verification"]["notes"] = "按驳回意见重新核实"
        response = author.post(route, json=corrected, headers=headers())
        assert response.status_code == 200, response.text
        pending2 = response.json()["data"]
        assert pending2["submission_revision"] == 2 and len(pending2["verification_history"]) == 2
        assert review(author, {"id": pending2["submission_id"], "revision": 2}).status_code == 200
        final = author.get(f"/api/v1/archives/{original['id']}").json()["data"]
        assert final["facts"] == original["facts"] and final["manifest"] == original["manifest"] and final["sha256"] == original["sha256"]


def test_compact_intake_concurrent_same_receipt_does_not_repeat_versions(environment):
    _, users = environment
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        raw = sample_bytes()
        item = upload(author, intake(author, {**import_payload(ctx["station_id"], raw), "filename": f"LOCAL_PERF-ARM-001_260101120000000000_{uuid4().hex}.csv"})["archive"], raw)
    payload, key, barrier = quick_payload(item, ctx), uuid4().hex, Barrier(2)
    def send(_):
        with signed_in(users["admin"]) as client:
            barrier.wait(timeout=10)
            response = client.post(f"/api/v1/archives/{item['id']}/verify-and-submit", json=payload, headers=headers(key))
            assert response.status_code == 200, response.text
            return response.json()["data"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(send, range(2)))
    assert receipts[0]["submission_id"] == receipts[1]["submission_id"]
    assert all(row["revision"] == item["revision"]+2 and len(row["verification_history"]) == 1 for row in receipts)


def test_import_resume_digest_duplicate_permissions_and_unknown_facts(environment):
    engine, users = environment
    raw = sample_bytes()
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author, signed_in(users["second"]) as other, signed_in(users["viewer"]) as viewer:
        ctx = context(admin)
        payload = import_payload(ctx["station_id"], raw)
        lookup = {k: payload[k] for k in ("sha256", "size_bytes")}
        assert author.get("/api/v1/archive-imports/lookup", params=lookup).json()["data"] == {"archive": None}
        key = uuid4().hex
        first = intake(author, payload, key)
        item = first["archive"]
        assert not first["reused"] and item["origin"] == "HISTORY"
        assert item["classification_state"] == "UNCLASSIFIED" and item["current_verification"] is None
        assert item["station_label"].startswith("AP-")
        assert item["facts"]["active_seconds"] is None and item["facts"]["completed_cycles"] is None
        assert item["facts"]["exception_count"] is None and item["facts"]["machine_status"] == "unknown"
        assert intake(author, payload, key)["archive"]["execution_uuid"] == item["execution_uuid"]
        assert author.post("/api/v1/archive-imports", json={**payload, "test_name": "different"}, headers=headers(key)).status_code == 409
        first_chunk = raw[:20]
        put = author.put(f"/api/v1/archives/{item['id']}/content?offset=0", content=first_chunk,
                         headers={**headers(), "Content-Type": "application/octet-stream"})
        assert put.status_code == 200, put.text
        retry = intake(author, {**payload, "filename": "renamed.csv", "test_name": "do not replace"})
        assert retry["reused"] and retry["archive"]["received_bytes"] == 20
        assert retry["archive"]["filename"] == payload["filename"] and retry["archive"]["test_name"] == payload["test_name"]
        # Resume at the acknowledged offset; chunk retry cannot change its bounds.
        resumed = author.put(f"/api/v1/archives/{item['id']}/content?offset=20", content=raw[20:],
                             headers={**headers(), "Content-Type": "application/octet-stream"})
        assert resumed.status_code == 200, resumed.text
        item = upload(author, item, raw)
        assert item["processing_state"] == "READY", item
        assert item["manifest"]["analysis"]["verdict"] == "blocked"
        assert item["manifest"]["row_count"] == 3
        assert ArchiveStorage(get_settings().archive_root).path(item["execution_uuid"], "raw.csv").read_bytes() == raw
        assert author.get(f"/api/v1/archives/{item['execution_uuid']}/curves?metrics=pos_rad").status_code == 200
        check = verify(author, item, ctx["station_id"], "blocked")
        assert check.status_code == 200, check.text
        item = check.json()["data"]
        assert item["current_verification"]["tester"] != item["owner_name"]
        blocked = submit(author, item)
        assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "invalid_archive"
        assert viewer.get("/api/v1/archive-imports/lookup", params=lookup).status_code == 403
        assert viewer.post("/api/v1/archive-imports", json=payload, headers=headers()).status_code == 403
        visible = other.get("/api/v1/archive-imports/lookup", params=lookup).json()["data"]["archive"]
        assert visible["id"] == item["id"] and visible["can_edit"] is False
        assert other.put(f"/api/v1/archives/{item['id']}/content?offset=0", content=raw,
                         headers={**headers(), "Content-Type": "application/octet-stream"}).status_code == 403
        reused = intake(author, payload)
        assert reused["archive"]["revision"] == item["revision"] and reused["archive"]["current_verification"] == item["current_verification"]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM integration.execution_archive WHERE sha256=:sha"), {"sha": payload["sha256"]}).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM integration.data_submission WHERE execution_uuid=:uuid"), {"uuid": item["execution_uuid"]}).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:uuid"), {"uuid": item["execution_uuid"]}).scalar_one() == 0


def test_import_csrf_invalid_station_and_client_fact_injection(environment):
    _, users = environment
    with signed_in(users["admin"]) as admin:
        ctx = context(admin)
        payload = import_payload(ctx["station_id"], sample_bytes())
        csrf = admin.headers.pop("X-CSRF-Token")
        assert admin.post("/api/v1/archive-imports", json=payload, headers=headers()).status_code == 403
        admin.headers["X-CSRF-Token"] = csrf
        for key in ("facts", "active_seconds", "execution_uuid", "acceptance"):
            assert admin.post("/api/v1/archive-imports", json={**payload, key: {}}, headers=headers()).status_code == 422
        assert admin.post("/api/v1/archive-imports", json={**payload, "station_id": str(uuid4())}, headers=headers()).status_code == 422
        assert admin.get("/api/v1/archive-imports/lookup", params={"sha256": "bad", "size_bytes": 1}).status_code == 422


def test_declared_history_enters_approval_and_preserves_raw_facts_and_rejected_versions(environment):
    engine, users = environment
    raw = sample_bytes()
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author, signed_in(users["second"]) as reviewer:
        ctx = context(admin)
        item = upload(author, intake(author, import_payload(ctx["station_id"], raw))["archive"], raw)
        original_facts, original_manifest = item["facts"], item["manifest"]
        declaration = {"expected_revision": item["revision"], "tester": "实际测试人", "station_id": ctx["station_id"],
            "verdict": "passed", "notes": "根据原始试验记录补录", "discrepancy_reason": "原文件未含机器终态，人工核实测试完成",
            "declared_started_at": "2026-10-01T00:00:00Z", "declared_ended_at": "2026-10-01T00:01:00Z",
            "declared_active_seconds": 55, "declared_completed_cycles": 3, "declared_exception_count": 0,
            "reason": "历史测试人工核实"}
        key = uuid4().hex
        checked = author.post(f"/api/v1/archives/{item['id']}/verification", json=declaration, headers=headers(key))
        assert checked.status_code == 200, checked.text
        item = checked.json()["data"]
        assert author.post(f"/api/v1/archives/{item['id']}/verification", json=declaration, headers=headers(key)).json()["data"]["revision"] == item["revision"]
        assert item["facts"] == original_facts and item["manifest"] == original_manifest
        classified = classify(author, item, ctx)
        assert classified.status_code == 200, classified.text
        item = classified.json()["data"]
        pending = submit(author, item)
        assert pending.status_code == 200, pending.text
        item = pending.json()["data"]
        assert item["submission_status"] == "PENDING" and item["facts"] == original_facts
        receipt = author.get(f"/api/v1/submissions/{item['submission_id']}").json()["data"]
        frozen = receipt["revisions"][0]
        assert frozen["summary"]["active_seconds"] == 55 and frozen["summary"]["completed_cycles"] == 3
        assert frozen["summary"]["source_status"] == "passed" and "人工申报" in frozen["summary"]["summary"]
        assert frozen["verification"]["declared_active_seconds"] == 55
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:uuid"), {"uuid": item["execution_uuid"]}).scalar_one() == 0
        blocked_edit = {**declaration, "expected_revision": item["revision"], "declared_active_seconds": 50}
        assert author.post(f"/api/v1/archives/{item['id']}/verification", json=blocked_edit, headers=headers()).status_code == 409
        rejected = review(reviewer, {"id": item["submission_id"], "revision": 1}, "REJECTED")
        assert rejected.status_code == 200, rejected.text
        item = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        revised = author.post(f"/api/v1/archives/{item['id']}/verification", json={**blocked_edit, "expected_revision": item["revision"], "reason": "按驳回意见核实申报时长"}, headers=headers())
        assert revised.status_code == 200, revised.text
        resubmitted = submit(author, revised.json()["data"], expected=1)
        assert resubmitted.status_code == 200, resubmitted.text
        item = resubmitted.json()["data"]
        assert item["submission_revision"] == 2
        approved = review(author, {"id": item["submission_id"], "revision": 2})
        assert approved.status_code == 200, approved.text
        final = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert final["submission_status"] == "APPROVED" and final["facts"] == original_facts
        assert final["manifest"] == original_manifest and final["sha256"] == import_payload(ctx["station_id"], raw)["sha256"]
        versions = author.get(f"/api/v1/submissions/{item['submission_id']}").json()["data"]["revisions"]
        assert [version["summary"]["active_seconds"] for version in versions] == [55, 50]
        with engine.connect() as conn:
            interval = conn.execute(text("SELECT source_kind,source_method,active_seconds FROM test.runtime_interval WHERE execution_id=(SELECT id FROM test.test_execution WHERE acquisition_uuid=:uuid) AND NOT voided"), {"uuid": item["execution_uuid"]}).mappings().one()
            assert interval["source_kind"] == "MANUAL" and interval["source_method"] == "approved-summary-unqualified"
            assert interval["active_seconds"] == 50


def test_filename_csv_timing_is_server_derived_and_frozen_with_approval(environment):
    engine, users = environment
    raw = f"time_s,j{uuid4().hex}_pos_rad\n0.0500576389968046,0\n1,9\n2.8765431123,-4\n".encode()
    with signed_in(users["admin"]) as author, signed_in(users["viewer"]) as viewer:
        ctx = context(author)
        payload = {**import_payload(ctx["station_id"], raw), "filename": f"LOCAL_PERF-ARM-001_260101235959123456_{uuid4().hex}.csv"}
        item = upload(author, intake(author, payload)["archive"], raw)
        original_facts, original_manifest = item["facts"], item["manifest"]
        detected = item["detected_timing"]
        assert detected["started_at"] == "2026-01-01T23:59:59.123456Z"
        assert detected["first_sample_at"] == "2026-01-01T23:59:59.173514Z"
        assert detected["ended_at"] == "2026-01-02T00:00:01.999999Z"
        assert detected["recorded_seconds"] == 2.876543
        assert detected["evidence_sha256"] == payload["sha256"] and detected["filename"] == payload["filename"]
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code == 404
        # Supplying different valid dates cannot replace the server calculation.
        declaration = {"expected_revision": item["revision"], "tester": "实际测试人", "station_id": ctx["station_id"],
            "verdict": "passed", "notes": "核对文件名与采样时间", "discrepancy_reason": "人工核实完成",
            "declared_started_at": "2026-01-01T00:00:00Z", "declared_ended_at": "2026-01-01T00:01:00Z",
            "declared_active_seconds": 59, "declared_completed_cycles": 1, "declared_exception_count": 0, "reason": "核实历史记录"}
        assert author.post(f"/api/v1/archives/{item['id']}/verification", json={**declaration, "timing_evidence": detected}, headers=headers()).status_code == 422
        key = uuid4().hex
        checked = author.post(f"/api/v1/archives/{item['id']}/verification", json=declaration, headers=headers(key))
        assert checked.status_code == 200, checked.text
        item = checked.json()["data"]
        assert author.post(f"/api/v1/archives/{item['id']}/verification", json=declaration, headers=headers(key)).json()["data"]["revision"] == item["revision"]
        verification = item["current_verification"]
        assert verification["declared_started_at"] == detected["started_at"]
        assert verification["declared_ended_at"] == detected["ended_at"]
        assert verification["declared_active_seconds"] == detected["recorded_seconds"]
        assert verification["timing_evidence"] == detected
        assert item["facts"] == original_facts and item["manifest"] == original_manifest
        item = classify(author, item, ctx).json()["data"]
        pending = submit(author, item)
        assert pending.status_code == 200, pending.text
        item = pending.json()["data"]
        frozen = author.get(f"/api/v1/submissions/{item['submission_id']}").json()["data"]["revisions"][0]
        assert frozen["verification"]["timing_evidence"] == detected
        assert frozen["summary"]["active_seconds"] == 2.876543
        assert review(author, {"id": item["submission_id"], "revision": 1}).status_code == 200
        final = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert final["facts"] == original_facts and final["manifest"] == original_manifest
        assert final["current_verification"]["timing_evidence"] == detected
        with engine.connect() as conn:
            interval = conn.execute(text("SELECT source_kind,source_method FROM test.runtime_interval WHERE execution_id=(SELECT id FROM test.test_execution WHERE acquisition_uuid=:uuid) AND NOT voided"), {"uuid": item["execution_uuid"]}).mappings().one()
            assert interval["source_kind"] == "MANUAL" and interval["source_method"] == "approved-summary-unqualified"


@pytest.mark.parametrize("stamp,times", [
    (None, "0\n1\n2"), ("260230120000000000", "0\n1\n2"),
    ("260101240000000000", "0\n1\n2"), ("991001120000000000", "0\n1\n2"),
    ("260101120000000000", "0\ninvalid\n2"), ("260101120000000000", "0\n2\n1"),
    ("260101120000000000", "0\n1\n1"), ("260101120000000000", "0"),
])
def test_filename_timing_refuses_ambiguous_names_or_invalid_sampling(environment, stamp, times):
    _, users = environment
    raw = (f"time_s,j{uuid4().hex}_pos_rad\n" + "".join(f"{value},0\n" for value in times.splitlines())).encode()
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        payload = import_payload(ctx["station_id"], raw)
        payload["filename"] = f"LOCAL_PERF-ARM-001_{stamp}_{uuid4().hex}.csv" if stamp else "original.csv"
        item = upload(author, intake(author, payload)["archive"], raw)
        assert item["processing_state"] == "READY", item
        assert item["detected_timing"] is None
        assert item["facts"]["started_at"] is None and item["facts"]["active_seconds"] is None


def test_same_csv_as_approved_hmi_record_reuses_frozen_execution(environment):
    _, users = environment
    raw = sample_bytes()
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        formal = {k: ctx[k] for k in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}
        item = upload(author, create(author, archive_payload(context=formal, raw=raw)), raw)
        checked = verify(author, item, ctx["station_id"])
        assert checked.status_code == 200, checked.text
        submitted = submit(author, checked.json()["data"])
        assert submitted.status_code == 200, submitted.text
        pending = submitted.json()["data"]
        approved = review(author, {"id": pending["submission_id"], "revision": pending["submission_revision"]})
        assert approved.status_code == 200, approved.text
        original = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        reused = intake(author, import_payload(ctx["station_id"], raw))
        assert reused["reused"] and reused["archive"]["id"] == original["id"]
        assert reused["archive"]["can_edit"] is False and reused["archive"]["submission_status"] == "APPROVED"
        for key in ("origin", "execution_uuid", "execution_id", "facts", "revision", "classification", "current_verification", "test_name", "filename"):
            assert reused["archive"][key] == original[key]


def test_concurrent_imports_share_one_identity_and_database_gate_blocks_fabrication(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin:
        ctx = context(admin)
    payload = import_payload(ctx["station_id"], sample_bytes())
    payload["classification"] = {k: ctx[k] for k in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}
    with engine.connect() as conn:
        actor = conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"), {"name": users["admin"]}).scalar_one()
    app_engine = create_engine(os.environ["DATABASE_URL"])
    barrier = Barrier(2)
    def initialize(_):
        with app_engine.begin() as conn:
            conn.execute(text("SELECT iam.set_request_context(:actor,:request,'isolated import concurrency')"), {"actor": actor, "request": uuid4().hex})
            barrier.wait(timeout=10)
            return dict(conn.execute(text("SELECT * FROM integration.archive_import(CAST(:payload AS jsonb),:key)"),
                {"payload": json.dumps(payload), "key": uuid4().hex}).mappings().one())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, range(2)))
    assert results[0]["archive_id"] == results[1]["archive_id"] and sorted(r["reused"] for r in results) == [False, True]
    def rejected(sql, p):
        with pytest.raises(DBAPIError) as error:
            with app_engine.begin() as conn:
                conn.execute(text("SELECT iam.set_request_context(:actor,:request,'isolated malicious client')"), {"actor": actor, "request": uuid4().hex})
                conn.execute(text(sql), p)
        assert error.value.orig.sqlstate == "22023"
    rejected("SELECT * FROM integration.archive_import(CAST(:payload AS jsonb),:key)",
             {"payload": json.dumps({**payload, "facts": {"active_seconds": 0}}), "key": uuid4().hex})
    with engine.connect() as conn:
        internal_id = conn.execute(text("SELECT id FROM integration.execution_archive WHERE public_id=:id"), {"id": results[0]["archive_id"]}).scalar_one()
    rejected("SELECT integration.archive_summary(:id)", {"id": internal_id})
    legacy = ArchiveCreate.model_validate(archive_payload(origin="HISTORY")).model_dump(mode="json")
    legacy["facts"]["active_seconds"] = None
    rejected("SELECT integration.archive_create(CAST(:payload AS jsonb),:key)", {"payload": json.dumps(legacy), "key": uuid4().hex})
    app_engine.dispose()
