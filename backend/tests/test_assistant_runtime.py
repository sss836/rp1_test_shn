"""M4 real HTTP/Pi/temporary PostgreSQL, synthetic model only."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from tests.test_assistant_integration import environment,signed_in,heartbeat,normal_model
from app.services.assistant_billing import usage_charge,RESERVATION,reliable_prices
from app.core.config import Settings
from app.core.errors import ApiError

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL') or bool(os.getenv('ASSISTANT_QA_LIVE')),reason='standalone synthetic M4 stack required')]


@pytest.fixture(autouse=True)
def clear_finance(environment):
    engine,_=environment
    with engine.begin() as c:
        c.execute(text('DELETE FROM integration.assistant_model_call'))
        c.execute(text('DELETE FROM integration.assistant_budget_day'))
        c.execute(text("UPDATE integration.assistant_finance SET blocked=false,reason=NULL"))


def control(**values):
    response=httpx.post('http://model-mock:3100/control',json={'mode':'normal',**values},trust_env=False)
    assert response.status_code==200


def submit(client,message='查询当前工位状态',thread=None,key=None,context=None):
    payload={'request_key':key or str(uuid4()),'message':message,'context':context or {'view':'stations','kind':'MODULE'}}
    if thread:payload['thread_id']=thread
    response=client.post('/api/v1/assistant/jobs',json=payload)
    assert response.status_code==202,response.text
    return response.json()['data']


def wait(client,identifier):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        response=client.get('/api/v1/assistant/jobs/'+identifier);assert response.status_code==200,response.text
        job=response.json()['data']
        if job['state'] not in {'QUEUED','RUNNING'}:return job
        time.sleep(.05)
    raise AssertionError('job did not finish')


def model_requests():return httpx.get('http://model-mock:3100/stats',trust_env=False).json()['requests']


def test_full_query_history_source_and_account_isolation(environment):
    _,users=environment
    with signed_in(users['first']) as first,signed_in(users['second']) as second:
        heartbeat(first,'SYNTHETIC-M4-FIRST')
        queued=submit(first);job=wait(first,queued['id'])
        assert job['state']=='COMPLETED',job
        cards=job['result']['cards'];assert cards and cards[0]['source']=='/api/v1/hmi/stations' and cards[0]['updated_at']
        assert cards[0]['data']['items'][0]['station_name']=='SYNTHETIC-M4-FIRST'
        assert first.get('/api/v1/assistant/threads/'+queued['thread_id']).json()['data']['messages'][0]['body']=='查询当前工位状态'
        assert second.get('/api/v1/assistant/jobs/'+queued['id']).status_code==404
        assert second.get('/api/v1/assistant/threads/'+queued['thread_id']).status_code==404
        budget=first.get('/api/v1/assistant/budget').json()['data'];assert Decimal(str(budget['spent_cny']))>0 and Decimal(str(budget['reserved_cny']))==0
        assert len(model_requests())==2
    with signed_in(users['first']) as again:
        assert any(t['id']==queued['thread_id'] for t in again.get('/api/v1/assistant/threads').json()['data'])


def test_jobs_replay_same_key_and_reject_changed_context(environment):
    _,users=environment;key=str(uuid4())
    with signed_in(users['first']) as client:
        job=submit(client,key=key,context={'view':'stations','kind':'MODULE'})
        done=wait(client,job['id']);count=len(model_requests())
        again=submit(client,key=key,context={'view':'stations','kind':'MODULE'});assert again['replayed']
        assert len(model_requests())==count
        changed=client.post('/api/v1/assistant/jobs',json={'request_key':key,'message':'查询当前工位状态','context':{'view':'home','kind':'MODULE'}})
        assert changed.status_code==409
        assert done['context']=={'view':'stations','kind':'MODULE','selection_empty':False,'metrics':[],'subjects':[]}


def test_exact_archive_statistics_match_existing_service_and_keep_existing_read_scope(environment):
    from test_archives import archive_payload
    from test_archives_integration import create,upload
    _,users=environment
    raw=b'time_s,stage,cycle,joint_pos_rad\n2,b,2,20\n1,a,1,\n1,a,1,10\n3,b,2,-4\n'
    control(tool='get_curve_statistics')
    with signed_in(users['first']) as owner,signed_in(users['second']) as other:
        archive=upload(owner,create(owner,archive_payload(origin='OTHER',raw=raw)),raw)
        assert archive['processing_state']=='READY',archive
        context={'view':'curves','kind':'MODULE','archive_id':archive['id'],'metrics':['pos_rad'],'start':1,'end':2}
        job=wait(owner,submit(owner,'解释当前归档所选区间',context=context)['id'])
        assert job['state']=='COMPLETED',job
        card=job['result']['cards'][0];series=card['data']['series'][0]
        assert card['range']=={'start':1,'end':2} and series['unit']=='rad'
        assert series['mean']==15 and series['count']==2 and series['missing']==1
        direct=owner.get(f"/api/v1/archives/{archive['id']}/statistics",params={'metrics':'pos_rad','start':1,'end':2}).json()['data']
        assert card['data']==direct and card['original_sha256']
        shared=wait(other,submit(other,'解释此归档',context=context)['id'])
        # Existing archive RLS intentionally allows all executors to inspect raw
        # archives; the assistant must preserve this, not invent creator-only reads.
        assert shared['result']['cards'][0]['data']==direct
        assert other.get(f"/api/v1/archives/{archive['id']}/statistics").status_code==200
    with signed_in(users['viewer']) as viewer:
        assert viewer.get(f"/api/v1/archives/{archive['id']}/statistics").status_code==404


def test_progress_has_source_units_and_real_configured_count(environment):
    from tests.test_unified_setup_integration import group
    _,users=environment;code='M4-PROGRESS-'+uuid4().hex[:6]
    control(tool='get_test_progress')
    with signed_in(users['first']) as client:
        response=client.post('/api/v1/setup/bundle',json=group(code,'998'));assert response.status_code==201,response.text
        campaign=response.json()['data']['objects']['campaign']['id']
        job=wait(client,submit(client,'查询当前计划进展',context={'view':'planning','kind':'MODULE','object_id':campaign})['id'])
        card=job['result']['cards'][0];assert card['type']=='test_progress' and card['unit']=='项/次'
        assert card['data']['items'][0]['configured_items']==1 and card['data']['items'][0]['executions']==[]


def test_empty_comparison_selection_never_falls_back_to_full_archive(environment):
    from app.services.assistant_queries import query
    from app.schemas.assistant_chat import PageContext
    context=PageContext(view='curves',archive_id=uuid4(),selection_empty=True).model_dump(mode='json')
    with pytest.raises(ApiError) as error:
        query(None,'get_curve_statistics',{},context)
    assert error.value.code=='archive_selection_empty'


def test_beijing_midnight_day_key_and_old_day_reservation_settlement(environment):
    engine,_=environment
    with engine.connect() as c:
        values=c.execute(text("SELECT ('2026-10-22T15:59:59Z'::timestamptz AT TIME ZONE 'Asia/Shanghai')::date,('2026-10-22T16:00:00Z'::timestamptz AT TIME ZONE 'Asia/Shanghai')::date")).one()
        assert str(values[0])=='2026-10-22' and str(values[1])=='2026-10-23'
    with engine.begin() as c:
        user=os.environ['ASSISTANT_QA_PILOT_IDS'].split(',')[1]
        owner=c.execute(text('SELECT id FROM iam.app_user WHERE public_id=:id'),{'id':user}).scalar_one()
        thread=str(uuid4());job=str(uuid4());rates=json.dumps({'input':'2','cache_hit':'0.04','output':'8'})
        c.execute(text('INSERT INTO integration.assistant_thread(id,user_id) VALUES(:id,:owner)'),{'id':thread,'owner':owner})
        c.execute(text("INSERT INTO integration.assistant_job(id,thread_id,user_id,request_hash,state,deadline) VALUES(:id,:thread,:owner,'midnight','RUNNING',now()+interval '120 seconds')"),{'id':job,'thread':thread,'owner':owner})
        c.execute(text("SELECT iam.set_request_context(:id,'midnight','synthetic midnight settlement')"),{'id':user})
        c.execute(text('SELECT integration.assistant_reserve(:id,1,:amount,CAST(:rates AS jsonb))'),{'id':job,'amount':RESERVATION,'rates':rates})
        # Advance only this synthetic call's billing day; never change host/DB time.
        c.execute(text("INSERT INTO integration.assistant_budget_day(day,reserved) VALUES((now() AT TIME ZONE 'Asia/Shanghai')::date-1,:amount)"),{'amount':RESERVATION})
        c.execute(text("UPDATE integration.assistant_budget_day SET reserved=0 WHERE day=(now() AT TIME ZONE 'Asia/Shanghai')::date"))
        c.execute(text("UPDATE integration.assistant_model_call SET day=day-1 WHERE job_id=:id"),{'id':job})
        c.execute(text("SELECT integration.assistant_settle(:id,1,0.01,'{}'::jsonb)"),{'id':job})
        rows=c.execute(text('SELECT spent,reserved FROM integration.assistant_budget_day ORDER BY day')).all()
        assert rows==[(Decimal('.01'),Decimal(0)),(Decimal(0),Decimal(0))]
        c.execute(text("UPDATE integration.assistant_job SET state='COMPLETED' WHERE id=:id"),{'id':job})


def test_wall_clock_timeout_reconciles_already_committed_receipt(environment,monkeypatch):
    import asyncio
    from app.services import assistant_chat as chat
    from app.core.auth import RequestActor
    from app.core.config import get_settings
    from app.core.db import SessionLocal
    from app.repositories.security import SecurityRepository
    from app.services.auth import AuthService
    from uuid import UUID
    engine,users=environment;identifier=str(uuid4());thread=str(uuid4())
    with signed_in(users['first']) as client:
        response=client.post('/api/v1/setup/programs',json={'program_code':'TIMEOUT-SAVED-'+uuid4().hex[:6],'name':'合成超时回执','reason':'synthetic committed receipt'},headers={'Idempotency-Key':identifier})
        assert response.status_code==201
        cookie=client.cookies['rp1_session'];settings=get_settings()
        with SessionLocal() as s:principal=AuthService(settings,SecurityRepository(s)).resolve(cookie)
        actor=RequestActor(public_id=principal.id,request_id=str(uuid4()),change_reason='synthetic timeout',source_ip='127.0.0.1',username=principal.username,display_name=principal.display_name,role=principal.role,principal_kind=principal.kind,session_public_id=principal.session_id,session_type=principal.session_type,expires_at=principal.expires_at,must_change_password=False)
        with engine.begin() as c:
            owner=c.execute(text('SELECT id FROM iam.app_user WHERE public_id=:id'),{'id':str(principal.id)}).scalar_one()
            c.execute(text('INSERT INTO integration.assistant_thread(id,user_id) VALUES(:id,:owner)'),{'id':thread,'owner':owner})
            snapshot={'messages':[{'id':1,'role':'user','body':'查询工位'}],'choices':{},'context':{}}
            from app.services.setup import canonical
            import hashlib
            encoded=canonical(snapshot)
            c.execute(text("INSERT INTO integration.assistant_job(id,thread_id,user_id,request_hash,input_snapshot,input_hash) VALUES(:id,:thread,:owner,'synthetic-timeout',CAST(:snapshot AS jsonb),:hash)"),{'id':identifier,'thread':thread,'owner':owner,'snapshot':encoded,'hash':hashlib.sha256(encoded.encode()).hexdigest()})
        class SlowPi:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def post(self,*args,**kwargs):await asyncio.sleep(5)
        monkeypatch.setattr(chat,'get_settings',lambda:settings.model_copy(update={'assistant_task_seconds':1}))
        monkeypatch.setattr(chat,'service_secret',lambda _: 'synthetic-timeout-secret-not-a-real-credential')
        monkeypatch.setattr(chat.httpx,'AsyncClient',SlowPi)
        started=time.monotonic();asyncio.run(chat.ChatRuntime().run(identifier,actor,cookie));assert time.monotonic()-started<3.5
        job=client.get('/api/v1/assistant/jobs/'+identifier).json()['data']
        assert job['state']=='TIMED_OUT' and job['receipt']['objects']['object']['id']==response.json()['data']['id']


def test_two_workers_fifo_third_job_queues(environment):
    _,users=environment;control(mode='pause_before_tool')
    with signed_in(users['first']) as client:
        jobs=[submit(client) for _ in range(3)]
        time.sleep(.25)
        states=[client.get('/api/v1/assistant/jobs/'+j['id']).json()['data']['state'] for j in jobs]
        assert states.count('RUNNING')==2 and states[2]=='QUEUED',states
        assert all(wait(client,j['id'])['state']=='COMPLETED' for j in jobs)


def test_budget_insufficient_stops_before_provider_and_existing_business_still_works(environment):
    engine,users=environment
    with engine.begin() as c:c.execute(text("INSERT INTO integration.assistant_budget_day(day,spent) VALUES((now() AT TIME ZONE 'Asia/Shanghai')::date,99)"))
    with signed_in(users['first']) as client:
        assert wait(client,submit(client)['id'])['state']=='FAILED'
        assert model_requests()==[]
        response=client.post('/api/v1/setup/programs',json={'program_code':'M4-NORMAL-'+uuid4().hex[:8],'name':'合成普通业务','reason':'normal business while assistant stopped'})
        assert response.status_code==201,response.text


def test_missing_usage_freezes_site_and_keeps_reservation(environment):
    _,users=environment;control(mode='no_usage')
    with signed_in(users['first']) as client:
        assert wait(client,submit(client)['id'])['state']=='FAILED'
        budget=client.get('/api/v1/assistant/budget').json()['data']
        assert budget['blocked'] and Decimal(str(budget['reserved_cny']))==RESERVATION
        before=len(model_requests());assert wait(client,submit(client)['id'])['state']=='FAILED'
        assert len(model_requests())==before


def test_six_round_limit_stops_repeated_tool_loop(environment):
    _,users=environment;control(mode='repeat')
    with signed_in(users['first']) as client:
        job=wait(client,submit(client)['id']);assert job['rounds']==6
        assert len(model_requests())==6


def test_write_then_lost_final_reply_returns_receipt_without_undo(environment):
    engine,users=environment;code='M4-SAVED-'+uuid4().hex[:8]
    control(mode='lose_final',tool='apply_setup_bundle',operations=[{'kind':'program','alias':'project','data':{'program_code':code,'name':'合成断流项目'}}])
    with signed_in(users['first']) as client:
        queued=submit(client,'创建项目，编号 '+code+'，名称 合成断流项目');job=wait(client,queued['id'])
        assert job['receipt']['objects']['project']['id'],job
        assert job['result']['setup']['status']=='created'
        assert job['result']['cards'][-1]['target']['view']=='planning'
        again=client.get('/api/v1/assistant/jobs/'+queued['id']).json()['data'];assert again['receipt']==job['receipt']
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM test.test_program WHERE program_code=:code'),{'code':code}).scalar_one()==1


def test_suggestion_adoption_uses_account_session_and_persists_receipt(environment):
    _,users=environment;code='M4-ADOPT-'+uuid4().hex[:8]
    control(tool='apply_setup_bundle',operations=[{'kind':'program','alias':'project','data':{'program_code':code,'name':'AI建议名称'}}])
    with signed_in(users['first']) as client:
        queued=submit(client,'创建项目，编号 '+code);job=wait(client,queued['id']);proposal=job['result']['setup']
        assert proposal['status']=='needs_adoption' and not job.get('receipt')
        response=client.post('/api/v1/assistant/setup/adopt',json={'proposal_id':proposal['proposal_id'],'proposal_hash':proposal['proposal_hash'],'publish_approved':False})
        assert response.status_code==200,response.text
        after=client.get('/api/v1/assistant/jobs/'+queued['id']).json()['data'];assert after['receipt']
        assert any(m['cards'] for m in client.get('/api/v1/assistant/threads/'+queued['thread_id']).json()['data']['messages'])


def test_task_deadline_prevents_late_write(environment):
    engine,users=environment;code='M4-LATE-'+uuid4().hex[:8]
    control(mode='pause_before_tool',tool='apply_setup_bundle',operations=[{'kind':'program','alias':'project','data':{'program_code':code,'name':code}}])
    with signed_in(users['first']) as client:
        queued=submit(client,'创建项目，编号 '+code+'，名称 '+code)
        time.sleep(.2)
        with engine.begin() as c:c.execute(text("UPDATE integration.assistant_job SET deadline=now()-interval '1 second' WHERE id=:id"),{'id':queued['id']})
        job=wait(client,queued['id']);assert not job.get('receipt')
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM test.test_program WHERE program_code=:code'),{'code':code}).scalar_one()==0


def test_cancel_running_task_keeps_any_existing_receipt_and_stops_new_calls(environment):
    _,users=environment;control(mode='pause_before_tool')
    with signed_in(users['first']) as client:
        queued=submit(client);time.sleep(.15)
        assert client.post('/api/v1/assistant/jobs/'+queued['id']+'/cancel',json={}).status_code==200
        job=wait(client,queued['id']);assert job['state']=='CANCELLED'
        assert len(model_requests())<=1


def test_session_disabled_during_wait_rejects_tool_and_has_no_open_db_transaction(environment):
    engine,users=environment;code='M4-REVOKED-'+uuid4().hex[:8]
    control(mode='pause_before_tool',tool='apply_setup_bundle',operations=[{'kind':'program','alias':'program','data':{'program_code':code,'name':code}}])
    with signed_in(users['first']) as client:
        queued=submit(client,'创建项目，编号 '+code+'，名称 '+code)
        until=time.monotonic()+5
        while not model_requests():
            assert time.monotonic()<until;time.sleep(.02)
        with engine.begin() as c:
            assert c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='rp1_app' AND state='idle in transaction'")).scalar_one()==0
            c.execute(text('UPDATE iam.app_user SET enabled=false WHERE username=:name'),{'name':users['first']})
        try:
            until=time.monotonic()+5
            while time.monotonic()<until:
                with engine.connect() as c:state=c.execute(text('SELECT state FROM integration.assistant_job WHERE id=:id'),{'id':queued['id']}).scalar_one()
                if state not in {'QUEUED','RUNNING'}:break
                time.sleep(.05)
            assert state in {'FAILED','COMPLETED'}
        finally:
            with engine.begin() as c:c.execute(text('UPDATE iam.app_user SET enabled=true WHERE username=:name'),{'name':users['first']})
        assert not wait(client,queued['id']).get('receipt')
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM test.test_program WHERE program_code=:code'),{'code':code}).scalar_one()==0


def test_retention_removes_chat_but_keeps_operation_receipt(environment):
    engine,users=environment
    with signed_in(users['first']) as client:
        queued=submit(client);wait(client,queued['id'])
        saved=client.post('/api/v1/setup/programs',json={'program_code':'M4-KEEP-'+uuid4().hex[:8],'name':'合成审计保留','reason':'retention'},headers={'Idempotency-Key':str(uuid4())}).json()['data']
        with engine.begin() as c:
            c.execute(text("UPDATE integration.assistant_message SET created_at=now()-interval '31 days' WHERE thread_id=:id"),{'id':queued['thread_id']})
            c.execute(text("UPDATE integration.assistant_job SET created_at=now()-interval '31 days' WHERE id=:id"),{'id':queued['id']})
            c.execute(text('SELECT integration.assistant_maintenance(false)'))
        assert client.get('/api/v1/assistant/threads/'+queued['thread_id']).json()['data']['messages']==[]
        assert client.get('/api/v1/setup/receipts/'+saved['request_key']).status_code==200


def test_restart_interrupts_jobs_and_keeps_unknown_accounting_blocked(environment):
    engine,users=environment
    with engine.begin() as c:
        user=c.execute(text('SELECT id FROM iam.app_user WHERE username=:name'),{'name':users['first']}).scalar_one()
        thread=str(uuid4());job=str(uuid4())
        c.execute(text('INSERT INTO integration.assistant_thread(id,user_id) VALUES(:id,:user)'),{'id':thread,'user':user})
        c.execute(text("INSERT INTO integration.assistant_job(id,thread_id,user_id,request_hash,state,deadline) VALUES(:id,:thread,:user,'synthetic','RUNNING',now()+interval '120 seconds')"),{'id':job,'thread':thread,'user':user})
        c.execute(text("SELECT iam.set_request_context(:user,'synthetic-recovery','recovery test')"),{'user':os.environ['ASSISTANT_QA_PILOT_IDS'].split(',')[1]})
        c.execute(text('SELECT integration.assistant_reserve(:id,1,:amount,CAST(:rates AS jsonb))'),{'id':job,'amount':RESERVATION,'rates':'{}'})
        c.execute(text('SELECT integration.assistant_maintenance(true)'))
    with signed_in(users['first']) as client:
        assert client.get('/api/v1/assistant/jobs/'+job).json()['data']['state']=='INTERRUPTED'
        assert client.get('/api/v1/assistant/budget').json()['data']['blocked']


def test_viewer_legacy_and_browser_private_callbacks_are_denied(environment):
    _,users=environment
    with signed_in(users['viewer']) as client:
        assert client.get('/api/v1/assistant/threads').status_code==403
        assert client.post('/api/v1/assistant/jobs',json={'request_key':str(uuid4()),'message':'查询工位','context':{}}).status_code==403
    with signed_in(users['first']) as client:
        assert client.post('/api/v1/assistant/probe',json={}).status_code==410
        assert client.post('/api/v1/assistant/setup/commands',json={'request_key':str(uuid4()),'messages':['创建项目']}).status_code==410
        assert client.post('/api/v1/assistant/tools/chat_read',json={'tool':'list_hmi_stations','arguments':{}}).status_code==404


@pytest.mark.parametrize('usage',[None,{},dict(prompt_tokens=1,completion_tokens=0,total_tokens=1),dict(prompt_tokens=100,completion_tokens=20,prompt_cache_hit_tokens=90,prompt_cache_miss_tokens=60,total_tokens=120)])
def test_unreliable_usage_is_never_guessed_zero(usage):assert usage_charge(usage) is None


def test_exact_accounting_upper_bound_and_price_expiry():
    raw=dict(prompt_tokens=100,completion_tokens=20,prompt_cache_hit_tokens=40,prompt_cache_miss_tokens=60,total_tokens=120)
    assert usage_charge(raw)==Decimal('0.00028160')
    with pytest.raises(ApiError):reliable_prices(Settings(assistant_price_valid_until='2026-01-01T00:00:00Z'))


def test_queued_job_freezes_future_parameters_and_source_hash(environment):
    import hashlib
    from app.services.setup import canonical
    engine,users=environment
    code='FIX-FUTURE-'+uuid4().hex[:6]
    with signed_in(users['first']) as client:
        control(mode='pause_before_tool')
        blockers=[submit(client),submit(client)]
        thread=blockers[0]['thread_id']
        first=submit(client,'创建项目，编号 FIX-EARLIER，名称 原先项目',thread=thread)
        later=submit(client,f'创建项目，编号 {code}，名称 后发项目',thread=thread)
        control(tool='apply_setup_bundle',operations=[{'kind':'program','alias':'saved','data':{'program_code':code,'name':'后发项目'}}])
        for queued in blockers:wait(client,queued['id'])
        a=wait(client,first['id']);b=wait(client,later['id'])
        assert not a.get('receipt'),a
        assert a['result']['business_status']=='needs_adoption'
        assert b['receipt']['request_key']==later['id'],b
        with engine.connect() as conn:
            row=conn.execute(text('SELECT input_message_id,input_snapshot,input_hash FROM integration.assistant_job WHERE id=:id'),{'id':first['id']}).mappings().one()
            assert code not in canonical(row['input_snapshot'])
            assert row['input_hash']==hashlib.sha256(canonical(row['input_snapshot']).encode()).hexdigest()
            assert max(m['id'] for m in row['input_snapshot']['messages'])==row['input_message_id']
            evidence=conn.execute(text('SELECT authorization_evidence FROM integration.setup_receipt WHERE request_key=:id'),{'id':later['id']}).scalar_one()
            assert evidence['source_hash']==b['input_hash']
        with pytest.raises(Exception,match='immutable'):
            with engine.begin() as conn:conn.execute(text("UPDATE integration.assistant_job SET context='{}',input_snapshot='{}' WHERE id=:id"),{'id':first['id']})


def test_later_negation_does_not_cancel_earlier_command(environment):
    engine,users=environment;code='FIX-NOT-CANCEL-'+uuid4().hex[:6]
    with signed_in(users['first']) as client:
        control(mode='pause_before_tool',tool='apply_setup_bundle',operations=[{'kind':'program','alias':'project','data':{'program_code':code,'name':code}}])
        first=submit(client,f'创建项目，编号 {code}，名称 {code}')
        later=submit(client,'不要创建，取消建档',thread=first['thread_id'])
        a=wait(client,first['id']);b=wait(client,later['id'])
        assert a.get('receipt') and not b.get('receipt')
        with engine.connect() as conn:assert conn.execute(text('SELECT count(*) FROM test.test_program WHERE program_code=:code'),{'code':code}).scalar_one()==1


def test_creation_without_tool_receipt_never_reports_saved(environment):
    _,users=environment;control(mode='no_tool')
    with signed_in(users['first']) as client:
        job=wait(client,submit(client,'创建项目，编号 FIX-NO-RECEIPT，名称 无回执项目')['id'])
        assert not job.get('receipt') and job['result']['business_status']=='needs_input'
        assert '尚未保存' in job['result']['reply']


def test_original_question_repeated_reads_preserve_one_card_and_final_answer(environment):
    _,users=environment
    control(mode='repeat',tool='search_business',arguments={'resource':'cases','query':'upper_body','published_only':True})
    with signed_in(users['first']) as client:
        job=wait(client,submit(client,'为什么我的上位机没有upper_body的可选用例编号')['id'])
        from pathlib import Path
        import json
        Path('/reports/original-controlled.json').write_text(json.dumps(job,ensure_ascii=False,default=str))
        assert job['rounds']==6 and job['state']=='COMPLETED',job
        assert job['result']['completion']=='partial' and job['result']['termination_reason']=='round_limit'
        assert len(job['result']['cards'])==1
        data=job['result']['cards'][0]['data']
        assert data['applied_filters']['target_part_code']=='UPPER'
        assert all(x['target_part_code']=='UPPER' for x in data['items'])
        assert '模型链路中断' not in job['result']['reply']
        stats=model_requests();assert len(stats)==6 and stats[-1]['tool_names']==[]


@pytest.mark.parametrize('mode,reason',[('no_usage','billing_unverified'),('disconnect','provider_network'),('limited','provider_http')])
def test_failure_reason_survives_model_bridge(environment,mode,reason):
    # Other integration cases deliberately freeze finance; clear only this
    # disposable QA database, never use the site's connection configuration.
    from sqlalchemy import text
    engine,users=environment
    with engine.begin() as c:
        c.execute(text('DELETE FROM integration.assistant_model_call'))
        c.execute(text('DELETE FROM integration.assistant_budget_day'))
        c.execute(text('UPDATE integration.assistant_finance SET blocked=false,reason=NULL'))
    control(mode=mode)
    with signed_in(users['first']) as client:
        job=wait(client,submit(client)['id'])
        assert job['state']=='FAILED' and job['result']['termination_reason']==reason,job
        assert len(model_requests())==1
        assert 'sensitive provider detail' not in job['result']['reply']
        assert client.get('/api/v1/assistant/budget').json()['data']['blocked']


def test_context_counts_follow_normal_campaign_read_scope(environment):
    engine,users=environment
    prefix='QUERY-CONTEXT-'+uuid4().hex[:8]
    with signed_in(users['first']) as owner,signed_in(users['second']) as other:
        def create(path,value):
            r=owner.post('/api/v1/setup/'+path,json={**value,'reason':'synthetic query scope'},headers={'Idempotency-Key':str(uuid4())})
            assert r.status_code==201,r.text
            return r.json()['data']
        program=create('programs',{'program_code':prefix,'name':'合成项目'})
        campaign=create('campaigns',{'program_id':program['id'],'campaign_code':prefix,'name':'合成计划','asset_kind':'MODULE'})
        with engine.begin() as c:
            owner_id=c.execute(text('SELECT id FROM iam.app_user WHERE username=:name'),{'name':users['first']}).scalar_one()
            asset_id=c.execute(text("""INSERT INTO test.asset(asset_code,asset_kind,product_family,serial_number,display_name,created_by)
                VALUES('RP1.3-UPPER-901','MODULE','SYNTHETIC',:serial,'合成查询样品',:owner) RETURNING id"""),{'serial':str(uuid4()),'owner':owner_id}).scalar_one()
            c.execute(text("""INSERT INTO test.campaign_coverage_item(campaign_id,test_case_version_id)
                SELECT campaign.id,v.id FROM test.test_campaign campaign,catalog.test_case tc
                JOIN catalog.test_case_version v ON v.test_case_id=tc.id AND v.status='PUBLISHED'
                WHERE campaign.public_id=:campaign AND tc.case_code='REL-UPPER-001' LIMIT 1"""),{'campaign':campaign['id']})
            c.execute(text("UPDATE test.campaign_coverage_item SET target_asset_id=:asset WHERE campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:campaign)"),{'asset':asset_id,'campaign':campaign['id']})
        for client in (owner,other):
            visible=client.get('/api/v1/setup/campaigns/'+campaign['id']+'/contexts')
            control(tool='search_business',arguments={'resource':'cases','query':'REL-UPPER-001','include_contexts':True})
            job=wait(client,submit(client,'核对合成用例的计划关联')['id'])
            assert job['state']=='COMPLETED',job
            actual=job['result']['cards'][0]['data']['items'][0]['plan_contexts']
            expected=len(visible.json()['data']) if visible.status_code==200 else 0
            assert actual=={'visible_contexts':expected,'active_plan_contexts':expected}
