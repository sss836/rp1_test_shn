"""Independent no-DB broker/route/runtime security tests, all data synthetic."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from queue import Queue
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import ApiError
from app.services import assistant_local_diagnostics as local


def actor(public_id=None, session=None):
    return SimpleNamespace(public_id=public_id or uuid4(), session_public_id=session or uuid4())


@pytest.fixture
def item():
    value={'id':str(uuid4()),'actor':actor(),
           'context':{'hmi':{'installation_id':str(uuid4()),'window_id':'synthetic-window'}},
           'deadline':datetime.now(timezone.utc)+timedelta(seconds=5),'_threads':[]}
    yield value
    local.close(value)
    for thread in value['_threads']:
        thread.join(2)
        assert not thread.is_alive(),'Broker waiter leaked after task close'


def begin(item, arguments=None, wait=2):
    output=Queue()
    def run():
        try:output.put(('ok',local.evidence(item,arguments or {'operation':'read','path':'/synthetic/log'},wait_seconds=wait)))
        except BaseException as exc:output.put(('error',exc))
    thread=threading.Thread(target=run,name='isolated-local-diagnostic',daemon=True)
    item['_threads'].append(thread);thread.start()
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        pending=local.poll(item)
        if pending:return pending[0],output,thread
        if not output.empty():
            _,error=output.get();raise error
        time.sleep(.001)
    raise AssertionError('No local request appeared')


def finish(output,thread):
    kind,value=output.get(timeout=2);thread.join(2)
    assert not thread.is_alive()
    if kind=='error':raise value
    return value


def error_code(code, action):
    with pytest.raises(ApiError) as error:action()
    assert error.value.code==code
    return error.value


@pytest.mark.parametrize('context',[{}, {'hmi':{}}, {'hmi':{'installation_id':'not-a-uuid','window_id':'w'}},
                                   {'hmi':{'installation_id':str(uuid4()),'window_id':''}}])
def test_without_valid_original_hmi_context_does_not_queue(context):
    value={'id':str(uuid4()),'actor':actor(),'context':context}
    error_code('local_diagnostics_unavailable',lambda:local.evidence(value,{'operation':'capabilities'}))
    assert 'local_diagnostic_requests' not in value


def test_original_login_session_is_required(item):
    item['actor'].session_public_id=None
    error_code('authentication_required',lambda:local.evidence(item,{'operation':'capabilities'}))
    assert 'local_diagnostic_requests' not in item


@pytest.mark.parametrize('arguments',[
    {'operation':'read','installation_id':str(uuid4())},{'operation':'read','window_id':'another'},
    {'operation':'read','user_id':str(uuid4())},{'operation':'read','url':'https://example.invalid'},
    {'operation':'read','command':'not executable'},{'operation':'shell'},{'operation':'read','limit':65537},
    {'operation':'read','limit':0},{'operation':'read','offset':-1},{'operation':'read','offset':True},
    {'operation':'read','path':'/some\ncommand'},{'operation':'search','query':'text\x00payload'},
])
def test_model_cannot_choose_machine_callback_command_or_unbounded_arguments(item,arguments):
    error_code('invalid_local_diagnostic_arguments',lambda:local.evidence(item,arguments))
    assert not local.poll(item)


def test_literal_search_is_data_and_result_is_an_immutable_copy(item):
    arguments={'operation':'search','path':'/synthetic','query':'$(literal); | not-a-shell','offset':0,'limit':65536}
    pending,output,thread=begin(item,arguments)
    assert pending['query']==arguments['query']
    assert set(pending)=={'id','operation','path','query','offset','limit','created_at','expires_at'}
    result={'status':'partial','data':{'text':'original'},'unread':['remaining']}
    assert local.complete(item,pending['id'],result)=={'accepted':True,'replayed':False}
    result['data']['text']='changed outside broker'
    evidence=finish(output,thread)
    assert evidence['data']['data']['text']=='original'
    assert evidence['data']['unread']==['remaining']
    assert not local.poll(item)


def test_same_result_replay_and_conflicting_replacement(item):
    pending,output,thread=begin(item)
    result={'status':'read','data':{'offset':0,'text':'synthetic'}}
    assert local.complete(item,pending['id'],result)['replayed'] is False
    assert local.complete(item,pending['id'],deepcopy(result))['replayed'] is True
    error_code('local_diagnostic_result_conflict',lambda:local.complete(item,pending['id'],{'status':'read','data':{'offset':0,'text':'different'}}))
    assert finish(output,thread)['data']['data']['text']=='synthetic'


def test_semantically_equal_result_reordered_keys_is_idempotent(item):
    pending,output,thread=begin(item)
    local.complete(item,pending['id'],{'status':'read','data':{'offset':0,'text':'synthetic'}})
    # JSON object order is not a different result. This intentionally exposed
    # the original noncanonical hash; do not relax it to expect a conflict.
    assert local.complete(item,pending['id'],{'data':{'text':'synthetic','offset':0},'status':'read'})=={'accepted':True,'replayed':True}
    finish(output,thread)


def test_only_one_pending_request_per_task_and_close_wakes_waiter(item):
    pending,output,thread=begin(item,wait=5)
    error_code('local_diagnostics_busy',lambda:local.evidence(item,{'operation':'capabilities'}))
    local.close(item)
    assert finish(output,thread)['data']['status']=='cancelled'
    assert local.poll(item)==[]
    error_code('local_diagnostic_request_ended',lambda:local.complete(item,pending['id'],{'status':'read'}))
    error_code('assistant_task_ended',lambda:local.evidence(item,{'operation':'read'}))


def test_timeout_stops_polling_and_rejects_late_result(item):
    value=local.evidence(item,{'operation':'execution_summary'},wait_seconds=.015)
    assert value['data']['status']=='timeout' and value['data']['unread'] is True
    assert local.poll(item)==[]
    error_code('local_diagnostic_request_ended',lambda:local.complete(item,value['data']['request_id'],{'status':'read'}))


def test_job_deadline_bounds_request_even_when_tool_wait_is_long(item):
    item['deadline']=datetime.now(timezone.utc)+timedelta(milliseconds=20)
    before=time.monotonic();value=local.evidence(item,{'operation':'capabilities'},wait_seconds=55)
    assert time.monotonic()-before<1 and value['data']['status']=='timeout'


@pytest.mark.parametrize('result',[[1],None,{'value':float('nan')},{'value':float('inf')}])
def test_non_object_or_nonfinite_result_is_rejected_without_consuming_request(item,result):
    pending,output,thread=begin(item)
    error_code('invalid_local_diagnostic_result',lambda:local.complete(item,pending['id'],result))
    assert len(local.poll(item))==1
    local.complete(item,pending['id'],{'status':'read'});finish(output,thread)


def test_result_size_is_original_utf8_json_and_has_exact_128kib_boundary(item):
    pending,output,thread=begin(item)
    prefix=len(json.dumps({'v':''},separators=(',',':')).encode())
    oversized={'v':'x'*(local.MAX_RESULT_BYTES-prefix+1)}
    assert error_code('local_diagnostic_result_size',lambda:local.complete(item,pending['id'],oversized)).status_code==413
    assert error_code('local_diagnostic_result_size',lambda:local.complete(item,pending['id'],{'v':'界'*50000})).status_code==413
    assert len(local.poll(item))==1
    exact={'v':'x'*(local.MAX_RESULT_BYTES-prefix)}
    assert local.complete(item,pending['id'],exact)['accepted']
    assert finish(output,thread)['data']['v']==exact['v']


def test_binding_uses_original_user_session_instance_and_window(item):
    runtime=SimpleNamespace(tokens={'synthetic':item})
    context=item['context']['hmi'];job={'id':item['id'],'context':item['context'],'state':'RUNNING','deadline':item['deadline']}
    bound=lambda who=item['actor'],inst=context['installation_id'],win=context['window_id']:local.bound_item(runtime,who,job,inst,win)
    assert bound() is item
    error_code('local_diagnostic_binding',lambda:bound(actor(session=item['actor'].session_public_id)))
    error_code('local_diagnostic_binding',lambda:bound(actor(public_id=item['actor'].public_id)))
    anonymous=actor(public_id=item['actor'].public_id);anonymous.session_public_id=None
    error_code('local_diagnostic_binding',lambda:bound(anonymous))
    error_code('local_diagnostic_binding',lambda:bound(inst=uuid4()))
    error_code('local_diagnostic_binding',lambda:bound(win='other-window'))


@pytest.mark.parametrize('change',[{'state':'QUEUED'},{'state':'COMPLETED'},{'cancel_requested':True},
                                  {'deadline':datetime.now(timezone.utc)-timedelta(seconds=1)}])
def test_terminal_cancelled_or_expired_job_cannot_receive_results(item,change):
    runtime=SimpleNamespace(tokens={'synthetic':item});context=item['context']['hmi']
    job={'id':item['id'],'context':item['context'],'state':'RUNNING','deadline':item['deadline'],**change}
    assert local.bound_item(runtime,item['actor'],job,context['installation_id'],context['window_id']) is None
    error_code('assistant_task_ended',lambda:local.bound_item(runtime,item['actor'],job,context['installation_id'],context['window_id'],for_result=True))


def test_api_restart_has_no_replayable_local_request(item):
    context=item['context']['hmi'];job={'id':item['id'],'context':item['context'],'state':'RUNNING','deadline':item['deadline']}
    restarted=SimpleNamespace(tokens={})
    assert local.bound_item(restarted,item['actor'],job,context['installation_id'],context['window_id']) is None
    error_code('assistant_task_ended',lambda:local.bound_item(restarted,item['actor'],job,context['installation_id'],context['window_id'],for_result=True))


def test_chat_attachment_read_uses_runtime_frozen_metadata_not_model_context(monkeypatch):
    from app.services import assistant_chat as chat
    from app.schemas.assistant_chat import ReadTool
    runtime=chat.ChatRuntime();frozen=[{'id':str(uuid4()),'sha256':'trusted'}]
    value={'id':str(uuid4()),'actor':actor(),'context':{'attachments':[{'id':'forged','sha256':'forged'}]},
           'attachment_metadata':frozen,'attachment_read_state':{},'cards':[],'reads':0}
    monkeypatch.setattr(runtime,'authorize',lambda token:value)
    calls=[]
    def query(_session,tool,args,context,**trusted):
        calls.append((tool,args,context,trusted))
        return {'type':'diagnostic_attachment','data':{'status':'partial'},'source':'synthetic','updated_at':'2026-10-30T00:00:00Z'}
    monkeypatch.setattr(chat,'query',query)
    payload=ReadTool(tool='read_diagnostic_attachment',arguments={'attachment_id':frozen[0]['id']})
    runtime.read('synthetic',payload)
    assert calls[0][3]['attachment_actor'] is value['actor']
    assert calls[0][3]['attachment_metadata'] is frozen
    assert calls[0][3]['attachment_read_state'] is value['attachment_read_state']
    assert calls[0][3]['attachment_metadata']!=value['context']['attachments']
    assert value['reads']==1


@pytest.fixture
def route_context(monkeypatch,item):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.core import auth
    from app.api.routes import assistant_local_diagnostics as routes
    who=[item['actor']]
    job={'id':item['id'],'context':item['context'],'state':'RUNNING','deadline':item['deadline']}
    runtime=SimpleNamespace(tokens={'synthetic':item})
    @contextmanager
    def session(_actor):yield SimpleNamespace()
    monkeypatch.setattr(routes,'require_runtime',lambda _actor:None)
    monkeypatch.setattr(routes,'actor_session',session)
    monkeypatch.setattr(routes,'own_job',lambda _session,_id:job)
    monkeypatch.setattr(routes,'runtime',runtime)
    app=FastAPI()
    @app.exception_handler(ApiError)
    async def failure(_request,exc):
        return JSONResponse(status_code=exc.status_code,content={'error':{'code':exc.code}})
    app.dependency_overrides[auth.get_request_actor]=lambda:who[0]
    app.include_router(routes.router)
    with TestClient(app) as client:yield client,who,job


def test_routes_return_only_original_session_request_and_accept_only_original_binding(route_context,item):
    client,who,_job=route_context
    pending,output,thread=begin(item)
    ctx=item['context']['hmi'];base='/api/v1/assistant/jobs/'+item['id']
    params={'installation_id':ctx['installation_id'],'window_id':ctx['window_id']}
    response=client.get(base+'/local-requests',params=params)
    assert response.status_code==200 and response.json()['data']['items'][0]['id']==pending['id']
    assert client.get(base+'/local-requests',params={**params,'window_id':'other'}).status_code==403
    original=who[0];who[0]=actor(public_id=original.public_id)
    assert client.get(base+'/local-requests',params=params).status_code==403
    who[0]=original
    body={'request_id':pending['id'],**params,'result':{'status':'permission_required','unread':True}}
    assert client.post(base+'/local-results',json={**body,'installation_id':str(uuid4())}).status_code==403
    assert client.post(base+'/local-results',json={**body,'callback_url':'https://example.invalid'}).status_code==422
    assert client.post(base+'/local-results',json={**body,'request_id':str(uuid4())}).status_code==409
    response=client.post(base+'/local-results',json=body)
    assert response.status_code==200 and response.json()['data']=={'accepted':True,'replayed':False}
    assert client.post(base+'/local-results',json=body).json()['data']['replayed'] is True
    assert finish(output,thread)['data']['status']=='permission_required'


def test_route_oversized_json_and_nonfinite_result_do_not_consume_pending_request(route_context,item):
    client,_who,_job=route_context
    pending,output,thread=begin(item)
    ctx=item['context']['hmi'];base='/api/v1/assistant/jobs/'+item['id']+'/local-results'
    body={'request_id':pending['id'],'installation_id':ctx['installation_id'],'window_id':ctx['window_id']}
    assert client.post(base,json={**body,'result':{'v':'x'*(local.MAX_RESULT_BYTES+2048)}}).status_code==413
    raw=json.dumps({**body,'result':{'v':float('nan')}})
    assert client.post(base,content=raw,headers={'Content-Type':'application/json'}).status_code==422
    assert len(local.poll(item))==1
    local.complete(item,pending['id'],{'status':'read'});finish(output,thread)


def test_route_rejects_results_after_task_terminal_transition(route_context,item):
    client,_who,job=route_context
    pending,output,thread=begin(item);ctx=item['context']['hmi']
    job['state']='CANCELLED'
    response=client.post('/api/v1/assistant/jobs/'+item['id']+'/local-results',json={
        'request_id':pending['id'],'installation_id':ctx['installation_id'],'window_id':ctx['window_id'],'result':{'status':'read'}})
    assert response.status_code==409
    local.close(item);assert finish(output,thread)['data']['status']=='cancelled'


def test_actual_route_auth_dependency_rejects_anonymous_bearer_and_invalid_csrf(monkeypatch):
    from starlette.requests import Request
    from app.core import auth,db
    from app.core.config import Settings
    from app.services.auth import AuthService
    settings=Settings(_env_file=None,auth_mode='session')
    monkeypatch.setattr(auth,'get_settings',lambda:settings)
    principal=SimpleNamespace(id=uuid4(),session_id=uuid4(),username='synthetic',display_name='Synthetic',
        role='TEST_EXECUTOR',kind='HUMAN',session_type='BROWSER',expires_at=datetime.now(timezone.utc)+timedelta(hours=1),must_change_password=False)
    class Session:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def commit(self):pass
    monkeypatch.setattr(db,'SessionLocal',Session)
    monkeypatch.setattr(AuthService,'resolve',lambda _self,token:principal if token=='synthetic-cookie' else None)
    monkeypatch.setattr(AuthService,'verify_csrf',staticmethod(lambda _principal,value:value=='synthetic-csrf'))
    def request(headers=None):
        return Request({'type':'http','method':'POST','path':'/api/v1/assistant/jobs/'+str(uuid4())+'/local-results',
            'headers':[(k.lower().encode(),v.encode()) for k,v in (headers or {}).items()],
            'client':('127.0.0.1',1234),'scheme':'https','server':('synthetic',443),'query_string':b''})
    error_code('authentication_required',lambda:auth.get_request_actor(request()))
    error_code('bearer_not_allowed',lambda:auth.get_request_actor(request({'Authorization':'Bearer synthetic'})))
    error_code('csrf_invalid',lambda:auth.get_request_actor(request({'Cookie':'rp1_session=synthetic-cookie; rp1_csrf=synthetic-csrf'})))
    who=auth.get_request_actor(request({'Cookie':'rp1_session=synthetic-cookie; rp1_csrf=synthetic-csrf','X-CSRF-Token':'synthetic-csrf'}))
    assert who.public_id==principal.id and who.session_public_id==principal.session_id


@pytest.mark.parametrize('revoked',[True,False])
def test_tool_authorize_rechecks_original_login_and_rejects_revocation_or_replacement(monkeypatch,item,revoked):
    from app.services import assistant_chat as chat
    runtime=chat.ChatRuntime();token='synthetic-tool-grant';runtime.tokens[chat.grants.key(token)]=item
    grant=SimpleNamespace(cookie='synthetic-cookie',user_id=item['actor'].public_id,session_id=item['actor'].session_public_id)
    monkeypatch.setattr(chat.grants,'access',lambda _token:grant)
    principal=None if revoked else SimpleNamespace(id=item['actor'].public_id,session_id=uuid4(),must_change_password=False)
    class Session:
        def __enter__(self):return self
        def __exit__(self,*_):pass
    monkeypatch.setattr(chat,'SessionLocal',Session)
    monkeypatch.setattr(chat.AuthService,'resolve',lambda _self,_cookie:principal)
    error_code('authentication_required',lambda:runtime.authorize(token))


@pytest.mark.parametrize('image_count,text_count',[(2,0),(3,0),(2,1),(1,2),(0,3)])
def test_real_uploaded_image_and_csv_previews_pack_into_six_field_pi_contract(monkeypatch,tmp_path,image_count,text_count):
    import io
    from PIL import Image
    from app.services import assistant_attachments as attachments
    from app.services.assistant_chat import attachment_model_payload
    settings=SimpleNamespace(assistant_attachment_root=str(tmp_path/'owned-attachments'),archive_root=str(tmp_path/'unused-archives'))
    monkeypatch.setattr(attachments,'get_settings',lambda:settings)
    owner=actor();image=io.BytesIO();Image.new('RGB',(4,4),'red').save(image,format='PNG')
    metadata=[attachments.upload(owner,image.getvalue(),f'screenshot-{n}.png','image/png') for n in range(image_count)]
    metadata.extend(attachments.upload(owner,b'time,temp\n0,30\n',f'run-{n}.csv','text/csv') for n in range(text_count))
    state={};content=attachments.model_content(owner,metadata,state)
    original={'view':'home','hmi':{'installation_id':str(uuid4()),'window_id':'original'}}
    before=deepcopy(original);context,images=attachment_model_payload(original,content)
    payload={'tool_grant':'synthetic','messages':[{'body':'diagnose'}],'choices':{},'context':context,'attachments':images,'timeout_ms':120000}
    assert len(payload)==6 and len(images)==image_count<=3
    assert original==before and 'diagnostic_attachment_previews' not in original
    assert sum(map(len,context['diagnostic_attachment_previews']))<=24000
    assert all(set(image)=={'type','mimeType','data'} and image['type']=='image' for image in images)
    combined='\n'.join(context['diagnostic_attachment_previews'])
    assert all(row['id'] in combined for row in metadata)
    if image_count>=2:
        # Preserve the original discovery: directly forwarding model_content
        # would send 4..6 blocks and violate Pi's unchanged max3 contract.
        assert len(content)>3


def test_pi_attachment_packer_refuses_oversized_preview_unknown_blocks_and_four_images():
    from app.services.assistant_chat import attachment_model_payload
    image={'type':'image','mimeType':'image/png','data':'YWJj'}
    for content in [[image]*4,[{'type':'text','text':'x'*24001}],[{'type':'file','path':'/outside'}]]:
        error_code('invalid_attachment_preview',lambda:attachment_model_payload({},content))


def test_frozen_attachment_inventory_keeps_all_three_ids_hashes_when_previews_are_missing():
    from app.services.assistant_chat import attachment_model_payload
    identifiers=[str(uuid4()) for _ in range(3)]
    metadata=[{'id':identifier,'name':f'raw-{n}.csv','kind':'text','sha256':f'{n+1:064x}',
               'size':30000,'owner_public_id':'must-not-send','internal_path':'must-not-send'}
              for n,identifier in enumerate(identifiers)]
    context={'view':'home','hmi':{'installation_id':str(uuid4()),'window_id':'paired'}}
    original=deepcopy(context);frozen=deepcopy(metadata)
    result,images=attachment_model_payload(context,[],metadata)
    inventory=result['diagnostic_attachment_inventory']
    assert len(inventory)==3 and [row['id'] for row in inventory]==identifiers
    assert [row['sha256'] for row in inventory]==[row['sha256'] for row in metadata]
    assert all(set(row)=={'id','name','kind','sha256','size'} for row in inventory)
    assert images==[] and context==original and metadata==frozen


def test_inventory_redacts_sensitive_filename_but_preserves_valid_uuid_and_checksum():
    from app.services.assistant_chat import attachment_model_payload
    identifier='11111111-1111-4111-8111-13800138000a'
    checksum='a'*20+'13800138000'+'b'*33
    metadata=[{'id':identifier,'name':'user@example.com-13800138000.csv','kind':'text','sha256':checksum,'size':100}]
    result,_=attachment_model_payload({},[],metadata)
    item=result['diagnostic_attachment_inventory'][0]
    assert item['id']==identifier
    assert item['sha256']==checksum
    assert 'user@example.com' not in item['name'] and '13800138000' not in item['name']


def test_frozen_attachment_inventory_is_independent_of_24k_text_budget():
    from app.services.assistant_chat import attachment_model_payload
    metadata=[{'id':str(uuid4()),'name':f'{i}.bin','kind':'binary','sha256':f'{i:064x}','size':1000} for i in range(3)]
    result,images=attachment_model_payload({'view':'home'},[{'type':'text','text':'x'*24000}],metadata)
    assert len(result['diagnostic_attachment_inventory'])==3 and images==[]
    assert sum(map(len,result['diagnostic_attachment_previews']))==24000


def test_public_tool_uuid_evidence_preserves_routing_ids_but_not_private_identity_or_plain_phone():
    from app.services.assistant_privacy import public_evidence
    identifier='11111111-1111-4111-8111-13800138000a'
    value={key:identifier for key in ('attachment_id','job_id','request_id')}
    value.update(session_id=identifier,owner_public_id=identifier,phone='13800138000',
                 message='call13800138000now',attachment_name='13800138000.csv')
    result=public_evidence(value)
    assert all(result[key]==identifier for key in ('attachment_id','job_id','request_id'))
    assert 'session_id' not in result and 'owner_public_id' not in result and 'phone' not in result
    assert '13800138000' not in result['message'] and '13800138000' not in result['attachment_name']
    assert public_evidence({'attachment_id':'13800138000'})['attachment_id']!='13800138000'
