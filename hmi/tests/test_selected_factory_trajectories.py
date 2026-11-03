"""User-selected immutable NPZs with explicit synthetic motor/parking limits.

Virtual monotonic time advances the original 1x timeline; no frame data is
retimed or modified, and no real CAN/PLC object is created.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.config import load_station_config
from factory_hmi.core.motion_completion import configuration_sha256, motion_readiness
from factory_hmi.core.playback import PlaybackController
from factory_hmi.core.state_machine import StationStateMachine
from factory_hmi.core.trajectory import import_trajectory
from factory_hmi.models import PlaybackOptions, StationState
from factory_hmi.quick_profiles import RESOURCE_ROOT, catalog, preset

SELECTED={
    'left_short_arm': '24f44bf9f112788c700c4f189379403476d76e4f43678ee0c6af934426f5227c',
    'right_short_arm': 'd9bdeda272e073146b3b8597fadc9b90a05731c40e8470ad3226587d48a7e7be',
}
OTHER={
    'left_arm':'984cf5a6764113800edf764d9f779a83e5bf29da90b9f9349e7b771b1719a746',
    'right_arm':'b610359b4b2813900ac4293980ca832149200ed3d63fa44a7a4257e8dc5cdbba',
    'left_short_leg':'a3066437b09168955c4d5fa14c690772c14afcdc87ef2ce54c739e5f0da820e4',
    'right_short_leg':'8a45ed836d1557fd149cf15b65b812d8d1cbd935189a5e1c88ef01a405d70d3b',
    'waist_hip':'cf68d2c8b21e65ea89d75b1017d81237a3ee09c15acf60a256cac2ab5d68aaf0',
}


@pytest.mark.parametrize('limb',SELECTED)
def test_selected_resource_and_source_provenance(limb):
    item=preset(limb)
    manifest=json.loads((RESOURCE_ROOT/'manifest.json').read_text())
    entry=next(e for e in manifest['trajectories'] if e['limb']==limb)
    assert item['bundle_version']==manifest['version']==2
    assert item['resource_revision']==2
    assert item['source_sha256']==item['sha256']==SELECTED[limb]
    assert entry['source_name']==limb+'_joint_inspect.npz'
    assert entry['selection_basis']=='user_explicit_selection_2026-10-28'
    source=Path(entry['source_path'])
    if source.exists():
        assert source.read_bytes()==item['trajectory_path'].read_bytes()
    data=import_trajectory(item['trajectory_path'],limb=limb,motor_count=5)
    assert data.motor_pos.shape==(633,5) and data.fps==50.
    assert data.duration_s==12.64
    np.testing.assert_array_equal(data.motor_pos[[0,-1]],np.zeros((2,5)))
    assert not motion_readiness(load_station_config(limb))['ready']


def test_other_five_resources_are_unchanged():
    assert len(catalog())==7
    for limb,expected in OTHER.items():
        item=preset(limb)
        assert item['sha256']==expected
        assert hashlib.sha256(item['trajectory_path'].read_bytes()).hexdigest()==expected


def isolated_config(limb):
    config=replace(load_station_config(limb),control_rate_hz=200.)
    raw=deepcopy(config.raw)
    raw.setdefault('factory_hmi',{})['motion_completion']=dict(
        verified=True,safe_to_disable=True,limb=limb,
        parameter_revision='SYNTHETIC-SELECTED-NPZ-ONLY',
        verification_evidence='isolated fake fixture; not product parameters',
        joint_min_rad=[-1000.]*5,joint_max_rad=[1000.]*5,
        motor_min_rad=[-1000.]*5,motor_max_rad=[1000.]*5,
        max_velocity_rad_s=[1000.]*5,max_acceleration_rad_s2=[100000.]*5,
        return_velocity_rad_s=[.25]*5,return_acceleration_rad_s2=[2.]*5,
        parking_joint_rad=[.04,.05,-.03,.08,.06],
        position_tolerance_rad=[.0001]*5,speed_tolerance_rad_s=[.001]*5,
        max_first_frame_delta_rad=[.01]*5,
        stable_seconds=.05,timeout_seconds=2.,feedback_stale_seconds=.1)
    config=replace(config,raw=raw)
    raw['factory_hmi']['motion_completion']['configuration_sha256']=configuration_sha256(config)
    return config


@pytest.mark.parametrize('limb',SELECTED)
@pytest.mark.parametrize('scenario',['normal','unreached','fault'])
def test_selected_full_cycle_then_verified_return_or_protection(tmp_path,monkeypatch,limb,scenario):
    config=isolated_config(limb)
    backend=FakeMotorBackend(config.entries)
    backend.connect()
    data=import_trajectory(preset(limb)['trajectory_path'],limb=limb,motor_count=5)
    playback=PlaybackController(backend,config,data,
        state_machine=StationStateMachine(StationState.ARMED),enable_watchdog=False,
        completion_output=tmp_path/(limb+'-'+scenario+'.json'))
    clock=[1000.]
    trace={'phases':{},'healthy_alarm':None,'disable_checks':[]}
    command=backend.command_mit
    disable=backend.disable_motors
    def capture_command(target,**kwargs):
        phase=playback.snapshot()['completion']['phase']
        trace['phases'].setdefault(phase,clock[0])
        if phase=='returning':
            if scenario=='unreached':backend.response_ratio=0.
            elif scenario=='fault':backend.inject_error(0,7)
        return command(target,**kwargs)
    def capture_disable():
        trace['disable_checks'].append(dict(time=clock[0],samples=backend.read_samples(),
            phase=playback.snapshot()['completion']['phase']))
        return disable()
    def advance(seconds):
        clock[0]+=max(.00001,float(seconds))
        if playback.snapshot()['completion']['phase']=='needs_attention':
            trace['healthy_alarm']=dict(snapshot=playback.snapshot(),enabled=backend.motors_enabled)
            playback.stop(wait=False,reason='synthetic protection after observing healthy alarm')
        return playback._stop_event.is_set()
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(playback._stop_event,'wait',advance)
    monkeypatch.setattr(backend,'command_mit',capture_command)
    monkeypatch.setattr(backend,'disable_motors',capture_disable)
    try:
        playback.start(PlaybackOptions(loop=True,duration_s=.25,speed=1.))
        assert playback.wait(5),playback.snapshot()
        snapshot=playback.snapshot()
        assert snapshot['progress']['completed_cycles']==1
        assert snapshot['progress']['trajectory_seconds']==12.64
        assert 12.64<=snapshot['progress']['active_seconds']<12.66
        assert snapshot['progress']['frame']==632
        assert 'finishing_cycle' in trace['phases']
        assert trace['phases']['returning']-1000.>=12.64
        # Exact full-cycle last frame precedes any separate return target.
        return_start=trace['phases']['returning']
        test_commands=[e['target'] for e in backend.command_history if e['timestamp_s']<return_start]
        assert test_commands
        np.testing.assert_array_equal(test_commands[-1],data.motor_pos[-1])
        if scenario=='normal':
            assert snapshot['completion']['safe_disabled']
            assert snapshot['progress']['outcome']=='duration_complete'
            assert not backend.motors_enabled and backend.connected
            assert snapshot['completion']['return_seconds']>0
            assert snapshot['completion']['settling_seconds']>=.05
            assert trace['disable_checks'] and trace['disable_checks'][-1]['phase']=='settling'
            samples=trace['disable_checks'][-1]['samples']
            params=playback._parameters
            assert np.all(np.abs([s.pos_rad for s in samples]-params.parking_motor_rad)<=params.values['position_tolerance_rad'])
            assert np.all(np.abs([s.spd_rad_s for s in samples])<=params.values['speed_tolerance_rad_s'])
        elif scenario=='unreached':
            assert trace['healthy_alarm']['enabled']
            assert trace['healthy_alarm']['snapshot']['completion']['active']
            assert not trace['healthy_alarm']['snapshot']['completion']['safe_disabled']
            assert not trace['disable_checks']
            assert snapshot['completion']['phase']=='interrupted'
        else:
            assert snapshot['completion']['phase']=='fault'
            assert not snapshot['completion']['safe_disabled']
            assert not backend.motors_enabled
            assert clock[0]-return_start<.05
    finally:
        backend.disconnect()
