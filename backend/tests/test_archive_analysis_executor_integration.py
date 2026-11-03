"""Published-definition reads, append-only analysis and publication isolation.

Uses real session cookies and application RLS on a disposable PostgreSQL only.
Published rules are never edited as a fixture: new definitions are new versions.
"""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from test_archive_analysis import RULES
from test_archive_analysis_integration import published_version
from test_archive_publication_integration import planned
from test_archives import archive_payload
from test_archives_integration import create, headers, submit, upload, verify
from test_hmi_presence_integration import environment, signed_in
from test_submissions_integration import context, mtbf_seconds, review


pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("HMI_TEST_ADMIN_URL"), reason="dedicated disposable database required")]
MEASURED_CSV = (b"time_s,joint_cmd_pos_rad,joint_pos_rad,joint_temp_c,joint_torque_nm\n"
                b"0,0,0,20,0\n1,0,0,120,0\n2,0,0,20,0\n")


def archive(owner, ctx):
    item = upload(owner, create(owner, archive_payload(context=planned(ctx), raw=MEASURED_CSV)), MEASURED_CSV)
    checked = verify(owner, item, ctx["station_id"])
    assert checked.status_code == 200, checked.text
    return checked.json()["data"]


def test_published_source_remains_readable_without_setup_update_privilege(environment):
    admin_engine, users = environment
    app_engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with signed_in(users["admin"]) as admin:
            ctx = context(admin)
            version = published_version(admin_engine, ctx, {"archive_acceptance": RULES})
        with admin_engine.connect() as conn:
            actor = conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"),
                                 {"name": users["first"]}).scalar_one()
        with app_engine.begin() as conn:
            conn.execute(text("SELECT iam.set_request_context(:actor,'analysis-rls-qa','Synthetic published read')"), {"actor": actor})
            query = """SELECT v.public_id FROM catalog.test_case_version v
                JOIN catalog.test_case tc ON tc.id=v.test_case_id
                WHERE v.public_id=:id AND v.status='PUBLISHED' AND tc.enabled"""
            assert str(conn.execute(text(query), {"id": version}).scalar_one()) == version
            # The writer policy remains restrictive. The application must use
            # the read policy rather than granting this executor setup writes.
            assert conn.execute(text(query + " FOR SHARE OF v,tc"), {"id": version}).first() is None
    finally:
        app_engine.dispose()


def test_executor_append_analysis_keeps_approved_result_and_published_rules_frozen(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as owner, \
            signed_in(users["second"]) as stranger, signed_in(users["viewer"]) as viewer:
        ctx = context(admin)
        item = submit(owner, archive(owner, ctx)).json()["data"]
        approved = review(owner, {"id": item["submission_id"], "revision": item["submission_revision"]})
        assert approved.status_code == 200, approved.text
        before = owner.get(f"/api/v1/archives/{item['id']}").json()["data"]
        with engine.connect() as conn:
            original_result = conn.execute(text("""SELECT to_jsonb(r) FROM test.execution_result r
                JOIN test.test_execution e ON e.id=r.execution_id WHERE e.public_id=:id"""),
                {"id": before["execution_id"]}).scalar_one()
        version = published_version(engine, ctx, {"archive_acceptance": RULES})
        base = f"/api/v1/archives/{item['id']}/analyses"
        request = {"test_case_version_id": version, "reason": "Synthetic executor retrospective reference"}
        key = uuid4().hex
        response = owner.post(base, json=request, headers=headers(key))
        assert response.status_code == 201, response.text
        first = response.json()["data"]
        assert first["version"] == 1 and first["report"]["verdict"] == "failed"
        assert first["rules_snapshot"]["decision_rules"]["archive_acceptance"] == RULES
        assert owner.post(base, json=request, headers=headers(key)).json()["data"]["id"] == first["id"]
        assert owner.post(base, json={**request, "reason": "Changed"}, headers=headers(key)).status_code == 409
        assert stranger.post(base, json=request, headers=headers()).status_code == 403
        assert viewer.post(base, json=request, headers=headers()).status_code == 403
        assert owner.post(base, json={**request, "acceptance": RULES}, headers=headers()).status_code == 422
        # Even an administrative SQL fixture cannot rewrite published rules.
        with engine.begin() as conn:
            with pytest.raises(DBAPIError) as rejected:
                conn.execute(text("UPDATE catalog.test_case_version SET decision_rules='{}'::jsonb WHERE public_id=:id"), {"id": version})
            assert rejected.value.orig.sqlstate == "23514"
            conn.rollback()
        # Legitimate maintenance appends a separate published definition.
        next_version = published_version(engine, ctx, {"archive_acceptance": {**RULES, "max_temperature_rise_c": 150}})
        second = owner.post(base, json={**request, "test_case_version_id": next_version}, headers=headers())
        assert second.status_code == 201, second.text
        assert second.json()["data"]["version"] == 2
        assert second.json()["data"]["report"]["verdict"] == "passed"
        history = owner.get(base).json()["data"]["items"]
        assert len(history) == 2 and history[0]["rules_snapshot"] == first["rules_snapshot"]
        assert owner.get(f"/api/v1/archives/{item['id']}").json()["data"] == before
        with engine.connect() as conn:
            assert conn.execute(text("""SELECT to_jsonb(r) FROM test.execution_result r
                JOIN test.test_execution e ON e.id=r.execution_id WHERE e.public_id=:id"""),
                {"id": before["execution_id"]}).scalar_one() == original_result


def test_reference_analysis_cannot_publish_pending_or_rejected_measurements(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as owner:
        ctx = context(admin)
        item = submit(owner, archive(owner, ctx)).json()["data"]
        version = published_version(engine, ctx, {"archive_acceptance": RULES})
        base = f"/api/v1/archives/{item['id']}/analyses"
        for status in ("PENDING", "REJECTED"):
            if status == "REJECTED":
                rejected = review(owner, {"id": item["submission_id"], "revision": item["submission_revision"]}, "REJECTED")
                assert rejected.status_code == 200, rejected.text
            before = owner.get(f"/api/v1/archives/{item['id']}").json()["data"]
            assert before["submission_status"] == status and before["execution_id"] is None
            response = owner.post(base, json={"test_case_version_id": version, "reason": "Synthetic reference while " + status}, headers=headers())
            assert response.status_code == 201, response.text
            assert response.json()["data"]["report"]["scope"] == "RETROSPECTIVE_REFERENCE_ONLY"
            assert owner.get(f"/api/v1/archives/{item['id']}").json()["data"] == before
            assert mtbf_seconds(engine, users["admin"], ctx["campaign_id"]) == 0
            with engine.connect() as conn:
                assert conn.execute(text("""SELECT count(*) FROM test.test_execution e
                    JOIN test.test_campaign c ON c.id=e.campaign_id WHERE c.public_id=:id"""),
                    {"id": ctx["campaign_id"]}).scalar_one() == 0
