"""This milestone: disposable PostgreSQL + real Pi + synthetic TokenHub SSE."""
import base64
import json
import os
import sys
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from decimal import Decimal

import pytest
import httpx
from sqlalchemy import text
from tests.test_assistant_integration import environment, signed_in, normal_model, heartbeat, PASSWORD
from tests.test_assistant_runtime import control, submit, wait, model_requests

ROOT = Path('/workspace') if Path('/workspace/hmi').is_dir() else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'hmi'))
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from fastapi import HTTPException

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL'), reason='dedicated isolated stack required')]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jBz0AAAAASUVORK5CYII=')


@pytest.fixture(autouse=True)
def finance(environment):
    engine, _ = environment
    with engine.begin() as c:
        c.execute(text('DELETE FROM integration.assistant_model_call'))
        c.execute(text('DELETE FROM integration.assistant_budget_day'))
        c.execute(text("UPDATE integration.assistant_finance SET blocked=false,reason=NULL"))


def test_hmi_login_context_text_tools_history_and_cross_account_rejection(environment, tmp_path):
    _, users = environment
    client = ReliabilityPlatformClient(os.environ['ASSISTANT_QA_HTTP_URL'])
    grant = client.authenticate_operator(users['first'], PASSWORD)
    bridge = DiagnosticBridge(tmp_path)
    bridge.bind(client, 'window-a', grant['user']['username'], 'SYNTHETIC-A10')
    linked = bridge.call('window-a', 'status')
    assert linked['hmi']['installation_id'] and linked['hmi']['station'] == 'SYNTHETIC-A10'
    generation = linked['generation']
    with pytest.raises(HTTPException) as exc: bridge.call('window-b', 'status')
    assert exc.value.status_code == 401
    queued = bridge.call('window-a', 'submit', {'generation': generation, 'request_key': str(uuid4()), 'message': '查询工位'})
    with signed_in(users['first']) as first, signed_in(users['second']) as other:
        result = wait(first, queued['id'])
        assert result['state'] == 'COMPLETED', result
        assert result['context']['hmi'] == linked['hmi']
        assert result['result']['cards'][0]['source'] == '/api/v1/hmi/stations'
        assert other.get('/api/v1/assistant/jobs/'+queued['id']).status_code == 404
        assert other.get('/api/v1/assistant/threads/'+queued['thread_id']).status_code == 404
        caps = first.get('/api/v1/assistant/capabilities').json()['data']
        assert caps['model']['provider'] == 'tokenhub' and caps['model']['verified']
        assert caps['limits'] == dict(daily_cny=100, concurrency=2, rounds=6, seconds=120, chat_days=30)
    history = bridge.call('window-a', 'history', {'generation': generation, 'id': queued['thread_id']})
    assert history['messages'][-1]['cards']
    client.logout_operator()
    with pytest.raises(HTTPException): bridge.call('window-a', 'status')
    assert bridge._binding is None


def test_screenshot_selects_vision_and_account_isolation_with_real_usage_ledger(environment):
    engine, users = environment
    with signed_in(users['first']) as first, signed_in(users['second']) as other:
        upload = first.post('/api/v1/assistant/attachments', content=PNG, headers={'Content-Type':'image/png', 'X-Filename':'fixture.png'})
        assert upload.status_code == 201, upload.text
        identifier = upload.json()['data']['id']
        assert other.get('/api/v1/assistant/attachments/'+identifier).status_code == 404
        response = first.post('/api/v1/assistant/jobs', json={'request_key': str(uuid4()), 'message':'查看合成截图并查询工位', 'attachments':[identifier]})
        assert response.status_code == 202, response.text
        done = wait(first, response.json()['data']['id'])
        assert done['state'] == 'COMPLETED', done
        requests = model_requests()
        assert requests[0]['model'] == 'fixture-vision' and requests[0]['image_count'] == 1
        assert len(requests) == 2 and not any(r['contains_rp1_credentials'] for r in requests)
        budget = first.get('/api/v1/assistant/budget').json()['data']
        assert Decimal(str(budget['spent_cny'])) == Decimal('0.00128000') and Decimal(str(budget['reserved_cny'])) == 0
        foreign = other.post('/api/v1/assistant/jobs', json={'request_key':str(uuid4()),'message':'读取附件','attachments':[identifier]})
        assert foreign.status_code == 404
    with engine.connect() as c:
        rows = c.execute(text("SELECT rates,usage,charged FROM integration.assistant_model_call")).mappings().all()
        assert len(rows) == 2
        assert all(r['rates']['model'] == 'fixture-vision' and r['rates']['profile_sha256'] and r['usage']['total_tokens'] == 120 for r in rows)


