"""Bounded cross-request and cross-object OTA lifecycle tests; synthetic only."""
import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch
import pytest

from test_module_firmware_maintenance import signed, make_task, begin, wait, observation
from test_module_firmware_gateway import setup_service
from factory_hmi.core.backend import FakeMotorBackend, BackendInUseError
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.app import create_app


class Exclusive(FakeMotorBackend):
    def _exclusive_resources(self):
        return ('isolated-terminal-persistence-bus',)


@pytest.mark.parametrize('failure', ['blocked-save', 'save-error', 'close-error'])
def test_terminal_persistence_and_close_keep_cross_object_reservations(tmp_path, failure):
    first, second = Exclusive(1), Exclusive(1)
    first.open_transport()
    task, selections, written, _ = make_task(tmp_path/'first')
    terminal_entered, finish_save = threading.Event(), threading.Event()
    fail = {'save': failure == 'save-error', 'close': failure == 'close-error'}
    real_save = task._save
    transport = first.maintenance_transport()
    class Reservation:
        def __enter__(self):return transport.__enter__()
        def close(self):
            if fail['close']:raise OSError('synthetic resource close failure')
            transport.close()
    task.reserve = Reservation
    def save():
        if task._job and task._job.get('ended_at'):
            terminal_entered.set()
            if failure == 'blocked-save' and not finish_save.wait(3):raise TimeoutError('bounded terminal gate')
            if fail['save']:raise OSError('synthetic terminal journal failure')
        return real_save()
    task._save = save
    other = None
    service = FactoryService(data_root=tmp_path/'service')
    service._maintenance = task
    try:
        begin(task, selections);wait(task,'awaiting_power');task._stop.set()
        assert terminal_entered.wait(1)
        if failure == 'blocked-save':
            assert task._thread.is_alive()
            with pytest.raises(TimeoutError):task.cancel(.01)
        else:
            task._thread.join(1);assert not task._thread.is_alive()
            assert task.active and task.snapshot()['cleanup_quarantined']
            with pytest.raises(RuntimeError, match='收尾失败'):service.close()
            assert not service._closed
        with pytest.raises(BackendInUseError):second.open_transport()
        other, selection2, written2, _ = make_task(tmp_path/'second')
        begin(other, selection2);wait(other,'failed')
        assert written2 == [] and written == [0,1]
        other.cancel()
        finish_save.set();fail.update(save=False,close=False)
        assert service.close()['closed']
        assert not task.active and not task._serial_owned
        second.open_transport();second.close_transport()
        # Explicit new work is allowed only after the previous terminal save
        # and resource close succeeded. No OTA was retried during cleanup.
        begin(other, selection2);wait(other,'awaiting_power')
        assert written2 == [0,1] and written == [0,1]
    finally:
        finish_save.set();fail.update(save=False,close=False)
        task.cancel()
        if other is not None:other.cancel()
        service.close();first.close_transport();second.close_transport()


