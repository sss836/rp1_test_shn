"""Quick verification uses mock PLC and fake motors only."""
import json
import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from factory_hmi.gateway.app import create_app
from factory_hmi.quick_profiles import catalog, preset


def headers(owner='quick-a'):
    return {'X-RP1-Client-ID': owner}


@pytest.fixture
def gateway(tmp_path):
    with TestClient(create_app(data_root=tmp_path, lease_timeout_s=30)) as client:
        yield client


@pytest.mark.parametrize('profile', [p['id'] for p in catalog()])
def test_bundled_original_trajectories_load_with_matching_gateway_defaults(gateway, profile):
    item = preset(profile)
    response = gateway.post('/api/v1/quick/prepare', headers=headers(), json={
        'profile': profile, 'interfaces': [], 'backend': 'fake'})
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['result']['phase'] == 'ready'
    snapshot = value['snapshot']
    assert snapshot['trajectory']['sha256'] == item['sha256']
    assert len(snapshot['configured_motors']) == item['motor_count']
    assert not snapshot['station']['motors_enabled']
    buses = [m['bus'] for m in snapshot['configured_motors']]
    if profile == 'waist_hip':
        assert buses == ['can1', 'can0', 'can1', 'can0']
    else:
        assert set(buses) == set(item['interfaces'])


def prepare(client, profile='left_arm', owner='quick-a'):
    result = client.post('/api/v1/quick/prepare', headers=headers(owner), json={
        'profile': profile, 'backend': 'fake'})
    assert result.status_code == 200, result.text


def start(client, hours=.0002, confirmed=True, owner='quick-a', power_source=None):
    body = {'duration_hours': hours, 'physical_estop_confirmed': confirmed, 'sample_id': '离线任意样机'}
    if power_source is not None:
        body['power_source'] = power_source
    return client.post('/api/v1/quick/start', headers=headers(owner), json=body)


def status(client, owner='quick-a'):
    return client.get('/api/v1/quick/status', headers=headers(owner)).json()


def test_offline_start_returns_default_preflights_loops_and_auto_disables(gateway):
    prepare(gateway)
    response = start(gateway)
    assert response.status_code == 200, response.text
    runtime = gateway.app.state.runtime.session('quick-a')
    deadline = time.monotonic()+5
    while status(gateway)['phase'] not in {'completed', 'fault'} and time.monotonic()<deadline:
        time.sleep(.05)
    assert status(gateway)['phase'] == 'completed', status(gateway)
    state = runtime.snapshot()
    assert not state['station']['motors_enabled']
    assert not state['can']['all_up']
    assert response.json()['snapshot']['playback']['options']['speed'] == 1.0
    deadline = time.monotonic()+3
    while not list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json')) and time.monotonic()<deadline:
        time.sleep(.05)
    context = list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json'))
    assert len(context) == 1
    metadata = json.loads(context[0].read_text())
    assert metadata['active_seconds'] >= .7
    assert metadata['outcome'] == 'duration_complete'
    assert metadata['recording_scope'] == 'local'
    assert metadata['sample_id'] == '离线任意样机'
    assert metadata['campaign_id'] is None
    assert gateway.app.state.runtime.plc.snapshot()['phase'] == 'RUNNING'


def test_pause_then_end_only_releases_own_bus(gateway):
    prepare(gateway)
    prepare(gateway, profile='right_arm', owner='quick-b')
    response = start(gateway, hours=.01)
    assert response.status_code == 200, response.text
    paused = gateway.post('/api/v1/playback/pause', headers=headers())
    assert paused.status_code == 200, paused.text
    deadline=time.monotonic()+2
    while status(gateway)['phase'] != 'paused' and time.monotonic()<deadline:time.sleep(.05)
    assert status(gateway)['phase'] == 'paused'
    ended = gateway.post('/api/v1/quick/end', headers=headers())
    assert ended.status_code == 200, ended.text
    owners = gateway.app.state.runtime.interface_owners()
    assert 'can0' not in owners and owners['can1'] == 'quick-b'
    assert gateway.app.state.runtime.plc.snapshot()['phase'] == 'RUNNING'


