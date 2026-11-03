"""Real ASGI routes with fake motors and synthetic signed distribution artifacts."""
import asyncio
from dataclasses import replace
import time
from unittest.mock import patch
import pytest
from test_module_firmware_maintenance import signed, observation
from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.service import FactoryService
from factory_hmi.core.config import load_station_config
from factory_hmi.gateway.app import create_app


def setup_service(tmp_path):
    config=load_station_config('left_short_arm')
    entries=tuple(replace(e,interface='can-fixture',motor_id=i+1,motor_model=0) for i,e in enumerate(config.entries[:2]))
    config=replace(config,entries=entries)
    backend=FakeMotorBackend(entries)
    backend.motor_versions=lambda:[observation(i) for i in range(2)]
    service=FactoryService(data_root=tmp_path/'service')
    service._controller=FactoryController(config,backend,enable_recording=False)
    service._backend_name='fake';service._can_interfaces=('can-fixture',);service._fake_can_connected=True
    service._can_bindings=(dict(index=0,interface='can-fixture',mode='canfd',bitrate=1000000,dbitrate=5000000),)
    service.controller.discover_motors()
    return service


def test_full_gateway_job_paired_session_and_independent_heartbeat(tmp_path,signed,monkeypatch):
    import httpx
    from factory_hmi.gateway import motor_maintenance as permissions
    from factory_hmi.core import motor_ota_transport as transport
    downloads,*_=signed
    writes=[]
    def writer(row,data,stop,progress):
        writes.append(row['index'])
        # A slow device operation must not hold the request/control heartbeat path.
        for i in range(20):
            if stop.wait(.01):raise InterruptedError('cancelled fixture')
        progress(len(data),len(data));return dict(selfcheck_response='ff0201',reset_sent=False)
    def reader(row,stop):
        r=observation(row['index'])
        if row['index'] in writes:r.update(raw_firmware='1.0.10',software='1.0.10')
        return r
    monkeypatch.setattr(transport,'write_firmware',writer);monkeypatch.setattr(transport,'read_version',reader)
    monkeypatch.setattr(permissions,'authorize',lambda d:dict(username='executor'))
    service=setup_service(tmp_path)
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app=create_app(service,data_root=tmp_path/'gateway',lease_timeout_s=5)
    hub=app.state.runtime
    hub.motor_firmware=downloads
    runtime=hub.session('client');runtime.lease.claim('client')
    async def run():
        async def pulse():
            while True:await asyncio.sleep(.01)
        timer=asyncio.create_task(pulse())
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'client'}) as client:
                    result=await client.get('/api/v1/motor-maintenance');assert result.status_code==200,result.text
                    assert len(result.json()['result']['targets'])==2
                    other=await client.post('/api/v1/motor-maintenance/preview',headers={'X-RP1-Client-ID':'wrong-client'},json={'selections':[dict(index=0,release_id='a'*32)]})
                    assert other.status_code==409
                    preview=await client.post('/api/v1/motor-maintenance/preview',json={'selections':[dict(index=0,release_id='a'*32)]})
                    assert preview.status_code==200,preview.text
                    start=await client.post('/api/v1/motor-maintenance/start',json=dict(preview_id=preview.json()['result']['preview_id'],confirmed=True))
                    assert start.status_code==200,start.text
                    job=start.json()['result']['job_id']
                    for _ in range(10):
                        at=time.monotonic()
                        beat=await client.post('/api/v1/lease/heartbeat',headers={'X-RP1-Lease-Generation':str(runtime.lease.snapshot().generation)})
                        assert beat.status_code==200,beat.text
                        assert time.monotonic()-at<.5
                        await asyncio.sleep(.02)
                    blocked=await client.post('/api/v1/motors/enable',json={'physical_estop_confirmed':True})
                    assert blocked.status_code == 409 and "维护占用" in blocked.text
                    for _ in range(100):
                        status=(await client.get('/api/v1/motor-maintenance')).json()['result']['job']
                        if status['status']=='awaiting_power':break
                        await asyncio.sleep(.01)
                    assert status['status']=='awaiting_power',status
                    early=await client.post('/api/v1/motor-maintenance/readback',json={'job_id':job});assert early.status_code==409
                    ack=await client.post('/api/v1/motor-maintenance/acknowledge',json={'job_id':job,'confirmed':True})
                    assert ack.status_code==200 and not ack.json()['result']['targets'][0]['actual_version_verified']
                    read=await client.post('/api/v1/motor-maintenance/readback',json={'job_id':job});assert read.status_code==200,read.text
                    for _ in range(100):
                        status=(await client.get('/api/v1/motor-maintenance')).json()['result']['job']
                        if not status['active']:break
                        await asyncio.sleep(.01)
                    assert status['status']=='verified' and writes==[0]
        finally:
            timer.cancel()
            try:await timer
            except asyncio.CancelledError:pass
    asyncio.run(run())
    assert service._closed and not service._maintenance._thread.is_alive()


@pytest.mark.parametrize('ending',['cancel','logout','lease-expiry'])
def test_interruption_cancels_worker_and_stops_next_target(tmp_path,signed,monkeypatch,ending):
    import httpx
    from factory_hmi.gateway import motor_maintenance as permissions
    from factory_hmi.core import motor_ota_transport as transport
    downloads,*_=signed
    writes=[]
    def writer(row,data,stop,progress):
        writes.append(row['index'])
        stop.wait(3)
        raise InterruptedError('synthetic partial-write interruption')
    monkeypatch.setattr(transport,'write_firmware',writer)
    monkeypatch.setattr(transport,'read_version',lambda row,stop:observation(row['index']))
    monkeypatch.setattr(permissions,'authorize',lambda d:dict(username='executor'))
    service=setup_service(tmp_path)
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app=create_app(service,data_root=tmp_path/'gateway',lease_timeout_s=.8)
    hub=app.state.runtime;hub.motor_firmware=downloads
    runtime=hub.session('client');runtime.lease.claim('client')
    async def run():
        async def pulse():
            while True:await asyncio.sleep(.01)
        timer=asyncio.create_task(pulse())
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'client'}) as client:
                    selections=[dict(index=i,release_id='a'*32) for i in range(2)]
                    preview=await client.post('/api/v1/motor-maintenance/preview',json={'selections':selections});assert preview.status_code==200,preview.text
                    start=await client.post('/api/v1/motor-maintenance/start',json=dict(preview_id=preview.json()['result']['preview_id'],confirmed=True));assert start.status_code==200,start.text
                    for _ in range(100):
                        if writes:break
                        await asyncio.sleep(.005)
                    assert writes==[0]
                    if ending=='cancel':
                        response=await client.post('/api/v1/motor-maintenance/cancel',json={'job_id':start.json()['result']['job_id']});assert response.status_code==200,response.text
                    elif ending=='logout':
                        response=await client.post('/api/v1/platform/logout');assert response.status_code==200,response.text
                    for _ in range(300):
                        status=service._maintenance.snapshot()
                        if not status['active']:break
                        await asyncio.sleep(.01)
                    assert status['status']=='interrupted',status
                    assert status['targets'][0]['power_cycle_required'] and status['targets'][1]['status']=='not_run' and writes==[0]
                    assert not service._maintenance._thread.is_alive()
                    if ending=='lease-expiry':
                        for _ in range(200):
                            if runtime.lease.snapshot().owner is None:break
                            await asyncio.sleep(.01)
                        assert runtime.lease.snapshot().owner is None and runtime._cleanup_error is None
        finally:
            timer.cancel()
            try:await timer
            except asyncio.CancelledError:pass
    asyncio.run(run())
