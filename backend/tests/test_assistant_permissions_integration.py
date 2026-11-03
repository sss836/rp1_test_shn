"""Real PostgreSQL/RLS and Cookie/CSRF against the dedicated synthetic stack."""
from datetime import datetime,timezone
import json
import os
from types import SimpleNamespace
from uuid import UUID,uuid4
import pytest
from sqlalchemy import text
from app.services import assistant_permissions as permissions
from app.services.assistant_chat import actor_session
from app.core.auth import RequestActor
from app.core.errors import ApiError
from tests.test_assistant_integration import environment,signed_in
from tests.test_assistant_support_integration import seed_job

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL'),reason='dedicated isolated stack required')]

def running_item(engine,username):
    job=seed_job(engine,username)
    with engine.begin() as connection:
        who=connection.execute(text('SELECT public_id,role FROM iam.app_user WHERE username=:name'),{'name':username}).mappings().one()
        sid=connection.execute(text('SELECT origin_session_id FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).scalar_one()
        connection.execute(text("UPDATE integration.assistant_job SET state='RUNNING',deadline=now()+interval '120 seconds',active_started_at=clock_timestamp(),active_elapsed_ms=12000,read_count=3 WHERE id=:id"),{'id':job['id']})
    actor=RequestActor(public_id=who['public_id'],request_id='isolated-permission',change_reason='synthetic',source_ip=None,
        username=username,display_name=username,role=who['role'],principal_kind='HUMAN',session_public_id=sid,
        session_type='BROWSER',expires_at=None,must_change_password=False)
    return job,{'id':job['id'],'actor':actor,'context':{'hmi':{'installation_id':job['installation_id'],'window_id':job['window_id']}}}

def test_permission_wait_persists_releases_budget_owner_web_approves_but_cannot_read(environment):
    engine,users=environment
    with signed_in(users['first']) as first,signed_in(users['second']) as other:
        job,item=running_item(engine,users['first'])
        card=permissions.evidence(item,{'operation':'read','path':'$HOME/private/log.txt'})
        request=card['data']['permission_request'];rid=request['id']
        with engine.connect() as connection:
            before=connection.execute(text('SELECT state,active_elapsed_ms,read_count,rounds,input_hash FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert before['state']=='WAITING_PERMISSION' and before['active_elapsed_ms']>=12000 and before['read_count']==3
        assert other.get('/api/v1/assistant/jobs/'+job['id']+'/permissions').status_code==404
        with signed_in(users['first']) as webpage:
            listed=webpage.get('/api/v1/assistant/jobs/'+job['id']+'/permissions').json()['data']
            row=next(x for x in listed['items'] if x['id']==str(rid))
            decision={'request_key':str(uuid4()),'expected_request_sha256':row['request_sha256'],'scope':'file'}
            endpoint=f"/api/v1/assistant/jobs/{job['id']}/permissions/{rid}/decision"
            approved=webpage.post(endpoint,json=decision)
            assert approved.status_code==200,approved.text
            assert webpage.post(endpoint,json=decision).json()['data']['replayed']
            assert webpage.post(endpoint,json={**decision,'scope':'all'}).status_code==409
            params={'installation_id':job['installation_id'],'window_id':job['window_id']}
            assert webpage.get('/api/v1/assistant/jobs/'+job['id']+'/local-requests',params=params).status_code==403
            assert webpage.post('/api/v1/assistant/jobs/'+job['id']+'/local-results',json={**params,'request_id':str(rid),'result':{'status':'read'}}).status_code==403
        result={'status':'read','text':'synthetic actual bytes','complete':True,'execution_elapsed_ms':8000}
        assert permissions.complete(item['actor'],job['id'],rid,job['installation_id'],job['window_id'],result,request['request_sha256'])['accepted']
        assert permissions.complete(item['actor'],job['id'],rid,job['installation_id'],job['window_id'],result,request['request_sha256'])['replayed']
        with pytest.raises(ApiError) as error:permissions.complete(item['actor'],job['id'],rid,job['installation_id'],job['window_id'],{'status':'read','text':'different'})
        assert error.value.status_code==409
        # A service restart's maintenance leaves waiting evidence and accumulated
        # execution limits intact; no cookie or replacement authority is saved.
        with engine.begin() as connection:
            connection.execute(text('SELECT integration.assistant_maintenance(true)'))
            after=connection.execute(text('SELECT * FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert after['state']=='WAITING_PERMISSION' and after['active_elapsed_ms']==before['active_elapsed_ms']+8000
            assert after['read_count']==before['read_count'] and after['input_hash']==before['input_hash']

def test_actual_local_execution_budget_is_charged_once_waiting_is_free_and_limit_stops_resume(environment):
    import time
    engine,users=environment
    with signed_in(users['first']) as first:
        job,item=running_item(engine,users['first'])
        request=permissions.evidence(item,{'operation':'capabilities'})['data']['permission_request']
        with engine.begin() as connection:
            connection.execute(text('UPDATE integration.assistant_job SET active_elapsed_ms=119000 WHERE id=:id'),{'id':job['id']})
        time.sleep(.025)
        with engine.connect() as connection:
            assert connection.execute(text('SELECT active_elapsed_ms FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).scalar_one()==119000
        result={'status':'ready','roots':['$STATE'],'path_aliases':{'$STATE':{'default_authorized':True}},'execution_elapsed_ms':1500}
        recorded=permissions.complete(item['actor'],job['id'],request['id'],job['installation_id'],job['window_id'],result)
        assert recorded['budget_exhausted'] is True and recorded['active_elapsed_ms']==120000
        assert permissions.complete(item['actor'],job['id'],request['id'],job['installation_id'],job['window_id'],result)['replayed']
        resumed=first.post('/api/v1/assistant/jobs/'+job['id']+'/resume',json={'request_key':str(uuid4())})
        assert resumed.status_code==409 and resumed.json()['error']['code']=='assistant_budget_exhausted'
        with engine.connect() as connection:
            row=connection.execute(text('SELECT state,active_elapsed_ms,read_count FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert row['state']=='TIMED_OUT' and row['active_elapsed_ms']==120000 and row['read_count']==3

def test_thirteenth_durable_tool_read_returns_409_through_real_grant_and_postgresql(environment):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.api.routes.assistant_chat import router
    from app.services.assistant_chat import runtime
    from app.services.assistant import grants
    engine,users=environment
    with signed_in(users['first']) as first:
        job,item=running_item(engine,users['first'])
        with engine.begin() as connection:
            connection.execute(text('UPDATE integration.assistant_job SET read_count=0 WHERE id=:id'),{'id':job['id']})
        token,_=grants.issue(first.cookies['rp1_session'],item['actor'],scope='setup',ttl=120)
        item.update(durable_permissions=True,cards=[],reads=0,read_cache={})
        runtime.tokens[grants.key(token)]=item
        app=FastAPI();app.include_router(router)
        @app.exception_handler(ApiError)
        async def api_error(_request,error):return JSONResponse(status_code=error.status_code,content={'error':{'code':error.code}})
        try:
            with TestClient(app) as client:
                for _ in range(12):
                    response=client.post('/api/v1/assistant/tools/chat_read',headers={'X-RP1-Tool-Grant':token},json={'tool':'search_shared_faq','arguments':{'query':'Synthetic-no-side-effects'}})
                    assert response.status_code==200,response.text
                response=client.post('/api/v1/assistant/tools/chat_read',headers={'X-RP1-Tool-Grant':token},json={'tool':'search_shared_faq','arguments':{'query':'Synthetic-no-side-effects'}})
                assert response.status_code==409 and response.json()['error']['code']=='assistant_tool_limit'
                with engine.begin() as connection:connection.execute(text("UPDATE iam.app_user SET role='VIEWER' WHERE username=:name"),{'name':users['first']})
                try:
                    revoked=client.post('/api/v1/assistant/tools/chat_read',headers={'X-RP1-Tool-Grant':token},json={'tool':'search_shared_faq','arguments':{'query':'Synthetic-no-side-effects'}})
                    assert revoked.status_code==403 and revoked.json()['error']['code']!='assistant_tool_limit'
                finally:
                    with engine.begin() as connection:connection.execute(text("UPDATE iam.app_user SET role='TEST_EXECUTOR' WHERE username=:name"),{'name':users['first']})
            with engine.connect() as connection:
                assert connection.execute(text('SELECT read_count FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).scalar_one()==12
        finally:
            runtime.tokens.pop(grants.key(token),None);grants.revoke(token)

def test_default_roots_reused_but_symlink_actual_target_requires_new_hash(environment):
    engine,users=environment
    with signed_in(users['first']) as first:
        job,item=running_item(engine,users['first'])
        cap=permissions.evidence(item,{'operation':'capabilities'})['data']['permission_request']
        caps={'status':'ready','roots':['$STATE'],'path_aliases':{'$STATE':{'path':'/synthetic/state','default_authorized':True},'$HOME':{'path':'/synthetic/home','default_authorized':False}}}
        permissions.complete(item['actor'],job['id'],cap['id'],job['installation_id'],job['window_id'],caps)
        requested=permissions.evidence(item,{'operation':'read','path':'$STATE/link.log'})['data']['permission_request']
        assert requested['state']=='approved' and requested['approval_origin']=='default'
        redirected=permissions.complete(item['actor'],job['id'],requested['id'],job['installation_id'],job['window_id'],{'status':'permission_required','resolved_path':'/synthetic/outside/private.log'})
        actual=redirected['permission_request']
        assert actual['state']=='pending' and actual['approval_origin'] is None
        assert actual['request_sha256']!=requested['request_sha256'] and actual['path']=='/synthetic/outside/private.log'
        with pytest.raises(ApiError):permissions.complete(item['actor'],job['id'],actual['id'],job['installation_id'],job['window_id'],{'status':'read','text':'not authorized'})
        request={'request_key':str(uuid4()),'expected_request_sha256':actual['request_sha256'],'scope':'directory'}
        url=f"/api/v1/assistant/jobs/{job['id']}/permissions/{actual['id']}/decision"
        assert first.post(url,json=request).status_code==200
        permissions.complete(item['actor'],job['id'],actual['id'],job['installation_id'],job['window_id'],{'status':'os_denied','message':'OS permissions unchanged','unread':True})
        card=permissions.evidence(item,{'operation':'read','path':'$STATE/link.log'})
        assert card['data']['status']=='os_denied' and card['data']['resolved_request_arguments']['path']=='/synthetic/outside/private.log'
        reused=permissions.evidence(item,{'operation':'read','path':'/synthetic/outside/another.log'})['data']['permission_request']
        assert reused['approval_origin']=='reuse' and reused['state']=='approved'

def test_expiry_retry_cancel_and_expired_original_session_do_not_gain_authority(environment,monkeypatch):
    engine,users=environment
    with signed_in(users['first']) as first:
        job,item=running_item(engine,users['first'])
        # Create an already expired synthetic request with a matching immutable
        # payload/hash. Never disable integrity triggers or alter a real row.
        with monkeypatch.context() as fixture_clock:
            fixture_clock.setattr(permissions,'WAIT_SECONDS',-1)
            row=permissions.evidence(item,{'operation':'read','path':'$HOME/private.log'})['data']['permission_request']
        path=f"/api/v1/assistant/jobs/{job['id']}/permissions/{row['id']}"
        decision={'request_key':str(uuid4()),'expected_request_sha256':row['request_sha256'],'scope':'file'}
        assert first.post(path+'/decision',json=decision).status_code==410
        retry={'request_key':str(uuid4()),'expected_request_sha256':row['request_sha256']}
        renewed=first.post(path+'/retry',json=retry)
        assert renewed.status_code==200,renewed.text
        assert renewed.json()['data']['id']!=str(row['id'])
        assert first.post(path+'/retry',json=retry).json()['data']['replayed']
        assert first.post('/api/v1/assistant/jobs/'+job['id']+'/cancel').status_code==200
        assert first.post(path+'/retry',json={**retry,'request_key':str(uuid4())}).status_code==409
        other_job,other_item=running_item(engine,users['first'])
        other_request=permissions.evidence(other_item,{'operation':'read','path':'$HOME/fault.log'})['data']['permission_request']
        assert first.post('/api/v1/auth/logout').status_code==204
        with signed_in(users['first']) as replacement:
            result=replacement.post(f"/api/v1/assistant/jobs/{other_job['id']}/permissions/{other_request['id']}/decision",json={'request_key':str(uuid4()),'expected_request_sha256':other_request['request_sha256'],'scope':'file'})
            assert result.status_code==409 and result.json()['error']['code']=='original_execution_session_expired'
