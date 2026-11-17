"""Offline sensor/control models: no CAN, PLC or real motor commands."""
import math

import numpy as np
import pytest

from factory_hmi.core.feedback_limit_search import LoadContact
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.arm_limit_defaults import builtin_arm_profile
from factory_hmi.core.live_limit_probe import PositionMove, PositionSettling, ProbeCycle
from factory_hmi.core.module_limit_check import ModuleCycle
from factory_hmi.core.mujoco_completion import preset_info


def setup(limb="left_arm"):
    config = nominal_station(limb)
    return Coordinates(config), builtin_arm_profile(config)


@pytest.mark.parametrize("limb", ["left_short_arm", "left_arm"])
@pytest.mark.parametrize("polarity", [-1, 1])
def test_all_directions_with_loaded_bias_changing_gravity_noise_and_real_retreat(limb, polarity):
    coords, profile = setup(limb)
    n = len(coords.names)
    bias = np.deg2rad(np.array([3.36, .43, -.14, -2.83, .16, .51, -.40][:n])*polarity)
    initial = np.deg2rad([20., 12., -8., -40., 9., 4., -6.][:n])
    cycle = ModuleCycle(coords, profile, initial, 1.)
    homed = False
    for i in range(1, 70000):
        old = cycle.target.copy()
        scale = cycle.move.progress/cycle.move.duration if cycle.phase == "homing" else float(homed)
        q = old+bias*scale
        torque = 2.+.15*np.sin(q)  # Gravity changes continuously along the arc.
        probe = cycle.probe
        if probe and probe.phase in {"search", "retreat"}:
            a, s = probe.axis, probe.point.direction
            stop = math.radians(probe.direction.reference_deg-.8*s)
            penetration = s*math.degrees(q[a]-stop)
            if penetration >= 0:
                q[a] = stop
                torque[a] += 1.8*penetration
        q += math.radians(.006*(-1)**i)
        # Small torque noise and isolated low samples while pushing a stop.
        torque += .015*(-1)**i
        if i % 47 == 0:
            torque *= .6
        was_homing = cycle.phase == "homing"
        cycle.step(1+i*.05, q, torque, torque)
        assert np.max(abs(np.rad2deg(cycle.target-old))) <= .5+1e-7
        if was_homing and cycle.phase != "homing":
            homed = True
        if cycle.phase == "finished":
            break
    else:
        pytest.fail(f"did not finish: {cycle.phase}")
    assert homed and len(cycle.results) == 2*n
    for result in cycle.results:
        assert result["outcome"] == "within_reference"
        assert result["deviation_deg"] == pytest.approx(-.8*result["sign"], abs=.02)
        assert result["retreat_deg"] >= .99
        assert result["torque_support_fraction"] >= .8
        assert result["contact_window_s"] >= 1.-1e-9
    parked = np.deg2rad(preset_info(coords.station)["target_joint_deg"])
    np.testing.assert_allclose(cycle.actual, parked+bias, atol=math.radians(.01))


def test_moving_baseline_tracks_load_with_large_absolute_error_then_freezes():
    _, profile = setup()
    p = profile.windows[0].point.model_copy(update={"direction": 1})
    window = LoadContact(p, 1.)
    for i in range(40):
        # 2.8 degrees static following error must not prevent load tracking.
        assert window.observe(i*.05, i*.25, 1.+i*.02, i*.25+2.8) is None
    assert window.baseline > 1.5
    assert window.load_offset_deg == pytest.approx(2.8)
    frozen = None
    candidate = None
    for i in range(1, 45):
        value = window.observe((39+i)*.05, 9.75, 4., 12.55+min(i*.05, 1.5))
        if i == 8:
            frozen = window.baseline
        candidate = value or candidate
    assert candidate and window.baseline == frozen
    assert candidate["actual_deg"] == 9.75 and candidate["torque_median_nm"] == 4.


@pytest.mark.parametrize("transient", [False, True])
def test_contact_at_held_command_is_robust_to_one_torque_glitch(transient):
    _, profile = setup()
    point = profile.windows[0].point.model_copy(update={"direction": 1})
    window = LoadContact(point, 2.)
    window.load_offset_deg = 2.8
    result = None
    for i in range(25):
        load = (5. if i == 10 else 2.) if transient else (0. if i == 10 else 5.)
        result = window.observe(i*.05, 120.+.006*(-1)**i, load, 124.) or result
    assert (result is None) == transient
    if result:
        assert result["torque_floor_nm"] == 0.  # Raw outlier remains in evidence.
        assert result["torque_median_nm"] == 5.


