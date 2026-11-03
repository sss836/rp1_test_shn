"""ASGI and client checks without network, upload workers or device operations."""
import asyncio
from unittest.mock import Mock, patch

import httpx
import pytest

from factory_hmi.client import GatewayClient
from factory_hmi.gateway.app import create_app
from test_execution_history import service, seed


def test_local_endpoints_offline_auth_validation_identity_and_no_upload(service, monkeypatch):
    seed(service, 'test', '2026-10-28T00:00:00Z', 'old')
    monkeypatch.setenv('RP1_FACTORY_OPERATOR_TOKEN', 'synthetic-token')
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app = create_app(service, data_root=service.report_manager.data_root)
    runtime = app.state.runtime.session('local-window')
    runtime._platform.wake = Mock(side_effect=AssertionError('must not wake upload'))
    runtime._platform.submit = Mock(side_effect=AssertionError('must not submit'))
    service.outbox.enqueue = Mock(side_effect=AssertionError('must not enqueue'))
    body = dict(changes={'sample_id': '000001'}, reason='合成更正', expected_revision=0, request_id='a' * 32)
    headers = {'X-RP1-Operator-Token': 'synthetic-token', 'X-RP1-Client-ID': 'local-window'}
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as c:
                denied = await c.post('/api/v1/executions/test/local-corrections', json=body)
                assert denied.status_code in {401, 403}
                read = await c.get('/api/v1/executions/test/local-corrections', headers=headers)
                assert read.status_code == 200 and read.json()['result']['revision'] == 0
                for invalid in [dict(body, actor='forged'), dict(body, expected_revision=True)]:
                    r = await c.post('/api/v1/executions/test/local-corrections', json=invalid, headers=headers)
                    assert r.status_code == 422, r.text
                r = await c.post('/api/v1/executions/test/local-corrections', json=dict(body, changes={'verdict': 'passed'}), headers=headers)
                assert r.status_code == 409 and '只允许' in r.text
                saved = await c.post('/api/v1/executions/test/local-corrections', json=body, headers=headers)
                assert saved.status_code == 200, saved.text
                state = saved.json()['result']
                assert state['records'][0]['actor'] == '本机窗口:local-window'
                retry = await c.post('/api/v1/executions/test/local-corrections', json=body, headers=headers)
                assert retry.status_code == 200 and retry.json()['result']['created'] is False
                conflict = await c.post('/api/v1/executions/test/local-corrections', json=dict(body, request_id='b' * 32, changes={'notes': 'changed'}), headers=headers)
                assert conflict.status_code == 409
                page = await c.get('/api/v1/executions?sn=000001', headers=headers)
                assert page.status_code == 200 and page.json()['total'] == 1
                export = await c.get('/api/v1/executions/export.csv?sn=000001', headers=headers)
                assert export.status_code == 200 and '本地已更正' in export.content.decode('utf-8-sig')
                assert runtime._platform._identity == '' and service.outbox.list() == []
    asyncio.run(run())
    runtime._platform.wake.assert_not_called(); runtime._platform.submit.assert_not_called()
    service.outbox.enqueue.assert_not_called()


def test_client_local_edit_calls_only_local_endpoint():
    client = GatewayClient('http://isolated')
    with patch.object(client, 'get', return_value={}) as get, patch.object(client, 'post', return_value={}) as post:
        client.execution_local_corrections('test')
        payload = dict(changes={'notes': 'new'}, reason='why', expected_revision=0, request_id='a' * 32)
        client.save_local_correction('test', payload)
        get.assert_called_once_with('/api/v1/executions/test/local-corrections')
        post.assert_called_once_with('/api/v1/executions/test/local-corrections', payload)
