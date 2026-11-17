import math

import numpy as np
import pytest

from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import ProbePoint, station_sha
from factory_hmi.core.module_limit_check import ModuleCycle, ModuleProfile, entry_poses, validate_module_plan
from factory_hmi.limit_replay_assets import PRESET_KEYS


def commissioned(limb):
    station = nominal_station(limb)
    coords = Coordinates(station, include_waist_holds=True)
    home = np.rad2deg(coords.home)
    num = len(home)
    windows = []
    for row in coords.directions:
        goal = home.copy()
        goal[row.index] = row.reference_deg - row.sign*3.
        point = ProbePoint(joint=row.joint, direction=row.sign, staging_pose_deg=tuple(goal),
                           search_pose_deg=tuple(goal), permitted_travel_deg=5.5, search_speed_deg_s=1.,
                           positioning_speed_deg_s=5., reset_kp_scale=.22, reset_kd_scale=.4,
                           search_kp_scale=.13, search_kd_scale=.25, motor_abort_nm=[20.] * num,
                           joint_abort_nm=[20.] * num, contact_floor_nm=.6, contact_rise_nm=3.,
                           following_error_deg=1.8, motor_step_deg=.3, retreat_deg=1., comparison_tolerance_deg=1.5)
        windows.append(dict(point=point))
    profile = ModuleProfile(schema_version=1, limb=limb, model="RP1.3.4", power_source="external", fixture="MOCK",
                            revision="test fixture", approved_by="mock", station_sha256=station_sha(station),
                            home_deg=tuple(home), windows=windows)
    return validate_module_plan(station, profile, "TEST-ONLY", "revision1")


def run(cycle, early=False, stop_after=60000):
    for i in range(1, stop_after):
        now = 1 + i*.05
        q = cycle.target.copy()
        torque = np.full(len(q), .7)
        probe = cycle.probe
        if probe and probe.phase in ("search", "unload"):
            direction = probe.direction
            stop = math.radians(direction.reference_deg - (2. if early else 0.)*direction.sign)
            if direction.sign*(q[probe.axis] - stop) >= -1e-9:
                q[probe.axis] = stop
                torque[probe.axis] = 5.5
        cycle.step(now, q, torque, torque)
        if cycle.fixed_hips is not None:
            np.testing.assert_array_equal(cycle.target[2:], cycle.fixed_hips)
        if cycle.phase == "finished":
            return
    pytest.fail("mock sequence did not reach the end")


@pytest.mark.parametrize("limb", sorted(PRESET_KEYS))
def test_one_start_checks_all_directions_and_returns(limb):
    coords, profile, _ = commissioned(limb)
    final = np.deg2rad(profile.home_deg)
    q = final.copy()
    q[:2] += np.deg2rad([7.5, -11.])
    if limb == "waist_hip":
        q[2:] += math.radians(.05)
        final[2:] = q[2:]
    cycle = ModuleCycle(coords, profile, q, 1.)
    run(cycle)
    assert len(cycle.results) == len(coords.directions)
    for result, row in zip(cycle.results, coords.directions, strict=True):
        assert result["joint"] == row.joint and result["sign"] == row.sign
        assert result["outcome"] == "within_reference"
        assert result["actual_deg"] == pytest.approx(row.reference_deg)
    np.testing.assert_allclose(cycle.actual, final, atol=1.e-8)


@pytest.mark.parametrize("limb", sorted(PRESET_KEYS))
def test_builtin_sequence_keeps_site_search_settings_and_runs_all_directions(limb):
    coords, site, _ = commissioned(limb)
    site = site.model_copy(update={"use_builtin_trajectory": True})
    if limb == "waist_hip":
        site = site.model_copy(update={"home_deg": (0., 0., 7., -7.)})
    site_points = {(w.point.joint, w.point.direction): w.point for w in site.windows}
    coords, profile, payload = validate_module_plan(coords.station, site, "MOCK-01", "revision1")
    assert payload["profile"]["use_builtin_trajectory"]
    for window in profile.windows:
        point = window.point
        original = site_points[point.joint, point.direction]
        axis = coords.names.index(point.joint)
        assert point.staging_pose_deg[axis] == original.staging_pose_deg[axis]
        assert point.search_pose_deg[axis] == original.search_pose_deg[axis]
        assert point.motor_abort_nm == original.motor_abort_nm
        assert point.joint_abort_nm == original.joint_abort_nm
        if "shoulder" in point.joint:
            assert point.staging_pose_deg[3] == (58 if limb.startswith("left") else -58)
        if "hip_roll" in point.joint:
            assert point.staging_pose_deg[2] == (-145 if limb.startswith("left") else 145)
        if limb == "waist_hip":
            assert point.staging_pose_deg[2:] == (7., -7.)
    cycle = ModuleCycle(coords, profile, np.deg2rad(profile.home_deg), 1.)
    run(cycle)
    assert [(r["joint"], r["sign"]) for r in cycle.results] == [
        (w.point.joint, w.point.direction) for w in profile.windows
    ]


