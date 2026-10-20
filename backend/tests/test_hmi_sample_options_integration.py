import os
from uuid import uuid4
import pytest
from sqlalchemy import text
from fastapi.testclient import TestClient
from app.main import app
from test_hmi_presence_integration import environment, signed_in, heartbeat_payload

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'), reason='isolated database required')]


def test_registered_samples_include_cross_account_running_but_no_operator_details(environment):
    engine, users = environment
    with signed_in(users['admin']) as admin, signed_in(users['first']) as first, signed_in(users['second']) as second:
        suffix = uuid4().hex[:8]
        code = f'RP1.3-SARM-{int(suffix,16)%900+100:03d}'
        created = admin.post('/api/v1/setup/assets', json=dict(reason='Isolated sample picker QA', asset_code=code,
            asset_kind='MODULE', target_part_code='SARM', product_family='QA', serial_number=suffix,
            display_name='Synthetic picker sample', model='QA', configuration_fingerprint='QA-'+suffix))
        assert created.status_code == 201, created.text
        def option():
            response = second.get('/api/v1/hmi/samples')
            assert response.status_code == 200, response.text
            return next(item for item in response.json()['data'] if item['code']==code)
        assert option()['running'] is False
        payload = heartbeat_payload()
        payload['runs'][0].update(sample_id=code, state='running')
        route = '/api/v1/hmi/presence/' + str(uuid4())
        assert first.put(route, json=payload).status_code == 200
        assert option()['running'] is True
        assert set(option()) == {'id','code','asset_kind','target_part_code','running'}
        # Local-only runs are also visible as busy; no execution context is needed.
        for seq,state,running in [(2,'paused',False),(3,'stopping',True),(4,'completed',False),(5,'running',True)]:
            payload['sequence']=seq;payload['runs'][0]['state']=state
            assert first.put(route, json=payload).status_code == 200
            assert option()['running'] is running
        with engine.begin() as db:
            db.execute(text("UPDATE integration.hmi_presence SET snapshot_at=clock_timestamp()-interval '21 seconds' WHERE connection_id=CAST(:id AS uuid)"), {'id':route.rsplit('/',1)[-1]})
        assert option()['running'] is False
        payload['sequence']=6
        assert first.put(route, json=payload).status_code == 200
        assert option()['running'] is True
        assert first.post('/api/v1/auth/logout').status_code == 204
        assert option()['running'] is False


def test_sample_endpoint_requires_active_operator_login(environment):
    _, users = environment
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/v1/hmi/samples').status_code == 401
    with signed_in(users['viewer']) as viewer:
        assert viewer.get('/api/v1/hmi/samples').status_code == 403
