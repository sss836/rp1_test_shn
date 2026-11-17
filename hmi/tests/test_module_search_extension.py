"""Synthetic full-module extension regressions without live PLC/CAN devices."""

from dataclasses import replace
import math
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.module_limit_check import ModuleCycle, ModuleProfile, path_segments, validate_module_plan
from factory_hmi.core.module_limit_defaults import builtin_module_profile
from factory_hmi.core.service import FactoryService
from factory_hmi.limit_replay_assets import PRESET_KEYS


MODULE_POINTS = {
    "left_short_arm": ("left_shoulder_roll_joint", -1),
    "right_short_arm": ("right_elbow_joint", -1),
    "left_arm": ("left_wrist_roll_joint", 1),
    "right_arm": ("right_wrist_yaw_joint", -1),
    "left_short_leg": ("left_ankle_pitch_joint", -1),
    "right_short_leg": ("right_ankle_roll_joint", 1),
    "waist_hip": ("waist_roll_joint", -1),
}
DT = .05


def synthetic_plan(limb):
    station = nominal_station(limb)
    coords = Coordinates(station, include_waist_holds=True)
    profile = builtin_module_profile(station).model_dump()
    profile.update(source="site", approved_by="mock", revision="synthetic extension fixture", use_builtin_order=False)
    joint, sign = MODULE_POINTS[limb]
    direction = next(d for d in coords.directions if (d.joint, d.sign) == (joint, sign))
    windows = list(profile["windows"])
    index = next(i for i, window in enumerate(windows)
                 if (window["point"]["joint"], window["point"]["direction"]) == (joint, sign))
    selected = windows.pop(index)
    point = selected["point"]
    pose = list(point["search_pose_deg"])
    pose[direction.index] = direction.reference_deg - sign * 4.
    point.update(
        staging_pose_deg=pose,
        search_pose_deg=pose,
        permitted_travel_deg=6.9,
        search_extension={
            "actual_limit_deg": direction.reference_deg + sign * 6.,
            "command_limit_deg": direction.reference_deg + sign * 7.5,
        },
    )
    profile["windows"] = [selected, *windows]
    return validate_module_plan(station, ModuleProfile.model_validate(profile), "MOCK-EXTENSION", "offline")


def first_window(coords, profile, *, contact_deg=None, tracking_lag_deg=0.):
    actual = np.deg2rad(profile.home_deg)
    if profile.limb == "waist_hip":
        actual[2:] += np.deg2rad((.05, -.05))
    cycle = ModuleCycle(coords, profile, actual, 1.)
    held = actual[2:].copy() if profile.limb == "waist_hip" else None
    point = profile.windows[0].point
    axis = coords.names.index(point.joint)
    sign = point.direction
    soft_cap = sign * point.search_pose_deg[axis] + point.permitted_travel_deg
    trace = []
    motors_before = None
    for i in range(1, 3500):
        probe = cycle.probe
        target = cycle.target.copy()
        q = target.copy()
        joint_tau = np.full(len(q), .7)
        if probe and probe.phase in {"search", "retreat"}:
            actual_deg = sign * math.degrees(target[axis])
            actual_deg -= min(tracking_lag_deg, max(0., actual_deg - soft_cap + 4.))
            if contact_deg is not None and actual_deg >= contact_deg:
                actual_deg = contact_deg
                joint_tau[axis] = 2.
            q[axis] = math.radians(sign * actual_deg)
        jacobian = coords.jacobian(q)
        motor_tau = np.linalg.solve(jacobian.T, joint_tau)
        motor = coords.motor_target(q)
        recovered = coords.joint_position(motor)
        np.testing.assert_allclose(recovered, q, atol=1.e-5)
        now = 1. + i * DT
        cycle.step(now, recovered, joint_tau, motor_tau)
        if held is not None:
            np.testing.assert_array_equal(cycle.target[2:], held)
        if probe and cycle.probe is probe:
            target_deg = sign * math.degrees(cycle.target[axis])
            assert target_deg <= sign * point.search_extension.command_limit_deg + 1.e-7
            trace.append((probe.phase, sign * math.degrees(recovered[axis]), target_deg))
            motor_target = coords.motor_target(cycle.target)
            if motors_before is not None:
                assert np.max(abs(np.rad2deg(motor_target - motors_before))) <= point.motor_step_deg + 1.e-7
            motors_before = motor_target
        if cycle.results:
            return cycle.results[0], trace, soft_cap
    pytest.fail(f"{profile.limb} synthetic search did not finish: {cycle.phase}")


