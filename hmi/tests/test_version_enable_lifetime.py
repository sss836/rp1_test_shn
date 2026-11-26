"""Version queries are driven by connection/operator actions, never by an enabled motor."""

from threading import Event
import time

import pytest

from factory_hmi.core.backend import MotorProtocolPending, MotorSafetyError
from factory_hmi.core.motor_versions import MotorVersions
from test_lro_runtime_profiles import open_backend
from test_motor_versions import entry
from test_torque_estimation import ActualKtMotor


def test_idle_and_enabled_reader_have_no_periodic_wakeup():
    motor = ActualKtMotor(308)
    observed = Event()
    reader = MotorVersions([entry()], [motor], refresh=0.01, on_observation=lambda *_: observed.set())
    reader.start()
    try:
        assert observed.wait(1)
        initial = motor.queries.copy()
        observed.clear()
        assert not observed.wait(0.06)
        assert reader.pause_for_enable()
        with pytest.raises(ValueError, match="使能"):
            reader.request_refresh()
        assert not observed.wait(0.06)
        reader.resume_after_disable()
        assert not observed.wait(0.06)
        assert motor.queries == initial == [0x31, 0x1E, 0x16]
        reader.request_refresh()
        assert observed.wait(1)
        assert motor.queries == initial * 2
    finally:
        reader.close()


def test_bound_kt_and_identity_last_one_enable_only(open_backend, monkeypatch):
    motor = ActualKtMotor(308)
    backend = open_backend((motor, None))
    backend.enable_motors()
    with monkeypatch.context() as mp:
        real_time = time.monotonic
        mp.setattr(time, "monotonic", lambda: real_time() + 600)
        backend._check_protocol_conflicts()
        sample = backend.read_samples()[0]
        assert sample.torque_nm == pytest.approx(30.8) and sample.torque_is_estimated
        assert backend.motor_versions()[0]["bound_to_enable"]
        with pytest.raises(ValueError, match="失能"):
            backend.request_motor_version_refresh()
        assert motor.queries == [0x31, 0x1E, 0x16]
        backend.disable_motors()
        assert not backend.motor_versions()[0]["bound_to_enable"]
        assert backend.read_samples()[0].torque_source == "Kt读回已过期"
        motor.kt = 231
        with pytest.raises(MotorProtocolPending, match="本次未使能"):
            backend.enable_motors()
        assert motor.enables == 1
        deadline = real_time() + 2
        while backend.motor_protocols_pending() and real_time() < deadline:
            time.sleep(0.005)
        backend.enable_motors()
        assert motor.enables == 2 and backend.read_samples()[0].torque_nm == pytest.approx(23.1)
        backend.disable_motors()


def test_unknown_kt_prevents_enable_and_identity_conflict_stops_bound_motor(open_backend):
    motor = ActualKtMotor(0)
    backend = open_backend((motor, None))
    with pytest.raises(MotorSafetyError, match="Kt 未确认"):
        backend.enable_motors()
    assert motor.enables == 0 and backend._enable_evidence is None
    motor.kt = 308
    backend.request_motor_version_refresh()
    deadline = time.monotonic() + 2
    while backend.motor_protocols_pending() and time.monotonic() < deadline:
        time.sleep(0.005)
    backend.enable_motors()
    identity = backend._protocol_observations[0]
    backend._observe_motor_protocol(0, {**identity, "hardware": "0.9.9"})
    with pytest.raises(MotorSafetyError, match="身份或 Kt 不符"):
        backend._check_protocol_conflicts()
    backend.disable_motors()


def test_busy_manual_refresh_cannot_overlap_enable(open_backend):
    motor = ActualKtMotor(308)
    backend = open_backend((motor, None))
    backend.prepare_motor_protocols()
    block = Event()
    entered = Event()
    original = motor.version_payload

    def payload(code):
        entered.set()
        assert block.wait(1)
        return original(code)

    motor.version_payload = payload
    backend.request_motor_version_refresh()
    try:
        assert entered.wait(1)
        with pytest.raises(MotorProtocolPending):
            backend.enable_motors()
        assert not motor.enabled and not motor.enables
    finally:
        block.set()
