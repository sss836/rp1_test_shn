"""Offline qualification of the two left wrist re-approaches."""

import math

import numpy as np
import pytest

from factory_hmi.core.arm_limit_defaults import builtin_arm_profile
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import ProbeCycle


YAW_LOWER = ("left_wrist_yaw_joint", -1)
PITCH_UPPER = ("left_wrist_pitch_joint", 1)
DT = .05


class WristModel:
    def __init__(self, direction, *, command_boundary=False):
        config = nominal_station("left_arm")
        coords = Coordinates(config)
        profile = builtin_arm_profile(config)
        point = next(w.point for w in profile.windows if (w.point.joint, w.point.direction) == direction)
        self.probe = ProbeCycle(coords, point, np.zeros(7), 1.)
        self.axis, self.sign = self.probe.axis, point.direction
        self.cap = point.permitted_travel_deg
        self.stop = self.cap - .5
        self.command_boundary = command_boundary
        self.searches = 0
        self.retry_start_deg = None
        self.retry_peak_speed = 0.
        self.angles = []

    def run(self, *, contact_attempt=None, overload=None):
        probe, axis, sign = self.probe, self.axis, self.sign
        for i in range(1, 3000):
            old_phase = probe.phase
            q = probe.target.copy()
            travel = sign * math.degrees(q[axis])
            # On the first pass, an encoder that keeps following reaches the
            # measured boundary. Pinning a little inside it without added load
            # reproduces a miss at the command boundary instead.
            if old_phase in {"search", "retreat"} and self.command_boundary and self.searches == 1:
                q[axis] = math.radians(sign * min(travel, self.cap - .4))
            reacquired_load = self.searches == 2 or (self.searches == 1 and old_phase == "baseline")
            torque = np.full(7, .95 if reacquired_load else .7)
            if (self.searches == contact_attempt and old_phase in {"search", "retreat"}
                    and travel >= self.stop):
                q[axis] = math.radians(sign * self.stop)
                torque[axis] = 2.
            motor_torque = torque.copy()
            if self.searches == 2 and old_phase == "search" and overload is not None:
                if overload == "joint":
                    torque[axis] = probe.point.joint_abort_nm[axis]
                else:
                    motor_torque[axis] = probe.point.motor_abort_nm[axis]
            now = 1 + i * DT
            probe.step(now, q, torque, motor_torque)
            if self.searches == 2 and old_phase == "search":
                advance = sign * math.degrees(probe.target[axis]) - travel
                self.retry_peak_speed = max(self.retry_peak_speed, advance / DT)
            if probe.phase == "ready":
                probe.start_search(now)
            if old_phase != "search" and probe.phase == "search":
                self.searches += 1
                if self.searches == 2:
                    self.retry_start_deg = sign * math.degrees(q[axis])
            np.testing.assert_array_equal(np.delete(probe.target, axis), np.zeros(6))
            self.angles.append(sign * math.degrees(q[axis]))
            if probe.phase == "finished":
                return probe.result
        pytest.fail(f"bounded wrist search did not finish: {probe.phase}")


@pytest.mark.parametrize("direction,command_boundary", [(YAW_LOWER, False), (PITCH_UPPER, True)])
def test_search_reapproaches_and_only_records_a_measured_contact(direction, command_boundary):
    model = WristModel(direction, command_boundary=command_boundary)
    result = model.run(contact_attempt=2)
    assert model.searches == len(result["search_attempts"]) == 2
    first, second = result["search_attempts"]
    assert first["end_reason"] == ("command_travel" if command_boundary else "actual_travel")
    assert first["retreat_deg"] >= model.probe.point.retreat_deg - 1e-6
    assert first["baseline_nm"] == pytest.approx(.7)
    assert second["end_reason"] == "contact"
    assert second["baseline_nm"] == pytest.approx(.95)
    assert model.retry_start_deg <= model.cap - model.probe.point.retreat_deg + 1e-6
    assert 0. < model.retry_peak_speed <= 1. + 1e-8
    assert max(model.angles) <= model.cap + 1e-8
    assert result["search_end_reason"] == "contact"
    assert result["outcome"] == "within_reference"
    assert result["actual_deg"] == pytest.approx(model.sign * model.stop)
    assert result["contact_window_s"] >= 1. - 1e-8
    assert result["torque_support_fraction"] >= .8


def test_first_pass_candidate_ends_the_search_without_reapproach():
    model = WristModel(YAW_LOWER)
    result = model.run(contact_attempt=1)
    assert model.searches == len(result["search_attempts"]) == 1
    assert result["search_end_reason"] == "contact"
    assert result["actual_deg"] == pytest.approx(model.sign * model.stop)


@pytest.mark.parametrize("direction,command_boundary", [(YAW_LOWER, False), (PITCH_UPPER, True)])
def test_two_unloaded_passes_do_not_report_the_search_endpoint_as_a_limit(direction, command_boundary):
    model = WristModel(direction, command_boundary=command_boundary)
    result = model.run()
    assert model.searches == len(result["search_attempts"]) == 2
    assert max(model.angles) <= model.cap + 1e-8
    assert result["search_end_reason"] in {"actual_travel", "command_travel"}
    assert result["outcome"] == "not_detected"
    assert result.get("actual_deg") is None and result["deviation_deg"] is None


@pytest.mark.parametrize("overload", ["joint", "motor"])
def test_wrist_reapproach_still_aborts_on_configured_torque(overload):
    model = WristModel(YAW_LOWER)
    with pytest.raises(FeedbackError, match="实测力矩达到工位中止门限"):
        model.run(overload=overload)
    assert model.searches == 2 and model.probe.result is None


@pytest.mark.parametrize("direction", [("left_wrist_yaw_joint", 1), ("left_wrist_pitch_joint", -1)])
def test_opposite_wrist_directions_keep_single_search(direction):
    model = WristModel(direction)
    result = model.run()
    assert model.searches == 1
    assert result["outcome"] == "not_detected"
