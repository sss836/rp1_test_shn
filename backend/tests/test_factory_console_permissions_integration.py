"""Real sessions in a NEW disposable PostgreSQL only; never configured site storage."""
import os
from uuid import uuid4
import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine,text
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import get_settings
from test_hmi_presence_integration import environment,signed_in,PASSWORD,heartbeat_payload
from test_factory_partition_integration import ctx_with_code,payload,ready,factory_review
from test_archives_integration import submit,headers
pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='disposable DB required')]


def guard(engine):
    assert engine.url.host=='127.0.0.1'
    assert engine.url.database=='rp1_presence_factory_partition'
    with engine.connect() as db:
        assert db.execute(text('SELECT current_database()')).scalar_one()=='rp1_presence_factory_partition'


def reviewer_client(admin):
    username='factory-reviewer-'+uuid4().hex[:12]
    response=admin.post('/api/v1/admin/users',json={'username':username,'display_name':'Factory QA Reviewer','role':'FACTORY_REVIEWER','password':PASSWORD,'must_change_password':False})
    assert response.status_code==201,response.text
    assert response.json()['data']['must_change_password'] is True
    client=TestClient(app,client=('127.0.0.1',50001))
    response=client.post('/api/v1/auth/login',json={'username':username,'password':PASSWORD})
    assert response.status_code==200,response.text
    client.headers['X-CSRF-Token']=client.cookies['rp1_csrf']
    assert client.get('/api/v1/factory/statistics').status_code==403
    response=client.post('/api/v1/auth/change-password',json={'current_password':PASSWORD,'new_password':PASSWORD+'-reviewer'})
    assert response.status_code==200,response.text
    return username,client


def test_reviewer_read_scope_and_real_factory_approval(environment):
    engine,users=environment;guard(engine)
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author:
        ctx=ctx_with_code(admin,engine)
        factory=ready(author,payload(ctx,sn='scoped-'+uuid4().hex),ctx)
        research=ready(author,payload(ctx,'RESEARCH'),ctx)
        response=submit(author,factory);assert response.status_code==200,response.text;factory=response.json()['data']
        username,client=reviewer_client(admin)
        try:
            own=client.get('/api/v1/auth/me');assert own.status_code==200,own.text
            assert own.json()['data']['user']['role']=='FACTORY_REVIEWER'
            assert own.json()['data']['campaign_access']==[]
            response=client.get('/api/v1/archives/'+factory['id']);assert response.status_code==200,response.text
            assert response.json()['data']['can_review'] is True
            assert response.json()['data']['can_edit'] is False
            assert client.get('/api/v1/archives/'+research['id']).status_code==404
            response=client.get('/api/v1/archives',params={'data_domain':'RESEARCH'});assert response.status_code==200,response.text
            assert response.json()['data']['items']==[]
            assert client.get('/api/v1/archives/'+factory['id']+'/curves').status_code==200
            assert client.get('/api/v1/archives/'+factory['id']+'/analyses').status_code==200
            assert client.get('/api/v1/factory/statistics').status_code==200
            for route in ['/api/v1/admin/users','/api/v1/setup/stations','/api/v1/hmi/stations','/api/v1/assets','/api/v1/dashboard/home']:
                assert client.get(route).status_code==403,route
            assert client.post('/api/v1/archives',json=payload(ctx),headers=headers()).status_code==403
            assert client.put('/api/v1/hmi/presence/'+str(uuid4()),json=heartbeat_payload()).status_code==403
            assert client.post('/api/v1/archives/'+factory['id']+'/domain-classification',json={'expected_revision':factory['revision'],'data_domain':'RESEARCH','reason':'Forbidden scope'},headers=headers()).status_code==403
            assert client.post('/api/v1/archives/'+research['id']+'/factory-review',json={'expected_submission_revision':1,'decision':'APPROVE','reason':'Forbidden research review'},headers=headers()).status_code==404
            key=uuid4().hex
            reviewed=factory_review(client,factory,key=key);assert reviewed.status_code==200,reviewed.text
            assert reviewed.json()['data']['submission_status']=='APPROVED'
            assert factory_review(client,factory,key=key).status_code==200
            assert factory_review(client,factory).status_code==409
            assert reviewed.json()['data']['factory_review_history'][-1]['actor_name']=='Factory QA Reviewer'
        finally:client.close()
        with engine.begin() as db:
            reviewer_id=db.execute(text('SELECT id FROM iam.app_user WHERE username=:name'),{'name':username}).scalar_one()
            campaign_id=db.execute(text('SELECT id FROM test.test_campaign WHERE public_id=:id'),{'id':ctx['campaign_id']}).scalar_one()
            db.execute(text("INSERT INTO iam.user_campaign_access(user_id,campaign_id,access_level,granted_by) VALUES(:uid,:cid,'VIEW',(SELECT id FROM iam.app_user WHERE username=:administrator))"),{'uid':reviewer_id,'cid':campaign_id,'administrator':users['admin']})
            db.execute(text("SELECT set_config('app.user_id',:uid,true)"),{'uid':str(reviewer_id)})
            assert db.execute(text('SELECT iam.can_access_campaign(:cid,false)'),{'cid':campaign_id}).scalar_one() is False
            assert db.execute(text('SELECT iam.can_write_business()')).scalar_one() is False


