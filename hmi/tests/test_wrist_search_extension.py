"""Validate wrist extension with synthetic feedback only."""

import math

import numpy as np
import pytest

from factory_hmi.core.arm_limit_defaults import builtin_arm_profile
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import ProbeCycle, ProbePoint
from factory_hmi.core.module_limit_check import ModuleProfile, path_segments, validate_module_plan


YAW_LOWER = ("left_wrist_yaw_joint", -1)
PITCH_UPPER = ("left_wrist_pitch_joint", 1)
DT = .05


def extension_fixture(direction, *, origin_deg=0., site=True):
    station = nominal_station("left_arm")
    profile = builtin_arm_profile(station)
    document = profile.model_dump()
    if site:
        document.update(source="site", approved_by="offline fixture", revision="mock extension")
    index = next(i for i, w in enumerate(profile.windows)
                 if (w.point.joint, w.point.direction) == direction)
    point = document["windows"][index]["point"]
    axis = Coordinates(station).names.index(direction[0])
    sign = direction[1]
    start = [0.] * 7
    start[axis] = origin_deg
    soft_deg = sign * point["permitted_travel_deg"]
    point.update(staging_pose_deg=tuple(start), search_pose_deg=tuple(start),
                 permitted_travel_deg=point["permitted_travel_deg"] - sign * origin_deg,
                 search_extension={"actual_limit_deg": soft_deg + sign * 6.,
                                   "command_limit_deg": soft_deg + sign * 7.5})
    return station, document, index


def extension_cycle(direction, *, origin_deg=0.):
    station, document, index = extension_fixture(direction, origin_deg=origin_deg)
    coords, profile, _ = validate_module_plan(station, ModuleProfile.model_validate(document), "MOCK", "offline")
    point = next(w.point for w in profile.windows if (w.point.joint, w.point.direction) == direction)
    assert point == ProbePoint.model_validate(document["windows"][index]["point"])
    return ProbeCycle(coords, point, np.deg2rad(point.search_pose_deg), 1.)


def drive_cycle(cycle, *, stop=None, lag_deg=0., incident=None):
    axis, sign = cycle.axis, cycle.point.direction
    point = cycle.point
    soft = sign * point.search_pose_deg[axis] + point.permitted_travel_deg
    command_cap = sign * point.search_extension.command_limit_deg
    trace = []
    for step in range(1, 1500):
        phase = cycle.phase
        target = cycle.target.copy()
        directed_target_deg = sign * math.degrees(target[axis])
        q = target.copy()
        joint_torque = np.full(7, .7)
        motor_torque = joint_torque.copy()
        if phase in {"search", "retreat"}:
            actual = min(directed_target_deg, max(soft - 5., directed_target_deg - lag_deg))
            if stop is not None and actual >= stop:
                actual = stop
                joint_torque[axis] = motor_torque[axis] = 2.
            if phase == "retreat" and incident == "retreat_overrun":
                actual = sign * point.search_extension.actual_limit_deg + .1
            if phase == "search" and directed_target_deg > soft + .3:
                if incident == "actual":
                    actual = sign * point.search_extension.actual_limit_deg + .1
                elif incident == "joint_torque":
                    joint_torque[axis] = point.joint_abort_nm[axis]
                elif incident == "motor_torque":
                    motor_torque[axis] = point.motor_abort_nm[axis]
            q[axis] = math.radians(sign * actual)
        now = 1. + step * DT
        cycle.step(now, q, joint_torque, motor_torque)
        if cycle.phase == "ready":
            cycle.start_search(now)
        next_target_deg = sign * math.degrees(cycle.target[axis])
        assert next_target_deg <= command_cap + 1e-7
        np.testing.assert_array_equal(np.delete(cycle.target, axis), np.zeros(6))
        trace.append((phase, directed_target_deg, sign * math.degrees(q[axis]), next_target_deg))
        if cycle.result is not None:
            return cycle.result, trace
    pytest.fail(f"extended search did not finish: {cycle.phase}")


