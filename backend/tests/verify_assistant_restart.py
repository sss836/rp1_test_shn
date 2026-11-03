"""Keep a synthetic original session in memory across a real isolated API restart.

Only scripts/test-assistant-workspace.py --restart-check orchestrates this. No
cookie is written to a marker, database, report or log by this fixture.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from uuid import uuid4
import httpx
from sqlalchemy import create_engine,text
from tests.test_assistant_integration import signed_in
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from factory_hmi.core.diagnostic_files import DiagnosticFileReader
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient

assert os.environ['ASSISTANT_QA_HTTP_URL']=='http://web:8080'
assert os.environ['ASSISTANT_QA_ADMIN_URL'].endswith('/rp1_presence_assistant')
report=Path('/reports');root=Path(tempfile.mkdtemp(prefix='restart-evidence-'))
engine=create_engine(os.environ['ASSISTANT_QA_ADMIN_URL'])
record={'mode':'real isolated API restart; synthetic files, original cookie held only in fixture memory','status':'not_run'}


def until(fn,predicate,seconds=20):
    deadline=time.monotonic()+seconds;value=None
    while time.monotonic()<deadline:
        value=fn()
        if predicate(value):return value
        time.sleep(.05)
    raise AssertionError('restart fixture condition timed out: '+str(value))


try:
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM integration.assistant_model_call'))
        connection.execute(text('DELETE FROM integration.assistant_budget_day'))
        connection.execute(text('UPDATE integration.assistant_finance SET blocked=false,reason=NULL'))
    httpx.post('http://model-mock:3100/control',json={'mode':'normal'},trust_env=False).raise_for_status()
    state=root/'state';state.mkdir();project=root/'project';project.mkdir()
    raw=b'time_s,value\n0,1\n1,2\n2,3\n4,99\n';(state/'data.csv').write_bytes(raw)
    (state/'communication.log').write_text('2026-11-01T00:00:03Z ConnectionRefusedError: connection refused\n')
    with signed_in('assistant-qa-first') as client:
        platform=ReliabilityPlatformClient('http://web:8080')
        for cookie in client.cookies.jar:platform.cookies.set_cookie(copy.copy(cookie))
        window='synthetic-restart-window'
        def new_bridge():
            bridge=DiagnosticBridge(state);bridge.files=DiagnosticFileReader(state,roots=[project,state])
            bridge.bind(platform,window,'assistant-qa-first','SYNTHETIC-RESTART')
            return bridge,bridge.call(window,'status')['generation']
        bridge,generation=new_bridge()
        job=bridge.call(window,'submit',{'generation':generation,'request_key':str(uuid4()),'message':'检查CSV缺失和连接超时日志'})
        path='/api/v1/assistant/jobs/'+job['id']
        until(lambda:client.get(path).json()['data'],lambda j:j['state']=='WAITING_PERMISSION')
        bridge.call(window,'job',{'generation':generation,'id':job['id']})
        before=until(lambda:client.get(path).json()['data'],lambda j:j['state']=='WAITING_PERMISSION' and j.get('read_count',0)>=2)
        with engine.connect() as connection:
            saved=connection.execute(text('SELECT input_hash,active_elapsed_ms,read_count,rounds,resume_state FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert saved['resume_state']['cards'] and saved['read_count']>=2
        record.update(job_id=job['id'],before_state=before['state'],before_read_count=saved['read_count'],before_rounds=saved['rounds'],before_active_elapsed_ms=saved['active_elapsed_ms'])
        (report/'restart-ready.json').write_text(json.dumps({'job_id':job['id'],'state':before['state']}))
        until(lambda:(report/'restart-completed').exists(),bool,seconds=40)
        def ready():
            try:return client.get('/readyz').status_code==200
            except httpx.HTTPError:return False
        until(ready,bool)
        restored=client.get(path).json()['data']
        assert restored['state']=='WAITING_PERMISSION' and restored['read_count']==saved['read_count'] and restored['rounds']==saved['rounds']
        assert restored['active_elapsed_ms']==saved['active_elapsed_ms']
        with engine.connect() as connection:
            assert connection.execute(text('SELECT input_hash FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).scalar_one()==saved['input_hash']
        # New bridge has no old generation, aggregate or receipt memory. The
        # same original login and paired window are re-attested by the server.
        bridge,generation=new_bridge()
        final=until(lambda:bridge.call(window,'job',{'generation':generation,'id':job['id']}),lambda j:j['state'] not in {'QUEUED','RUNNING','WAITING_PERMISSION'})
        assert final['state']=='COMPLETED',final
        cards=final['result']['cards'];csv=next(c['data'] for c in cards if c.get('type')=='local_diagnostic' and c.get('data',{}).get('operation')=='csv_diagnosis')
        assert csv['complete'] and csv['source_sha256']==hashlib.sha256(raw).hexdigest()
        assert final['read_count']<=12 and final['rounds']<=6
        with engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM integration.setup_receipt WHERE request_key=:id'),{'id':job['id']}).scalar_one()==0
        record.update(status='passed',after_state=final['state'],after_read_count=final['read_count'],after_rounds=final['rounds'],
            original_job_retained=True,original_input_hash_retained=True,original_cookie_revalidated=True,
            prior_evidence_retained=True,temporary_file_full_hash_matched=True,business_writes=0)
finally:
    (report/'restart-result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2))
    engine.dispose();shutil.rmtree(root)
print('isolated API restart and original-session evidence recovery: passed')