def test_factory_monitor_requires_membership_and_redacts_mixed_runs(environment):
    engine,users=environment;guard(engine)
    with signed_in(users['admin']) as admin:
        ctx=ctx_with_code(admin,engine);_,client=reviewer_client(admin)
        admin_id=admin.get('/api/v1/auth/me').json()['data']['user']['id']
        with engine.begin() as db:
            db.execute(text('SELECT iam.set_request_context(:id,:request,:reason)'),{'id':admin_id,'request':'factory-scope-test-'+uuid4().hex,'reason':'Synthetic factory membership'})
            db.execute(text('SELECT integration.set_factory_station_scope(:id,true,:reason)'),{'id':ctx['station_id'],'reason':'Synthetic factory membership'})
        p=heartbeat_payload();p['bench_id']=ctx['station_code']
        p['runs']=[{**p['runs'][0],'data_domain':'FACTORY','execution_uuid':str(uuid4()),'serial_number':'000FactorySN'}]
        response=admin.put('/api/v1/hmi/presence/'+str(uuid4()),json=p);assert response.status_code==200,response.text
        # Inject a mixed snapshot only in this explicitly guarded disposable DB.
        with engine.begin() as db:
            db.execute(text("UPDATE integration.hmi_presence SET runs=runs||CAST(:run AS jsonb) WHERE installation_id=:id"),{'id':p['installation_id'],'run':'[{"session_key":"research","state":"running","data_domain":"RESEARCH","sample_id":"DO-NOT-LEAK","serial_number":"RESEARCH-SECRET"},{"session_key":"legacy-factory","state":"running","data_domain":"UNCLASSIFIED","test_id":"FACTORY-LEGACY-UNCONFIRMED"}]'})
        try:
            response=client.get('/api/v1/factory/monitor');assert response.status_code==200,response.text
            assert 'RESEARCH-SECRET' not in response.text and 'DO-NOT-LEAK' not in response.text
            assert '000FactorySN' in response.text
            with engine.begin() as db:
                uid=db.execute(text("SELECT id FROM iam.app_user WHERE public_id=:id"),{'id':client.get('/api/v1/auth/me').json()['data']['user']['id']}).scalar_one()
                db.execute(text("SELECT set_config('app.user_id',:id,true)"),{'id':str(uid)})
                rows=db.execute(text('SELECT * FROM integration.list_factory_benches(NULL,100)')).mappings().all()
                station=next(row for row in rows if str(row['station_id'])==ctx['station_id'])
                assert station['connections'][0]['scope_conflict'] is True
                assert all(r['data_domain'] in ('FACTORY','UNCLASSIFIED') for c in station['connections'] for r in c['runs'])
                legacy=next(r for c in station['connections'] for r in c['runs'] if r.get('session_key')=='legacy-factory')
                assert legacy['scope_unconfirmed'] is True and legacy['data_domain']=='UNCLASSIFIED'
                assert station['connections'][0]['scope_unconfirmed'] is True
        finally:client.close()


def test_maintenance_provision_is_fixed_role_audited_and_app_cannot_execute(environment):
    engine,_=environment;guard(engine)
    username='maintenance-factory-'+uuid4().hex[:12];request='maintenance-test-'+uuid4().hex
    with engine.begin() as db:
        public_id=db.execute(text('SELECT iam.provision_factory_reviewer(:username,:display,:hash,:reason,:request)'),{'username':username,'display':'Synthetic Factory Reviewer','hash':PasswordHasher().hash(PASSWORD),'reason':'Explicit isolated test provisioning','request':request}).scalar_one()
        row=db.execute(text('SELECT role,must_change_password,principal_kind FROM iam.app_user WHERE public_id=:id'),{'id':public_id}).one()
        assert row==('FACTORY_REVIEWER',True,'HUMAN')
        event=db.execute(text('SELECT actor_user_id,metadata FROM audit.security_event WHERE request_id=:request'),{'request':request}).one()
        assert event.actor_user_id is None and event.metadata['operator']=='local-maintenance'
        assert 'password' not in str(event.metadata).lower()
    app_engine=create_engine(get_settings().database_url,hide_parameters=True)
    try:
        with app_engine.begin() as db:
            with pytest.raises(DBAPIError):db.execute(text("SELECT iam.provision_factory_reviewer('forbidden','Forbidden','$argon2id$invalid','Denied SQL caller','denied-test-key')"))
    finally:app_engine.dispose()
