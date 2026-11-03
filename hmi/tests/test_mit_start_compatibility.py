"""A synthetic static MIT tracking offset is not proof of a driver fault."""
from dataclasses import replace
import csv,time
import numpy as np
import pytest
from factory_hmi.core.config import load_station_config
from factory_hmi.core.first_frame_approach import plan_first_frame_approach
from factory_hmi.core.fault_diagnosis import DiagnosisThresholds
from factory_hmi.models import StationState
from test_runtime_policy import make_controller,wait_phase


def test_ordinary_start_reuses_existing_tracking_threshold_and_explicit_precision_wins():
    config=load_station_config('left_arm');origin=np.zeros(len(config.entries));target=origin.copy()
    assert config.raw['factory_hmi']['first_frame_approach']['position_tolerance_rad'] is None
    plan=plan_first_frame_approach(config,origin,target)
    assert plan.position_tolerance_rad==DiagnosisThresholds.from_config(config.raw).tracking_error_rad
    assert plan.position_tolerance_source=='runtime_tracking_limit'
    assert not plan.to_dict()['precise_position_required']
    config.raw['factory_hmi']['first_frame_approach']['position_tolerance_rad']=.03
    strict=plan_first_frame_approach(config,origin,target)
    assert strict.position_tolerance_rad==.03 and strict.to_dict()['precise_position_required']


@pytest.mark.parametrize('strict',[False,True])
def test_same_static_offset_runs_in_ordinary_mode_but_blocks_explicit_precision(tmp_path,monkeypatch,strict):
    controller,backend=make_controller(tmp_path,gap=0.)
    config=controller.config.raw['factory_hmi']['first_frame_approach']
    config['position_tolerance_rad']=.03 if strict else None
    bias=np.zeros(len(controller.config.entries));bias[0]=.112;bias[3]=-.128
    backend.positions[:]=bias
    original=backend._command_mit_impl
    def biased_commands(target,**kwargs):
        before=backend.positions.copy();original(target,**kwargs)
        backend.positions[:]+=bias
        backend.speeds[:]=backend.positions-before
    monkeypatch.setattr(backend,'_command_mit_impl',biased_commands)
    try:
        controller.arm();controller.start();assert controller.wait(3)
        snapshot=controller.snapshot()['playback']
        if strict:
            assert controller.state==StationState.FAULT
            assert snapshot['recording']['row_count']==0 and snapshot['progress']['active_seconds']==0
            assert '首帧缓慢接近后未到位' in snapshot['fault']['message']
        else:
            assert controller.state==StationState.COMPLETED
            assert snapshot['completion']['approach_completed']
            evidence=controller.playback._completion_evidence['first_frame_approach']
            assert evidence['position_confirmation']=='within_running_tracking_envelope'
            assert evidence['actual_start_error_rad']==pytest.approx(bias)
            assert snapshot['recording']['row_count']>0
            with controller.record_output.open() as stream:rows=list(csv.DictReader(stream))
            name=controller.config.entries[0].joint_name
            assert rows and all(float(r[name+'_pos_rad'])-float(r[name+'_cmd_pos_rad'])==pytest.approx(bias[0]) for r in rows)
    finally:controller.disconnect()


@pytest.mark.parametrize('fault',['error','stale','temperature'])
def test_ordinary_mode_does_not_accept_driver_fault_stale_or_hot_feedback(tmp_path,fault):
    controller,backend=make_controller(tmp_path,gap=.9)
    controller.config.raw['factory_hmi']['first_frame_approach']['position_tolerance_rad']=None
    try:
        controller.arm();controller.start();wait_phase(controller)
        if fault=='error':backend.inject_error(0,7)
        elif fault=='temperature':backend.temperatures[0]=100.
        else:backend.stale[0]=True;backend.feedback_timestamps[0]=time.monotonic()-3
        assert controller.wait(3) and controller.state==StationState.FAULT
        snapshot=controller.snapshot()['playback']
        assert snapshot['recording']['row_count']==0 and snapshot['progress']['active_seconds']==0
        assert not backend.motors_enabled
    finally:controller.disconnect()


def test_parking_precision_does_not_become_an_unrequested_startup_gate():
    from types import SimpleNamespace
    config=load_station_config('left_arm');count=len(config.entries)
    tolerances=np.linspace(.001,.007,count)
    profile=SimpleNamespace(values={'position_tolerance_rad':tolerances,'max_velocity_rad_s':np.ones(count)},check_target=lambda *args:None)
    plan=plan_first_frame_approach(config,np.zeros(count),np.zeros(count),parameters=profile)
    assert plan.position_tolerance_source=='runtime_tracking_limit'
    assert plan.position_tolerance_rad==DiagnosisThresholds.from_config(config.raw).tracking_error_rad
    assert not plan.to_dict()['precise_position_required']
    assert profile.values['position_tolerance_rad']==pytest.approx(tolerances)