@pytest.mark.parametrize('transition', ['logout', 'login'])
@pytest.mark.parametrize('first_action', ['transition', 'start'])
def test_account_transition_and_start_share_commit_boundary(tmp_path, signed, monkeypatch, transition, first_action):
    import httpx
    from factory_hmi.gateway import motor_maintenance as permissions
    from factory_hmi.core import motor_ota_transport as transport
    downloads, *_ = signed
    authorized = {'name': 'executor'}
    revoke_entered, release_revoke = threading.Event(), threading.Event()
    commit_entered, release_commit = threading.Event(), threading.Event()
    writes = []
    def authorize(_):
        if authorized['name'] is None:raise ValueError('logged out')
        return {'username': authorized['name']}
    def writer(row, data, stop, progress):
        writes.append(row['index'])
        if not stop.wait(2):raise TimeoutError('bounded writer gate')
        raise InterruptedError('synthetic account transition')
    monkeypatch.setattr(permissions,'authorize',authorize)
    monkeypatch.setattr(transport,'write_firmware',writer)
    monkeypatch.setattr(transport,'read_version',lambda row,stop:observation(row['index']))
    service = setup_service(tmp_path)
    with patch('factory_hmi.gateway.app.PlcCabinetService'):
        app = create_app(service,data_root=tmp_path/'gateway',lease_timeout_s=5)
    hub=app.state.runtime;hub.motor_firmware=downloads
    runtime=hub.session('client');runtime.lease.claim('client')
    def revoke():
        revoke_entered.set()
        if not release_revoke.wait(3):raise TimeoutError('bounded revoke gate')
    hub.diagnostics.revoke=revoke
    def logout():authorized['name']=None;return {}
    def login(body):authorized['name']='other';return {'operator':'other'}
    runtime.platform_logout=logout;runtime.platform_login=login
    async def poll(event):
        for _ in range(300):
            if event.is_set():return
            await asyncio.sleep(.005)
        raise AssertionError('gate not reached')
    async def run():
        async def pulse():
            while True:await asyncio.sleep(.01)
        timer=asyncio.create_task(pulse())
        pending=[]
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'client'}) as client:
                    preview=await client.post('/api/v1/motor-maintenance/preview',json={'selections':[dict(index=0,release_id='a'*32)]})
                    assert preview.status_code==200,preview.text
                    task=service._maintenance
                    real_artifact=task.artifact
                    def gate_artifact(*args):
                        if first_action=='start':
                            commit_entered.set()
                            if not release_commit.wait(3):raise TimeoutError('bounded commit gate')
                        return real_artifact(*args)
                    task.artifact=gate_artifact
                    def start():return asyncio.create_task(client.post('/api/v1/motor-maintenance/start',json=dict(preview_id=preview.json()['result']['preview_id'],confirmed=True)))
                    def change():return asyncio.create_task(client.post('/api/v1/platform/'+transition,json={} if transition=='logout' else dict(username='other',password='synthetic',station_id='')))
                    if first_action=='transition':
                        changing=change();pending.append(changing);await poll(revoke_entered)
                        starting=start();pending.append(starting)
                        await asyncio.sleep(.05)
                        assert not starting.done() and writes==[]
                    else:
                        starting=start();pending.append(starting);await poll(commit_entered)
                        changing=change();pending.append(changing)
                        await asyncio.sleep(.05)
                        assert not changing.done() and not revoke_entered.is_set()
                    # Account transition / a waiting start may hold the account
                    # boundary, but the independent heartbeat must still work.
                    at=time.monotonic()
                    heartbeat=await client.post('/api/v1/lease/heartbeat',headers={'X-RP1-Lease-Generation':str(runtime.lease.snapshot().generation)})
                    assert heartbeat.status_code==200 and time.monotonic()-at<.5
                    release_commit.set();release_revoke.set()
                    start_result,change_result=await asyncio.wait_for(asyncio.gather(starting,changing),3)
                    assert change_result.status_code==200,change_result.text
                    if first_action=='transition':
                        assert start_result.status_code==409 and writes==[]
                    else:
                        assert start_result.status_code==200,start_result.text
                        assert task._stop.is_set()
                    assert not task.active and (task._thread is None or not task._thread.is_alive())
                    count=len(writes);await asyncio.sleep(.05);assert len(writes)==count
                    assert authorized['name']==(None if transition=='logout' else 'other')
        finally:
            release_revoke.set();release_commit.set()
            for request in pending:
                if not request.done():request.cancel()
            if pending:await asyncio.gather(*pending,return_exceptions=True)
            timer.cancel()
            try:await timer
            except asyncio.CancelledError:pass
    asyncio.run(run())


def test_failed_reservation_entry_cannot_release_an_existing_backend_owner():
    first,second=Exclusive(1),Exclusive(1)
    first.open_transport();first.enable_motors()
    lease=first.maintenance_transport()
    try:
        with pytest.raises(RuntimeError):lease.__enter__()
        lease.close() # cleanup of a failed entry must not steal an older claim
        assert first.connected and first.motors_enabled
        with pytest.raises(BackendInUseError):second.open_transport()
    finally:first.close_transport();second.close_transport()


def test_backend_release_error_retains_the_actual_handle_until_explicit_retry(tmp_path):
    first,second=Exclusive(1),Exclusive(1)
    first.open_transport()
    task,selections,written,_=make_task(tmp_path)
    task.reserve=first.maintenance_transport
    release=first._release_resources
    failure={'enabled':True}
    def failed_release():
        if failure['enabled']:raise OSError('synthetic backend ownership release failure')
        release()
    first._release_resources=failed_release
    try:
        begin(task,selections);wait(task,'awaiting_power');task._stop.set();task._thread.join(1)
        assert not task._thread.is_alive() and task.active and first._maintenance_active
        with pytest.raises(BackendInUseError):second.open_transport()
        with pytest.raises(RuntimeError,match='收尾失败'):task.cancel()
        assert task._serial_owned and written==[0,1]
        failure['enabled']=False;task.cancel()
        assert not task.active and not first._maintenance_active
        second.open_transport();second.close_transport()
    finally:
        failure['enabled']=False;task.cancel();first.close_transport();second.close_transport()
