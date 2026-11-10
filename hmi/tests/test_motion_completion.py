"""Synthetic-only verification: no PLC service or real CAN backend is created."""
from dataclasses import replace
from pathlib import Path
import json
import time

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.config import load_station_config
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.motion_completion import MotionParameters, MotionParameterError, configuration_sha256, motion_readiness
from factory_hmi.core.playback import PlaybackController
from factory_hmi.models import PlaybackOptions, StationState, TrajectoryData
from factory_hmi.core.state_machine import StationStateMachine


def synthetic_config(**overrides):
    config = replace(load_station_config('left_short_arm'), control_rate_hz=200.)
    raw = dict(config.raw)
    raw['factory_hmi'] = dict(raw.get('factory_hmi', {}))
    raw['factory_hmi']['motion_completion'] = dict(
        verified=True, safe_to_disable=True, limb=config.limb,
        parameter_revision='SYNTHETIC-NOT-PRODUCT-1', verification_evidence='synthetic isolated fixture',
        joint_min_rad=[-4.]*5, joint_max_rad=[4.]*5,
        motor_min_rad=[-4.]*5, motor_max_rad=[4.]*5,
        max_velocity_rad_s=[5.]*5, max_acceleration_rad_s2=[100.]*5,
        return_velocity_rad_s=[2.]*5, return_acceleration_rad_s2=[20.]*5,
        parking_joint_rad=[.08]*5, position_tolerance_rad=[.0001]*5,
        speed_tolerance_rad_s=[.001]*5, max_first_frame_delta_rad=[.01]*5,
        stable_seconds=.025, timeout_seconds=.7, feedback_stale_seconds=.2,
        )
    raw['factory_hmi']['motion_completion'].update(overrides)
    config = replace(config, raw=raw)
    raw['factory_hmi']['motion_completion']['configuration_sha256'] = configuration_sha256(config)
    return config


def make_playback(tmp_path, *, response=1., recording=False, config=None, duration=.12):
    config = config or synthetic_config()
    backend = FakeMotorBackend(config.entries, response_ratio=response)
    backend.connect()
    values = np.zeros((3, 5)); values[1] = .02
    trajectory = TrajectoryData(tmp_path/'synthetic.npz', config.limb, values,
        np.array([0., duration/2, duration]), 2/duration, 'synthetic', {})
    playback = PlaybackController(backend, config, trajectory,
        state_machine=StationStateMachine(StationState.ARMED), enable_watchdog=False,
        enable_recording=recording, record_output=tmp_path/'test.csv' if recording else None,
        completion_output=tmp_path/'completion.json')
    return playback, backend


def await_phase(playback, phase, timeout=2.):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if playback.snapshot()['completion']['phase'] == phase:
            return
        time.sleep(.003)
    raise AssertionError(playback.snapshot())


def test_missing_completion_preserves_legacy_arm_but_stale_verified_parameters_block(tmp_path):
    config = load_station_config('left_short_arm')
    backend = FakeMotorBackend(config.entries)
    controller = FactoryController(config, backend, enable_watchdog=False)
    path = tmp_path/'synthetic.npz'
    np.savez(path, motor_pos=np.zeros((2,5)), time=[0., .1], fps=[10.], limb=['left_short_arm'])
    controller.connect(); controller.load_trajectory(path)
    try:
        assert not controller.snapshot()['station']['motion_readiness']['ready']
        assert controller.snapshot()['station']['motion_readiness']['test_allowed']
        controller.arm()
        assert controller.state == StationState.ARMED
    finally:
        controller.disconnect()
    configured = synthetic_config()
    configured.raw['motor_zero_offset'][0] += .1
    assert not motion_readiness(configured)['ready']
    assert not motion_readiness(configured)['test_allowed']


def test_verified_file_speed_is_strict_for_validation_but_not_playback(tmp_path):
    config = synthetic_config(max_velocity_rad_s=[.25]*5, return_velocity_rad_s=[.25]*5)
    params = MotionParameters.load(config)
    with pytest.raises(MotionParameterError, match='限位'):
        params.check_target(config, np.array([5.,0,0,0,0]))
    playback, backend = make_playback(tmp_path, config=config)
    try:
        with pytest.raises(MotionParameterError, match='首帧'):
            params.check_trajectory(config, playback.trajectory, current=np.ones(5))
        with pytest.raises(MotionParameterError, match='轨迹速度'):
            params.check_trajectory(config, playback.trajectory)
        playback.start()
        assert playback.wait(2)
        assert playback.snapshot()['completion']['safe_disabled']
    finally:
        backend.disconnect()


