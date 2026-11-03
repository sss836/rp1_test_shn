"""Operator-requested runtime policy; fake motors/synthetic facts only."""
from copy import deepcopy
from dataclasses import replace
import csv
import math
import time
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.config import load_station_config
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.fault_diagnosis import DiagnosisThresholds, FaultDiagnosisEngine
from factory_hmi.core.first_frame_approach import plan_first_frame_approach
from factory_hmi.core.report import ReportManager
from factory_hmi.models import MotorSample, PlaybackOptions, StationState
from factory_hmi.gateway.schemas import PlaybackStartRequest


def make_controller(tmp_path, *, gap=.9, follow=1., default_approach=False):
    config = load_station_config('right_short_arm')
    # Explicit accelerated motion profile for fake-only lifecycle tests.
    # The separate planner test verifies the real default 10 degrees/s cap.
    if not default_approach:
        config.raw['factory_hmi']['first_frame_approach'].update(
            speed_rad_s=5., min_duration_s=.05, stable_seconds=.02, settling_timeout_s=.15)
    backend = FakeMotorBackend(config.entries, response_ratio=follow)
    controller = FactoryController(config, backend, enable_watchdog=False, enable_recording=True,
        record_output=tmp_path / 'test.csv')
    controller.completion_output = tmp_path / 'completion.json'
    values = np.full((3, len(config.entries)), gap)
    values[1] += .01
    path = tmp_path / 'trajectory.npz'
    np.savez(path, motor_pos=values, time=[0., .06, .12], fps=[16.6666667], limb=[config.limb])
    controller.connect(); controller.load_trajectory(path)
    return controller, backend


def wait_phase(controller):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        playback = controller.snapshot().get('playback') or {}
        if playback.get('completion', {}).get('phase') in {'approaching','approach_settling'}:
            return playback
        time.sleep(.002)
    raise AssertionError('approach phase not observed')


@pytest.mark.parametrize('speed', [.1, 1., 5.])
def test_playback_speed_boundaries_match_api_and_core(speed):
    assert PlaybackOptions(speed=speed).speed == speed
    assert PlaybackStartRequest(speed=speed,test_case_id='SYNTHETIC').speed == speed


@pytest.mark.parametrize('speed', [0., .099, 5.001, math.inf, math.nan])
def test_playback_speed_outside_range_is_rejected(speed):
    with pytest.raises(ValueError): PlaybackOptions(speed=speed)
    with pytest.raises(ValueError): PlaybackStartRequest(speed=speed,test_case_id='SYNTHETIC')


def test_defaults_and_100C_boundary_keep_combined_load_as_warning():
    config = load_station_config('right_short_arm')
    assert config.safety_limits.max_velocity_rad_s == 20.
    assert config.safety_limits.max_first_frame_delta_rad is None
    thresholds = DiagnosisThresholds.from_config(config.raw)
    assert thresholds.overtemperature_c == 100.
    base = MotorSample(pos_rad=0., temp_c=25., torque_nm=40., timestamp_s=1.)
    engine = FaultDiagnosisEngine(thresholds)
    engine.diagnose([base], now_s=0.)
    diagnoses = engine.diagnose([replace(base, temp_c=99.9)], now_s=2.1)
    assert any(d.rule == 'persistent_torque_temperature_rise' for d in diagnoses)
    assert all(d.severity == 'warning' for d in diagnoses)
    hot = engine.diagnose([replace(base, temp_c=100.)], now_s=2.2)
    assert any(d.rule == 'motor_overtemperature' and d.severity == 'critical' for d in hot)


def test_default_approach_exceeds_old_gap_without_wrap_and_remains_slow():
    config = load_station_config('right_short_arm')
    origin = np.zeros(len(config.entries)); target = np.full_like(origin, 2.)
    plan = plan_first_frame_approach(config, origin, target)
    assert plan.required and plan.duration_s >= 1.875 * 2 / math.radians(10)
    times = np.linspace(0, plan.duration_s, 1001)
    values = np.array([plan.at(t) for t in times])
    assert np.all(np.diff(values, axis=0) >= -1e-12)
    assert np.max(abs(np.diff(values, axis=0) / np.diff(times)[:,None])) <= math.radians(10) + 1e-6
    assert np.allclose(values[0], origin) and np.allclose(values[-1], target)
    config = replace(config, safety_limits=replace(config.safety_limits, motor_max_rad=1.))
    with pytest.raises(ValueError, match='位置限位'):
        plan_first_frame_approach(config, origin, target)


