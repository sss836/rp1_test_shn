from dataclasses import replace
from pathlib import Path
import json
import math

import numpy as np
import pytest

from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import ProbeCycle, ProbePoint, ProbeProfile, point_description, station_sha


def fixture(point_name="left_knee_joint", direction=1):
    limb = "right_short_leg" if point_name.startswith("right_") else "left_short_leg"
    station = nominal_station(limb)
    coords = Coordinates(station)
    row = next(c for c in coords.directions if c.joint == point_name and c.sign == direction)
    near = np.rad2deg(coords.home)
    index = coords.names.index(point_name)
    near[index] = row.reference_deg - direction * 7
    staging = near.copy()
    staging[index] -= direction * 1.5
    point = ProbePoint(joint=point_name, direction=direction, staging_pose_deg=staging.tolist(),
                       search_pose_deg=near.tolist(), permitted_travel_deg=10., search_speed_deg_s=1.,
                       positioning_speed_deg_s=3., reset_kp_scale=.23, reset_kd_scale=.4,
                       search_kp_scale=.12, search_kd_scale=.25, motor_abort_nm=[25.] * 5,
                       joint_abort_nm=[20.] * 5, contact_floor_nm=.6, contact_rise_nm=3.,
                       following_error_deg=1.6, motor_step_deg=.22, retreat_deg=1.,
                       comparison_tolerance_deg=1.5)
    profile = ProbeProfile(schema_version=1, limb=limb, model="RP1.3.4", power_source="external",
                           fixture="MOCK FIXTURE ONLY", revision="synthetic", approved_by="mock",
                           station_sha256=station_sha(station), points=[point])
    return coords, point, profile


def play(offset=None, *, joint="left_knee_joint", direction=1, fault=None, point=None, baseline=.7,
         feedback_period=.05):
    coords, default_point, _ = fixture(joint, direction)
    if point is None:
        point = default_point
    model = ProbeCycle(coords, point, np.deg2rad(point.staging_pose_deg), 1.)
    axis = model.axis
    for i in range(1, 1000):
        now = round(1 + feedback_period*i, 4)
        q = model.target.copy()
        torque = np.full(5, baseline)
        if model.phase == "ready":
            model.start_search(now)
        if model.phase == "search":
            if fault == "stale":
                model.step(now + .3, q, torque, torque)
            if fault == "motor_abort":
                motor = torque.copy()
                motor[axis] = point.motor_abort_nm[axis]
                model.step(now, q, torque, motor)
            if fault == "joint_abort":
                torque[axis] = point.joint_abort_nm[axis]
        if offset is not None and model.phase in {"search", "unload"}:
            stop = model.direction.reference_deg + direction * offset
            if direction * (math.degrees(q[axis]) - stop) >= -1e-7:
                q[axis] = math.radians(stop)
                torque[axis] = 5.5
        # Synthesize motor torques from a logical torque and prove the opposite
        # conversion on the same code path as physical closed-chain feedback.
        motor_torque = np.linalg.solve(coords.jacobian(q).T, torque)
        model.step(now, q, coords.jacobian(q).T @ motor_torque, motor_torque)
        if model.result is not None:
            return model
    pytest.fail(f"no terminal result, phase={model.phase}")


@pytest.mark.parametrize("offset,outcome", [(0, "within_reference"), (-4, "early"), (1.8, "late"),
                                            (None, "not_detected")])
def test_stop_results_and_retreat(offset, outcome):
    model = play(offset)
    assert model.result["outcome"] == outcome
    assert model.result["retreat_deg"] >= .88
    assert max(row["target_deg"][model.axis] for row in model.samples) <= model.point.search_pose_deg[model.axis] + 10.
    motor_targets = np.array([model.coords.motor_target(np.deg2rad(r["target_deg"])) for r in model.samples])
    assert np.max(abs(np.rad2deg(np.diff(motor_targets, axis=0)))) < model.point.motor_step_deg
    assert model.phase == "finished"
    assert any(row["phase"] == "baseline" and 0 < row["kp_kd_scale"][0] < .23 for row in model.samples)


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("side", ["left", "right"])
def test_closed_chain_two_sided_probe(side, direction):
    model = play(0, joint=f"{side}_ankle_pitch_joint", direction=direction)
    assert model.result["outcome"] == "within_reference"
    motor_targets = np.array([model.coords.motor_target(np.deg2rad(r["target_deg"])) for r in model.samples])
    assert np.max(abs(np.rad2deg(np.diff(motor_targets, axis=0)))) < model.point.motor_step_deg
    assert abs(model.result["actual_deg"] - model.direction.reference_deg) < 1e-5