def test_abnormal_candidate_stops_before_advancing():
    coords, profile, _ = commissioned("left_short_arm")
    cycle = ModuleCycle(coords, profile, np.deg2rad(profile.home_deg), 1.)
    with pytest.raises(FeedbackError, match="结果异常"):
        run(cycle, early=True)
    assert cycle.index == 0
    assert cycle.results[0]["outcome"] == "early"


def test_ready_pose_and_transfers_reject_bad_feedback():
    coords, profile, _ = commissioned("left_short_leg")
    q = np.deg2rad(profile.home_deg)
    with pytest.raises(FeedbackError, match="超出模块参考行程"):
        ModuleCycle(coords, profile, q+3.5, 1.)
    cycle = ModuleCycle(coords, profile, q, 1.)
    with pytest.raises(FeedbackError, match="转场中超过核定中止力矩"):
        cycle.step(1.05, q, np.zeros(5), np.ones(5)*20.)
    with pytest.raises(FeedbackError, match="反馈不连续"):
        cycle.step(1.2, q, np.zeros(5), np.zeros(5))


def test_homing_adjusts_to_motor_step_and_accepts_small_feedback_noise():
    coords, profile, _ = commissioned("left_short_leg")
    first = profile.windows[0]
    first = first.model_copy(update={"point": first.point.model_copy(update={"motor_step_deg": .05})})
    profile = profile.model_copy(update={"windows": (first, *profile.windows[1:])})
    q = np.deg2rad(profile.home_deg)
    q[0] += math.radians(8.)
    cycle = ModuleCycle(coords, profile, q, 1.)
    torque = np.zeros(len(q))
    was_homing = False
    for i in range(1, 500):
        target = cycle.target.copy()
        q = target.copy()
        q[0] += math.radians(.006 * (-1)**i)
        cycle.step(1 + i*.025, q, torque, torque)
        motor_delta = coords.motor_target(cycle.target) - coords.motor_target(target)
        assert np.max(abs(np.rad2deg(motor_delta))) <= .05 + 1.e-10
        if cycle.phase == "homing":
            was_homing = True
        elif was_homing:
            assert i*.025 > 8. / 5.
            np.testing.assert_allclose(np.rad2deg(cycle.target), profile.home_deg, atol=1.e-8)
            return
    pytest.fail("homing did not complete")


def test_commissioned_transfer_speed_reduces_homing_time():
    coords, profile, _ = commissioned("left_short_leg")
    q = np.deg2rad(profile.home_deg)
    q[0] += math.radians(30.)
    torque = np.zeros(len(q))

    def homing_time(profile):
        _, profile, context = validate_module_plan(coords.station, profile, "SPEED-MOCK", "1")
        cycle = ModuleCycle(coords, profile, q, 1.)
        began = None
        for i in range(1, 600):
            now = 1 + i*.025
            previous = cycle.target.copy()
            cycle.step(now, previous, torque, torque)
            speed = np.max(abs(np.rad2deg(cycle.target - previous))) / .025
            assert speed <= context["transfer_speed_deg_s"] + 1.e-8
            motor_step = coords.motor_target(cycle.target) - coords.motor_target(previous)
            assert np.max(abs(np.rad2deg(motor_step))) <= cycle.point.motor_step_deg + 1.e-8
            if cycle.phase == "homing" and began is None:
                began = now
            elif cycle.phase != "homing" and began is not None:
                np.testing.assert_allclose(np.rad2deg(cycle.target), profile.home_deg, atol=1.e-8)
                return now - began
        pytest.fail("homing did not complete")

    fast = ModuleProfile.model_validate({**profile.model_dump(), "transfer_speed_deg_s": 10.})
    assert homing_time(profile) == pytest.approx(11.475, abs=.075)
    assert homing_time(fast) == pytest.approx(5.85, abs=.075)
    assert fast.windows[0].point.search_speed_deg_s == 1.
    with pytest.raises(ValueError, match="transfer_speed_deg_s"):
        ModuleProfile.model_validate({**profile.model_dump(), "transfer_speed_deg_s": 10.1})