def test_full_range_waits_for_settling_past_old_three_second_deadline():
    coords, profile = setup()
    point = profile.windows[0].point
    move = PositionMove(np.zeros(7), np.zeros(7), 5., settling=PositionSettling.for_point(point))
    for i in range(1, 100):
        q = np.zeros(7); q[0] = math.radians(.8*(-1)**i)
        _, _, done = move.step(coords, point, i*.05, .05, q, "test")
        assert not done
    for i in range(100, 110):
        q = np.zeros(7); q[0] = math.radians(3.36)
        _, _, done = move.step(coords, point, i*.05, .05, q, "test")
    assert done


@pytest.mark.parametrize("phase", ["baseline", "search", "retreat"])
def test_torque_abort_precedes_contact_retreat_and_all_transitions(phase):
    coords, profile = setup()
    point = profile.windows[0].point
    c = ProbeCycle(coords, point, np.zeros(7), 1.)
    c.phase = phase
    loads = np.zeros(7); loads[3] = point.joint_abort_nm[3]
    with pytest.raises(FeedbackError, match="实测力矩达到"):
        c.step(1.05, np.zeros(7), loads, np.zeros(7))
    assert c.candidate is None and c.result is None


def test_missing_feedback_still_stops_without_fixed_mit_error_gate():
    coords, profile = setup()
    point = profile.windows[0].point
    c = ProbeCycle(coords, point, np.zeros(7), 1.)
    with pytest.raises(FeedbackError, match="反馈不连续"):
        c.step(1.2, np.zeros(7), np.zeros(7), np.zeros(7))
    q = np.zeros(7); q[4] = math.radians(6.)
    c.step(1.05, q, np.zeros(7), np.zeros(7))
    assert c.phase != "aborted"


def drive_probe(*, physical_stop=None, jam_retreat=False, dt=.05):
    coords, profile = setup()
    p = profile.windows[0].point
    c = ProbeCycle(coords, p, np.zeros(7), 1.)
    stop = c.direction.reference_deg if physical_stop is None else physical_stop
    for i in range(1, 16000):
        q = c.target.copy()
        tau = np.ones(7)*.7
        if c.phase in {"search", "retreat"} and (p.direction*(math.degrees(q[c.axis])-stop) >= 0
                                                or (jam_retreat and c.phase == "retreat")):
            q[c.axis] = math.radians(stop)
            tau[c.axis] = 2.
        c.step(1+i*dt, q, tau, tau)
        if c.phase == "ready":
            c.start_search(1+i*dt)
        if c.phase == "finished":
            return c
    pytest.fail(f"probe did not finish: {c.phase}")


@pytest.mark.parametrize("dt", [.025, .075, .125])
def test_last_part_of_travel_completes_contact_window_even_after_command_stops(dt):
    coords, profile = setup()
    d = coords.directions[0]
    c = drive_probe(physical_stop=d.reference_deg+d.sign*2.85, dt=dt)
    assert c.result["outcome"] == "within_reference"
    assert abs(c.result["deviation_deg"]) == pytest.approx(2.85)
    assert c.result["contact_window_s"] >= 1.-1e-9
    assert any(row.get("search_hold") for row in c.samples)


@pytest.mark.parametrize("distance", [0., .1])
def test_very_early_stop_releases_only_available_measured_distance(distance):
    coords, _ = setup()
    c = drive_probe(physical_stop=coords.directions[0].sign*distance)
    assert c.result["outcome"] == "early"
    assert c.result["available_retreat_deg"] == pytest.approx(distance)
    assert c.result["retreat_deg"] >= distance-1e-7
    assert abs(math.degrees(c.target[c.axis])) <= .2  # Extra pressure has been unloaded.


def test_blocked_retreat_does_not_advance_or_turn_candidate_into_completed_result():
    with pytest.raises(FeedbackError, match="退让命令|偏差超过"):
        drive_probe(jam_retreat=True)


@pytest.mark.parametrize("joint,direction", [("left_shoulder_roll_joint", 1),
                                           ("left_shoulder_pitch_joint", -1)])