@pytest.mark.parametrize(('speed', 'bounds', 'message'), [
    (5., {}, '轨迹加速度'),
    (1., {'motor_max_rad': [.021]*5, 'parking_joint_rad': [0.]*5}, 'motor 限位'),
])
def test_verified_acceleration_and_braking_limits_still_block_start(tmp_path, speed, bounds, message):
    config = synthetic_config(max_velocity_rad_s=[.25]*5, return_velocity_rad_s=[.25]*5, **bounds)
    playback, backend = make_playback(tmp_path, config=config)
    try:
        with pytest.raises(MotionParameterError, match=message):
            playback.start(PlaybackOptions(speed=speed))
        assert not playback.running
        assert not backend.command_history
    finally:
        backend.disconnect()


def test_duration_completes_whole_cycle_and_separates_return_csv(tmp_path):
    playback, backend = make_playback(tmp_path, recording=True)
    try:
        playback.start(PlaybackOptions(loop=True, duration_s=.025))
        assert playback.wait(2)
        assert playback.wait_recording(2)
        snapshot = playback.snapshot()
        assert snapshot['completion']['safe_disabled']
        assert snapshot['progress']['completed_cycles'] == 1
        assert .12 <= snapshot['progress']['active_seconds'] < .22
        assert snapshot['progress']['outcome'] == 'duration_complete'
        assert snapshot['completion']['return_seconds'] > 0
        assert not backend.motors_enabled and backend.connected
        assert snapshot['recording']['finished']
        import csv
        rows = list(csv.DictReader((tmp_path/'test.csv').open()))
        assert rows
        command_columns = [c for c in rows[0] if 'cmd_pos' in c]
        assert command_columns
        for row in rows:
            for column in command_columns:
                if row[column]:
                    assert abs(float(row[column])) <= .02001
        deadline=time.monotonic()+1
        while not (tmp_path/'completion.json').exists() and time.monotonic()<deadline:
            time.sleep(.005)
        evidence=json.loads((tmp_path/'completion.json').read_text())
        assert evidence['configuration_sha256'] == configuration_sha256(playback.config)
        assert evidence['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_manual_finish_is_nonblocking_and_does_not_complete_cycle(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8)
    try:
        playback.start(PlaybackOptions(loop=True))
        time.sleep(.05)
        started=time.monotonic(); playback.finish('synthetic operator finish')
        assert time.monotonic()-started < .05
        assert playback.wait(2)
        snap=playback.snapshot()
        assert snap['progress']['completed_cycles'] == 0
        assert snap['progress']['outcome'] == 'stopped'
        assert snap['completion']['safe_disabled']
        assert snap['stop_reason'] == 'synthetic operator finish'
    finally:
        backend.disconnect()


def test_pause_holds_and_finish_from_pause_works(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8)
    try:
        playback.start(PlaybackOptions(loop=True))
        time.sleep(.03); playback.pause(); time.sleep(.02)
        before=playback.snapshot()['progress']['active_seconds']
        target=backend.command_history[-1]['target'].copy()
        time.sleep(.03)
        assert playback.snapshot()['progress']['active_seconds'] == before
        np.testing.assert_allclose(backend.command_history[-1]['target'], target)
        playback.finish()
        assert playback.wait(2)
        assert playback.snapshot()['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_cycle_finish_stays_paused_until_resume_and_keeps_first_request(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8)
    try:
        playback.start(PlaybackOptions(loop=True))
        assert playback._test_started.wait(1)
        playback.pause()
        playback.finish('first cycle finish', complete_cycle=True)
        playback.finish('duplicate cycle finish', complete_cycle=True)
        assert not playback.wait(.05)
        pending = playback.snapshot()
        assert playback.state_machine.state == StationState.PAUSED
        assert pending['completion']['phase'] == 'finishing_cycle'
        assert pending['progress']['completed_cycles'] == 0
        assert backend.motors_enabled
        playback.resume()
        assert playback.wait(2)
        finished = playback.snapshot()
        assert finished['progress']['completed_cycles'] == 1
        assert finished['progress']['frame'] == playback.trajectory.frame_count - 1
        assert finished['progress']['outcome'] == 'stopped'
        assert finished['stop_reason'] == 'first cycle finish'
        assert finished['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_cycle_finish_requested_after_timer_keeps_timed_outcome(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8)
    try:
        playback.start(PlaybackOptions(loop=True, duration_s=.025))
        await_phase(playback, 'finishing_cycle')
        playback.finish('late cycle finish', complete_cycle=True)
        assert playback.wait(2)
        playback.finish('cycle finish received after completion', complete_cycle=True)
        finished = playback.snapshot()
        assert finished['progress']['completed_cycles'] == 1
        assert finished['progress']['outcome'] == 'duration_complete'
        assert finished['stop_reason'] is None
        assert finished['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_cycle_finish_while_waiting_for_first_frame_cancels_without_testing(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8, recording=True)
    backend.positions[:] = .01
    try:
        playback.start(PlaybackOptions(loop=True))
        await_phase(playback, 'approach_settling')
        playback.finish('cancel preparation', complete_cycle=True)
        assert playback.wait(2)
        assert playback.wait_recording(2)
        interrupted = playback.snapshot()
        assert interrupted['completion']['phase'] == 'interrupted'
        assert interrupted['progress']['completed_cycles'] == 0
        assert interrupted['recording']['row_count'] == 0
        assert interrupted['test_started_at'] is None
    finally:
        backend.disconnect()


def test_protective_stop_preempts_pending_cycle_finish(tmp_path):
    playback, backend = make_playback(tmp_path, duration=.8)
    try:
        playback.start(PlaybackOptions(loop=True))
        assert playback._test_started.wait(1)
        playback.pause()
        playback.finish('pending cycle finish', complete_cycle=True)
        playback.stop(reason='synthetic protection')
        interrupted = playback.snapshot()
        assert interrupted['progress']['completed_cycles'] == 0
        assert interrupted['completion']['phase'] == 'interrupted'
        assert interrupted['stop_reason'] == 'synthetic protection'
        assert not interrupted['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_unreached_healthy_target_holds_alarm_until_protection_interrupt(tmp_path):
    playback, backend = make_playback(tmp_path, response=0.)
    try:
        playback.start()
        await_phase(playback, 'needs_attention')
        assert playback.running and backend.motors_enabled
        assert not playback.snapshot()['completion']['safe_disabled']
        before=len(backend.command_history); time.sleep(.02)
        assert len(backend.command_history)>before
        playback.stop(timeout=1., reason='synthetic protection')
        assert playback.snapshot()['completion']['phase'] == 'interrupted'
    finally:
        backend.disconnect()


def test_motor_fault_during_return_preempts_without_waiting_for_stable(tmp_path):
    playback, backend = make_playback(tmp_path)
    try:
        playback.start(); await_phase(playback, 'returning')
        backend.inject_error(0, 7)
        assert playback.wait(1)
        assert playback.snapshot()['completion']['phase'] == 'fault'
        assert not backend.motors_enabled
    finally:
        backend.disconnect()


def test_cycle_limits_and_loop_continuity_are_checked(tmp_path):
    playback, backend = make_playback(tmp_path)
    try:
        playback.trajectory.motor_pos[-1] = .01
        with pytest.raises(MotionParameterError, match='首尾不连续'):
            playback.start(PlaybackOptions(loop=True))
    finally:
        backend.disconnect()
    other=tmp_path/'cycles'; other.mkdir()
    playback, backend = make_playback(other, duration=.05)
    try:
        playback.start(PlaybackOptions(loop=True, cycles=2))
        assert playback.wait(2)
        assert playback.snapshot()['progress']['completed_cycles'] == 2
        assert playback.snapshot()['progress']['outcome'] == 'cycles_complete'
        assert playback.snapshot()['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_explicit_j5_derivative_preserves_source_and_other_axes(tmp_path):
    from factory_hmi.core.motion_tools import extend_j5
    config=synthetic_config()
    playback, backend = make_playback(tmp_path, config=config)
    source=playback.trajectory
    source.path.write_bytes(b'synthetic-source-preserved')
    original=source.path.read_bytes()
    try:
        output=tmp_path/'new-j5.npz'
        evidence=extend_j5(config, source, output, -.3, .4)
        result=np.load(output)
        np.testing.assert_array_equal(result['motor_pos'][:source.frame_count], source.motor_pos)
        np.testing.assert_array_equal(np.min(result['motor_pos'][:,:4], axis=0), np.min(source.motor_pos[:,:4], axis=0))
        np.testing.assert_array_equal(np.max(result['motor_pos'][:,:4], axis=0), np.max(source.motor_pos[:,:4], axis=0))
        assert result['motor_pos'][:,4].min() == pytest.approx(-.3)
        assert result['motor_pos'][:,4].max() == pytest.approx(.4)
        assert source.path.read_bytes() == original
        assert evidence['physical_effect_verified'] is False
        with pytest.raises(FileExistsError):
            extend_j5(config, source, output, -.3, .4)
        with pytest.raises(ValueError, match='7DOF'):
            extend_j5(load_station_config('left_arm'), source, tmp_path/'bad.npz', -.3, .4)
    finally:
        backend.disconnect()


def test_quick_end_keeps_ownership_until_verified_safe_disable(tmp_path):
    from types import SimpleNamespace
    import threading
    from factory_hmi.gateway.quick_run import QuickRun
    config=synthetic_config()
    backend=FakeMotorBackend(config.entries)
    c=FactoryController(config, backend, enable_watchdog=False)
    path=tmp_path/'trajectory.npz'
    values=np.zeros((3,5)); values[1]=.02
    np.savez(path, motor_pos=values, time=[0., .3, .6], fps=[1/.3], limb=[config.limb])
    c.completion_output=tmp_path/'quick-completion.json'
    c.connect(); c.load_trajectory(path); c.arm(); c.start(loop=True)
    session=c.playback
    released=[]
    owners={'can0':'quick-a', 'can1':'quick-b'}
    def release(owner):
        released.append(owner)
        for key in list(owners):
            if owners[key] == owner: del owners[key]
    def invoke(names, **kwargs):
        if names[0] == 'can_disconnect': return
        return getattr(c, names[0])(**kwargs)
    runtime=SimpleNamespace(controller=SimpleNamespace(controller=c,request_motion_stop=c.request_motion_stop),
        _invoke=invoke, _lease_guard=threading.RLock(),
        lease=SimpleNamespace(snapshot=lambda: SimpleNamespace(expired=False)))
    hub=SimpleNamespace(release_interfaces=release, _key=lambda owner: owner)
    quick=QuickRun(runtime,hub,'quick-a'); quick._power_source='external'
    quick._state('running', 'synthetic')
    watcher=threading.Thread(target=quick._watch,args=(quick._stop,),daemon=True); watcher.start()
    try:
        assert session._test_started.wait(1)
        result=quick.end()
        assert result['active']
        assert result['phase'] == 'finishing_cycle'
        assert not released and backend.motors_enabled
        watcher.join(2)
        assert not watcher.is_alive()
        assert quick.snapshot()['phase'] == 'completed'
        assert released == ['quick-a'] and owners == {'can1':'quick-b'}
        finished = session.snapshot()
        assert finished['progress']['completed_cycles'] == 1
        assert finished['completion']['safe_disabled']
        assert not backend.connected
    finally:
        quick._stop.set()
        c.disconnect()


def test_quick_missing_parameters_rejects_before_power_or_can(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from factory_hmi.gateway.quick_run import QuickRun
    current={'station':{'limb':'left_short_arm','motion_readiness':{'ready':False,'reason':'synthetic unverified'}},
             'trajectory':{'sha256':'source'}}
    runtime=SimpleNamespace(snapshot=lambda:current)
    quick=QuickRun(runtime,SimpleNamespace(),'owner')
    quick._prepared={'id':'left_short_arm','sha256':'source'}
    quick._state('ready','synthetic',active=False)
    quick._power=Mock()
    with pytest.raises(RuntimeError, match='synthetic unverified'):
        quick.start(.1,True,'synthetic','external')
    quick._power.assert_not_called()


def test_repeated_cached_feedback_cannot_satisfy_stability_window(tmp_path):
    from dataclasses import replace as sample_replace
    playback, backend = make_playback(tmp_path)
    original=backend.read_samples
    frozen_stamp=[None]
    def frozen_samples():
        samples=original()
        if playback.snapshot()['completion']['phase'] in {'returning','settling','needs_attention'}:
            if frozen_stamp[0] is None:
                frozen_stamp[0]=[s.timestamp_s for s in samples]
            return [sample_replace(s,timestamp_s=frozen_stamp[0][i],feedback_age_s=0.) for i,s in enumerate(samples)]
        return samples
    backend.read_samples=frozen_samples
    try:
        playback.start()
        await_phase(playback,'needs_attention')
        assert backend.motors_enabled
        assert not playback.snapshot()['completion']['safe_disabled']
        playback.stop(timeout=1.)
    finally:
        backend.disconnect()


def test_quick_paused_station_takes_priority_over_finishing_cycle_phase():
    from types import SimpleNamespace
    import threading
    from factory_hmi.gateway.quick_run import QuickRun
    station={'state':'paused'}
    snapshot=lambda:{'station':dict(station),'playback':{'completion':{'phase':'finishing_cycle'}}}
    runtime=SimpleNamespace(controller=SimpleNamespace(controller=SimpleNamespace(snapshot=snapshot)),
        _lease_guard=threading.RLock(),lease=SimpleNamespace(snapshot=lambda:SimpleNamespace(expired=False)))
    quick=QuickRun(runtime,SimpleNamespace(),'synthetic-paused');quick._power_source='external'
    quick._state('finishing_cycle','synthetic')
    watcher=threading.Thread(target=quick._watch,args=(quick._stop,),daemon=True);watcher.start()
    try:
        deadline=time.monotonic()+1
        while quick.snapshot()['phase']!='paused' and time.monotonic()<deadline:
            time.sleep(.005)
        assert quick.snapshot()['phase']=='paused'
        assert '按继续后补完当前周期' in quick.snapshot()['detail']
        station['state']='running'
        deadline=time.monotonic()+1
        while quick.snapshot()['phase']!='finishing_cycle' and time.monotonic()<deadline:
            time.sleep(.005)
        assert quick.snapshot()['phase']=='finishing_cycle'
        assert '补完当前周期' in quick.snapshot()['detail']
    finally:
        quick._stop.set();watcher.join(1)


def test_report_wall_and_paused_seconds_use_frozen_test_boundary(tmp_path):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    import threading
    from factory_hmi.core.report import _build_summary
    from factory_hmi.core.service import FactoryService
    playback,backend=make_playback(tmp_path,recording=True,duration=.15)
    context={'execution_uuid':'synthetic-execution','test_id':'synthetic-report',
        'started_at':datetime.now(timezone.utc).isoformat(),'record_rate_hz':20.,
        'entries':[{'motor_id':e.motor_id,'joint_name':e.joint_name} for e in playback.config.entries]}
    result={}
    service=FactoryService.__new__(FactoryService)
    service._lock=threading.RLock();service._report_errors={};service._report_threads=set();service._queued_report_ids=set()
    def finalize(path,context):
        result['context']=dict(context)
        result['summary']=_build_summary(path,context)
    service.report_manager=SimpleNamespace(data_root=tmp_path/"report-spool")
    service.capacity_reservations=SimpleNamespace(complete=lambda execution_uuid:None)
    service._reap_report_receipts=lambda: None
    try:
        playback.start(PlaybackOptions(loop=True,duration_s=.02))
        time.sleep(.035);playback.pause();time.sleep(.04);playback.resume()
        assert playback.wait(2) and playback.wait_recording(2)
        boundary=datetime.fromisoformat(playback.snapshot()['ended_at'].replace('Z','+00:00'))
        return_complete=datetime.now(timezone.utc)
        assert (return_complete-boundary).total_seconds()>.15
        from unittest.mock import patch
        with patch('factory_hmi.core.report_jobs.enqueue_report_job',side_effect=lambda root,path,ctx:finalize(path,ctx)):
            service._finalize_after_run(SimpleNamespace(),tmp_path/'test.csv',context,playback)
        assert not service._report_errors
        timing=result['summary']['timing']
        started=datetime.fromisoformat(timing['started_at'].replace('Z','+00:00'))
        assert timing['wall_seconds']==pytest.approx((boundary-started).total_seconds())
        assert result['context']['ended_at']==playback.snapshot()['ended_at']
        assert .025 <= timing['paused_seconds'] < .15
        assert timing['wall_seconds'] < (return_complete-started).total_seconds()-.15
    finally:
        backend.disconnect()