def test_busy_bus_and_invalid_mapping_cannot_enable_or_steal_other_session(gateway):
    prepare(gateway)
    conflict = gateway.post('/api/v1/quick/prepare', headers=headers('quick-b'), json={
        'profile': 'left_short_arm', 'backend': 'fake'})
    assert conflict.status_code == 409
    assert gateway.app.state.runtime.interface_owners()['can0'] == 'quick-a'
    invalid = gateway.post('/api/v1/quick/prepare', headers=headers('quick-b'), json={
        'profile': 'waist_hip', 'interfaces': ['can0']*4, 'backend': 'fake'})
    assert invalid.status_code == 409
    assert gateway.app.state.runtime.plc.snapshot()['phase'] == 'OFF'


def test_unconfirmed_safety_nonfinite_hours_and_plc_failure_do_not_enable(gateway):
    prepare(gateway)
    assert start(gateway, confirmed=False).status_code == 409
    assert start(gateway, hours=0).status_code == 422
    runtime = gateway.app.state.runtime.session('quick-a')
    with mock.patch.object(gateway.app.state.runtime.plc, 'snapshot', return_value={
            'access_mode': 'monitor', 'communication_state': 'LIVE', 'stale': False}):
        response = start(gateway)
    assert response.status_code == 409
    assert not runtime.controller.controller.backend.motors_enabled
    assert status(gateway)['phase'] == 'fault'


def test_end_during_return_cancels_before_playback(gateway):
    import threading
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    reached = threading.Event()
    original = runtime.controller.reset
    def reset():
        reached.set()
        return original()
    result = []
    with mock.patch.object(runtime.controller, 'reset', side_effect=reset):
        worker = threading.Thread(target=lambda: result.append(start(gateway, hours=.01)))
        worker.start()
        assert reached.wait(5)
        ended = gateway.post('/api/v1/quick/end', headers=headers())
        worker.join(5)
    assert not worker.is_alive()
    assert ended.status_code == 200, ended.text
    assert result[0].status_code == 409
    assert not runtime.controller.controller.backend.motors_enabled
    assert runtime.snapshot()['playback'] is None
    assert status(gateway)['phase'] == 'ended'


def test_report_captures_own_playback_after_delayed_finalize_and_auto_disconnect(gateway):
    import threading
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    gate = threading.Event()
    original = runtime.controller._finalize_after_run
    def delayed(*args):
        assert gate.wait(10)
        return original(*args)
    with mock.patch.object(runtime.controller, '_finalize_after_run', side_effect=delayed):
        response = start(gateway)
        assert response.status_code == 200, response.text
        deadline = time.monotonic()+4
        while status(gateway)['phase'] != 'completed' and time.monotonic()<deadline:
            time.sleep(.05)
        assert status(gateway)['phase'] == 'completed'
        assert runtime.controller.controller.playback is None
        gate.set()
        deadline=time.monotonic()+4
        while not list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json')) and time.monotonic()<deadline:
            time.sleep(.05)
    paths=list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json'))
    assert len(paths)==1
    metadata=json.loads(paths[0].read_text())
    assert metadata['active_seconds'] >= .7
    assert metadata['outcome'] == 'duration_complete'


def test_two_windows_run_independent_buses_and_ending_one_keeps_other_running(gateway):
    prepare(gateway)
    prepare(gateway, profile='right_arm', owner='quick-b')
    first=start(gateway,hours=.01)
    assert first.status_code==200,first.text
    second=start(gateway,hours=.01,owner='quick-b')
    assert second.status_code==200,second.text
    ended=gateway.post('/api/v1/quick/end',headers=headers())
    assert ended.status_code==200,ended.text
    other=gateway.app.state.runtime.session('quick-b').snapshot()
    assert other['state']=='running'
    assert other['station']['motors_enabled'] is True
    assert gateway.app.state.runtime.interface_owners()=={'can1':'quick-b'}
    assert gateway.app.state.runtime.plc.snapshot()['status']['ChannelPermit'][1] is True
    assert gateway.post('/api/v1/quick/end',headers=headers('quick-b')).status_code==200


