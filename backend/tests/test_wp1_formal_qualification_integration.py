"""WP1 formal qualification, with real sessions and a disposable PostgreSQL."""
import os
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_archives import archive_payload
from test_archives_integration import RAW, create, upload, verify, classify, submit, headers
from test_archive_publication_integration import planned
from test_factory_partition_integration import factory_review, stats, ctx_with_code
from test_hmi_presence_integration import environment, signed_in
from test_submissions_integration import grant, review

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'), reason='disposable database required')]

def research_exposure_inputs(engine, username, campaign_id):
    # Inspect the actual MTBF worker input path without placing shared jobs in
    # the global queue; each check stays within its own rollback-only session.
    from datetime import datetime, timezone
    from sqlalchemy.orm import Session
    from app.services.worker import _interval_rows
    with engine.connect() as db:
        actor = db.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"), {'name':username}).scalar_one()
        db.execute(text("SELECT iam.set_request_context(:actor,'wp1-qualification-check','synthetic isolated check')"), {'actor':actor})
        cid, sid = db.execute(text("SELECT c.id,s.id FROM test.test_campaign c JOIN reliability.mtbf_scope s ON s.asset_kind=c.asset_kind AND s.scope_code='OVERALL' AND s.status='PUBLISHED' WHERE c.public_id=:id"), {'id':campaign_id}).one()
        with Session(bind=db) as session:
            now = datetime.now(timezone.utc)
            rows = _interval_rows(session,cid,sid,now,now)
            return sum(float(row['active_seconds'] or 0) for row in rows if row['disposition'] == 'INCLUDED')

@pytest.mark.parametrize('domain', ['RESEARCH', 'FACTORY'])
@pytest.mark.parametrize('actor', ['admin', 'first'])
def test_other_curve_only_pending_rejected_then_context_and_self_review(environment, domain, actor):
    engine, users = environment
    with signed_in(users['admin']) as admin, signed_in(users[actor]) as client:
        ctx = ctx_with_code(admin, engine)
        if actor != 'admin':
            grant(admin, engine, users[actor], ctx['campaign_id'])
        sn = 'wp1-' + uuid4().hex
        p = {**archive_payload(origin='OTHER', raw=RAW), 'data_domain': domain, 'bench_id':ctx['station_code']}
        if domain == 'FACTORY':
            p['serial_number'] = sn
        item = upload(client, create(client, p))
        assert item['processing_state'] == 'READY'
        assert client.get(f"/api/v1/archives/{item['id']}/curves?metrics=pos_rad").status_code == 200
        item = verify(client, item, ctx['station_id']).json()['data']
        # A measurement file and manual verification alone do not grant formal use.
        blocked = submit(client, item)
        assert blocked.status_code == 422, blocked.text
        direct = client.post('/api/v1/submissions', json={'summary':{**ctx, 'execution_uuid':item['execution_uuid']},'reason':'synthetic bypass'}, headers=headers())
        assert direct.status_code in {409,422}, direct.text
        if domain == 'FACTORY':
            assert stats(client, sn)['approved_count'] == 0
        else:
            assert research_exposure_inputs(engine, users['admin'], ctx['campaign_id']) == 0
        classified = classify(client, item, ctx)
        assert classified.status_code == 200, classified.text
        item = classified.json()['data']
        original = (item['facts'], item['sha256'], item['manifest'])
        key = uuid4().hex
        sent = submit(client, item, key=key)
        assert sent.status_code == 200, sent.text
        pending = sent.json()['data']
        assert pending['submission_status'] == 'PENDING'
        assert submit(client, item, key=key).status_code == 200
        if domain == 'FACTORY':
            assert stats(client, sn)['approved_count'] == 0
            rejected = factory_review(client, pending, 'REJECT')
        else:
            assert research_exposure_inputs(engine, users['admin'], ctx['campaign_id']) == 0
            rejected = review(client, {'id':pending['submission_id'],'revision':pending['submission_revision']}, 'REJECTED')
        assert rejected.status_code == 200, rejected.text
        item = client.get(f"/api/v1/archives/{item['id']}").json()['data']
        assert item['submission_status'] == 'REJECTED'
        if domain == 'FACTORY':
            assert stats(client, sn)['approved_count'] == 0
        else:
            assert research_exposure_inputs(engine, users['admin'], ctx['campaign_id']) == 0
        again = submit(client, item, expected=1)
        assert again.status_code == 200, again.text
        item = again.json()['data']
        assert item['submission_revision'] == 2
        if domain == 'FACTORY':
            approved = factory_review(client, item)
        else:
            approved = review(client, {'id':item['submission_id'],'revision':item['submission_revision']})
        assert approved.status_code == 200, approved.text
        item = client.get(f"/api/v1/archives/{item['id']}").json()['data']
        assert item['submission_status'] == 'APPROVED'
        assert (item['facts'],item['sha256'],item['manifest']) == original
        if domain == 'FACTORY':
            assert stats(client, sn)['approved_count'] == 1
            assert stats(client, sn)['passed_count'] == 1
        else:
            result = client.get(f"/api/v1/executions/{item['execution_id']}").json()['data']['result']
            assert result is not None


def test_factory_nominal_hmi_origin_cannot_bypass_context(environment):
    engine, users = environment
    with signed_in(users['admin']) as client:
        ctx = ctx_with_code(client, engine)
        p = {**archive_payload(raw=RAW), 'data_domain':'FACTORY', 'serial_number':uuid4().hex, 'bench_id':ctx['station_code']}
        item = upload(client, create(client,p))
        item = verify(client,item,ctx['station_id']).json()['data']
        assert submit(client,item).status_code == 422
        assert stats(client,p['serial_number'])['approved_count'] == 0


def test_predecessor_functions_cannot_be_called_by_app_role(environment):
    engine, _ = environment
    with engine.connect() as db:
        for function in ['integration.factory_summary_pre_wp1(bigint)', 'integration.archive_summary_pre_wp1(bigint)', 'integration.factory_summary(bigint)']:
            assert not db.execute(text("SELECT has_function_privilege('rp1_app',:f,'EXECUTE')"), {'f':function}).scalar_one()
