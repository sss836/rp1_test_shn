"""Synthetic drive replies only: never open CAN/PLC or enable real hardware."""
from dataclasses import replace
import time
from types import SimpleNamespace

import pytest

from factory_hmi.core.backend import FakeMotorBackend, MotorsPyBackend, MotorSafetyError


class Drive:
    def __init__(self, *, profile="v1.03", drops=0, never=False, delay=0., replies=True,
                 cached_false=False, fail_send=False, fail_feedback=False):
        self.profile, self.drops, self.never, self.delay = profile, drops, never, delay
        self.replies, self.fail_send, self.fail_feedback = replies, fail_send, fail_feedback
        self.enabled = not cached_false
        self.count = 7
        self.disables, self.enables, self.refreshes = 0, 0, 0
        self.disabled_after = None

    def init_motor(self):
        self.enables += 1
        pytest.fail("disable confirmation attempted enable")

    def get_lro_protocol_profile(self):
        return self.profile

    def deinit_motor(self):
        self.disables += 1
        if self.fail_send:
            raise RuntimeError("synthetic send failed")
        if not self.never and self.disables > self.drops and self.disabled_after is None:
            self.disabled_after = time.monotonic()+self.delay

    unlock_motor = deinit_motor

    def refresh_motor_status(self):
        self.refreshes += 1
        if self.fail_feedback:
            raise RuntimeError("synthetic query failed")
        if self.replies:
            self.count += 1
            if self.disabled_after is not None and time.monotonic() >= self.disabled_after:
                self.enabled = False

    def get_feedback_count(self):
        return self.count

    def get_motor_drive_enabled(self):
        return self.enabled if self.profile == "v1.03" else None


def backend(*motors, enabled=True):
    entries = tuple(replace(e, motor_type="LRO", motor_model=0, interface="offline-disable")
                    for e in FakeMotorBackend(len(motors)).entries)
    b = MotorsPyBackend(entries)
    b._motors, b._connected, b._enabled = list(motors), True, enabled
    b._selected_protocols = [m.profile for m in motors]
    b._DISABLE_CONFIRM_TIMEOUT_S = .09
    b._DISABLE_RETRY_INTERVAL_S = .015
    return b


def test_seven_drives_retry_only_last_three_and_confirm_actual_replies():
    motors = [Drive(drops=0 if i<4 else 1) for i in range(7)]
    b = backend(*motors)
    b.disable_motors()
    assert not b.motors_enabled and b.commands_inhibited
    assert [m.disables for m in motors] == [1,1,1,1,2,2,2]
    assert all(not m.enabled and not m.enables for m in motors)
    status = b.disable_status()
    assert b.disable_feedback() == status["state"] == "confirmed"
    assert all(row["feedback_count"] > row["request_feedback_count"] for row in status["motors"])
    assert all(row["feedback_age_s"] <= .1 for row in status["motors"])


def test_never_disabled_is_bounded_for_entire_module_and_does_not_claim_disabled():
    b = backend(*(Drive(never=True) for _ in range(7)))
    start = time.monotonic()
    with pytest.raises(MotorSafetyError, match="offline-disable#7"):
        b.disable_motors()
    assert time.monotonic()-start < .3  # A shared 90ms window, not 7*90ms.
    assert b.motors_enabled and b.commands_inhibited
    status = b.disable_status()
    assert status["state"] == "unconfirmed"
    assert all(r["attempts"] == 3 and r["drive_enabled"] is True for r in status["motors"])


def test_late_fresh_disabled_feedback_confirms_without_reenabling():
    motor = Drive(delay=.035)
    b = backend(motor)
    b.disable_motors()
    assert not b.motors_enabled and not motor.enabled and motor.enables == 0
    assert b.disable_feedback() == "confirmed"


def test_cached_false_without_new_feedback_never_confirms():
    b = backend(Drive(cached_false=True, replies=False))
    with pytest.raises(MotorSafetyError, match="失能反馈未确认"):
        b.disable_motors()
    row = b.disable_status()["motors"][0]
    assert row["drive_enabled"] is False
    assert row["feedback_count"] == row["request_feedback_count"]
    assert row["state"] == "unconfirmed" and b.commands_inhibited


def test_mixed_108_109_keeps_legacy_unknown_and_each_109_confirmed():
    old, new = Drive(profile="legacy"), Drive()
    b = backend(old, new)
    b.disable_motors()
    assert not b.motors_enabled
    status = b.disable_status()
    assert status["state"] == b.disable_feedback() == "unknown"
    assert [r["state"] for r in status["motors"]] == ["unknown", "confirmed"]
    assert old.refreshes == 0 and old.disables == 1 and new.refreshes > 0