def test_large_gap_approaches_before_test_time_and_csv_start(tmp_path):
    controller, backend = make_controller(tmp_path)
    try:
        controller.arm()
        preview = controller.snapshot()['trajectory']['preflight']['approach']
        assert preview['required'] and preview['max_distance_rad'] == pytest.approx(.9)
        assert np.allclose(backend.positions, 0), 'arm must never execute approach motion'
        controller.start()
        phase = wait_phase(controller)
        assert phase['progress']['active_seconds'] == 0
        assert phase['recording']['row_count'] == 0 and not phase['recording']['active']
        assert controller.wait(3)
        snap = controller.snapshot()['playback']
        assert controller.state == StationState.COMPLETED
        assert snap['completion']['approach_completed']
        assert snap['progress']['active_seconds'] < .3
        rows = list(csv.DictReader(controller.record_output.open()))
        assert rows and float(rows[0]['time_s']) < .1
        position_columns = [name for name in rows[0] if name.endswith('_pos_rad') and not name.endswith('_cmd_pos_rad')]
        assert all(float(row[name]) >= .87 for row in rows for name in position_columns)
        assert controller.position_hold_active and backend.motors_enabled
    finally:
        controller.disconnect()


@pytest.mark.parametrize('interrupt', ['finish','fault','temperature','stale'])
def test_approach_interruption_never_starts_test(tmp_path, interrupt):
    controller, backend = make_controller(tmp_path, gap=2.)
    try:
        controller.arm(); controller.start(); wait_phase(controller)
        if interrupt == 'finish': controller.finish('fake operator cancellation')
        elif interrupt == 'fault': backend.inject_error(0,7)
        elif interrupt == 'temperature': backend.temperatures[0] = 100.
        else: backend.stale[0] = True; backend.feedback_timestamps[0] = time.monotonic()-3
        assert controller.wait(3)
        snap = controller.snapshot()['playback']
        assert snap['progress']['active_seconds'] == 0
        assert not snap['completion'].get('approach_completed', False)
        assert snap['recording']['row_count'] == 0
        if interrupt != 'finish':
            assert controller.state == StationState.FAULT and not backend.motors_enabled
    finally:
        controller.disconnect()


def test_approach_requires_actual_arrival_not_only_elapsed_duration(tmp_path):
    controller, backend = make_controller(tmp_path, follow=0.)
    try:
        controller.arm(); controller.start()
        assert controller.wait(3)
        snap = controller.snapshot()['playback']
        assert controller.state == StationState.FAULT and not backend.motors_enabled
        assert '跟踪偏差超出工位运行范围' in snap['fault']['message']
        failed=controller.playback._completion_evidence['first_frame_approach']
        assert failed['outcome']=='timeout' and not all(failed['within_position'])
        assert len(failed['last_feedback'])==len(controller.config.entries)
        assert snap['progress']['active_seconds'] == 0 and snap['recording']['row_count'] == 0
    finally:
        controller.disconnect()


def test_real_default_slow_approach_does_not_trip_csv_stall_and_preserves_actual_start(tmp_path):
    from factory_hmi.core.service import FactoryService
    from factory_hmi.core.report import SUMMARY_PATH
    import json
    controller, backend=make_controller(tmp_path,gap=.3,default_approach=True)
    service=FactoryService(data_root=tmp_path/'service')
    try:
        requested=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
        controller.arm(); controller.start()
        assert controller.wait(7)
        snap=controller.snapshot()['playback']
        assert snap['completion']['approach_elapsed_s'] > 3
        assert controller.state==StationState.COMPLETED and not snap['recording']['error']
        assert snap['progress']['active_seconds'] < .3
        assert datetime.fromisoformat(snap['test_started_at'].replace('Z','+00:00')) > datetime.fromisoformat(requested.replace('Z','+00:00'))
        context=dict(test_id='approach-clock',sample_id='SYNTHETIC',bench_id='A10',started_at=requested,
            record_rate_hz=20,config=controller.config.raw,acceptance=controller.config.raw['factory_hmi_acceptance'])
        service._finalize_after_run(controller,controller.record_output,context,controller.playback)
        assert not service._report_errors
        manifest=service.report_manager.manifest('approach-clock')
        assert manifest['started_at']==snap['test_started_at']
        execution=service.report_manager.execution_dir('approach-clock')
        frozen=json.loads((execution/'config/execution.json').read_text())
        assert frozen['preparation']['requested_at']==requested
    finally:
        service.close(); controller.disconnect()


