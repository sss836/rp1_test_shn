"""Cached evidence stays intact while current-status views expire safely."""
from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from factory_hmi import FactoryController, MotorSample, StationConfig, StationState
from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.state_machine import StationStateMachine
from factory_hmi.desktop.feedback_freshness import DISPLAY_STALE_S, STATUS_SILENCE_S, feedback_age_s
from test_motor_feedback_display import window, cell


def snapshot(stamp=100., *, motor_stamp=None, age=0., temperature=46., error=0):
    return {
        "timestamp_s": stamp, "configured": True, "connected": True,
        "station": {"state": "connected", "backend_connected": True,
                    "motors_discovered": True, "motors_enabled": False},
        "motors": [{"motor_id": 3, "bus": "can3", "joint_name": "left_shoulder_yaw_joint",
                    "timestamp_s": stamp if motor_stamp is None else motor_stamp,
                    "feedback_age_s": age, "position_rad": .1, "speed_rad_s": 0.,
                    "torque_nm": 0., "temperature_c": temperature, "error_id": error}],
    }


def test_aging_preserves_measurements_and_original_sample():
    observed = MotorSample(.1, temp_c=46., error_id=7, timestamp_s=100., feedback_age_s=.01)
    view = observed.with_feedback_age(110.)
    assert view.feedback_age_s == 10.
    assert (view.pos_rad, view.temp_c, view.error_id, view.timestamp_s) == (.1, 46., 7, 100.)
    assert observed.feedback_age_s == .01


@pytest.mark.parametrize("stamp,age", [(0., 0.), (math.nan, 0.), (120., 0.), (100., math.nan), (100., -1.)])
def test_invalid_time_or_age_does_not_become_fresh(stamp, age):
    view = MotorSample(.1, temp_c=46., timestamp_s=stamp, feedback_age_s=age).with_feedback_age(110.)
    assert math.isinf(view.feedback_age_s)
    assert view.to_dict()["feedback_age_s"] is None


def test_read_failure_ages_cached_evidence_and_recovers_without_changing_error(window, monkeypatch, caplog):
    backend = FakeMotorBackend(1)
    config = StationConfig(limb="left_arm", entries=backend.entries, raw={})
    controller = FactoryController(config, backend, enable_watchdog=False)
    clock = [100.]
    monkeypatch.setattr("factory_hmi.core.controller.time.monotonic", lambda: clock[0])
    backend.open_transport()
    backend.temperatures[:] = 46.
    backend.error_ids[:] = 7
    original_refresh = backend.refresh_feedback
    try:
        first = controller.snapshot()
        original_sample = controller._last_samples[0]
        assert first["motors"][0]["feedback_age_s"] == 0.

        def failing_refresh():
            raise OSError("injected receive failure")

        monkeypatch.setattr(backend, "refresh_feedback", failing_refresh)
        clock[0] = 110.
        failed = controller.snapshot()
        assert failed["motors"][0]["feedback_age_s"] == 10.
        assert failed["motors"][0]["temperature_c"] == 46.
        assert failed["motors"][0]["error_id"] == 7
        assert failed["motors"][0]["timestamp_s"] == 100.
        assert "injected receive failure" in failed["feedback_read_error"]
        assert failed["joint_positions_rad"] == []
        assert controller._last_samples[0] is original_sample
        assert original_sample.feedback_age_s == 0.
        window._render_snapshot(failed)
        assert cell(window, 4) == cell(window, 8) == "—"
        assert cell(window, 11) == "7"
        assert "读取失败" in cell(window, 12)

        clock[0] = 111.
        assert controller.snapshot()["motors"][0]["feedback_age_s"] == 11.
        assert sum("Motor snapshot read failed" in r.message for r in caplog.records) == 1

        monkeypatch.setattr(backend, "refresh_feedback", original_refresh)
        clock[0] = 112.
        backend.temperatures[:] = 47.
        recovered = controller.snapshot()
        assert recovered["feedback_read_error"] is None
        assert recovered["motors"][0]["temperature_c"] == 47.
        window._render_snapshot(recovered)
        assert cell(window, 8) == "47.000"
        assert "DRV_ERROR" in cell(window, 12)  # Recovery did not clear device error 7.
    finally:
        backend.close_transport()


def test_even_a_recent_cached_sample_is_not_shown_as_live_when_reading_failed(window, monkeypatch):
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: 100.)
    data = snapshot()
    data["feedback_read_error"] = "OSError: injected query failure"
    window._render_snapshot(data)
    assert cell(window, 8) == "—"
    assert "读取失败" in cell(window, 12)


def test_running_snapshot_ages_playback_cache_instead_of_restamping_it(monkeypatch):
    backend = FakeMotorBackend(1)
    controller = FactoryController(StationConfig(limb="left_arm", entries=backend.entries, raw={}), backend,
                                   enable_watchdog=False)
    observed = MotorSample(.1, temp_c=46., timestamp_s=100., feedback_age_s=0.)
    controller._playback = SimpleNamespace(last_samples=lambda: [observed], snapshot=lambda: {"progress": {}})
    controller.state_machine = StationStateMachine(StationState.RUNNING)
    monkeypatch.setattr("factory_hmi.core.controller.time.monotonic", lambda: 110.)
    result = controller.snapshot()
    assert result["motors"][0]["feedback_age_s"] == 10.
    assert result["motors"][0]["timestamp_s"] == 100.


