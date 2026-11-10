"""Production station compatibility using fake motors only; no real CAN/PLC."""
from dataclasses import replace
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.config import load_station_config
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.motion_completion import automatic_completion_parameters, MotionParameterError
from factory_hmi.models import PlaybackOptions, StationState


def controller_for(tmp_path, limb='left_arm', duration=.12):
    config = load_station_config(limb)
    backend = FakeMotorBackend(config.entries)
    controller = FactoryController(config, backend, enable_watchdog=False)
    controller.completion_output = tmp_path / 'completion.json'
    values = np.zeros((3, len(config.entries))); values[1] = .01
    path = tmp_path / 'trajectory.npz'
    np.savez(path, motor_pos=values, time=[0., duration / 2, duration], fps=[2 / duration], limb=[limb])
    controller.connect(); controller.load_trajectory(path)
    return controller, backend


@pytest.mark.parametrize('limb', ['left_arm', 'right_arm', 'left_short_arm', 'right_short_arm'])
def test_existing_arm_configs_can_preflight_start_and_complete_without_auto_disable(tmp_path, limb):
    controller, backend = controller_for(tmp_path, limb)
    try:
        original = controller.config.raw.copy()
        controller.arm(); controller.start(loop=True, duration_s=.025)
        assert controller.wait(2)
        snap = controller.snapshot()
        assert controller.state == StationState.COMPLETED
        assert snap['playback']['progress']['completed_cycles'] == 1
        assert snap['playback']['completion']['phase'] == 'holding'
        assert not snap['playback']['completion']['safe_disabled']
        assert not snap['playback']['completion']['automatic_return_enabled']
        assert backend.motors_enabled and backend.connected and controller.position_hold_active
        assert controller.config.raw == original and 'motion_completion' not in controller.config.raw.get('factory_hmi', {})
    finally:
        controller.disconnect()


def test_large_initial_gap_is_prepared_without_motion(tmp_path):
    controller, backend = controller_for(tmp_path)
    try:
        controller._trajectory.motor_pos += 2.
        controller.arm()
        assert controller._trajectory.preflight.approach['required']
        assert controller._trajectory.preflight.approach['max_distance_rad'] == 2.
        assert controller.position_hold_active and backend.motors_enabled
        assert np.allclose(backend.positions, 0)
    finally:
        controller.disconnect()


def test_file_peak_does_not_block_normal_start_but_live_overspeed_stops_playback(tmp_path, monkeypatch):
    controller, backend = controller_for(tmp_path)
    try:
        controller.config = replace(controller.config, safety_limits=replace(
            controller.config.safety_limits, max_velocity_rad_s=.5))
        controller.arm()
        assert controller.trajectory.preflight.max_velocity_rad_s * 5 > .5
        controller.start(PlaybackOptions(speed=5., loop=True))
        playback = controller.playback
        assert playback._test_started.wait(1)
        assert controller.state is StationState.RUNNING

        read_samples = backend.read_samples

        def overspeed_feedback():
            samples = read_samples()
            samples[0] = replace(samples[0], spd_rad_s=.6)
            return samples

        monkeypatch.setattr(backend, 'read_samples', overspeed_feedback)
        assert controller.wait(2)
        assert controller.state is StationState.FAULT
        assert '实际电机速度超过当前配置上限' in playback.fault.message
    finally:
        controller.disconnect()


def test_operator_finish_preserves_hold_and_does_not_report_safe_disable(tmp_path):
    controller, backend = controller_for(tmp_path, duration=1.)
    try:
        controller.arm(); controller.start(loop=True)
        assert backend.wait_for_commands(5)
        controller.finish('operator finish')
        assert controller.wait(2)
        snap = controller.snapshot()['playback']
        assert snap['progress']['outcome'] == 'stopped'
        assert snap['completion']['phase'] == 'holding' and not snap['completion']['safe_disabled']
        assert controller.position_hold_active and backend.motors_enabled
    finally:
        controller.disconnect()


def test_legacy_path_preserves_motor_fault_protection(tmp_path):
    controller, backend = controller_for(tmp_path, duration=1.)
    try:
        controller.arm(); controller.start(loop=True)
        assert backend.wait_for_commands(5)
        backend.inject_error(0, 7)
        assert controller.wait(2)
        assert controller.state == StationState.FAULT
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


def test_malformed_explicit_completion_profile_does_not_silently_fall_back():
    config = load_station_config('left_arm')
    config.raw.setdefault('factory_hmi', {})['motion_completion'] = {'verified': 'true'}
    with pytest.raises(MotionParameterError, match='布尔值'):
        automatic_completion_parameters(config)


def test_factory_completed_legacy_run_keeps_hold_can_ownership_and_monitoring(tmp_path):
    from factory_hmi.gateway.quick_run import QuickRun
    controller, backend = controller_for(tmp_path)
    controller.arm(); controller.start()
    released = []
    runtime = SimpleNamespace(controller=SimpleNamespace(controller=controller, request_motion_stop=controller.request_motion_stop),
        _lease_guard=threading.RLock(), lease=SimpleNamespace(snapshot=lambda: SimpleNamespace(expired=False)),
        _invoke=lambda names, **kwargs: getattr(controller, names[0])(**kwargs))
    quick = QuickRun(runtime, SimpleNamespace(release_interfaces=lambda owner: released.append(owner)), 'fake-owner')
    quick._power_source = 'external'; quick._state('running', 'fake')
    watcher = threading.Thread(target=quick._watch, args=(quick._stop,), daemon=True); watcher.start()
    try:
        assert controller.wait(2)
        deadline = time.monotonic() + 2
        while quick.snapshot()['phase'] != 'holding' and time.monotonic() < deadline:
            time.sleep(.01)
        assert quick.snapshot()['phase'] == 'holding' and quick.snapshot()['active']
        assert watcher.is_alive() and not released and backend.connected and backend.motors_enabled
        assert quick.end()['phase'] == 'holding'
        assert not released and backend.motors_enabled
    finally:
        quick._stop.set(); watcher.join(1); controller.disconnect()