@pytest.mark.parametrize("limb", sorted(PRESET_KEYS))
def test_supported_module_keeps_searching_past_ordinary_window_and_records_contact(limb):
    coords, profile, context = synthetic_plan(limb)
    point = profile.windows[0].point
    axis = coords.names.index(point.joint)
    contact = point.direction * point.search_pose_deg[axis] + point.permitted_travel_deg + 1.2
    row, trace, soft_cap = first_window(coords, profile, contact_deg=contact)
    assert context["windows"][0]["point"]["search_extension"] == point.search_extension.model_dump()
    assert row["search_end_reason"] == "contact"
    assert row["actual_deg"] == pytest.approx(point.direction * contact, abs=1.e-4)
    assert row["outcome"] == "late"
    assert row["retreat_completed"] and row["retreat_deg"] >= point.retreat_deg - 1.e-6
    phases = [p for i, (p, _, _) in enumerate(trace) if i == 0 or p != trace[i-1][0]]
    assert phases.count("search") == phases.count("retreat") == 1
    assert max(q for phase, q, _ in trace if phase == "search") > soft_cap
    increments = [after[2] - before[2] for before, after in zip(trace, trace[1:])
                  if before[0] == after[0] == "search" and before[2] > soft_cap + .1]
    assert increments and max(increments) <= DT + 1.e-7


@pytest.mark.parametrize("limb,lag_deg,reason", [
    ("waist_hip", 0., "actual_travel"),
    ("right_short_leg", 4., "command_travel"),
])
def test_measured_and_target_bounds_remain_independent_without_contact(limb, lag_deg, reason):
    coords, profile, _ = synthetic_plan(limb)
    point = profile.windows[0].point
    row, trace, soft_cap = first_window(coords, profile, tracking_lag_deg=lag_deg)
    searched_angles = [q for phase, q, _ in trace if phase == "search"]
    assert soft_cap < max(searched_angles) <= point.direction * point.search_extension.actual_limit_deg + 1.e-5
    assert row["search_end_reason"] == reason
    assert row.get("actual_deg") is None
    assert row["outcome"] != "within_reference"


@pytest.mark.parametrize("setting", ["joint_max_rad", "motor_max_rad"])
def test_path_preflight_rejects_larger_search_than_station_bounds(setting):
    coords, profile, _ = synthetic_plan("left_arm")
    point = profile.windows[0].point
    axis = coords.names.index(point.joint)
    start = np.array(point.search_pose_deg)
    nominal = start.copy()
    nominal[axis] += point.direction * point.permitted_travel_deg
    limit_pose = nominal.copy()
    limit_pose[axis] += .5
    if setting == "motor_max_rad":
        limit = coords.motor_target(np.deg2rad(limit_pose))[axis]
    else:
        limit = np.deg2rad(limit_pose)[axis]
    max_angles = np.full(len(start), 7.).tolist()
    max_angles[axis] = limit
    safety = replace(coords.station.safety_limits, **{setting: max_angles})
    station = replace(coords.station, safety_limits=safety)
    diagnosis = SimpleNamespace(thresholds=SimpleNamespace(overtemperature_c=80.))
    controller = SimpleNamespace(config=station, backend=None, _diagnosis=diagnosis)
    _, bounds, _ = FactoryService._probe_io(controller)
    FactoryService._check_probe_path(coords, bounds, [(start, nominal)])
    with pytest.raises(FeedbackError, match="目标超出工位配置的位置边界"):
        FactoryService._check_probe_path(coords, bounds, path_segments(profile, coords.names))
