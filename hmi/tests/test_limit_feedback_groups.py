"""No sockets or real motor handles: per-motor freshness, not group deadlines."""
from dataclasses import replace
import math
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.models import MotorSample
from factory_hmi.core.arm_limit_defaults import builtin_arm_profile
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import ProbePoint, ProbeWorker


class Clock:
    now = 1.
    reads = 0


class Event:
    def __init__(self, clock, stop_after):
        self.clock, self.stop_after, self.stopped = clock, stop_after, False

    def is_set(self):
        return self.stopped or self.clock.reads >= self.stop_after

    def wait(self, seconds):
        self.clock.now += seconds
        return self.is_set()

    def set(self):
        self.stopped = True


class Backend:
    def __init__(self, coords):
        self.positions = np.zeros(len(coords.names))
        self.positions[0] = math.radians(20.)
        self.commands, self.disabled = [], False

    def command_mit(self, target, **gains):
        self.commands.append(target.copy())
        self.positions = target.copy()

    def disable_motors(self):
        self.disabled = True

    def disable_feedback(self):
        return "confirmed_mock"


def exercise(monkeypatch, mode, *, stop_after=9):
    from factory_hmi.core import live_limit_probe
    coords = Coordinates(nominal_station("left_arm"))
    point = builtin_arm_profile(coords.station).windows[0].point
    backend, clock = Backend(coords), Clock()
    frozen = None

    def read():
        nonlocal frozen
        clock.reads += 1
        if mode == "gap" and clock.reads == 2:
            clock.now += .16
        rows = [MotorSample(pos_rad=float(p), torque_nm=.3, timestamp_s=clock.now-.09*i/6,
                            feedback_age_s=.09*i/6, bus=e.interface, motor_id=e.motor_id,
                            joint_name=e.joint_name)
                for i, (p,e) in enumerate(zip(backend.positions, coords.entries))]
        if frozen is None:
            frozen = rows
        if mode.startswith("repeated") and clock.reads > 1:
            rows = [replace(r, feedback_age_s=clock.now-r.timestamp_s) for r in frozen]
            if mode == "repeated_fault":
                rows[-1] = replace(rows[-1], error_id=3)
            if mode == "repeated_torque":
                rows[-1] = replace(rows[-1], torque_nm=point.motor_abort_nm[-1])
        if mode == "expired":
            rows[-1] = replace(rows[-1], timestamp_s=clock.now-.151, feedback_age_s=.151)
        return rows

    monkeypatch.setattr(live_limit_probe.time, "monotonic", lambda: clock.now)
    worker = ProbeWorker(backend, coords, point, {}, read, lambda *_: None, lambda *_: None)
    worker.stop_event = Event(clock, stop_after)
    worker._run()
    return worker, backend, clock


def test_group_skew_above_35_ms_does_not_block_fresh_motor_motion(monkeypatch):
    worker, backend, _ = exercise(monkeypatch, "skew")
    assert len(backend.commands) >= 6
    assert backend.commands[-1][0] < backend.commands[0][0]
    assert np.max(abs(np.rad2deg(np.diff(backend.commands, axis=0)))) <= .5+1e-7
    report = worker.snapshot(include_samples=True)
    assert report["feedback_diagnostics"]["max_group_skew_ms"] == pytest.approx(90.)
    assert report["feedback_diagnostics"]["accepted_groups"] >= 6
    assert report["last_rejected_feedback_group"] is None
    assert backend.disabled


def test_received_load_check_uses_active_window_and_transfer_limits_by_phase():
    coords = Coordinates(nominal_station("left_arm"))
    point = builtin_arm_profile(coords.station).windows[0].point
    worker = ProbeWorker(Backend(coords), coords, point, {}, lambda: [], lambda *_: None, lambda *_: None)
    worker.cycle = SimpleNamespace(point=point, transfer_joint=np.ones(7)*5.,
                                   transfer_motor=np.ones(7)*5., probe=object())
    worker._check_received_loads(np.ones(7)*6., np.ones(7)*6.)
    worker.cycle.probe = None
    with pytest.raises(FeedbackError, match="中止门限"):
        worker._check_received_loads(np.ones(7)*6., np.ones(7)*6.)


def test_expired_motor_frame_stops_and_preserves_actual_rejected_group(monkeypatch):
    worker, backend, _ = exercise(monkeypatch, "expired")
    assert not backend.commands and backend.disabled
    report = worker.snapshot(include_samples=True)
    assert "超时" in report["fault"]
    rejected = report["last_rejected_feedback_group"]
    assert rejected["samples"][-1]["motor_id"] == 7
    assert rejected["samples"][-1]["feedback_age_s"] == .151
    assert rejected["received"] and rejected["skew_ms"] >= 150.


def test_repeated_frames_never_advance_and_expiry_budget_is_not_extended(monkeypatch):
    worker, backend, clock = exercise(monkeypatch, "repeated", stop_after=100)
    assert len(backend.commands) == 1 and backend.disabled
    assert clock.now < 1.2
    report = worker.snapshot(include_samples=True)
    assert "超时" in report["fault"] or "新鲜" in report["fault"]
    assert report["feedback_diagnostics"]["repeated_groups"] > 0
    assert report["last_rejected_feedback_group"]["samples"]


def test_all_new_frames_do_not_extend_150_ms_complete_group_deadline(monkeypatch):
    worker, backend, _ = exercise(monkeypatch, "gap")
    assert len(backend.commands) == 1 and backend.disabled
    report = worker.snapshot(include_samples=True)
    assert "反馈超过允许间隔" in report["fault"]
    assert report["last_rejected_feedback_group"]["samples"][-1]["feedback_age_s"] < .15


@pytest.mark.parametrize("mode,reason", [("repeated_fault", "驱动故障"),
                                        ("repeated_torque", "中止门限")])
def test_repeated_group_wait_does_not_swallow_fault_or_overload(monkeypatch, mode, reason):
    worker, backend, clock = exercise(monkeypatch, mode)
    assert len(backend.commands) == 1 and backend.disabled
    assert clock.now < 1.05
    report = worker.snapshot(include_samples=True)
    assert reason in report["fault"]
    rejected = report["last_rejected_feedback_group"]["samples"][-1]
    assert rejected["error_id"] == 3 if mode == "repeated_fault" else rejected["torque_nm"] == 30.


def test_new_speed_fields_keep_legacy_pilot_bounds():
    full = builtin_arm_profile(nominal_station("left_arm")).windows[0].point
    assert ProbePoint.model_validate({**full.model_dump(), "search_speed_deg_s": 10.,
                                      "positioning_speed_deg_s": 20.}).contact_speed_deg_s == 2.
    legacy = {**full.model_dump(), "full_range_search": False, "permitted_travel_deg": 10.,
              "search_speed_deg_s": 1., "following_error_deg": 1.6, "positioning_speed_deg_s": 5.}
    assert ProbePoint.model_validate(legacy).contact_speed_deg_s == 1.
    assert ProbePoint.model_validate({**legacy, "positioning_speed_deg_s": 20.}).positioning_speed_deg_s == 20.
    for field,value in (("search_speed_deg_s", 2.),):
        with pytest.raises(ValueError, match="单方向试点"):
            ProbePoint.model_validate({**legacy, field: value})