def test_csv_open_failure_also_interrupts_initial_approach(tmp_path):
    controller,backend=make_controller(tmp_path,gap=2.)
    try:
        controller.arm()
        with patch('factory_hmi.core.playback.MotorTelemetryRecorder._open_csv',side_effect=OSError('synthetic ENOSPC')):
            controller.start(); assert controller.wait(3)
        snap=controller.snapshot()['playback']
        assert controller.state==StationState.FAULT and not backend.motors_enabled
        assert snap['progress']['active_seconds']==0
        assert 'telemetry_io_error' in snap['fault']['message']
    finally:
        controller.disconnect()


@pytest.mark.parametrize('raw_speed, multiplier, accepted', [(30.,.1,True),(3.,5.,True),(5.,5.,False)])
def test_selected_multiplier_is_applied_before_velocity_gate(tmp_path, raw_speed, multiplier, accepted):
    controller, backend = make_controller(tmp_path, gap=0.)
    try:
        values=np.zeros((3,len(controller.config.entries))); values[1]=.15
        dt=.15/raw_speed
        path=tmp_path/'scaled.npz'
        np.savez(path,motor_pos=values,time=[0.,dt,2*dt],fps=[1/dt],limb=[controller.config.limb])
        controller.load_trajectory(path); controller.arm()
        if accepted:
            controller.start(speed=multiplier)
            assert controller.wait(2) and controller.state == StationState.COMPLETED
        else:
            with pytest.raises(ValueError,match='velocity'):
                controller.start(speed=multiplier)
            assert controller.position_hold_active and backend.motors_enabled
    finally:
        controller.disconnect()


def test_runtime_v2_removes_three_acceptance_rules_but_preserves_measurements_and_old_rules(tmp_path):
    csv = tmp_path / 'facts.csv'
    csv.write_text('time_s,a_pos_rad,a_cmd_pos_rad,a_torque_nm,a_temp_c,a_error_id\n'
        '0,0,2,999,20,0\n1,0,2,999,99,0\n')
    context = dict(test_id='new', started_at='2026-10-30T00:00:00Z', ended_at='2026-10-30T00:00:01Z',
        active_seconds=1, completed_cycles=1, record_rate_hz=1, outcome='completed',
        acceptance=deepcopy(load_station_config('right_short_arm').raw['factory_hmi_acceptance']))
    context['acceptance']['max_gap_seconds'] = 1.
    manager = ReportManager(tmp_path/'data')
    new = manager.finalize(csv, context)
    assert new.summary['verdict']['status'] == 'passed'
    names = [r['name'] for r in new.summary['verdict']['rules']]
    assert not any(n.endswith(('temperature_peak_rise_c','torque_p999_utilization','tracking_rms_rad')) for n in names)
    assert new.summary['joint_metrics'][0]['temperature_peak_rise_c'] == 79
    assert new.summary['joint_metrics'][0]['torque_p999_abs_nm'] == 999
    assert new.summary['joint_metrics'][0]['tracking_rms_rad'] == 2
    old_context = deepcopy(context); old_context['test_id']='old'
    old_context['acceptance'].update(criteria_version='generic-v1',max_temperature_rise_c=30.,
        max_torque_p999_utilization=1.,max_tracking_rms_rad=.5)
    old = manager.finalize(csv, old_context)
    assert old.summary['verdict']['status'] == 'failed'
    assert manager.rebuild('old',actor='fake',reason='historical rule check').summary['verdict']['status'] == 'failed'


def test_async_joint_feedback_can_start_after_real_arrival_without_counting_approach(tmp_path,monkeypatch):
    controller,backend=make_controller(tmp_path,gap=.2)
    original=backend._read_samples_impl
    cached=original();counter={'value':0}
    def interleaved_feedback():
        latest=original();i=counter['value']%len(cached);counter['value']+=1
        cached[i]=latest[i]
        return [sample.with_feedback_age(time.monotonic()) for sample in cached]
    monkeypatch.setattr(backend,'_read_samples_impl',interleaved_feedback)
    try:
        controller.arm();controller.start();assert controller.wait(3)
        snap=controller.snapshot()['playback']
        assert controller.state==StationState.COMPLETED
        assert snap['completion']['approach_completed'] and snap['recording']['row_count']>0
        evidence=controller.playback._completion_evidence['first_frame_approach']
        assert evidence['confirmed_feedback_sets']>=2
        assert len(evidence['arrival_feedback'])==len(controller.config.entries)
        assert snap['progress']['active_seconds']<.3
    finally:controller.disconnect()
