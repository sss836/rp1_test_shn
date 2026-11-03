"""Stream protocol and request identity checks without DB, models or devices."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from app.schemas.assistant_chat import ChatSubmit, SteeringInput
from app.services.assistant_workspace import consume_bridge,context_payload,request_digest_payload,thread_title,reference_rows


def test_new_optional_fields_preserve_old_request_identity():
    payload=ChatSubmit(request_key=uuid4(),message='查询工位',context={'view':'stations','kind':'MODULE'})
    result=request_digest_payload(payload)
    assert 'after_job_id' not in result
    assert 'references' not in result['context']
    assert context_payload(payload.context)=={'view':'stations','kind':'MODULE','selection_empty':False,'metrics':[],'subjects':[]}
    next_payload=payload.model_copy(update={'after_job_id':uuid4()})
    assert request_digest_payload(next_payload)['after_job_id']


def test_reference_inputs_cannot_choose_an_account_or_host():
    with pytest.raises(ValidationError):
        ChatSubmit(request_key=uuid4(),message='test',context={'references':[{'kind':'assets','id':str(uuid4()),'user_id':str(uuid4())}]})
    with pytest.raises(ValidationError):
        ChatSubmit(request_key=uuid4(),message='test',context={'references':[{'kind':'files','id':'/etc/passwd'}]})
    with pytest.raises(ValidationError):
        SteeringInput(request_key=uuid4(),expected_job_id=uuid4(),message='new',role='SYSTEM_ADMIN')


def test_automatic_titles_redact_secrets_and_keep_readable_names():
    assert thread_title('   解释\n电机状态  ')=='解释 电机状态'
    assert len(thread_title('界'*200))==64
    assert '13800138000' not in thread_title('电话13800138000')


class Parts(httpx.AsyncByteStream):
    def __init__(self,parts):self.parts=parts
    async def __aiter__(self):
        for part in self.parts:yield part


def read(parts,events):
    response=httpx.Response(200,headers={'Content-Type':'application/x-ndjson'},stream=Parts(parts))
    async def on_event(event):events.append(event)
    return asyncio.run(consume_bridge(response,on_event))


def test_fragmented_unicode_ndjson_keeps_order_and_final_receipt():
    stream=('\n'.join(json.dumps(row,ensure_ascii=False) for row in [
        {'kind':'text','data':{'text':'正在读取诊断日志','round':1}},
        {'kind':'tool','data':{'tool':'read_local_diagnostic','status':'completed'}},
        {'kind':'result','data':{'reply':'仅合成数据','rounds':1}}])+'\n').encode()
    events=[];status,data=read([stream[:13],stream[13:51],stream[51:]],events)
    assert status==200 and data['reply']=='仅合成数据'
    assert [e['kind'] for e in events]==['text','tool']
    assert events[0]['data']['text']=='正在读取诊断日志'


@pytest.mark.parametrize('parts',[
    [b'{"kind":"text","data":{"text":"unsettled"}}\n'],
    [b'{"kind":"result","data":{}}'],
    [b'{"kind":"result","data":{}}\n{"kind":"result","data":{}}\n'],
    [b'x'*(256*1024+1)],
])
def test_eof_without_final_or_invalid_record_never_means_completed(parts):
    with pytest.raises((ValueError,json.JSONDecodeError)):read(parts,[])


def test_legacy_json_bridge_preserves_failure_and_billing_reason():
    async def on_event(_event):raise AssertionError('not a progress stream')
    response=httpx.Response(502,json={'error':'billing_unverified'})
    assert asyncio.run(consume_bridge(response,on_event))==(502,{'error':'billing_unverified'})


def test_reference_kind_never_interpolates_user_sql():
    from app.core.errors import ApiError
    with pytest.raises(ApiError):reference_rows(None,'assets; DROP TABLE test.asset')


def test_waiting_followup_does_not_occupy_an_execution_worker(monkeypatch):
    from app.services.assistant_chat import ChatRuntime
    async def exercise():
        runtime=ChatRuntime();runtime.queue=asyncio.Queue(maxsize=100)
        runtime.pending={'child':('owner','cookie'),'independent':('other','cookie')}
        runtime.queue.put_nowait('child');runtime.queue.put_nowait('independent')
        started=asyncio.Event();order=[]
        monkeypatch.setattr(runtime,'dependency_ready',lambda key,actor:key!='child')
        async def run(key,*args):order.append(key);started.set()
        monkeypatch.setattr(runtime,'run',run)
        worker=asyncio.create_task(runtime.consume())
        try:
            await asyncio.wait_for(started.wait(),1)
            assert order==['independent']
            assert 'child' in runtime.pending and 'child' not in runtime.running
        finally:
            worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(exercise())


def test_references_freeze_authoritative_labels_not_client_text(monkeypatch):
    from app.services import assistant_workspace as workspace
    from app.schemas.assistant_chat import AssistantReference
    ref=AssistantReference(kind='assets',id=uuid4(),label='untrusted description')
    monkeypatch.setattr(workspace,'reference_rows',lambda *args,**kwargs:[{'label':'actual-visible-asset'}])
    workspace.validate_references(None,[ref])
    assert ref.label=='actual-visible-asset'
