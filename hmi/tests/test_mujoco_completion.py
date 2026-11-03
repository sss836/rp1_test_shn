"""Actual selected arm NPZs and return controller with fake motors only."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
import time

import numpy as np
import pytest
from pydantic import ValidationError

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.config import load_station_config
from factory_hmi.core.manual_control import joint_positions_to_motor
from factory_hmi.core.mujoco_completion import plan_return, preset_info
from factory_hmi.core.playback import PlaybackController
from factory_hmi.core.state_machine import StationStateMachine
from factory_hmi.core.trajectory import import_trajectory
from factory_hmi.gateway.schemas import PlaybackStartRequest, QuickStartRequest
from factory_hmi.models import PlaybackOptions, StationState
from factory_hmi.quick_profiles import preset

ARMS = ['left_arm','right_arm','left_short_arm','right_short_arm']


@pytest.mark.parametrize('limb', ARMS)
def test_model_preset_binds_limb_mapping_and_retains_selected_geometry(limb):
    config = load_station_config(limb)
    info = preset_info(config)
    assert info['available'] and info['duration_seconds'] == 5.
    expected = -1.410305586 if limb.startswith('left') else 1.410305586
    plan = plan_return(config, np.zeros(len(config.entries)))
    end = np.zeros(len(config.entries)); end[3] = expected
    np.testing.assert_allclose(plan.target_motor, joint_positions_to_motor(config,end),atol=1e-12)
    assert plan.duration == 5. and plan.bridge_seconds == 0.
    assert plan.source['scope'] == 'model_geometry_not_physical_calibration'
    with pytest.raises(ValueError,match='范围'):
        plan_return(config, np.full(len(config.entries),100.))
    modified = replace(config, entries=(replace(config.entries[0],joint_name='wrong_joint'),*config.entries[1:]))
    assert not preset_info(modified)['available']
    assert 'motion_completion' not in config.raw.get('factory_hmi',{})


def test_confirmation_required_in_both_protocols_and_plain_bool_is_strict():
    with pytest.raises(ValueError,match='确认'):
        PlaybackOptions(completion_mode='mujoco_hanging')
    for cls, fields in [(PlaybackStartRequest,{'test_case_id':'REL-ARM-001'}),
                        (QuickStartRequest,{'duration_hours':1,'physical_estop_confirmed':True,'sample_id':'test'})]:
        for confirmation in (False,1,'true'):
            with pytest.raises(ValidationError):
                cls(completion_mode='mujoco_hanging', parking_pose_confirmed=confirmation, **fields)
        assert cls(completion_mode='mujoco_hanging', parking_pose_confirmed=True, **fields).parking_pose_confirmed


def test_model_return_respects_configured_frame_delta():
    config=load_station_config('left_arm')
    config=replace(config,safety_limits=replace(config.safety_limits,max_frame_delta_rad=.00001))
    with pytest.raises(ValueError,match='速度'):
        plan_return(config,np.zeros(7))


@pytest.mark.parametrize('limb', ARMS)
@pytest.mark.parametrize('scenario', ['normal','unreached','fault','cached_feedback','moving','cancel'])
def test_whole_cycle_then_model_return_and_independent_feedback_confirmation(tmp_path,monkeypatch,limb,scenario):
    config = load_station_config(limb)
    raw = deepcopy(config.raw)
    # Isolate arrival checks from the existing tracking-error fault in the
    # intentionally non-following fake backend. Never written to a real YAML.
    raw['factory_hmi']['faults']['tracking_error_rad'] = 10.
    config = replace(config,raw=raw)
    backend = FakeMotorBackend(config.entries); backend.connect()
    trajectory = import_trajectory(preset(limb)['trajectory_path'],limb=limb,motor_count=len(config.entries))
    playback = PlaybackController(backend,config,trajectory,
        state_machine=StationStateMachine(StationState.ARMED),enable_watchdog=False,
        completion_output=tmp_path/'completion.json')
    clock = [1000.]
    trace = {'disable':[], 'phases':{}, 'healthy_alarm':None}
    command, disable, read = backend.command_mit, backend.disable_motors, backend.read_samples
    def captured_command(target,**kwargs):
        phase = playback.snapshot()['completion']['phase']
        trace['phases'].setdefault(phase,clock[0])
        if phase == 'returning':
            if scenario == 'unreached': backend.response_ratio = 0.
            if scenario == 'fault': backend.inject_error(0,7)
            if scenario == 'cancel': playback.stop(wait=False,reason='isolated protection')
        return command(target,**kwargs)
    def captured_read():
        samples = read()
        if scenario == 'cached_feedback' and playback.snapshot()['completion']['phase'] in {'returning','settling','needs_attention'}:
            samples = [replace(s,timestamp_s=1001.,feedback_age_s=0.) for s in samples]
        if scenario == 'moving' and playback.snapshot()['completion']['phase'] in {'returning','settling','needs_attention'}:
            samples = [replace(s,spd_rad_s=.2) for s in samples]
        return samples
    def captured_disable():
        trace['disable'].append({'phase':playback.snapshot()['completion']['phase'],'samples':read(),'time':clock[0]})
        return disable()
    def advance(seconds):
        clock[0] += max(.00001,float(seconds))
        if playback.snapshot()['completion']['phase'] == 'needs_attention':
            trace['healthy_alarm'] = backend.motors_enabled
            playback.stop(wait=False,reason='isolated timeout observed')
        return playback._stop_event.is_set()
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(playback._stop_event,'wait',advance)
    monkeypatch.setattr(backend,'command_mit',captured_command)
    monkeypatch.setattr(backend,'read_samples',captured_read)
    monkeypatch.setattr(backend,'disable_motors',captured_disable)
    try:
        playback.start(PlaybackOptions(loop=True,duration_s=.25,completion_mode='mujoco_hanging',parking_pose_confirmed=True))
        assert playback.wait(8),playback.snapshot()
        snap=playback.snapshot()
        assert snap['progress']['completed_cycles'] == 1
        assert snap['progress']['active_seconds'] >= 12.64
        assert trace['phases']['returning']-1000. >= 12.64
        # Evidence uses the existing asynchronous writer, separate from control.
        for _ in range(200):
            try:
                evidence=json.loads((tmp_path/'completion.json').read_text())
                break
            except (FileNotFoundError,json.JSONDecodeError):
                pass
            threading.Event().wait(.005)
        assert evidence['source']=='mujoco_hanging'
        if scenario=='normal':
            assert snap['completion']['safe_disabled']
            assert not backend.motors_enabled and backend.connected
            assert snap['completion']['return_seconds'] == 5.
            assert snap['completion']['settling_seconds'] >= 1.
            assert trace['disable'][-1]['phase']=='settling'
            target=plan_return(config,np.zeros(len(config.entries))).target_motor
            assert max(abs(np.array([s.pos_rad for s in trace['disable'][-1]['samples']])-target)) <= .03
            assert evidence['parking_pose_confirmed'] is True
            assert evidence['automatic_disable_requested'] is True
        elif scenario=='fault':
            assert snap['completion']['phase']=='fault'
            assert not backend.motors_enabled
            assert not snap['completion']['safe_disabled']
        elif scenario=='cancel':
            assert snap['completion']['phase']=='interrupted'
            assert not snap['completion']['safe_disabled']
            assert not trace['disable']
        else:
            assert trace['healthy_alarm'] is True
            assert not trace['disable']
            assert not snap['completion']['safe_disabled']
            assert snap['completion']['phase']=='interrupted'
    finally:
        backend.disconnect()


def test_connection_is_continuous_and_manual_impossible_connection_keeps_torque(tmp_path):
    config=load_station_config('left_arm')
    start=np.array([.1,.1,.1,-.1,.1,.1,.1])
    velocity=np.full(7,.02)
    plan=plan_return(config,start,velocity)
    np.testing.assert_allclose(plan.at(0),start,atol=1e-12)
    np.testing.assert_allclose((plan.at(1e-5)-plan.at(0))/1e-5,velocity,atol=1e-6)
    np.testing.assert_allclose(plan.at(plan.bridge_seconds),np.zeros(7),atol=1e-12)
    backend=FakeMotorBackend(config.entries); backend.connect()
    trajectory=import_trajectory(preset('left_arm')['trajectory_path'],limb='left_arm',motor_count=7)
    playback=PlaybackController(backend,config,trajectory,enable_watchdog=False,completion_output=tmp_path/'unused.json')
    try:
        enabled=backend.motors_enabled
        commands=len(backend.command_history)
        playback._return_mujoco(np.full(7,100.),np.zeros(7),0.,None)
        assert playback.snapshot()['completion']['phase']=='holding'
        assert backend.motors_enabled == enabled
        assert len(backend.command_history)==commands
        assert not playback.snapshot()['completion']['safe_disabled']
    finally:
        backend.disconnect()


def test_real_time_return_keeps_test_csv_separate(tmp_path):
    import csv
    from factory_hmi.models import TrajectoryData
    config=load_station_config('left_arm')
    backend=FakeMotorBackend(config.entries); backend.connect()
    trajectory=TrajectoryData(tmp_path/'synthetic.npz','left_arm',np.zeros((3,7)),np.array([0.,.15,.3]),
                              2/.3,'isolated-test-only',{})
    playback=PlaybackController(backend,config,trajectory,
        state_machine=StationStateMachine(StationState.ARMED),enable_watchdog=False,
        enable_recording=True,record_output=tmp_path/'test.csv',completion_output=tmp_path/'completion.json')
    try:
        playback.start(PlaybackOptions(completion_mode='mujoco_hanging',parking_pose_confirmed=True))
        assert playback.wait(12),playback.snapshot()
        assert playback.wait_recording(2)
        snap=playback.snapshot()
        assert snap['completion']['safe_disabled']
        assert snap['completion']['return_seconds']==5.
        assert snap['completion']['settling_seconds']>=1.
        assert .3 <= snap['progress']['active_seconds'] < .6
        rows=list(csv.DictReader((tmp_path/'test.csv').open()))
        assert rows
        columns=[key for key in rows[0] if 'cmd_pos' in key]
        assert columns
        assert all(abs(float(row[key]))<1e-8 for row in rows for key in columns if row[key])
    finally:
        backend.disconnect()
