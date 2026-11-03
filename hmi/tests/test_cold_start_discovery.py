"""Cold-start readiness checks with a virtual clock and fake devices only."""
from __future__ import annotations

from dataclasses import replace
import concurrent.futures
import math
import threading
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from factory_hmi import FactoryController, StationConfig, StationState
from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.discovery import MotorDiscoveryPolicy
from factory_hmi.gateway.app import create_app


class Clock:
    value = 100.0

    def now(self):
        return self.value

    def wait(self, seconds):
        self.value += seconds
        return False


class StartupBackend(FakeMotorBackend):
    def __init__(self, ready_after, *, entries=None):
        super().__init__(len(ready_after) if entries is None else entries)
        self.ready_after = list(ready_after)
        self.stop_after = [math.inf] * len(ready_after)
        self.only_once = set()
        self.seen = set()
        self.events = []
        self.first_query = threading.Event()

    def _open_transport_impl(self):
        super()._open_transport_impl()
        self.started = time.monotonic()
        self.feedback_timestamps[:] = 0.
        self.events.append("open")

    def _refresh_feedback_impl(self):
        self.first_query.set()
        elapsed = time.monotonic() - self.started
        for i, ready in enumerate(self.ready_after):
            self.stale[i] = elapsed < ready or elapsed >= self.stop_after[i] or (i in self.only_once and i in self.seen)
            if not self.stale[i]:
                self.seen.add(i)
        self.events.append("query")
        super()._refresh_feedback_impl()

    def _enable_motors_impl(self):
        self.events.append("enable")
        super()._enable_motors_impl()

    def _clear_errors_impl(self, index):
        self.events.append("clear")
        super()._clear_errors_impl(index)


def controller_for(backend, *, timeout=5., stale=2.):
    config = StationConfig(
        limb="left_arm", entries=backend.entries,
        raw={"factory_hmi": {"discovery": {"timeout_s": timeout}},
             "factory_hmi_faults": {"stale_seconds": stale}},
    )
    return FactoryController(config, backend, enable_watchdog=False)


def virtual_clock(monkeypatch, controller):
    clock = Clock()
    monkeypatch.setattr("factory_hmi.core.controller.time.monotonic", clock.now)
    monkeypatch.setattr(controller._default_return_stop, "wait", clock.wait)
    return clock


def test_motor_ready_after_one_second_can_be_discovered_without_enable_or_clear(monkeypatch):
    backend = StartupBackend([0., 2.2])
    controller = controller_for(backend)
    clock = virtual_clock(monkeypatch, controller)
    try:
        controller.discover_motors()
        assert 2.2 < clock.value - 100. < 2.5
        assert controller.state is StationState.CONNECTED
        assert controller.snapshot()["station"]["motors_discovered"] is True
        assert backend.events.count("open") == 1
        assert not backend.motors_enabled
        assert "enable" not in backend.events and "clear" not in backend.events
        assert not backend.command_history
    finally:
        controller.disconnect()


def test_missing_motor_times_out_with_bus_joint_and_confirmation_count(monkeypatch):
    template = FakeMotorBackend(2).entries
    entries = (replace(template[0], interface="can0", motor_id=3),
               replace(template[1], interface="can3", motor_id=3, joint_name="left_shoulder_yaw_joint"))
    backend = StartupBackend([0., math.inf], entries=entries)
    controller = controller_for(backend)
    clock = virtual_clock(monkeypatch, controller)
    try:
        with pytest.raises(RuntimeError, match="fresh feedback timeout") as failure:
            controller.discover_motors()
        assert clock.value - 100. == pytest.approx(5.)
        assert "can3#3[left_shoulder_yaw_joint](0/2,age_s=none)" in str(failure.value)
        assert "can0#3" not in str(failure.value)
        assert controller.state is StationState.FAULT
        assert controller.snapshot()["station"]["backend_connected"] is True
        assert controller.snapshot()["station"]["motors_discovered"] is False
        assert not backend.motors_enabled
        assert "enable" not in backend.events and "clear" not in backend.events
    finally:
        controller.disconnect()


