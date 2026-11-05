"""Home query equivalence and deadline failures in a disposable PostgreSQL only."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import os
from time import monotonic
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

import app.api.routes.dashboard as routes
from app.core.db import engine as app_engine
from app.core.errors import ApiError
from app.repositories.read_models import ReadModelRepository
from app.services.dashboard_queries import dashboard_home_deadline
from test_hmi_presence_integration import environment, signed_in
from test_setup_integration import data, post

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("HMI_TEST_ADMIN_URL"), reason="disposable database required"
)]


@contextmanager
def actor_session(admin_engine, username):
    with admin_engine.connect() as connection:
        actor = str(connection.execute(text(
            "SELECT public_id FROM iam.app_user WHERE username=:name"
        ), {"name": username}).scalar_one())
    with Session(app_engine) as session:
        with session.begin():
            session.execute(text("SELECT iam.set_request_context(:actor,'home-qa','isolated home regression')"), {"actor": actor})
            yield session


def test_cumulative_deadline_cancels_sql_and_pool_connection_is_clean(environment):
    admin_engine, users = environment
    with actor_session(admin_engine, users["admin"]) as session:
        connection = session.connection()
        original_timeout = connection.execute(text("SHOW statement_timeout")).scalar_one()
        backend_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
        listener_count = len(connection.dispatch.before_cursor_execute)
        started = monotonic()
        with pytest.raises(ApiError) as failure:
            with dashboard_home_deadline(session, seconds=0.30):
                session.execute(text("SELECT pg_sleep(0.18)"))
                session.execute(text("SELECT pg_sleep(0.18)"))
        assert len(connection.dispatch.before_cursor_execute) == listener_count
        assert failure.value.status_code == 503
        assert failure.value.code == "dashboard_home_timeout"
        assert monotonic() - started < 1.0
        # The failed request must roll back before this pooled connection is reused.
        session.rollback()
    with actor_session(admin_engine, users["admin"]) as reused:
        assert reused.execute(text("SELECT pg_backend_pid()")).scalar_one() == backend_pid
        assert reused.execute(text("SHOW statement_timeout")).scalar_one() == original_timeout
        assert reused.execute(text("SELECT 42")).scalar_one() == 42
        reused.execute(text("SELECT pg_sleep(0.04)"))


def test_home_http_timeout_is_503_and_next_request_succeeds(environment, monkeypatch):
    _, users = environment
    original = ReadModelRepository.dashboard_home

    def delayed(self):
        self.session.execute(text("SELECT pg_sleep(0.2)"))
        return original(self)

    with signed_in(users["admin"]) as admin:
        with monkeypatch.context() as patch:
            patch.setattr(routes, "dashboard_home_deadline", partial(dashboard_home_deadline, seconds=0.05))
            patch.setattr(ReadModelRepository, "dashboard_home", delayed)
            response = admin.get("/api/v1/dashboard/home")
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "dashboard_home_timeout"
        response = admin.get("/api/v1/dashboard/home")
        assert response.status_code == 200, response.text


def _synthetic_facts(admin_engine, users):
    # These are explicitly synthetic fixture facts, never production imports.
    tag = uuid4().hex[:8].upper()
    with signed_in(users["admin"]) as admin:
        samples = [post(admin, "assets", {
            "asset_code": f"RP1.3-LEG-{i:03d}", "asset_kind": "MODULE",
            "target_part_code": "LEG", "product_family": "QA", "serial_number": uuid4().hex,
            "display_name": "Synthetic home sample", "model": "QA", "configuration_fingerprint": "QA-"+tag,
        }) for i in (1, 2)]
        program = post(admin, "programs", {"program_code": "HOME-"+tag, "name": "Synthetic home QA"})
        campaign = post(admin, "campaigns", {"program_id": program["id"], "campaign_code": "HOME-"+tag, "name": "Synthetic home QA", "asset_kind": "MODULE"})
        station = post(admin, "stations", {"site_code": "HOME-"+tag, "site_name": "QA", "lab_code": "HOME-"+tag, "lab_name": "QA", "station_code": "B-HOME-"+tag, "name": "QA", "station_type": "MODULE"})
        case = next(item for item in data(admin, "setup/catalog") if item["target_part_code"] == "LEG" and item["enabled"] and item["version_id"])
        ctx = post(admin, f"campaigns/{campaign['id']}/contexts", {"asset_id": samples[0]["id"], "configuration_id": samples[0]["configuration_id"], "test_case_version_id": case["version_id"], "station_id": station["id"], "cycle_code": "HOME-"+tag})["context"]
    with admin_engine.begin() as db:
        db.execute(text("SELECT iam.set_request_context(public_id,'home-fixture','isolated synthetic fixture') FROM iam.app_user WHERE username=:name"), {"name": users["admin"]})
        ids = {}
        for field, table in (("campaign_id", "test.test_campaign"), ("asset_id", "test.asset"), ("configuration_id", "test.configuration_snapshot"), ("test_case_version_id", "catalog.test_case_version"), ("station_id", "test.station"), ("cycle_id", "test.test_cycle")):
            ids[field] = db.execute(text(f"SELECT id FROM {table} WHERE public_id=:id"), {"id": ctx[field]}).scalar_one()
        for number, (domain, disposition, seconds, voided) in enumerate([
            ("RESEARCH", "INCLUDED", 10, False), ("RESEARCH", "EXCLUDED", 20, False),
            ("RESEARCH", "PENDING", 30, False), ("FACTORY", "INCLUDED", 40, False),
            ("UNCLASSIFIED", "INCLUDED", 50, False), ("RESEARCH", "INCLUDED", 60, True),
        ]):
            execution = db.execute(text("""INSERT INTO test.test_execution(
                execution_code,campaign_id,asset_id,configuration_snapshot_id,test_case_version_id,station_id,cycle_id,status,
                received_at,normalized_started_at,normalized_ended_at)
                VALUES(:code,:campaign_id,:asset_id,:configuration_id,:test_case_version_id,:station_id,:cycle_id,'COMPLETED',
                '2026-10-01T00:01:00Z','2026-10-01T00:00:00Z','2026-10-01T00:01:00Z') RETURNING id"""), {**ids, "code": f"HOME-{tag}-{number}"}).scalar_one()
            db.execute(text("""INSERT INTO integration.execution_domain_evidence(execution_id,data_domain,data_domain_source,evidence)
                VALUES(:execution,:domain,'EXPLICIT','{}')"""), {"execution": execution, "domain": domain})
            interval = db.execute(text("""INSERT INTO test.runtime_interval(campaign_id,asset_id,execution_id,source_kind,
                active_seconds,clock_quality,data_quality,source_method,received_at,voided,void_reason,voided_at)
                VALUES(:campaign_id,:asset_id,:execution,'LEGACY_REPORTED',:seconds,'VALID','VALID','synthetic-home-qa',
                '2026-10-01T00:01:00Z',:voided,CASE WHEN :voided THEN 'synthetic voided fact' ELSE '' END,
                CASE WHEN :voided THEN clock_timestamp() END) RETURNING id"""), {**ids, "execution": execution, "seconds": seconds, "voided": voided}).scalar_one()
            if disposition != "PENDING":
                assessment = db.execute(text("""INSERT INTO reliability.exposure_assessment(campaign_id,asset_id,runtime_interval_id,
                    eligible,assignment_status,quality_status,assessment_method)
                    VALUES(:campaign_id,:asset_id,:interval,:eligible,'CONFIRMED','VALID','synthetic-home-qa') RETURNING id"""), {**ids, "interval": interval, "eligible": disposition == "INCLUDED"}).scalar_one()
                db.execute(text("""INSERT INTO reliability.exposure_scope_assignment(campaign_id,assessment_id,scope_id,inclusion_status)
                    SELECT :campaign_id,:assessment,id,:disposition FROM reliability.mtbf_scope
                    WHERE asset_kind='MODULE' AND scope_code='OVERALL' AND status='PUBLISHED'
                    ORDER BY version DESC LIMIT 1"""), {**ids, "assessment": assessment, "disposition": disposition})
        db.execute(text("DELETE FROM iam.user_campaign_access WHERE user_id=(SELECT id FROM iam.app_user WHERE username=:name) AND campaign_id=:campaign_id"), {**ids, "name": users["second"]})
    return ids


def test_materialized_home_preserves_scope_counts_partition_and_qualification(environment):
    admin_engine, users = environment
    _synthetic_facts(admin_engine, users)
    now = datetime.now(timezone.utc)
    for username in (users["admin"], users["second"]):
        with actor_session(admin_engine, username) as session:
            # A finite budget also applies to the old-plan comparison in this isolated fixture.
            session.execute(text("SET LOCAL statement_timeout='20s'"))
            repo = ReadModelRepository(session)
            repo._as_of = lambda: now
            current = repo.dashboard_home()
            connection = session.connection()

            def old_plan(conn, cursor, statement, parameters, context, executemany):
                statement = statement.replace("\nledger AS MATERIALIZED (", "\nledger AS (", 1)
                statement = statement.replace("execution_ledger AS MATERIALIZED (", "execution_ledger AS (", 1)
                return statement, parameters

            event.listen(connection, "before_cursor_execute", old_plan, retval=True)
            try:
                baseline = repo.dashboard_home()
            finally:
                event.remove(connection, "before_cursor_execute", old_plan)
            assert current.groups == baseline.groups
            scopes = [scope for group in current.groups for scope in group.objects]
            cases = [case for scope in scopes for case in scope.test_cases]
            assert any(case.execution_count == 0 for case in cases), "cases without executions must remain visible"
            if username == users["admin"]:
                head = next(scope for scope in scopes if scope.id == "MODULE:LEG")
                assert head.part_asset_count == 2, "the second, unexecuted asset must remain in scope"
                assert head.part_durations.total_duration_seconds == 60
                assert head.part_durations.effective_exposure_seconds == 10
                assert head.part_durations.excluded_seconds == 20
                assert head.part_durations.pending_seconds == 30
                assert sum(case.execution_count for case in cases) == 4
                # Voiding an interval does not void its immutable execution fact.
            else:
                assert sum(case.execution_count for case in cases) == 0
                assert sum(scope.part_durations.total_duration_seconds for scope in scopes) == 0