@pytest.mark.parametrize("fault,reason", [("stale", "不连续"), ("motor_abort", "力矩"), ("joint_abort", "力矩")])
def test_feedback_and_load_stops(fault, reason):
    with pytest.raises(FeedbackError, match=reason):
        play(fault=fault)


def test_contact_threshold_leaves_room_below_abort():
    coords, point, profile = fixture()
    limits = list(point.joint_abort_nm)
    limits[coords.names.index(point.joint)] = 7.
    point = point.model_copy(update={"joint_abort_nm": tuple(limits), "contact_floor_nm": 5.})

    point_description(coords.station, profile, point, coords=coords)
    assert play(0, point=point, baseline=1.5).result["outcome"] == "within_reference"
    with pytest.raises(FeedbackError, match="实测候选力矩门限"):
        play(0, point=point, baseline=4.)
    with pytest.raises(FeedbackError, match="中止门限"):
        play(0, point=point, baseline=1.5, fault="joint_abort")

    for field in ("contact_floor_nm", "contact_rise_nm"):
        impossible = point.model_copy(update={field: 7.})
        with pytest.raises(ValueError, match="候选力矩"):
            point_description(coords.station, profile, impossible, coords=coords)


def test_search_uses_valid_feedback_intervals_past_old_deadline():
    coords, point, profile = fixture()
    axis = coords.names.index(point.joint)
    direction = next(row for row in coords.directions if row.joint == point.joint and row.sign == point.direction)
    start = list(point.search_pose_deg)
    start[axis] = direction.reference_deg - 9.5
    point = ProbePoint.model_validate({
        **point.model_dump(), "search_pose_deg": start, "staging_pose_deg": start,
        "search_speed_deg_s": .21, "permitted_travel_deg": 12.,
    })
    point_description(coords.station, profile, point, coords=coords)
    cycle = play(point=point, feedback_period=.075)
    search = [row for row in cycle.samples if row["phase"] == "search"]
    assert search[-1]["time_s"] - search[0]["time_s"] > 50.
    assert search[-1]["target_deg"][axis] - start[axis] == pytest.approx(12.)
    assert cycle.result["outcome"] == "not_detected"
    assert search[0]["time_s"] < 2.


def test_joint_peak_checks_configured_cutoff_only_when_available():
    coords, point, profile = fixture()
    point_description(coords.station, profile, point, coords=coords)
    prof = profile.model_copy(update={"joint_peak_nm": (20.,) * len(coords.names)})
    point_description(coords.station, prof, point, coords=coords)
    ratings = list(prof.joint_peak_nm)
    ratings[coords.names.index(point.joint)] = 19.
    with pytest.raises(ValueError, match=point.joint):
        point_description(coords.station, prof.model_copy(update={"joint_peak_nm": tuple(ratings)}), point,
                          coords=coords)


def test_reviewed_paths_must_match_geometry():
    coords, point, profile = fixture()
    data = point_description(coords.station, profile, point)
    assert data["motors"][2]["search_kp"] == pytest.approx(coords.entries[2].kp * point.search_kp_scale)
    corrupted = list(point.staging_pose_deg)
    corrupted[0] += 1
    with pytest.raises(ValueError, match="当前逻辑关节"):
        point_description(coords.station, profile, point.model_copy(update={"staging_pose_deg": corrupted}))
    start = list(point.search_pose_deg)
    start[coords.names.index(point.joint)] = data["reference_deg"] - .7
    too_short = point.model_copy(update={"search_pose_deg": start, "staging_pose_deg": start,
                                        "permitted_travel_deg": 3.})
    with pytest.raises(ValueError, match="不足以缓退"):
        point_description(coords.station, profile, too_short)
    with pytest.raises(FeedbackError, match="核定复位起点"):
        ProbeCycle(coords, point, coords.home.copy(), 1.)


