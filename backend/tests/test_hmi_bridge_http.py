"""Actual ASGI requests through the HMI routes, without application startup."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx

ROOT = Path('/workspace') if Path('/workspace/hmi').is_dir() else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'hmi'))


def test_hmi_http_login_screenshot_provenance_logout_and_size_limits(tmp_path,monkeypatch):
    from factory_hmi.gateway.app import create_app
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    monkeypatch.setenv('RP1_FACTORY_OPERATOR_TOKEN','synthetic-local-token')
    monkeypatch.setenv('RP1_FACTORY_PLATFORM_MODE','reliability_v1')
    app = create_app(controller=SimpleNamespace(),data_root=tmp_path)
    hub = app.state.runtime
    hub._platform._uploader = SimpleNamespace(client=ReliabilityPlatformClient('https://fixture.invalid'))
    hub._platform.login = Mock(return_value={'operator':'synthetic-user'})
    hub._platform.logout = Mock(return_value={'status':'logged_out'})
    remote = hub.diagnostics._remote = Mock(return_value={'mode':'assistant_runtime'})
    async def check():
        # ASGITransport does not start lifespan, so PLC/CAN cannot start.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://synthetic.local') as client:
            headers = {'X-RP1-Client-ID':'a','X-RP1-Operator-Token':'synthetic-local-token'}
            assert (await client.post('/api/v1/diagnostics/status',json={})).status_code == 401
            response = await client.post('/api/v1/platform/login',headers=headers,json={'username':'user','password':'synthetic-password','station_id':'A10'})
            assert response.status_code == 200,response.text
            linked = await client.post('/api/v1/diagnostics/status',headers=headers,json={})
            assert linked.status_code == 200,linked.text
            binding = linked.json()['result']
            generation = binding['generation']
            remote.return_value = {'id':str(uuid4()),'thread_id':str(uuid4()),'state':'QUEUED'}
            response = await client.post('/api/v1/diagnostics/submit',headers=headers,json={'generation':generation,'request_key':str(uuid4()),'message':'synthetic query','context':{'hmi':{'installation_id':'forged'}}})
            assert response.status_code == 200,response.text
            assert remote.call_args.args[3]['context']['hmi'] == binding['hmi']
            assert (await client.post('/api/v1/diagnostics/status',headers={**headers,'X-RP1-Client-ID':'b'},json={})).status_code == 401
            response = await client.post('/api/v1/diagnostics/upload',headers={**headers,'X-RP1-Diagnostic-Generation':generation,'Content-Type':'image/png'},content=b'synthetic-image')
            assert response.status_code == 200,response.text
            assert remote.call_args.kwargs['raw'] == b'synthetic-image'
            calls = remote.call_count
            for upload_headers in (
                {**headers, 'Content-Type': 'image/png'},
                {**headers, 'X-RP1-Diagnostic-Generation': 'stale-generation', 'Content-Type': 'image/png'},
            ):
                assert (await client.post('/api/v1/diagnostics/upload',headers=upload_headers,content=b'synthetic-image')).status_code == 401
                assert remote.call_count == calls
            upload_headers = {**headers, 'X-RP1-Diagnostic-Generation': generation, 'Content-Type': 'image/png'}
            response = await client.post('/api/v1/diagnostics/upload',headers=upload_headers,content=b'x'*(32*1024**2))
            assert response.status_code == 200,response.text
            assert len(remote.call_args.kwargs['raw']) == 32*1024**2
            calls = remote.call_count
            assert (await client.post('/api/v1/diagnostics/upload',headers=upload_headers,content=b'x'*(32*1024**2+1))).status_code == 413
            assert remote.call_count == calls
            assert (await client.post('/api/v1/diagnostics/status',headers=headers,content=b'[]')).status_code == 422
            assert (await client.post('/api/v1/platform/logout',headers=headers,json={})).status_code == 200
            assert (await client.post('/api/v1/diagnostics/submit',headers=headers,json={'generation':generation})).status_code == 401
            assert hub._started is False
    asyncio.run(check())
