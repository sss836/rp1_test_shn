from dataclasses import replace
import math

import numpy as np
import pytest

from factory_hmi.core.config import load_station_config
from factory_hmi.core.limit_block_check import (
    ContactWindow, Coordinates, FeedbackError, LimitCheckSimulation, MODULES,
)
from factory_hmi.models import MotorSample


def advance(session, expected):
    for _ in range(1400):
        session.tick()
        if session.phase in expected or session.phase == "aborted":
            return session.phase
    pytest.fail(f"simulation remained in {session.phase}: {session.reason}")


@pytest.mark.parametrize("limb, expected", zip(
    [key for key, title in MODULES], [10, 10, 12, 12, 10, 10, 14, 14, 4], strict=True
))
def test_all_module_directions_match_named_mapping_and_finish(limb, expected):
    coordinates = Coordinates(load_station_config(limb))
    assert len(coordinates.directions) == expected
    if limb == "waist_hip":
        assert [(node, name) for _, node, name in coordinates.identity] == [
            (1, "waist_roll_joint"), (1, "waist_yaw_joint"),
        ]
    session = LimitCheckSimulation(limb, "MOCK-LIMIT", list(range(expected)), "normal")
    session.connect(session.digest)
    assert not session.enabled
    session.enable(session.digest)
    session.reset(session.digest)
    assert advance(session, {"ready"}) == "ready"
    for index in range(expected):
        session.start(session.digest)
        assert advance(session, {"review"}) == "review", session.reason
        assert len(session.results) == index + 1
        assert session.results[-1]["outcome"] == "within_reference"
        assert abs(session.results[-1]["deviation_deg"]) < .01
        assert session.results[-1]["retreat_deg"] >= .97
    session.start(session.digest)
    assert advance(session, {"completed"}) == "completed"
    assert not session.enabled
    np.testing.assert_allclose(session.actual, coordinates.home, atol=math.radians(.1))
    report = session.report()
    assert report["calibration_passed"] is None and report["source_kind"] == "simulation"
    assert report["plan_sha256"] == session.digest


@pytest.mark.parametrize("scenario, outcome, phase", [
    ("early", "early", "review"), ("late", "late", "review"),
    ("missing", "not_detected", "review"), ("stale", "aborted", "aborted"),
    ("overload", "aborted", "aborted"),
])
def test_distinguishes_off_reference_missing_and_protective_stops(scenario, outcome, phase):
    session = LimitCheckSimulation("left_short_arm", "MOCK-FAULT", [0, 1], scenario)
    for action in (session.connect, session.enable, session.reset):
        action(session.digest)
    assert advance(session, {"ready"}) == "ready"
    session.start(session.digest)
    assert advance(session, {"review"}) == phase
    assert [row["outcome"] for row in session.report()["results"]] == [outcome, "uncovered"]
    if phase == "aborted":
        assert not session.enabled and session.reason


def test_start_is_separate_from_enable_and_has_a_plan_digest():
    session = LimitCheckSimulation("right_short_leg", "S-1", [9], "normal")
    with pytest.raises(ValueError, match="摘要"):
        session.connect("stale digest")
    with pytest.raises(ValueError, match="状态"):
        session.enable(session.digest)
    session.connect(session.digest)
    session.enable(session.digest)
    with pytest.raises(ValueError, match="状态"):
        session.start(session.digest)
    session.reset(session.digest)
    assert advance(session, {"ready"}) == "ready"
    assert len(session.samples) >= 80
    session.abort()
    assert session.report()["results"][0]["outcome"] == "uncovered"


def test_ankle_torque_virtual_work_and_motor_pair_provenance():
    c = Coordinates(load_station_config("left_short_leg"))
    q = c.home.copy()
    q[-2:] = (.1, -.15)
    motor = c.motor_target(q)
    torque = np.linspace(2.0, 7.0, len(q))
    samples = [MotorSample(pos_rad=p, torque_nm=t, timestamp_s=1,
                           bus=b, motor_id=m, joint_name=j)
               for (b, m, j), p, t in zip(c.identity, motor, torque, strict=True)]
    actual, joint_torque, stamps = c.decode(samples, 1.01, ())
    np.testing.assert_allclose(actual, q, atol=1e-6)
    assert c.directions[-1].motor_labels == c.directions[-3].motor_labels == ("can0#6", "can0#7")
    delta = np.array([0., 0., 0., 1e-6, -2e-6])
    assert float(joint_torque @ delta) == pytest.approx(float(torque @ (c.motor_target(q + delta) - motor)), rel=1e-5)
    with pytest.raises(FeedbackError, match="全部更新"):
        c.decode(samples, 1.02, stamps)
    skewed = [*samples[:-1], replace(samples[-1], timestamp_s=.95)]
    # Fresh motor frames may arrive at different times; 50ms is diagnostic,
    # not a separate stop condition. Per-motor expiry still applies.
    q_skewed, _, _ = c.decode(skewed, 1.02, ())
    np.testing.assert_allclose(q_skewed, q, atol=1e-6)
    expired = [*samples[:-1], replace(samples[-1], timestamp_s=.80)]
    with pytest.raises(FeedbackError, match="无效、超时"):
        c.decode(expired, 1.02, ())
    with pytest.raises(FeedbackError, match="不一致"):
        c.decode(samples[::-1], 1.02, ())


def test_candidate_requires_stall_and_sustained_load_during_advancing_target():
    direction = Coordinates(load_station_config("waist_hip")).directions[1]
    for angle_slope, torque, target_slope in ((1., 6., 1.), (0., 4.9, 1.), (0., 6., 0.)):
        detector = ContactWindow(direction, baseline=2)
        assert all(detector.observe(i / 20, .1 + i / 20 * angle_slope, torque, i / 20 * target_slope) is None
                   for i in range(25))
    detector = ContactWindow(direction, baseline=2)
    outcomes = [detector.observe(i / 20, 40, 5, i / 20) for i in range(21)]
    assert all(item is None for item in outcomes[:-1])
    assert outcomes[-1]["fresh_samples"] == 21