def test_no_motion_before_separate_user_start_and_expected_pose():
    coords, point, _ = fixture()
    model = ProbeCycle(coords, point, np.deg2rad(point.staging_pose_deg), 1.)
    with pytest.raises(ValueError, match="复位"):
        model.start_search(1.0)
    for i in range(1, 110):
        model.step(round(1 + i*.05, 4), model.target, np.zeros(5), np.zeros(5))
    assert model.phase == "ready"
    np.testing.assert_allclose(model.target, np.deg2rad(point.search_pose_deg))


def test_repeated_motor_feedback_cannot_keep_worker_moving():
    from factory_hmi.core.backend import FakeMotorBackend
    from factory_hmi.core.live_limit_probe import ProbeWorker
    coords, point, _ = fixture()
    fake = FakeMotorBackend(coords.entries, initial_positions=coords.motor_target(np.deg2rad(point.staging_pose_deg)))
    fake.open_transport()
    fake.enable_motors()
    unchanged = fake.read_samples()
    worker = ProbeWorker(fake, coords, point, {}, lambda: unchanged, lambda *_: None, lambda *_: None)
    try:
        worker.start()
        worker.thread.join(2.)
        snapshot = worker.snapshot()
        assert snapshot["phase"] == "aborted" and "新鲜" in snapshot["fault"]
        assert len(fake.command_history) == 1
        assert not fake.motors_enabled and snapshot["disable"]["feedback"] == "confirmed_mock"
    finally:
        fake.close_transport()


def test_motion_can_be_cancelled_by_station_safety_request():
    import time
    from factory_hmi.core.backend import FakeMotorBackend
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.core.live_limit_probe import ProbeWorker
    coords, point, _ = fixture()
    fake = FakeMotorBackend(coords.entries, initial_positions=coords.motor_target(np.deg2rad(point.staging_pose_deg)))
    controller = FactoryController(coords.station, fake)
    fake.open_transport()
    fake.enable_motors()
    worker = ProbeWorker(fake, coords, point, {}, fake.read_samples, lambda *_: None, lambda *_: None)
    controller._manual = worker
    try:
        worker.start()
        deadline = time.monotonic() + 1.
        while not fake.command_history and time.monotonic() < deadline:
            time.sleep(.01)
        assert fake.command_history
        assert controller.request_motion_stop()
        controller._stop_manual_session()
        assert worker.snapshot()["phase"] == "aborted"
        assert controller._manual is None
        assert not fake.motors_enabled
    finally:
        fake.close_transport()


def test_station_service_uses_frozen_profile_and_stops_mock_adapter(tmp_path, monkeypatch):
    import time
    from factory_hmi.core.backend import FakeMotorBackend
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.core.service import FactoryService
    from factory_hmi.models import StationState

    coords, point, profile = fixture()
    fake = FakeMotorBackend(coords.entries, initial_positions=coords.motor_target(np.deg2rad(point.staging_pose_deg)))
    controller = FactoryController(coords.station, fake)
    service = FactoryService(data_root=tmp_path)
    service._controller, service._backend_name = controller, "motors_py"
    fake.open_transport()
    fake.enable_motors()
    controller.state_machine.transition(StationState.CONNECTED, expected=StationState.DISCONNECTED)
    monkeypatch.setattr("factory_hmi.core.live_limit_probe.load_site_profile", lambda _: profile)
    monkeypatch.setattr(service, "can_status", lambda: {"all_up": True})
    monkeypatch.setattr(fake, "limit_probe_samples", fake.read_samples, raising=False)
    args = (coords.station.limb, point.joint, point.direction, "MOCK-STATION")
    try:
        plan = service.limit_probe_preview(*args)
        with pytest.raises(ValueError, match="摘要"):
            service.limit_probe_reset(*args, "0"*64, True)
        assert not fake.command_history
        service.limit_probe_reset(*args, plan["digest"], True)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and service.limit_probe_status()["phase"] not in {"ready", "aborted"}:
            time.sleep(.03)
        assert service.limit_probe_status()["phase"] == "ready", service.limit_probe_status()
        service.limit_probe_start(plan["digest"])
        assert service.limit_probe_status()["phase"] in {"baseline", "search"}
        service.disable_motors()
        report = service.limit_probe_status(report=True)
        assert report["phase"] == "aborted" and report["context"]["digest"] == plan["digest"]
        assert report["disable"]["feedback"] == "confirmed_mock"
        assert report["samples"] and report["samples"][0]["drive_feedback"]
    finally:
        service.close()