def test_waist_rejects_a_path_that_moves_hips_and_missing_windows():
    coords, profile, _ = commissioned("waist_hip")
    altered = list(profile.home_deg)
    altered[2] += 1.
    with pytest.raises(ValueError, match="保持两个髋轴固定"):
        validate_module_plan(coords.station, profile.model_copy(update={"return_via_deg": [tuple(altered)]}), "TEST", "1")
    with pytest.raises(ValueError, match="全部上下限"):
        validate_module_plan(coords.station, profile.model_copy(update={"windows": profile.windows[:-1]}), "TEST", "1")


def test_waypoints_are_reached_before_the_first_search_and_hips_are_fixed():
    coords, profile, _ = commissioned("waist_hip")
    waypoint = (6., 4., *profile.home_deg[2:])
    hips = tuple((p-2., p+2.) for p in profile.home_deg[2:])
    profile = profile.model_copy(update={"entry_via_deg": (waypoint,), "waist_hip_hold_bounds_deg": hips})
    _, profile, _ = validate_module_plan(coords.station, profile, "WAIST-MOCK", "new-profile")
    current = np.deg2rad((12., 10., profile.home_deg[2]+.5, profile.home_deg[3]-.5))
    expected = tuple(np.deg2rad((a,b,*np.rad2deg(current[2:]))) for a,b in ((6.,4.), profile.home_deg[:2]))
    route = entry_poses(coords, profile, current)
    np.testing.assert_allclose(np.deg2rad(route[1:]), expected, atol=1.e-9)
    cycle = ModuleCycle(coords, profile, current, 1.)
    reached = []
    last_goal = None
    for i in range(1,1000):
        q = cycle.target.copy()
        if cycle.phase == "homing" and np.allclose(q, cycle.goal) and tuple(cycle.goal) != last_goal:
            reached.append(q)
            last_goal = tuple(cycle.goal)
        cycle.step(1+i*.05,q,np.zeros(4),np.zeros(4))
        np.testing.assert_array_equal(cycle.target[2:], current[2:])
        if cycle.probe is not None:
            break
    assert cycle.probe is not None
    np.testing.assert_allclose(reached, expected, atol=1.e-9)

    outside = current.copy()
    outside[2] += math.radians(4.)
    with pytest.raises(FeedbackError, match="保持范围"):
        entry_poses(coords,profile,outside)


def test_worker_owns_motor_commands_and_saves_aborted_report(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    import time
    from factory_hmi.core.backend import FakeMotorBackend
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.core.service import FactoryService
    from factory_hmi.models import StationState

    coords, profile, _ = commissioned("waist_hip")
    current = np.deg2rad(profile.home_deg)
    current[0] += math.radians(8.)
    start = coords.motor_target(current)
    fake = FakeMotorBackend(coords.entries, initial_positions=start)
    controller = FactoryController(coords.station, fake)
    service = FactoryService(data_root=tmp_path)
    service._controller, service._backend_name = controller, "motors_py"
    fake.open_transport()
    fake.enable_motors()
    controller.state_machine.transition(StationState.CONNECTED, expected=StationState.DISCONNECTED)
    monkeypatch.setattr(service, "can_status", lambda: {"all_up": True})
    monkeypatch.setattr(fake, "limit_probe_samples", fake.read_samples, raising=False)
    monkeypatch.setattr("factory_hmi.core.module_limit_check.load_module_plan",
                        lambda config, serial, rev: validate_module_plan(config, profile, serial, rev))
    try:
        approved = service.limit_module_preview("MOCK")
        with pytest.raises(ValueError, match="方案已变化"):
            service.limit_module_start("OTHER", approved["digest"], True)
        assert not fake.command_history
        service.limit_module_start("MOCK", approved["digest"], True)
        deadline = time.monotonic() + 2
        while (not any(abs(c["target"][0] - start[0]) > 1.e-6 for c in tuple(fake.command_history))
               and time.monotonic() < deadline):
            time.sleep(.01)
        assert any(abs(c["target"][0] - start[0]) > 1.e-6 for c in tuple(fake.command_history))
        assert service.limit_probe_status()["phase"] == "homing"
        assert service._limit_probe.active
        with pytest.raises(ValueError, match="方案已变化"):
            service.limit_module_start("MOCK", approved["digest"], True)
        service.disable_motors()
        status = service.limit_probe_status(report=True)
        assert status["phase"] == "aborted" and status["disable"]["feedback"] == "confirmed_mock"
        archived = json.loads(Path(status["report_file"]).read_text())
        assert archived["context"]["digest"] == approved["digest"]
        assert archived["context"]["entry_pose_deg"][0] == pytest.approx(8.)
        assert archived["context"]["entry_target_deg"][0] == pytest.approx(0.)
        assert archived["direction_total"] == 4 and archived["active"] is False
        for command in fake.command_history:
            np.testing.assert_array_equal(command["target"][2:], start[2:])
    finally:
        service.close()