def test_mit_loaded_motion_passes_old_4_8_and_5_degree_cutoffs(joint, direction):
    coords, profile = setup()
    point = next(w.point for w in profile.windows
                 if (w.point.joint, w.point.direction) == (joint, direction))
    c = ProbeCycle(coords, point, np.zeros(7), 1.)
    a = c.axis
    for i in range(1, 1800):
        q = c.target.copy()
        tau = np.ones(7)*.7
        if c.phase == "search":
            # Increasing gravity load creates 7 degrees MIT error without
            # any physical stop. Both the old hold latch and 5 degree abort
            # incorrectly ended this otherwise moving sensor/control model.
            target = direction*math.degrees(c.target[a])
            lag = min(7., max(0., target*.2))
            q[a] -= direction*math.radians(lag)
            tau[a] += lag
        c.step(1+i*.025, q, tau, tau)
        if c.phase == "ready": c.start_search(1+i*.025)
        assert c.phase not in {"retreat", "finished"}
        if direction*math.degrees(q[a]) > 65.:
            assert direction*math.degrees(c.target[a]-q[a]) > 5.
            assert c.feedback_search.hold_since is None
            assert c.window.load_offset_deg == pytest.approx(7.)
            return
    pytest.fail(f"motion ended early: {c.phase}")


@pytest.mark.parametrize("limb", ["left_short_arm", "left_arm"])
def test_yaw_keeps_forearm_down_until_yaw_returns_to_zero(limb):
    coords, profile = setup(limb)
    cycle = ModuleCycle(coords, profile, np.zeros(len(coords.names)), 1.)
    yaw_runs = set()
    for i in range(1, 70000):
        q = cycle.target.copy()
        tau = np.ones(len(q))*.7
        p = cycle.probe
        if p and p.phase in {"search", "retreat"}:
            stop = math.radians(p.direction.reference_deg-.8*p.point.direction)
            if p.point.direction*(q[p.axis]-stop) >= 0:
                q[p.axis] = stop
                tau[p.axis] = 2.
            if p.point.joint == "left_shoulder_yaw_joint":
                yaw_runs.add(p.point.direction)
                assert math.degrees(cycle.target[3]) == pytest.approx(-80.803, abs=.01)
        if cycle.phase == "clearance" and cycle.point.joint == "left_shoulder_yaw_joint":
            assert math.degrees(cycle.target[3]) == pytest.approx(-80.803, abs=.01)
        if cycle.phase == "positioning" and cycle.point.joint == "left_shoulder_yaw_joint":
            assert abs(math.degrees(cycle.target[2])) < .01
        cycle.step(1+i*.05, q, tau, tau)
        if cycle.phase == "finished": break
    assert cycle.phase == "finished" and yaw_runs == {-1, 1}


@pytest.mark.parametrize("limb", ["left_short_arm", "left_arm"])
def test_seven_degree_mit_load_completes_contact_retreat_clearance_and_next_items(limb):
    coords, profile = setup(limb)
    n = len(coords.names)
    cycle = ModuleCycle(coords, profile, np.zeros(n), 1.)
    bias = math.radians(7.)
    phases = set()
    for i in range(1, 70000):
        # This remains present after contact, during unloading/clearance and
        # on held axes. A search-only exemption otherwise hides later stalls.
        q = cycle.target.copy()-bias
        tau = np.ones(n)*2.
        p = cycle.probe
        if p and p.phase in {"search", "retreat"}:
            a, s = p.axis, p.point.direction
            stop = math.radians(p.direction.reference_deg-.8*s)
            penetration = s*math.degrees(q[a]-stop)
            if penetration >= 0:
                q[a] = stop
                tau[a] += 2.*penetration
        phases.add(cycle.phase)
        cycle.step(1+i*.05, q, tau, tau)
        if cycle.phase == "finished":
            break
    assert cycle.phase == "finished"
    assert {"baseline", "search", "retreat", "clearance"} <= phases
    assert len(cycle.results) == 2*n
    assert all(row["outcome"] == "within_reference" for row in cycle.results)
    assert all(row["retreat_deg"] >= .99 for row in cycle.results)
    parked = np.deg2rad(preset_info(coords.station)["target_joint_deg"])
    np.testing.assert_allclose(cycle.actual, parked-np.ones(n)*bias)