def test_logout_revokes_inflight_tool_and_role_loss_revokes_association(environment, tmp_path):
    engine, users = environment
    control(mode='pause_before_tool')
    with signed_in(users['first']) as first:
        queued = submit(first)
        time.sleep(.3)
        assert first.post('/api/v1/auth/logout').status_code == 204
        time.sleep(2)
        with signed_in(users['first']) as again:
            done = wait(again, queued['id'])
            assert done['state'] != 'COMPLETED', done
            assert not (done.get('result') or {}).get('cards')
    client = ReliabilityPlatformClient(os.environ['ASSISTANT_QA_HTTP_URL'])
    client.authenticate_operator(users['second'], PASSWORD)
    bridge = DiagnosticBridge(tmp_path)
    bridge.bind(client, 'w', users['second'], 'A10')
    assert bridge.call('w','status')
    with engine.begin() as c: c.execute(text("UPDATE iam.app_user SET role='VIEWER' WHERE username=:name"), {'name':users['second']})
    try:
        with pytest.raises(HTTPException): bridge.call('w','status')
        assert bridge._binding is None
    finally:
        with engine.begin() as c: c.execute(text("UPDATE iam.app_user SET role='TEST_EXECUTOR' WHERE username=:name"), {'name':users['second']})


def test_round_limit_concurrency_and_timeout_keep_business_http_responsive(environment):
    _, users = environment
    control(mode='repeat')
    with signed_in(users['first']) as first:
        done = wait(first, submit(first)['id'])
        assert done['state'] == 'COMPLETED' and done['result']['completion'] == 'partial'
        assert len(model_requests()) == 6
    control(mode='pause_before_tool')
    with signed_in(users['first']) as first:
        jobs = [submit(first) for _ in range(3)]
        time.sleep(.3)
        states = [first.get('/api/v1/assistant/jobs/'+j['id']).json()['data']['state'] for j in jobs]
        assert states.count('RUNNING') <= 2 and 'QUEUED' in states
        start = time.monotonic()
        heartbeat(first, 'CONTROL-INDEPENDENCE-MOCK')
        elapsed = time.monotonic()-start
        assert elapsed < 1, elapsed
        for job in jobs: assert first.post('/api/v1/assistant/jobs/'+job['id']+'/cancel').status_code == 200
    # Pending/abandoned calls freeze new spending. Test recovery is explicit and
    # isolated; production has no automatic finance reset.
    time.sleep(2)


def test_budget_exhaustion_refuses_model_request(environment):
    engine, users = environment
    with engine.begin() as c: c.execute(text("INSERT INTO integration.assistant_budget_day(day,spent,reserved) VALUES((now() AT TIME ZONE 'Asia/Shanghai')::date,100,0)"))
    with signed_in(users['first']) as first:
        done = wait(first, submit(first)['id'])
        assert done['state'] == 'FAILED' and model_requests() == []


def test_missing_usage_freezes_finance(environment):
    _, users = environment
    control(mode='no_usage')
    with signed_in(users['first']) as first:
        done = wait(first, submit(first)['id'])
        assert done['state'] == 'FAILED'
        budget = first.get('/api/v1/assistant/budget').json()['data']
        assert budget['blocked'] and Decimal(str(budget['reserved_cny'])) > 0


def test_slow_model_hits_shortened_deadline_and_freezes_unverified_spend(environment):
    _, users = environment
    control(mode='slow')
    with signed_in(users['first']) as first:
        start = time.monotonic()
        done = wait(first, submit(first)['id'])
        elapsed = time.monotonic()-start
        assert done['state'] == 'TIMED_OUT', done
        assert 2 <= elapsed < 6, elapsed
        budget = first.get('/api/v1/assistant/budget').json()['data']
        assert budget['blocked'] and Decimal(str(budget['reserved_cny'])) > 0
