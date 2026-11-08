"""Original HMI transport -> API/Pi/mock -> native temporary CSV/log receipts."""
import copy
import hashlib
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.test_assistant_integration import environment, normal_model, signed_in
from tests.test_assistant_runtime import clear_finance, control, submit, wait


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("ASSISTANT_QA_ADMIN_URL") or bool(os.getenv("ASSISTANT_QA_LIVE")),
        reason="standalone synthetic assistant diagnosis stack required",
    ),
]


def test_web_approval_then_original_hmi_actually_reads_external_temporary_file(environment,tmp_path):
    from factory_hmi.gateway.diagnostics import DiagnosticBridge
    from factory_hmi.core.diagnostic_files import DiagnosticFileReader
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    engine,users=environment
    state=tmp_path/'state';project=tmp_path/'project';state.mkdir();project.mkdir()
    outside=tmp_path/'outside.csv';raw=b'time_s,value\n0,1\n1,2\n2,3\n4,99\n';outside.write_bytes(raw)
    control(tool='read_local_diagnostic',arguments={'operation':'csv_diagnosis','path':str(outside)})
    bridge=DiagnosticBridge(state);bridge.files=DiagnosticFileReader(state,roots=[project,state])
    with signed_in(users['first']) as original,signed_in(users['first']) as webpage:
        platform=ReliabilityPlatformClient('http://web:8080')
        for cookie in original.cookies.jar:platform.cookies.set_cookie(copy.copy(cookie))
        window='synthetic-consent-window';bridge.bind(platform,window,users['first'],'SYNTHETIC-CONSENT')
        generation=bridge.call(window,'status')['generation']
        queued=bridge.call(window,'submit',{'generation':generation,'request_key':str(uuid4()),'message':'读取这个文件'})
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            current=bridge.call(window,'job',{'generation':generation,'id':queued['id']})
            if current.get('local_requests'):break
            time.sleep(.03)
        pending=current['local_requests'][0]
        assert pending['state']=='pending' and pending['path']==str(outside)
        assert original.delete('/api/v1/assistant/threads/'+queued['thread_id']).status_code==409
        with engine.connect() as connection:
            assert connection.execute(text('SELECT result FROM integration.assistant_read_permission WHERE id=:id'),{'id':pending['id']}).scalar_one() is None
        decision={'request_key':str(uuid4()),'expected_request_sha256':pending['request_sha256'],'scope':'file'}
        path=f"/api/v1/assistant/jobs/{queued['id']}/permissions/{pending['id']}/decision"
        response=webpage.post(path,json=decision);assert response.status_code==200,response.text
        assert webpage.post(path,json=decision).json()['data']['replayed']
        with engine.connect() as connection:
            assert connection.execute(text('SELECT result FROM integration.assistant_read_permission WHERE id=:id'),{'id':pending['id']}).scalar_one() is None
        params={'installation_id':bridge.installation,'window_id':window}
        assert webpage.get('/api/v1/assistant/jobs/'+queued['id']+'/local-requests',params=params).status_code==403
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            current=bridge.call(window,'job',{'generation':generation,'id':queued['id']})
            if current['state'] not in {'QUEUED','RUNNING','WAITING_PERMISSION'}:break
            time.sleep(.03)
        assert current['state']=='COMPLETED',current
        receipts=[c['data'] for c in current['result']['cards'] if c.get('type')=='local_diagnostic' and c.get('data',{}).get('complete')]
        assert any(r.get('source_sha256')==hashlib.sha256(raw).hexdigest() and r['data']['gap_count']==1 for r in receipts)
        assert current['read_count']<=12 and current['rounds']<=6
        assert outside.read_bytes()==raw


def test_uploaded_csv_without_hmi_uses_real_frozen_inventory_for_proactive_analysis(environment):
    _,users=environment
    raw=('time_s,value\n'+''.join(f'{i},1\n' for i in range(15000))+'15002,9876\n').encode()
    with signed_in(users['first']) as client:
        response=client.post('/api/v1/assistant/attachments',content=raw,headers={'Content-Type':'text/csv','X-Filename-UTF8':'measurements.csv'})
        assert response.status_code==201,response.text
        meta=response.json()['data']
        response=client.post('/api/v1/assistant/jobs',json={'request_key':str(uuid4()),'message':'检查这个文件','attachments':[meta['id']]})
        assert response.status_code==202,response.text
        final=wait(client,response.json()['data']['id']);assert final['state']=='COMPLETED',final
        prepared=next(c['data'] for c in final['result']['cards'] if c['type']=='diagnosis_preparation')
        csv=next(c for c in prepared['evidence'] if c['type']=='diagnostic_attachment')
        assert csv['data']['range']['rows_analyzed']==15001
        assert csv['data']['time_analysis']['gap_examples'][0]['data_row']==15000
        assert csv['data']['columns'][1]['maximum']==9876
        assert csv['data']['full_file_statistics'] and csv['source_sha256']==hashlib.sha256(raw).hexdigest()