def test_window_heartbeat_expiry_cancels_long_default_return(gateway):
    prepare(gateway)
    runtime=gateway.app.state.runtime.session('quick-a')
    original=runtime.controller.reset
    def expire_before_return():
        runtime.lease.timeout_s=.01
        return original()
    with mock.patch.object(runtime.controller,'reset',side_effect=expire_before_return):
        result=start(gateway,hours=.01)
    assert result.status_code==409,result.text
    assert not runtime.controller.controller.backend.motors_enabled
    assert runtime.controller.controller.playback is None
    assert status(gateway)['phase']=='fault'


def test_external_power_completes_without_any_plc_reads_or_commands(gateway):
    prepare(gateway)
    plc = gateway.app.state.runtime.plc
    with mock.patch.object(plc, 'snapshot', side_effect=AssertionError('External mode must not depend on PLC')) as plc_read, \
         mock.patch.object(plc.coordination, 'execute', side_effect=AssertionError('No PLC writes in external mode')) as plc_write:
        response = start(gateway, power_source='external')
        assert response.status_code == 200, response.text
        assert response.json()['result']['power_source'] == 'external'
        deadline = time.monotonic()+5
        while status(gateway)['phase'] not in {'completed','fault'} and time.monotonic()<deadline:
            time.sleep(.05)
        assert status(gateway)['phase'] == 'completed', status(gateway)
        plc_read.assert_not_called()
        plc_write.assert_not_called()
    runtime = gateway.app.state.runtime.session('quick-a')
    assert not runtime.controller.controller.backend.motors_enabled
    deadline=time.monotonic()+3
    while not list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json')) and time.monotonic()<deadline:
        time.sleep(.05)
    metadata=json.loads(next(runtime.controller.report_manager.executions_root.glob('*/config/execution.json')).read_text())
    assert metadata['stage_code'] == 'QUICK_EXTERNAL'
    assert '外部供电' in metadata['test_name']
    assert metadata['recording_scope'] == 'local'
    assert plc.snapshot()['phase'] == 'OFF'


def test_external_power_does_not_bypass_can_motor_feedback_failure(gateway):
    prepare(gateway)
    runtime=gateway.app.state.runtime.session('quick-a')
    with mock.patch.object(runtime.controller, 'discover_motors', side_effect=RuntimeError('motor feedback timeout')), \
         mock.patch.object(gateway.app.state.runtime.plc, 'snapshot', side_effect=AssertionError('No PLC dependency')):
        assert start(gateway, power_source='external', confirmed=False).status_code == 409
        response=start(gateway, power_source='external')
    assert response.status_code==409,response.text
    assert not runtime.controller.controller.backend.motors_enabled
    value=status(gateway)
    assert value['phase']=='fault'
    assert value['failure']=='motor feedback timeout'
    assert value['failed_phase']=='discovering'
    assert value['power_source']=='external'


def test_plc_rejection_retains_original_reason_even_after_end(gateway):
    prepare(gateway)
    plc=gateway.app.state.runtime.plc
    with mock.patch.object(plc,'snapshot',return_value={'access_mode':'monitor','communication_state':'LIVE','stale':False}):
        response=start(gateway,power_source='plc')
    assert response.status_code==409,response.text
    failure=status(gateway)['failure']
    assert '只读监控' in failure
    response=gateway.post('/api/v1/quick/end',headers=headers())
    assert response.status_code==200,response.text
    assert response.json()['result']['failure']==failure
    assert failure in response.json()['result']['detail']
    assert failure in status(gateway)['detail']


def test_invalid_power_source_is_rejected_without_starting(gateway):
    prepare(gateway)
    assert start(gateway,power_source='unknown').status_code==422
    assert gateway.app.state.runtime.plc.snapshot()['phase']=='OFF'