@pytest.mark.parametrize("direction,origin_deg", [(YAW_LOWER, 0.), (PITCH_UPPER, 2.)])
def test_extension_continues_same_search_and_records_measured_stop(direction, origin_deg):
    cycle = extension_cycle(direction, origin_deg=origin_deg)
    sign = cycle.point.direction
    soft = sign * origin_deg + cycle.point.permitted_travel_deg
    actual_stop = soft + 3.
    row, trace = drive_cycle(cycle, stop=actual_stop)
    phases = [row[0] for i, row in enumerate(trace) if i == 0 or trace[i-1][0] != row[0]]
    assert phases.count("search") == phases.count("retreat") == 1
    extended = [row for row in trace if row[0] == "search" and row[1] > soft + .1]
    assert extended and max(new - old for _, old, _, new in extended) <= DT + 1e-7
    assert any(actual > soft for _, _, actual, _ in extended)
    assert row["search_end_reason"] == "contact"
    assert row["outcome"] == "late"
    assert row["actual_deg"] == pytest.approx(sign * actual_stop)
    assert row["contact_window_s"] >= 1. - 1e-8 and row["torque_support_fraction"] >= .8
    assert row["retreat_deg"] >= cycle.point.retreat_deg - 1e-6


@pytest.mark.parametrize("direction,lag_deg,reason", [(YAW_LOWER, 0., "actual_travel"),
                                                     (PITCH_UPPER, 3., "command_travel")])
def test_extension_stops_at_confirmed_boundary_without_manufacturing_contact(direction, lag_deg, reason):
    cycle = extension_cycle(direction)
    sign = cycle.point.direction
    row, trace = drive_cycle(cycle, lag_deg=lag_deg)
    final_actual = max(actual for phase, _, actual, _ in trace if phase == "search")
    assert final_actual > cycle.point.permitted_travel_deg
    assert final_actual <= sign * cycle.point.search_extension.actual_limit_deg + 1e-7
    assert row["search_end_reason"] == reason
    assert row["outcome"] != "within_reference"
    assert row.get("actual_deg") is None and row["deviation_deg"] is None


@pytest.mark.parametrize("direction,incident,pattern", [
    (YAW_LOWER, "actual", "行程|边界"), (PITCH_UPPER, "actual", "行程|边界"),
    (YAW_LOWER, "joint_torque", "实测力矩达到工位中止门限"),
    (PITCH_UPPER, "motor_torque", "实测力矩达到工位中止门限"),
])
def test_protections_apply_during_extended_search(direction, incident, pattern):
    cycle = extension_cycle(direction)
    with pytest.raises(FeedbackError, match=pattern):
        drive_cycle(cycle, incident=incident)
    assert cycle.result is None


def test_hard_actual_guard_remains_in_force_while_unloading():
    cycle = extension_cycle(PITCH_UPPER)
    with pytest.raises(FeedbackError, match="实际搜索姿态超过核定行程"):
        drive_cycle(cycle, stop=cycle.point.permitted_travel_deg + 3., incident="retreat_overrun")
    assert cycle.phase == "retreat"
    assert cycle.candidate is not None and cycle.result is None


def test_extension_uses_only_site_profile_and_preflights_both_endpoints():
    station, document, _ = extension_fixture(YAW_LOWER, site=False)
    with pytest.raises(ValueError):
        validate_module_plan(station, ModuleProfile.model_validate(document), "MOCK", "offline")

    station, document, _ = extension_fixture(PITCH_UPPER, origin_deg=2.)
    coords, profile, _ = validate_module_plan(station, ModuleProfile.model_validate(document), "MOCK", "offline")
    point = next(w.point for w in profile.windows if (w.point.joint, w.point.direction) == PITCH_UPPER)
    axis = coords.names.index(point.joint)
    endpoints = [end[axis] for begin, end in path_segments(profile, coords.names)
                 if tuple(begin) == point.search_pose_deg and end[axis] > begin[axis]]
    # A swept interval to the farther bound covers the entire extension.
    assert max(endpoints) == max(point.search_extension.actual_limit_deg,
                                 point.search_extension.command_limit_deg)


@pytest.mark.parametrize("field,value", [("actual_limit_deg", -87.9),
                                           ("command_limit_deg", 70.),
                                           ("command_limit_deg", float("inf"))])
def test_extension_rejects_wrong_direction_or_insufficient_envelope(field, value):
    station, document, index = extension_fixture(YAW_LOWER)
    document["windows"][index]["point"]["search_extension"][field] = value
    with pytest.raises(ValueError):
        validate_module_plan(station, ModuleProfile.model_validate(document), "MOCK", "offline")