def test_repeated_disable_reaches_drives_despite_software_flag_false():
    motor = Drive()
    b = backend(motor, enabled=False)
    b.disable_motors()
    assert motor.disables == 1 and b.disable_feedback() == "confirmed"
    motor.enabled = True
    b.disable_motors()
    assert motor.disables == 2 and not motor.enabled


def test_second_stop_can_recover_unconfirmed_subset():
    good, bad = Drive(), Drive(never=True)
    b = backend(good, bad)
    with pytest.raises(MotorSafetyError):
        b.disable_motors()
    assert good.enabled is False and bad.enabled is True
    bad.never = False
    b.disable_motors()
    assert b.disable_feedback() == "confirmed" and not b.motors_enabled


@pytest.mark.parametrize("failure", ["fail_send", "fail_feedback"])
def test_partial_driver_exception_does_not_skip_other_motors(failure):
    bad, good = Drive(**{failure:True}), Drive()
    b = backend(bad, good)
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        b.disable_motors()
    assert good.disables == 1 and good.enabled is False
    assert b.disable_status()["motors"][1]["state"] == "confirmed"
    assert b.motors_enabled and b.commands_inhibited


def test_single_motor_stop_preserves_selected_scope_and_other_drives():
    first, second = Drive(), Drive()
    b = backend(first,second)
    b.disable(1)
    assert first.enabled and not second.enabled
    assert b.motors_enabled
    status = b.disable_status()
    assert status["scope"] == "selected" and status["requested_indices"] == [1]


def test_disable_status_snapshot_cannot_mutate_backend_result():
    b = backend(Drive())
    b.disable_motors()
    status = b.disable_status()
    status["motors"][0]["state"] = "fake"
    assert b.disable_status()["motors"][0]["state"] == "confirmed"


def test_confirmed_stop_reused_after_power_off_preserves_original_acknowledgment():
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    previous = b.disable_status()
    previous_io = motor.disables, motor.refreshes
    motor.replies = False  # Operator powers off after the confirmed stop.
    b.disable_motors()
    status = b.disable_status()
    assert not b.motors_enabled and status["state"] == "confirmed"
    assert status["reused_confirmation"] is True
    assert (motor.disables, motor.refreshes) == previous_io
    for field in ("started_at_monotonic_s", "finished_at_monotonic_s", "elapsed_s"):
        assert status[field] == previous[field]
    assert status["motors"][0]["observed_at_monotonic_s"] == previous["motors"][0]["observed_at_monotonic_s"]
    assert status["reused_at_monotonic_s"] >= status["finished_at_monotonic_s"]


def test_new_enabled_feedback_invalidates_confirmed_stop_reuse():
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    motor.enabled, motor.disabled_after = True, None
    motor.count += 1
    b.disable_motors()
    status = b.disable_status()
    assert motor.disables == 2 and motor.enabled is False
    assert status["state"] == "confirmed" and status["reused_confirmation"] is False


def test_confirmed_stop_not_reused_for_replaced_driver_handles():
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    replacement = Drive()
    b._motors = [replacement]
    b.disable_motors()
    assert replacement.disables == 1 and replacement.enabled is False
    assert b.disable_status()["reused_confirmation"] is False


def test_enable_clears_prior_stop_confirmation(monkeypatch):
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    monkeypatch.setattr(b, "prepare_motor_protocols", lambda: None)
    monkeypatch.setattr(b, "_require_disabled_v103", lambda: None)
    monkeypatch.setattr(b, "_confirm_enabled_v103", lambda baseline, generation: None)
    b._motors_py = SimpleNamespace(MotorControlMode=SimpleNamespace(MIT="synthetic-mit"))

    def synthetic_enable():
        motor.enables += 1
        motor.enabled, motor.disabled_after = True, None
        return 0

    monkeypatch.setattr(motor, "init_motor", synthetic_enable)
    monkeypatch.setattr(motor, "set_motor_control_mode", lambda mode: None, raising=False)
    b.enable_motors()
    assert b.motors_enabled and b.disable_feedback() == "unknown"
    assert b._disable_confirmation_key is None
    assert b._host_enable_attempt_epoch == b._enable_open_epoch
    b.disable_motors()
    assert motor.disables == 2 and motor.enabled is False
    assert b.disable_feedback() == "confirmed"
    assert b.disable_status()["reused_confirmation"] is False


