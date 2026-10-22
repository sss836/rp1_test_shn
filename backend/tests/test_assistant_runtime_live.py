"""Small official-model smoke check; not the M5 accuracy evaluation."""
import os
import time
from uuid import uuid4
import pytest
from tests.test_assistant_integration import environment,signed_in,heartbeat

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_LIVE'),reason='requires explicit live-key isolated runner')]


def wait(client,identifier):
    until=time.monotonic()+125
    while time.monotonic()<until:
        response=client.get('/api/v1/assistant/jobs/'+identifier);assert response.status_code==200,response.text
        job=response.json()['data']
        if job['state'] not in {'RUNNING','QUEUED'}:return job
        time.sleep(.2)
    raise AssertionError('live task deadline')


def test_official_query_and_explicit_creation_are_accounted(environment):
    _,users=environment
    with signed_in(users['first']) as client:
        heartbeat(client,'SYNTHETIC-LIVE-M4')
        response=client.post('/api/v1/assistant/jobs',json={'request_key':str(uuid4()),'message':'请查询我当前有权查看的工位状态，列出工位名和更新时间。','context':{'view':'stations','kind':'MODULE'}})
        assert response.status_code==202,response.text
        queried=wait(client,response.json()['data']['id']);assert queried['state']=='COMPLETED',queried
        assert any(card['type']=='station_status' for card in queried['result']['cards']),queried
        code='SYNTHETIC-LIVE-M4-'+uuid4().hex[:8]
        key=str(uuid4());payload={'request_key':key,'message':f'请创建项目，项目编号 {code}，项目名称 合成官方链路项目。不要添加项目目标或其他未提供的字段。','context':{'view':'planning','kind':'MODULE'}}
        queued=client.post('/api/v1/assistant/jobs',json=payload);assert queued.status_code==202,queued.text
        created=wait(client,key);assert created.get('receipt'),created
        assert client.post('/api/v1/assistant/jobs',json=payload).json()['data']['replayed']
        budget=client.get('/api/v1/assistant/budget').json()['data'];assert not budget['blocked'] and float(budget['spent_cny'])>0 and float(budget['reserved_cny'])==0