def test_waiting_for_two_hmi_reads_does_not_occupy_both_execution_workers(environment):
    engine,users=environment
    with signed_in(users['first']) as client:
        jobs=[]
        try:
            for i in range(2):
                context={'hmi':{'installation_id':str(uuid4()),'window_id':'synthetic-wait-'+str(i),'station':'SYNTHETIC','running_version':'synthetic'}}
                jobs.append(submit(client,'检查连接超时日志',context=context))
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                states=[client.get('/api/v1/assistant/jobs/'+j['id']).json()['data']['state'] for j in jobs]
                if all(s=='WAITING_PERMISSION' for s in states):break
                time.sleep(.02)
            assert states==['WAITING_PERMISSION','WAITING_PERMISSION'],states
            done=wait(client,submit(client,'查询当前工位状态')['id'])
            assert done['state']=='COMPLETED',done
            with engine.connect() as connection:
                assert connection.execute(text('SELECT count(*) FROM integration.assistant_model_call WHERE job_id IN (:a,:b)'),{'a':jobs[0]['id'],'b':jobs[1]['id']}).scalar_one()==0
                assert connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='rp1_app' AND state='idle in transaction'")).scalar_one()==0
        finally:
            for job in jobs:client.post('/api/v1/assistant/jobs/'+job['id']+'/cancel')


def test_diagnosis_auto_reads_csv_tail_and_existing_unuploaded_logs_with_original_job(environment,tmp_path):
    from factory_hmi.gateway.diagnostics import DiagnosticBridge
    from factory_hmi.core.diagnostic_files import DiagnosticFileReader
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    engine,users=environment
    state=tmp_path/'state';project=tmp_path/'project';state.mkdir();project.mkdir()
    raw=('time_s,current\n'+''.join(f'{i},1\n' for i in range(15000))+'15002,999\n').encode()
    (state/'current.csv').write_bytes(raw)
    log=b'2026-11-01T00:00:01Z open connection\n2026-11-01T00:00:02Z ConnectionRefusedError: connection refused\n2026-11-01T00:00:03Z timeout waiting for heartbeat\n'
    (state/'communication.log').write_bytes(log)
    bridge=DiagnosticBridge(state);bridge.files=DiagnosticFileReader(state,roots=[project,state])
    with signed_in(users['first']) as client:
        platform=ReliabilityPlatformClient('http://web:8080')
        for cookie in client.cookies.jar:platform.cookies.set_cookie(copy.copy(cookie))
        window='synthetic-evidence-window';bridge.bind(platform,window,users['first'],'SYNTHETIC')
        generation=bridge.call(window,'status')['generation']
        job=bridge.call(window,'submit',{'generation':generation,'request_key':str(uuid4()),'message':'检查CSV缺失与连接超时，读取相关日志给出原因'})
        deadline=time.monotonic()+25;states=[]
        while time.monotonic()<deadline:
            current=bridge.call(window,'job',{'generation':generation,'id':job['id']});states.append(current['state'])
            assert not current.get('local_requests'),current
            if current['state'] not in {'QUEUED','RUNNING','WAITING_PERMISSION'}:break
            time.sleep(.05)
        assert current['state']=='COMPLETED',current
        assert 'WAITING_PERMISSION' in states
        cards=current['result']['cards'];local=[c['data'] for c in cards if c.get('type')=='local_diagnostic']
        csv=next(c for c in local if c.get('operation')=='csv_diagnosis' and c.get('complete'))
        assert csv['data']['rows_analyzed']==15001 and csv['data']['gap_count']==1
        assert csv['data']['gaps'][0]['row']==15001 and csv['source_sha256']==hashlib.sha256(raw).hexdigest()
        logs=next(c for c in local if c.get('operation')=='log_diagnosis' and c.get('complete'))
        assert logs['data']['match_count']==2 and 'ConnectionRefusedError' in str(logs['data']['matches'])
        assert logs['source_sha256']==hashlib.sha256(log).hexdigest()
        with engine.connect() as connection:
            persisted=connection.execute(text('SELECT rounds,read_count,active_elapsed_ms,resume_count FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert 1<=persisted['rounds']<=6 and 4<=persisted['read_count']<=12 and persisted['active_elapsed_ms']<=120000
            assert persisted['resume_count']>=4
            assert connection.execute(text('SELECT count(*) FROM integration.setup_receipt WHERE request_key=:id'),{'id':job['id']}).scalar_one()==0
        assert (state/'current.csv').read_bytes()==raw and (state/'communication.log').read_bytes()==log
