"""Quick verification uses mock PLC and fake motors only."""
from dataclasses import replace
import hashlib
import json
import threading
import time
from unittest import mock

import numpy as np
import pytest
from fastapi.testclient import TestClient

from factory_hmi.gateway.app import create_app
from factory_hmi.gateway import quick_run
from factory_hmi.quick_profiles import catalog, preset


def headers(owner='quick-a'):
    return {'X-RP1-Client-ID': owner}


@pytest.fixture
def gateway(tmp_path):
    with TestClient(create_app(data_root=tmp_path, lease_timeout_s=30)) as client:
        assert client.app.state.runtime.plc.config.driver == 'mock'
        yield client


@pytest.fixture
def powered_gateway(gateway):
    # The production default is communication OFF. Only PLC-powered cases
    # explicitly opt in; external-power tests retain the passive default.
    gateway.app.state.runtime.plc.connect(user='explicit-offline-test')
    return gateway


@pytest.fixture
def short_trajectory(monkeypatch, tmp_path):
    def use_trajectory(initial_position=0., *, offsets=(0., .01, 0., 0.), timestamps=(0., .12, .24, .4)):
        def simulated_preset(profile):
            item = preset(profile)
            path = tmp_path / f'{profile}-synthetic.npz'
            positions = np.zeros((len(timestamps), item['motor_count']))
            positions[:, 0] = initial_position + np.asarray(offsets)
            fps = (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
            np.savez(path, motor_pos=positions, time=timestamps, fps=fps, limb=profile)
            return {**item, 'trajectory': path.name, 'trajectory_path': path,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        monkeypatch.setattr(quick_run, 'preset', simulated_preset)
    return use_trajectory


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


def start(client, hours=.0002, confirmed=True, owner='quick-a', power_source=None, *, model=False):
    body = {'duration_hours': hours, 'physical_estop_confirmed': confirmed, 'sample_id': '离线任意样机',
            'completion_mode': 'mujoco_hanging' if model else 'configured', 'parking_pose_confirmed': model}
    if power_source is not None:
        body['power_source'] = power_source
    return client.post('/api/v1/quick/start', headers=headers(owner), json=body)


def status(client, owner='quick-a'):
    return client.get('/api/v1/quick/status', headers=headers(owner)).json()


def wait_for_status(client, *phases, timeout=12.):
    deadline = time.monotonic() + timeout
    while True:
        value = status(client)
        if value['phase'] in phases:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f'快速验证未进入 {phases}，实际状态 {value}')
        time.sleep(.02)


def test_arm_default_downward_finish_requires_confirmation_before_power(gateway):
    prepare(gateway)
    body = {'duration_hours': .01, 'physical_estop_confirmed': True,
            'sample_id': '模拟左臂', 'power_source': 'external'}
    rejected = gateway.post('/api/v1/quick/start', headers=headers(), json=body)
    assert rejected.status_code == 409
    assert 'MuJoCo' in rejected.json()['detail']
    runtime = gateway.app.state.runtime.session('quick-a')
    assert not runtime.snapshot()['can']['all_up']
    assert not runtime.snapshot()['station']['motors_enabled']

    accepted = gateway.post('/api/v1/quick/start', headers=headers(),
                            json={**body, 'parking_pose_confirmed': True})
    assert accepted.status_code == 200, accepted.text
    assert runtime.controller.controller.playback._options.completion_mode == 'mujoco_hanging'
    aborted = gateway.post('/api/v1/quick/abort', headers=headers())
    assert aborted.status_code == 200, aborted.text


def test_quick_playback_ignores_peak_speed_in_cycle_and_completes_normal_return(gateway, short_trajectory):
    short_trajectory(offsets=(0., .1, 0., 0.), timestamps=(0., .1, .103, .4))
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    limits = runtime.controller.controller.config.safety_limits
    preflight = runtime.snapshot()['trajectory']['preflight']
    assert preflight['max_velocity_rad_s'] > limits.max_velocity_rad_s
    assert preflight['max_frame_delta_rad'] < limits.max_frame_delta_rad

    controller = runtime.controller.controller
    read_samples = controller.backend.read_samples
    injected = False

    def feedback():
        nonlocal injected
        samples = read_samples()
        playback = controller.playback
        if playback and playback.snapshot()['completion']['phase'] in {'running', 'finishing_cycle'}:
            samples[0] = replace(samples[0], spd_rad_s=limits.max_velocity_rad_s + 1.)
            injected = True
        return samples

    with mock.patch.object(controller.backend, 'read_samples', side_effect=feedback):
        response = start(gateway, hours=.0001, power_source='external', model=True)
        assert response.status_code == 200, response.text
        assert response.json()['snapshot']['playback']['options']['speed'] == 1.0
        playback = controller.playback
        completed = wait_for_status(gateway, 'completed', 'fault')
    assert injected
    assert completed['phase'] == 'completed', completed
    result = playback.snapshot()
    assert result['progress']['completed_cycles'] == 1
    assert result['completion']['source'] == 'mujoco_hanging'
    assert result['completion']['safe_disabled']


def test_quick_playback_rejects_overspeed_return_connection_before_enabling(gateway, short_trajectory):
    short_trajectory(offsets=(0., .1, .1, 0.), timestamps=(0., .1, .36, .4))
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    controller = runtime.controller.controller
    # A low test limit isolates the return speed error from the model's position limits.
    controller.config = replace(controller.config, safety_limits=replace(
        controller.config.safety_limits, max_velocity_rad_s=2.0))

    response = start(gateway, hours=.0001, power_source='external', model=True)
    assert response.status_code == 409, response.text
    assert 'MuJoCo 收尾接续速度超出本机配置上限' in response.json()['detail']
    assert not runtime.snapshot()['station']['motors_enabled']
    assert not runtime.snapshot()['can']['all_up']


@pytest.mark.parametrize("profile", ["waist_hip", "left_arm", "left_short_leg"])
def test_factory_quick_run_seals_quality_from_builtin_trajectory(gateway, profile):
    preset_info = preset(profile)
    prepare(gateway, profile=profile)
    response = gateway.post('/api/v1/quick/start', headers=headers(), json={
        'duration_hours': .0002, 'physical_estop_confirmed': True,
        'sample_id': '000024', 'bench_id': 'MP-01', 'data_domain': 'FACTORY',
        'power_source': 'external', 'parking_pose_confirmed': profile == 'left_arm',
    })
    assert response.status_code == 200, response.text
    runtime = gateway.app.state.runtime.session('quick-a')
    completed = wait_for_status(gateway, 'holding', 'completed', 'fault', timeout=26.)
    assert completed['phase'] in {'holding', 'completed'}, completed
    service = runtime.controller
    service.wait_for_reports(timeout=10)
    item, = service.execution_history_page(sn='000024', data_domain='FACTORY')['items']
    summary = service.report_manager.summary(item['test_id'])
    quality = summary['factory_quality']
    assert item['factory_quality_status'] == quality['status'] == 'passed', quality
    assert quality['profile']['test_case_id'] == preset_info['test_case_id']
    assert item['factory_quality_basis'] == ('reference' if profile == 'waist_hip' else 'trial')
    assert len(quality['metrics']) == preset_info['motor_count']
    assert summary['timing']['completed_cycles'] == 1
    assert not service.outbox.list()


def test_plc_powered_run_finishes_cycle_and_auto_disables(powered_gateway, short_trajectory):
    gateway = powered_gateway
    short_trajectory()
    prepare(gateway)
    response = start(gateway, model=True)
    assert response.status_code == 200, response.text
    runtime = gateway.app.state.runtime.session('quick-a')
    completed = wait_for_status(gateway, 'completed', 'fault')
    assert completed['phase'] == 'completed', completed
    state = runtime.snapshot()
    assert not state['station']['motors_enabled']
    assert not state['can']['all_up']
    assert response.json()['snapshot']['playback']['options']['speed'] == 1.0
    runtime.controller.wait_for_reports()
    context = list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json'))
    assert len(context) == 1
    metadata = json.loads(context[0].read_text())
    assert metadata['active_seconds'] >= .7
    assert metadata['completed_cycles'] == 2
    assert metadata['outcome'] == 'duration_complete'
    assert metadata['recording_scope'] == 'local'
    assert metadata['sample_id'] == '离线任意样机'
    assert metadata['campaign_id'] is None
    assert gateway.app.state.runtime.plc.snapshot()['phase'] == 'RUNNING'


def test_pause_then_end_only_releases_own_bus(powered_gateway, short_trajectory):
    gateway = powered_gateway
    short_trajectory()
    prepare(gateway)
    prepare(gateway, profile='right_arm', owner='quick-b')
    response = start(gateway, hours=.01, model=True)
    assert response.status_code == 200, response.text
    hub = gateway.app.state.runtime
    playback = hub.session('quick-a').controller.controller.playback
    assert playback._test_started.wait(2)
    paused = gateway.post('/api/v1/playback/pause', headers=headers())
    assert paused.status_code == 200, paused.text
    wait_for_status(gateway, 'paused', timeout=2)
    ended = gateway.post('/api/v1/quick/end', headers=headers())
    assert ended.status_code == 200, ended.text
    assert ended.json()['result']['phase'] == 'paused'
    assert hub.interface_owners() == {'can0': 'quick-a', 'can1': 'quick-b'}
    resumed = gateway.post('/api/v1/playback/resume', headers=headers())
    assert resumed.status_code == 200, resumed.text
    completed = wait_for_status(gateway, 'completed', 'fault')
    assert completed['phase'] == 'completed', completed
    assert playback.snapshot()['progress']['completed_cycles'] == 1
    assert hub.interface_owners() == {'can1': 'quick-b'}
    assert hub.plc.snapshot()['phase'] == 'RUNNING'


def test_busy_bus_and_invalid_mapping_cannot_enable_or_steal_other_session(gateway):
    prepare(gateway)
    conflict = gateway.post('/api/v1/quick/prepare', headers=headers('quick-b'), json={
        'profile': 'left_short_arm', 'backend': 'fake'})
    assert conflict.status_code == 409
    assert gateway.app.state.runtime.interface_owners()['can0'] == 'quick-a'
    invalid = gateway.post('/api/v1/quick/prepare', headers=headers('quick-b'), json={
        'profile': 'waist_hip', 'interfaces': ['can0']*4, 'backend': 'fake'})
    assert invalid.status_code == 409
    assert gateway.app.state.runtime.plc.snapshot()['communication_enabled'] is False
    assert gateway.app.state.runtime.plc.snapshot()['communication_state'] == 'DISABLED'


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


def test_end_during_first_frame_approach_cancels_before_test(powered_gateway, short_trajectory):
    gateway = powered_gateway
    short_trajectory(initial_position=.05)
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    response = start(gateway, hours=.01)
    assert response.status_code == 200, response.text
    playback = runtime.controller.controller.playback
    assert playback.snapshot()['completion']['phase'] in {'approaching', 'approach_settling'}
    ended = gateway.post('/api/v1/quick/end', headers=headers())
    assert ended.status_code == 200, ended.text
    stopped = wait_for_status(gateway, 'ended', 'fault')
    assert stopped['phase'] == 'ended', stopped
    snapshot = playback.snapshot()
    assert snapshot['test_started_at'] is None
    assert snapshot['progress']['completed_cycles'] == 0
    assert snapshot['recording']['row_count'] == 0
    assert not runtime.controller.controller.backend.motors_enabled
    assert runtime.snapshot()['playback'] is None


def test_report_captures_own_playback_after_delayed_finalize_and_auto_disconnect(powered_gateway, short_trajectory):
    gateway = powered_gateway
    short_trajectory()
    prepare(gateway)
    runtime = gateway.app.state.runtime.session('quick-a')
    gate = threading.Event()
    original = runtime.controller._finalize_after_run
    def delayed(*args):
        assert gate.wait(15)
        return original(*args)
    with mock.patch.object(runtime.controller, '_finalize_after_run', side_effect=delayed):
        try:
            response = start(gateway, model=True)
            assert response.status_code == 200, response.text
            completed = wait_for_status(gateway, 'completed', 'fault')
            assert completed['phase'] == 'completed', completed
            assert runtime.controller.controller.playback is None
        finally:
            gate.set()
        runtime.controller.wait_for_reports()
    paths=list(runtime.controller.report_manager.executions_root.glob('*/config/execution.json'))
    assert len(paths)==1
    metadata=json.loads(paths[0].read_text())
    assert metadata['active_seconds'] >= .7
    assert metadata['completed_cycles'] == 2
    assert metadata['outcome'] == 'duration_complete'


def test_two_windows_run_independent_buses_and_ending_one_keeps_other_running(powered_gateway):
    gateway = powered_gateway
    prepare(gateway)
    prepare(gateway, profile='right_arm', owner='quick-b')
    first=start(gateway,hours=.01)
    assert first.status_code==200,first.text
    second=start(gateway,hours=.01,owner='quick-b')
    assert second.status_code==200,second.text
    first_runtime = gateway.app.state.runtime.session('quick-a')
    playback = first_runtime.controller.controller.playback
    assert playback is not None, status(gateway)
    assert playback._test_started.wait(2)
    ended=gateway.post('/api/v1/quick/end',headers=headers())
    assert ended.status_code==200,ended.text
    other=gateway.app.state.runtime.session('quick-b').snapshot()
    assert other['state']=='running'
    assert other['station']['motors_enabled'] is True
    assert gateway.app.state.runtime.interface_owners()=={'can0':'quick-a', 'can1':'quick-b'}
    assert gateway.app.state.runtime.plc.snapshot()['status']['ChannelPermit'][1] is True
    assert gateway.post('/api/v1/quick/abort',headers=headers()).status_code==200
    assert gateway.post('/api/v1/quick/abort',headers=headers('quick-b')).status_code==200


def test_window_heartbeat_expiry_cancels_after_arm_before_playback(powered_gateway):
    gateway = powered_gateway
    prepare(gateway)
    runtime=gateway.app.state.runtime.session('quick-a')
    original = runtime.controller.arm
    def arm_and_expire():
        snapshot = original()
        runtime.lease.timeout_s=.01
        time.sleep(.04)
        return snapshot
    try:
        with mock.patch.object(runtime.controller, 'arm', side_effect=arm_and_expire):
            result = start(gateway, hours=.01)
    finally:
        runtime.lease.timeout_s = 30
    assert result.status_code==409,result.text
    assert '心跳已失效' in result.json()['detail']
    assert not runtime.controller.controller.backend.motors_enabled
    assert runtime.controller.controller.playback is None
    assert status(gateway)['phase']=='fault'


def test_external_power_completes_without_any_plc_reads_or_commands(gateway, short_trajectory):
    short_trajectory()
    prepare(gateway)
    plc = gateway.app.state.runtime.plc
    with mock.patch.object(plc, 'snapshot', side_effect=AssertionError('External mode must not depend on PLC')) as plc_read, \
         mock.patch.object(plc.coordination, 'execute', side_effect=AssertionError('No PLC writes in external mode')) as plc_write:
        response = start(gateway, power_source='external', model=True)
        assert response.status_code == 200, response.text
        assert response.json()['result']['power_source'] == 'external'
        completed = wait_for_status(gateway, 'completed', 'fault')
        assert completed['phase'] == 'completed', completed
        plc_read.assert_not_called()
        plc_write.assert_not_called()
    runtime = gateway.app.state.runtime.session('quick-a')
    assert not runtime.controller.controller.backend.motors_enabled
    runtime.controller.wait_for_reports()
    metadata=json.loads(next(runtime.controller.report_manager.executions_root.glob('*/config/execution.json')).read_text())
    assert metadata['stage_code'] == 'QUICK_EXTERNAL'
    assert '外部供电' in metadata['stage_name']
    assert metadata['test_case_id'] == 'UNIT-AGING-006' and metadata['test_name'] == '左臂老化'
    assert metadata['recording_scope'] == 'local'
    assert plc.snapshot()['communication_enabled'] is False
    assert plc.snapshot()['communication_state'] == 'DISABLED'


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
    assert gateway.app.state.runtime.plc.snapshot()['communication_enabled'] is False
    assert gateway.app.state.runtime.plc.snapshot()['communication_state']=='DISABLED'
