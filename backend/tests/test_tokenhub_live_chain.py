"""Opt-in paid provider check, isolated accounts/database and synthetic evidence."""
import io
import json
import os
from pathlib import Path
import sys
import time
from decimal import Decimal
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import text
from tests.test_assistant_integration import environment, signed_in, heartbeat, PASSWORD

ROOT = Path('/workspace') if Path('/workspace/hmi').is_dir() else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'hmi'))

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv('ASSISTANT_TOKENHUB_LIVE') != '1' or not os.getenv('ASSISTANT_QA_ADMIN_URL'),
    reason='explicit isolated live TokenHub runner required')]


def test_real_pi_gateway_text_image_tools_and_budget(environment,tmp_path):
    from factory_hmi.gateway.diagnostics import DiagnosticBridge
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    engine,users = environment
    client = ReliabilityPlatformClient(os.environ['ASSISTANT_QA_HTTP_URL'])
    grant = client.authenticate_operator(users['first'],PASSWORD)
    bridge = DiagnosticBridge(tmp_path)
    bridge.bind(client,'live-synthetic-window',grant['user']['username'],'SYNTHETIC-A10')
    linked = bridge.call('live-synthetic-window','status')
    model = linked['capabilities']['model']
    assert model['provider'] == 'tokenhub' and model['verified'] is True
    assert model['key_type'] == 'enterprise_api_key'
    assert model['billing_basis'] == 'published_peak_upper_bound'
    assert model['supplier_invoice_verified'] is False
    assert linked['capabilities']['limits'] == dict(daily_cny=100,concurrency=2,rounds=6,seconds=120,chat_days=30)
    generation = linked['generation']
    observed = {'model_configuration':model,'hmi':linked['hmi'],'checks':[]}
    target = Path('/reports/live-chain-results.json')

    def save(): target.write_text(json.dumps(observed,ensure_ascii=False,indent=2,default=str))

    def call(action,payload=None,**kwargs):
        return bridge.call('live-synthetic-window',action,{'generation':generation,**(payload or {})},**kwargs)

    def wait(job):
        deadline = time.monotonic()+130
        while time.monotonic()<deadline:
            result = call('job',{'id':job['id']})
            if result['state'] not in {'QUEUED','RUNNING'}:
                observed['checks'].append({'job':result});save()
                return result
            time.sleep(.2)
        raise AssertionError('isolated live job did not reach a terminal receipt')

    with signed_in(users['first']) as first,signed_in(users['second']) as other:
        heartbeat(first,'SYNTHETIC-LIVE-TOKENHUB')
        queued = call('submit',{'request_key':str(uuid4()),'message':
            '这是隔离验收。请务必调用 list_hmi_stations 查询当前账号可见的工位，回答其名称、在线状态和来源。仅查询，不创建或修改任何业务对象。'})
        job = wait(queued)
        assert job['state'] == 'COMPLETED',job
        assert job['context']['hmi'] == linked['hmi']
        cards = job['result']['cards']
        stations = [c for c in cards if c.get('source') == '/api/v1/hmi/stations' and c.get('type') != 'query_issue']
        assert stations and 'SYNTHETIC-LIVE-TOKENHUB' in json.dumps(stations),job
        assert other.get('/api/v1/assistant/jobs/'+queued['id']).status_code == 404
        assert other.get('/api/v1/assistant/threads/'+queued['thread_id']).status_code == 404
        buffer = io.BytesIO()
        Image.new('RGB',(32,32),(32,112,224)).save(buffer,format='PNG')
        uploaded = call('upload',raw=buffer.getvalue(),mime='image/png')
        assert other.get('/api/v1/assistant/attachments/'+uploaded['id']).status_code == 404
        queued_image = call('submit',{'request_key':str(uuid4()),'attachments':[uploaded['id']],
            'message':'这是合成图片验收。仅根据本条上传的图片，用中文回答图片主要是什么颜色；不需要查询或修改业务对象。'})
        image_job = wait(queued_image)
        assert image_job['state'] == 'COMPLETED' and '蓝' in image_job['result']['reply'],image_job
        budget = first.get('/api/v1/assistant/budget').json()['data']
        assert not budget['blocked'] and Decimal(str(budget['reserved_cny'])) == 0
        assert 0 < Decimal(str(budget['spent_cny'])) < 100
        history = call('history',{'id':queued['thread_id']})
        assert history['messages'][-1]['cards']
        observed['budget'] = budget
        with engine.connect() as connection:
            rows = connection.execute(text('SELECT job_id,sequence,state,reserved,charged,rates,usage FROM integration.assistant_model_call ORDER BY job_id,sequence')).mappings().all()
            assert 3 <= len(rows) <= 12
            from app.services.assistant_billing import usage_charge
            for row in rows:
                assert row['state'] == 'SETTLED' and row['charged'] == usage_charge(row['usage'],row['rates'])
                assert row['rates']['model'] == 'deepseek-v4.1-flash-1017'
                assert row['rates']['basis'] == 'published_peak_upper_bound'
                assert row['rates']['profile_sha256'] == model['profile_sha256']
                assert row['rates']['supplier_invoice_verified'] is False
            assert sum(r['charged'] for r in rows) == Decimal(str(budget['spent_cny']))
            assert connection.execute(text('SELECT count(*) FROM integration.setup_receipt')).scalar_one() == 0
            assert connection.execute(text('SELECT count(*) FROM test.test_execution')).scalar_one() == 0
            observed['model_calls'] = [dict(row) for row in rows]
        observed.update(status='passed',supplier_invoice_verified=False,enterprise_billing_gate_waived=True)
        save()
    client.logout_operator()
    from fastapi import HTTPException
    with pytest.raises(HTTPException): bridge.call('live-synthetic-window','status')