def test_enable_rollback_failure_keeps_possible_enabled_state_and_inhibit(monkeypatch):
    motor = Drive(never=True)
    b = backend(motor, enabled=False)
    monkeypatch.setattr(b, "prepare_motor_protocols", lambda: None)

    def failed_partial_enable():
        motor.enabled = True
        raise RuntimeError("synthetic partial enable failed")

    monkeypatch.setattr(b, "_enable_motors_impl", failed_partial_enable)
    with pytest.raises(RuntimeError, match="partial enable failed"):
        b.enable_motors()
    assert b.motors_enabled and b.commands_inhibited
    assert "offline-disable#1" in b.control_stop_error
    assert b.disable_status()["state"] == "unconfirmed"


def test_send_failure_is_not_hidden_by_fresh_false_feedback():
    motor = Drive(cached_false=True, fail_send=True)
    b = backend(motor)
    with pytest.raises(MotorSafetyError, match="synthetic send failed"):
        b.disable_motors()
    row = b.disable_status()["motors"][0]
    assert row["feedback_count"] > row["request_feedback_count"]
    assert row["drive_enabled"] is False and row["state"] == "unconfirmed"
    assert b.motors_enabled and b.commands_inhibited


@pytest.mark.parametrize("operation", ["close", "maintenance"])
def test_release_cannot_trust_software_false_when_drive_stays_enabled(operation):
    motor = Drive(never=True)
    b = backend(motor, enabled=False)
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        if operation == "close":
            b.close_transport()
        else:
            with b.maintenance_transport():
                pytest.fail("enabled drive handed to maintenance")
    assert b.connected and b.motors == [motor] and motor.enabled
    assert b.motors_enabled and b.commands_inhibited
    assert not getattr(b, "_maintenance_active", False)


@pytest.mark.parametrize("operation", ["close", "maintenance"])
def test_release_reuses_stop_after_power_off_despite_stop_and_cancel_generations(operation):
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    previous = b.disable_status()
    previous_refreshes = motor.refreshes
    motor.replies = False
    b.cancel_protective_damping()
    b._revoke_damping()
    b.disable_motors()
    b.cancel_protective_damping()
    if operation == "close":
        b.close_transport()
    else:
        with b.maintenance_transport():
            assert not b.connected
    assert not b.connected and not b.motors and not b.motors_enabled
    status = b.disable_status()
    assert status["state"] == "confirmed" and status["reused_confirmation"] is True
    assert status["finished_at_monotonic_s"] == previous["finished_at_monotonic_s"]
    assert status["confirmation_enable_open_epoch"] == previous["confirmation_enable_open_epoch"]
    assert status["control_generation"] > previous["control_generation"]
    assert motor.refreshes == previous_refreshes


@pytest.mark.parametrize("operation", ["close", "maintenance"])
def test_release_rechecks_new_enabled_feedback_instead_of_historical_stop(operation):
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    motor.enabled, motor.never, motor.disabled_after = True, True, None
    motor.count += 1
    b.cancel_protective_damping()
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        if operation == "close":
            b.close_transport()
        else:
            with b.maintenance_transport():
                pytest.fail("fresh enabled feedback ignored")
    assert b.connected and b.motors == [motor] and motor.enabled
    assert b.motors_enabled and b.commands_inhibited
    assert b.disable_status()["reused_confirmation"] is False


@pytest.mark.parametrize("operation", ["close", "maintenance"])
def test_release_does_not_bypass_failed_enable_rollback_with_software_false(operation):
    motor = Drive(never=True)
    b = backend(motor)
    with pytest.raises(MotorSafetyError):
        b.disable_motors()
    b._enabled = False  # Model a stale upper layer or older package flag.
    motor.replies = False
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        if operation == "close":
            b.close_transport()
        else:
            with b.maintenance_transport():
                pytest.fail("unconfirmed prior stop ignored")
    assert b.connected and b.motors == [motor] and b.motors_enabled


def test_mixed_legacy_stop_stays_unknown_when_reused_after_cancel():
    old, new = Drive(profile="legacy"), Drive()
    b = backend(old, new)
    b.disable_motors()
    old.replies = new.replies = False
    b.cancel_protective_damping()
    b._revoke_damping()
    b.disable_motors()
    status = b.disable_status()
    assert status["state"] == "unknown" and status["reused_confirmation"] is True
    assert [row["state"] for row in status["motors"]] == ["unknown", "confirmed"]