def test_local_silence_expires_values_and_local_rerender_does_not_rejuvenate_them(window, monkeypatch):
    clock = [100.]
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    window._render_snapshot(snapshot(error=7))
    assert cell(window, 8) == "46.000"
    clock[0] = 100.31
    window._refresh_feedback_freshness()
    assert cell(window, 4) == cell(window, 8) == "—"
    assert cell(window, 11) == "7"
    assert "数据已过期" in cell(window, 12)
    clock[0] = 101.
    window._render_snapshot(dict(window._last_snapshot))
    assert window._telemetry_received_at == 100.
    assert cell(window, 13) == "1.000"
    assert window._last_snapshot["motors"][0]["feedback_age_s"] == 0.


def test_server_and_motor_time_difference_exposes_bad_reported_zero_age(window, monkeypatch):
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: 999999.)
    window._render_snapshot(snapshot(110., motor_stamp=100., age=0.))
    assert cell(window, 8) == "—"
    assert cell(window, 13) == "10.000"
    assert cell(window, 12) == "通信反馈超时"


def test_local_expiry_does_not_replay_old_login_or_control_state(window, monkeypatch):
    clock = [100.]
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    window._render_snapshot(snapshot())
    side_effects = []
    for method in ("_render_platform_presence", "_sync_state", "_render_fault", "_render_aging_status", "_render_manual_status"):
        monkeypatch.setattr(window, method, lambda *args, name=method: side_effects.append(name))
    window._platform_operator = "newer-local-state"
    window._motors_enabled = False
    clock[0] = 101.
    window._refresh_feedback_freshness()
    assert side_effects == []
    assert window._platform_operator == "newer-local-state"
    assert window._motors_enabled is False
    assert cell(window, 8) == "—"


def test_local_expiry_repaints_once_per_motor_observation(window, monkeypatch):
    clock = [100.]
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    renders = []
    render = window._render_snapshot

    def observe(payload, *, refresh_state=True):
        if not refresh_state:
            renders.append(clock[0])
        render(payload, refresh_state=refresh_state)

    monkeypatch.setattr(window, "_render_snapshot", observe)
    for count, temperature in enumerate((46., 47.), start=1):
        window._render_snapshot(snapshot(clock[0], temperature=temperature))
        assert cell(window, 8) == f"{temperature:.3f}"
        clock[0] += DISPLAY_STALE_S + .01
        for _ in range(8):
            window._refresh_feedback_freshness()
            clock[0] += .1
        assert cell(window, 8) == "—"
        assert len(renders) == count


def test_silent_connected_websocket_uses_one_pending_fallback_and_recovers(window, monkeypatch):
    clock = [100.]
    calls = []
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(window, "_run_rest", lambda *args, **kwargs: calls.append((args, kwargs)))
    window._ws_connected = True
    window._ws_message(json.dumps({"type": "snapshot", "snapshot": snapshot()}))
    clock[0] = 100.5
    window._poll_snapshot()
    assert calls == []
    clock[0] = 101.1
    window._poll_snapshot()
    window._poll_snapshot()
    assert len(calls) == 1 and window._snapshot_pending
    assert "静默" in window.connection_badge.text()
    calls[0][1]["on_finished"]()
    assert not window._snapshot_pending
    clock[0] = 102.
    window._ws_message(json.dumps({"type": "snapshot", "snapshot": snapshot(102., temperature=47.)}))
    assert cell(window, 8) == "47.000"
    assert cell(window, 12) == "正常"
    assert "已连接" in window.connection_badge.text()
    window._poll_snapshot()
    assert len(calls) == 1


def test_repeated_identical_status_keeps_transport_live_without_renewing_motor_feedback(window, monkeypatch):
    clock = [100.]
    calls = []
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(window, "_run_rest", lambda *args, **kwargs: calls.append(args))
    window._ws_connected = True
    message = json.dumps({"type": "snapshot", "snapshot": snapshot()})
    window._ws_message(message)
    clock[0] = 102.
    window._ws_message(message)
    assert window._telemetry_received_at == 100.
    assert cell(window, 8) == "—"
    window._poll_snapshot()
    assert calls == []
    clock[0] += STATUS_SILENCE_S + .01
    window._poll_snapshot()
    assert len(calls) == 1


def test_constant_temperature_with_advancing_feedback_remains_valid(window, monkeypatch):
    clock = [100.]
    monkeypatch.setattr("factory_hmi.desktop.app.time.monotonic", lambda: clock[0])
    window._render_snapshot(snapshot())
    clock[0] = 102.
    data = snapshot(102.)
    data["motors"][0]["position_rad"] = .2
    window._render_snapshot(data)
    assert cell(window, 8) == "46.000"
    assert cell(window, 12) == "正常"


@pytest.mark.parametrize("motor", [{}, {"feedback_age_s": None}, {"feedback_age_s": -1.}, {"feedback_age_s": math.nan}])
def test_unknown_or_invalid_reported_age_is_not_treated_as_zero(motor):
    assert math.isinf(feedback_age_s(motor, None, 0.))
