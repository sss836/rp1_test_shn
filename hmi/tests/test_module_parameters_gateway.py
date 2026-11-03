"""ASGI lease/account pairing and cancellation; no hardware or model calls."""
import asyncio
from copy import deepcopy
import time
from unittest.mock import patch
import pytest
from test_module_parameters import template,record
from test_module_firmware_gateway import setup_service
from test_module_firmware_maintenance import observation
from factory_hmi.gateway.app import create_app

@pytest.mark.parametrize('ending',['verify','cancel','logout','service-close'])
def test_gateway_parameter_workflow(tmp_path,monkeypatch,ending):
    import httpx
    from factory_hmi.gateway import motor_maintenance as permissions,motor_parameters as templates
    from factory_hmi.core import motor_parameter_transport as params,motor_ota_transport as versions
    monkeypatch.setattr(permissions,'authorize',lambda d:dict(username='executor',client_session='test',role='TEST_EXECUTOR'))
    rec=record();monkeypatch.setattr(templates.Templates,'query',lambda self,o:[deepcopy(rec)])
    monkeypatch.setattr(versions,'read_version',lambda row,stop:observation(row['index']))
    values={(i,key):([100] if key=='kt' else [0,400]) for i in range(2) for key in ('kt','kp')};writes=[]
    def reader(row,item,stop):return dict(status='read',raw=deepcopy(values[row['index'],item['key']]))
    def writer(row,item,stop):
        for _ in range(5):
            if stop.wait(.005):raise InterruptedError('synthetic cancelled')
        writes.append((row['index'],item['key']));values[row['index'],item['key']]=deepcopy(item['expected_raw']);return {'synthetic':True}
    monkeypatch.setattr(params,'read_parameter',reader);monkeypatch.setattr(params,'write_parameter',writer)
    service=setup_service(tmp_path)
    with patch('factory_hmi.gateway.app.PlcCabinetService'):app=create_app(service,data_root=tmp_path/'gateway',lease_timeout_s=5)
    hub=app.state.runtime;runtime=hub.session('paired');runtime.lease.claim('paired')
    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'paired'}) as c:
                get=lambda:c.get('/api/v1/motor-parameters')
                async def action(name,**body):return await c.post('/api/v1/motor-parameters/'+name,json=body)
                async def wait_state(expected):
                    for _ in range(200):
                        r=await get();assert r.status_code==200,r.text
                        j=r.json()['result']['job']
                        if j['status']==expected and (expected=='awaiting_power' or not j['active']):return j
                        await asyncio.sleep(.005)
                    raise AssertionError(j)
                r=await c.get('/api/v1/motor-parameters/0/templates');assert r.status_code==200
                selections=[dict(index=0,template_id='a'*32)]
                wrong=await c.post('/api/v1/motor-parameters/read',headers={'X-RP1-Client-ID':'wrong'},json={'selections':selections});assert wrong.status_code==409
                r=await action('read',selections=selections);assert r.status_code==200,r.text
                j=await wait_state('checked_with_differences');assert not writes
                r=await action('preview',read_job_id=j['job_id']);assert r.status_code==200,r.text
                p=r.json()['result'];r=await action('start',preview_id=p['preview_id'],confirmed=True);assert r.status_code==200,r.text
                j=await wait_state('awaiting_power')
                t=time.monotonic();heartbeat=await c.post('/api/v1/lease/heartbeat',headers={'X-RP1-Lease-Generation':str(runtime.lease.snapshot().generation)})
                assert heartbeat.status_code==200 and time.monotonic()-t<.5
                denied=await c.post('/api/v1/motors/enable',json={'physical_estop_confirmed':True});assert denied.status_code==409
                early=await action('readback',job_id=j['job_id']);assert early.status_code==409
                if ending=='verify':
                    ack=await action('acknowledge',job_id=j['job_id'],confirmed=True);assert ack.status_code==200
                    assert not ack.json()['result']['targets'][0]['post_power_verified']
                    r=await action('readback',job_id=j['job_id']);assert r.status_code==200
                    j=await wait_state('verified');assert j['targets'][0]['post_power_verified']
                elif ending=='cancel':
                    r=await action('cancel',job_id=j['job_id']);assert r.status_code==200
                    j=await wait_state('interrupted')
                elif ending=='logout':
                    runtime.platform_logout=lambda:{}
                    r=await c.post('/api/v1/platform/logout');assert r.status_code==200,r.text
                    assert service._parameters.snapshot()['status']=='interrupted'
                else:
                    service.close();assert not service._parameters.active
    asyncio.run(run())
    assert service._closed and not service._parameters.active and writes==[(0,'kt'),(0,'kp')]