def test_single_reply_followed_by_cached_values_cannot_pass(monkeypatch):
    backend = StartupBackend([1.2])
    backend.only_once.add(0)
    controller = controller_for(backend)
    virtual_clock(monkeypatch, controller)
    try:
        with pytest.raises(RuntimeError, match="fresh feedback timeout"):
            controller.discover_motors()
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


def test_early_replies_that_expire_while_other_motor_boots_cannot_pass(monkeypatch):
    backend = StartupBackend([0., 3.])
    backend.stop_after[0] = .15
    controller = controller_for(backend)
    virtual_clock(monkeypatch, controller)
    try:
        with pytest.raises(RuntimeError, match="fresh feedback timeout") as failure:
            controller.discover_motors()
        assert "motor_ids=1;" in str(failure.value)
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


def test_enable_confirmation_keeps_one_second_budget_after_cold_scan(monkeypatch):
    backend = StartupBackend([0.])
    controller = controller_for(backend)
    clock = virtual_clock(monkeypatch, controller)
    try:
        controller.discover_motors()
        backend.ready_after[:] = [math.inf]
        started = clock.value
        with pytest.raises(RuntimeError, match="before motor enable: fresh feedback timeout"):
            controller.enable_motors()
        assert clock.value - started == pytest.approx(1.)
        assert "enable" not in backend.events
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


def test_fresh_error_seven_is_visible_and_still_blocks_enable(monkeypatch):
    backend = StartupBackend([0., 0.])
    backend.inject_error(1, 7)
    controller = controller_for(backend)
    virtual_clock(monkeypatch, controller)
    try:
        controller.discover_motors()
        assert controller._last_samples[1].error_id == 7
        with pytest.raises(RuntimeError, match="before motor enable: motor error: bus=.*id=2:error_id=7"):
            controller.enable_motors()
        assert "enable" not in backend.events and "clear" not in backend.events
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


@pytest.mark.parametrize("value", [0, -1, 10.1, float("nan"), float("inf"), True, "bad"])
def test_invalid_startup_budget_rejected_before_any_device_is_opened(value):
    backend = StartupBackend([0.])
    with pytest.raises(ValueError, match="discovery.timeout_s"):
        controller_for(backend, timeout=value)
    assert not backend.events


def test_policy_defaults_and_configured_budget_are_separate_from_stale_limit():
    assert MotorDiscoveryPolicy.from_config({}).timeout_s == 5.
    assert MotorDiscoveryPolicy.from_config({"factory_hmi": {"discovery": {"timeout_s": 3.}}}).timeout_s == 3.
    backend = StartupBackend([0.])
    controller = controller_for(backend, stale=.5)
    assert controller._discovery_policy.timeout_s == 5.
    assert controller._diagnosis.thresholds.stale_seconds == .5


def test_gateway_disable_interrupts_cold_scan_without_enabling(tmp_path):
    backend = StartupBackend([math.inf])
    controller = controller_for(backend)
    try:
        with TestClient(create_app(controller, data_root=tmp_path, lease_timeout_s=30.)) as client:
            headers = {"X-RP1-Client-ID": "offline-scan-operator"}
            with concurrent.futures.ThreadPoolExecutor() as pool:
                scanning = pool.submit(client.post, "/api/v1/motors/discover", headers=headers)
                assert backend.first_query.wait(1.)
                runtime = client.app.state.runtime.session("offline-scan-operator")
                heartbeat_headers = {**headers, "X-RP1-Lease-Generation": str(runtime.lease_snapshot()["generation"])}
                heartbeat = pool.submit(client.post, "/api/v1/lease/heartbeat", headers=heartbeat_headers)
                assert heartbeat.result(timeout=1.).status_code == 200
                assert not scanning.done()
                stopped = client.post("/api/v1/motors/disable", headers=headers)
                assert stopped.status_code == 200, stopped.text
                assert stopped.json()["snapshot"]["connected"] is False
                result = scanning.result(timeout=1.)
                assert result.status_code == 409
                assert "interrupted by stop request" in result.json()["detail"]
        assert "enable" not in backend.events and "clear" not in backend.events
        assert not backend.command_history
    finally:
        controller.disconnect()