def test_never_enabled_read_only_close_does_not_require_new_disabled_frames():
    motor = Drive(cached_false=True, replies=False)
    b = backend(motor, enabled=False)
    b.close_transport()
    assert not b.connected and not b.motors and not b.motors_enabled
    assert motor.enables == motor.refreshes == 0
    status = b.disable_status()
    assert status["state"] == b.disable_feedback() == "unknown"
    assert status["read_only_release"] is True
    assert status["host_enable_attempt_epoch"] is None
    assert status["release_reason"] == "read_only_transport"
    assert status["released_at_monotonic_s"] >= status["release_requested_at_monotonic_s"]


def test_never_enabled_maintenance_still_requires_fresh_disabled_reply():
    motor = Drive(cached_false=True, replies=False)
    b = backend(motor, enabled=False)
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        with b.maintenance_transport():
            pytest.fail("maintenance accepted cached false without fresh acknowledgment")
    assert b.connected and b.motors == [motor] and b.commands_inhibited
    assert b.disable_status()["state"] == "unconfirmed"
    assert not b.disable_status().get("read_only_release", False)


def test_host_enable_attempt_disallows_read_only_close_even_with_software_and_cached_false():
    motor = Drive(cached_false=True, replies=False)
    b = backend(motor, enabled=False)
    b._host_enable_attempt_epoch = b._enable_open_epoch
    with pytest.raises(MotorSafetyError, match="offline-disable#1"):
        b.close_transport()
    assert b.connected and b.motors == [motor] and b.motors_enabled
    assert b.disable_status()["host_enable_attempt_epoch"] == b._enable_open_epoch


@pytest.mark.parametrize("fail_send", [False, True])
def test_trusted_site_isolated_release_preserves_unconfirmed_stop_evidence(fail_send):
    motor = Drive(never=True, replies=False, fail_send=fail_send)
    b = backend(motor)
    b.disconnect_site_isolated()
    assert not b.connected and not b.motors and not b.motors_enabled
    assert b.commands_inhibited and motor.enables == 0 and motor.disables > 0
    status = b.disable_status()
    assert status["state"] == b.disable_feedback() == "unconfirmed"
    assert status["site_isolated_release"] is True
    assert status["site_isolated_release_requested"] is True
    assert "offline-disable#1" in status["release_disable_error"]
    assert status["release_reason"] == "site_isolated"
    assert status["released_at_monotonic_s"] >= status["finished_at_monotonic_s"]
    assert bool(status["transport_release_error"]) is fail_send
    assert all(row["state"] != "confirmed" for row in status["motors"])


def test_isolated_release_does_not_discard_handles_owned_by_a_live_query_thread():
    motor = Drive(never=True, replies=False)
    b = backend(motor)

    def blocked_query_owner():
        raise RuntimeError("synthetic versions thread still running")

    b._versions = SimpleNamespace(close=blocked_query_owner)
    with pytest.raises(RuntimeError, match="versions thread still running"):
        b.disconnect_site_isolated()
    assert b.connected and b.motors == [motor] and b.commands_inhibited
    status = b.disable_status()
    assert status["state"] == "unconfirmed" and status["site_isolated_release"] is False
    assert status["site_isolated_release_requested"] is True
    assert "versions thread" in status["transport_release_error"]
    assert "released_at_monotonic_s" not in status


def test_site_isolated_release_retains_a_real_prior_confirmation():
    motor = Drive()
    b = backend(motor)
    b.disable_motors()
    acknowledged = b.disable_status()["finished_at_monotonic_s"]
    motor.replies = False
    b.cancel_protective_damping()
    b.disconnect_site_isolated()
    status = b.disable_status()
    assert status["state"] == "confirmed" and status["reused_confirmation"] is True
    assert status["finished_at_monotonic_s"] == acknowledged
    assert status["site_isolated_release"] is True and not b.connected


@pytest.mark.parametrize("failed, expected", [(False, "sent"), (True, "failed")])
def test_alarm_distinguishes_disable_send_failure_from_unconfirmed_reply(failed, expected):
    from factory_hmi.core.fault_response import request_fault_disable
    b = backend(Drive(fail_send=failed, replies=False))
    response = request_fault_disable(b)
    assert response["disable_request"] == expected
    assert response["disable_feedback"] == "unconfirmed" and not response["safe_confirmed"]
    assert "offline-disable#1" in response["error"] and b.commands_inhibited
