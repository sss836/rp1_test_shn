"""Real production-mode HTTPS gate on disposable data and official DeepSeek."""
import os
import time
from uuid import uuid4
import httpx
import pytest
from tests.test_assistant_integration import environment,PASSWORD

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_PRODUCTION'),reason='explicit production-shaped disposable stack required')]


@pytest.mark.parametrize('role',['admin','first'])
def test_https_pilot_query_and_explicit_project_write(environment,role):
    _,users=environment
    with httpx.Client(base_url='https://web:8443',verify=False,trust_env=False,timeout=130) as client:
        login=client.post('/api/v1/auth/login',json={'username':users[role],'password':PASSWORD})
        assert login.status_code==200
        assert any(cookie.secure for cookie in client.cookies.jar if cookie.name=='rp1_session')
        client.headers['X-CSRF-Token']=client.cookies['rp1_csrf']
        assert client.get('/api/v1/assistant/capabilities').json()['data']['mode']=='assistant_runtime'
        assert client.post('/api/v1/assistant/probe',json={}).status_code==410
        assert client.post('/api/v1/assistant/tools/model_call',json={}).status_code==404
        code='SYNTHETIC-PROD-'+uuid4().hex[:8]
        key=str(uuid4())
        message=f'请创建项目，编号 {code}，名称 {code}。不要添加其他未提供的字段。'
        payload={'request_key':key,'message':message,'context':{'view':'planning','kind':'MODULE'}}
        assert client.post('/api/v1/assistant/jobs',json=payload).status_code==202
        until=time.monotonic()+125
        while time.monotonic()<until:
            job=client.get('/api/v1/assistant/jobs/'+key).json()['data']
            if job['state'] not in {'QUEUED','RUNNING'}:break
            time.sleep(.1)
        assert job.get('receipt') and job['result']['business_status']=='saved',job
        assert client.post('/api/v1/assistant/jobs',json=payload).json()['data']['replayed']
        budget=client.get('/api/v1/assistant/budget').json()['data']
        assert not budget['blocked'] and float(budget['reserved_cny'])==0 and float(budget['spent_cny'])>0
        assert client.get('/api/v1/hmi/stations').status_code==200


def test_https_anonymous_and_viewer_remain_denied(environment):
    _,users=environment
    with httpx.Client(base_url='https://web:8443',verify=False,trust_env=False) as client:
        assert client.get('/api/v1/assistant/capabilities').status_code==401
        assert client.post('/api/v1/auth/login',json={'username':users['viewer'],'password':PASSWORD}).status_code==200
        assert client.get('/api/v1/assistant/capabilities').status_code==403
