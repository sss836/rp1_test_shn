"""Shared aging parking path in a feedback model, without PLC/CAN handles."""
import math

import numpy as np
import pytest

from factory_hmi.core.limit_arm_completion import ArmCompletion, prepare_arm_completion
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.module_limit_check import ModuleCycle, ModuleWorker, load_module_plan
from factory_hmi.core.mujoco_completion import ARM_LIMBS, preset_info


def setup(limb):
    return load_module_plan(nominal_station(limb), "OFFLINE-PARKING", "test")[:2]


@pytest.mark.parametrize("limb", sorted(ARM_LIMBS))
def test_normal_arm_sequence_lowers_elbow_after_all_results_before_finished(limb):
    coords, profile = setup(limb)
    initial = np.zeros(len(coords.names))
    initial[0] = math.radians(12.)
    initial[3] = math.radians(-30. if limb.startswith("left") else 30.)
    cycle = ModuleCycle(coords, profile, initial, 1.)
    bias = np.zeros(len(initial))
    bias[0] = math.radians(7.)  # No reintroduced all-axis absolute-error gate.
    phases = set()
    first_completion_target = None
    last_completion_target = None
    for i in range(1, 100000):
        old = cycle.target.copy()
        q = old+bias
        torque = np.ones(len(q))*.7
        p = cycle.probe
        if p and p.phase in {"search", "retreat"}:
            a, s = p.axis, p.point.direction
            stop = math.radians(p.direction.reference_deg-.8*s)
            if s*(q[a]-stop) >= 0:
                q[a] = stop
                torque[a] = 2.
        cycle.step(1+i*.025, q, torque, torque)
        if cycle.arm_completion is not None:
            phases.add(cycle.phase)
            assert len(cycle.results) == len(profile.windows)
            if first_completion_target is None:
                first_completion_target = cycle.target.copy()
            if last_completion_target is not None:
                delta = np.rad2deg(cycle.target-last_completion_target)
                assert np.max(abs(delta)) <= .5+1e-7
                assert np.max(abs(delta))/.025 <= 15.+1e-6
            last_completion_target = cycle.target.copy()
        if cycle.phase == "finished":
            break
    assert cycle.phase == "finished" and {"arm_hanging", "arm_settling"} <= phases
    parked = np.deg2rad(preset_info(coords.station)["target_joint_deg"])
    np.testing.assert_allclose(first_completion_target, np.zeros(len(initial)), atol=1e-8)
    np.testing.assert_allclose(cycle.target, parked)
    np.testing.assert_allclose(cycle.actual, parked+bias)
    evidence = cycle.arm_completion.snapshot()
    assert evidence["normal_completed"] and evidence["stable_seconds"] >= 1.-1e-9
    assert evidence["evidence"]["preset_sha256"] == preset_info(coords.station)["preset_sha256"]
    assert evidence["evidence"]["final_error_joint_deg"][0] == pytest.approx(7.)


@pytest.mark.parametrize("limb", sorted(ARM_LIMBS))
@pytest.mark.parametrize("dt", [.025, .075])
def test_shared_path_connects_from_recent_command_without_jump_and_retimes(limb, dt):
    coords, profile = setup(limb)
    command = np.zeros(len(coords.names)); command[0] = math.radians(10.)
    command[3] = math.radians(-20. if limb.startswith("left") else 20.)
    actual = command.copy(); actual[0] += math.radians(7.)
    completion = ArmCompletion(coords, command, actual, 1.)
    previous = command.copy()
    for i in range(1, 5000):
        measured = previous.copy(); measured[0] += math.radians(7.)
        target, done = completion.step(1+i*dt, measured, profile.windows[0].point)
        motor_delta = coords.motor_target(target)-coords.motor_target(previous)
        assert np.max(abs(np.rad2deg(motor_delta))) <= .5+1e-7
        assert np.max(abs(np.rad2deg(target-previous)))/dt <= 15.+1e-6
        previous = target
        if done:
            break
    assert done
    assert completion.duration > completion.plan.duration
    assert completion.evidence["initial_command_joint_rad"] == command.tolist()
    assert completion.evidence["initial_actual_joint_rad"] == actual.tolist()


def test_unsettled_hanging_keeps_target_and_never_becomes_finished_on_elapsed_time():
    coords, profile = setup("left_arm")
    completion = ArmCompletion(coords, np.zeros(7), np.zeros(7), 1.)
    now = 1.
    for _ in range(2000):
        now += .025
        q = completion.target.copy()
        _, done = completion.step(now, q, profile.windows[0].point)
        assert not done
        if completion.commanded_complete:
            break
    parked = completion.target.copy()
    for i in range(200):
        now += .025
        q = parked.copy(); q[3] += math.radians(.8*(-1)**i)
        target, done = completion.step(now, q, profile.windows[0].point)
        assert not done and completion.phase == "arm_settling"
        np.testing.assert_array_equal(target, parked)
    for i in range(50):
        now += .025
        q = parked.copy(); q[0] += math.radians(7.)
        _, done = completion.step(now, q, profile.windows[0].point)
    assert done


def test_plan_failure_holds_recent_command_instead_of_finishing(monkeypatch):
    from factory_hmi.core import limit_arm_completion
    coords, profile = setup("left_arm")
    def bad_plan(*args):
        raise ValueError("offline invalid preset")
    monkeypatch.setattr(limit_arm_completion, "plan_return", bad_plan)
    cycle = ModuleCycle(coords, profile, np.zeros(7), 1.)
    cycle.index, cycle.homing, cycle.via = len(profile.windows), False, iter(())
    cycle._next_waypoint(np.zeros(7), 1.)
    for i in range(1, 100):
        cycle.step(1+i*.025, np.zeros(7), np.zeros(7), np.zeros(7))
        assert cycle.phase == "arm_holding"
        np.testing.assert_array_equal(cycle.target, np.zeros(7))
    assert not cycle.arm_completion.snapshot()["normal_completed"]
    assert "未自动失能" in cycle.arm_completion.alarm


def test_interruption_does_not_schedule_more_hanging_targets():
    coords, profile = setup("right_short_arm")
    completion = ArmCompletion(coords, np.zeros(5), np.zeros(5), 1.)
    completion.step(1.025, np.zeros(5), profile.windows[0].point)
    target = completion.target.copy()
    completion.interrupt("operator stopped")
    new_target, done = completion.step(1.05, target, profile.windows[0].point)
    np.testing.assert_array_equal(new_target, target)
    assert not done and completion.phase == "interrupted"


def test_fault_before_normal_finish_never_starts_hanging():
    coords, profile = setup("left_arm")
    cycle = ModuleCycle(coords, profile, np.zeros(7), 1.)
    torque = np.zeros(7); torque[0] = cycle.transfer_motor[0]
    with pytest.raises(FeedbackError, match="中止力矩"):
        cycle.step(1.025, np.zeros(7), torque, torque)
    assert cycle.arm_completion is None


@pytest.mark.parametrize("limb", ["left_short_leg", "right_short_leg", "waist_hip"])
def test_nonarm_normal_completion_retains_its_home_and_never_uses_arm_resource(limb):
    coords, profile = setup(limb)
    cycle = ModuleCycle(coords, profile, np.deg2rad(profile.home_deg), 1.)
    for i in range(1, 100000):
        q = cycle.target.copy(); torque = np.ones(len(q))*.7
        p = cycle.probe
        if p and p.phase in {"search", "retreat"}:
            a, s = p.axis, p.point.direction
            stop = math.radians(p.direction.reference_deg-.8*s)
            if s*(q[a]-stop) >= 0:
                q[a] = stop; torque[a] = 2.
        cycle.step(1+i*.025, q, torque, torque)
        assert cycle.arm_completion is None
        if cycle.phase == "finished":
            break
    assert cycle.phase == "finished"
    np.testing.assert_allclose(cycle.target, np.deg2rad(profile.home_deg))


def test_worker_prepares_shared_plan_before_start_and_does_not_replan_after_fourteen_checks(monkeypatch, tmp_path):
    from factory_hmi.core import limit_arm_completion
    coords, profile, context = load_module_plan(nominal_station("left_arm"), "OFFLINE-PARKING", "test")
    calls = []
    original_plan = limit_arm_completion.plan_return
    original_retiming = limit_arm_completion._retiming_scale

    def plan(*args, **kwargs):
        calls.append("plan")
        return original_plan(*args, **kwargs)

    def retime(*args, **kwargs):
        calls.append("retime")
        return original_retiming(*args, **kwargs)

    monkeypatch.setattr(limit_arm_completion, "plan_return", plan)
    monkeypatch.setattr(limit_arm_completion, "_retiming_scale", retime)
    worker = ModuleWorker(object(), coords, profile, context, None, None, None,
                          tmp_path / "not-written.json", np.zeros(7))
    assert calls == ["plan", "retime"]
    assert not worker.started and worker.cycle is None
    assert worker._prepared_arm_completion.evidence["preparation_phase"] == "before_motion_execution"

    def start_without_control_thread():
        assert calls == ["plan", "retime"]
        calls.append("start")

    monkeypatch.setattr(worker.thread, "start", start_without_control_thread)
    worker.start()  # No CAN, PLC, or control thread is started in this test.

    def forbidden(*args, **kwargs):
        raise AssertionError("Expensive completion planning ran after worker.start")

    monkeypatch.setattr(limit_arm_completion, "plan_return", forbidden)
    monkeypatch.setattr(limit_arm_completion, "_retiming_scale", forbidden)
    cycle = worker._new_cycle(np.zeros(7), 1.)
    assert cycle._prepared_arm_completion is worker._prepared_arm_completion
    first_tail_target = None
    for i in range(1, 100000):
        before = cycle.target.copy()
        q = before.copy()
        torque = np.ones(7)*.7
        p = cycle.probe
        if p and p.phase in {"search", "retreat"}:
            a, s = p.axis, p.point.direction
            stop = math.radians(p.direction.reference_deg-.8*s)
            if s*(q[a]-stop) >= 0:
                q[a] = stop
                torque[a] = 2.
        cycle.step(1+i*.025, q, torque, torque)
        if cycle.arm_completion is not None and first_tail_target is None:
            first_tail_target = cycle.target.copy()
            assert len(cycle.results) == 14
            np.testing.assert_allclose(first_tail_target, before, atol=1e-10)
        if cycle.phase == "finished":
            break
    assert first_tail_target is not None and cycle.phase == "finished"
    assert calls == ["plan", "retime", "start"]


def test_prepared_template_activations_have_independent_mutable_state():
    coords, profile = setup("left_arm")
    command = np.zeros(7)
    prepared = prepare_arm_completion(coords, command)
    first = prepared.activate(coords, command, command, 1.)
    second = prepared.activate(coords, command, command, 20.)
    first.step(1.025, command, profile.windows[0].point)
    first.stable.append((1.025, command.copy(), command.copy()))
    first.evidence["model_source"]["independence_test"] = True
    first.target[0] = .01
    assert second.progress == 0. and second.previous_at == 20.
    assert not second.stable and "independence_test" not in second.evidence["model_source"]
    assert second.target[0] == 0.
    np.testing.assert_array_equal(prepared.command, command)
    assert not prepared.command.flags.writeable and not prepared.goal.flags.writeable
    for field in ("joint_positions", "timestamps", "bounds", "start_motor", "initial_velocity"):
        assert not getattr(prepared.plan, field).flags.writeable


def test_prepared_binding_change_holds_recent_command_without_replanning(monkeypatch):
    from factory_hmi.core import limit_arm_completion
    coords, profile = setup("left_short_arm")
    prepared = prepare_arm_completion(coords, np.zeros(5))

    def forbidden(*args, **kwargs):
        raise AssertionError("Changed binding must hold rather than replan in a frame")

    monkeypatch.setattr(limit_arm_completion, "plan_return", forbidden)
    monkeypatch.setattr(limit_arm_completion, "_retiming_scale", forbidden)
    command = np.ones(5)*.01
    changed = prepared.activate(coords, command, command, 1.)
    target, done = changed.step(1.025, command, profile.windows[0].point)
    np.testing.assert_array_equal(target, command)
    assert not done and changed.phase == "arm_holding"
    assert "绑定已变化" in changed.alarm
    replaced_config, _ = setup("left_short_arm")
    assert not prepared.matches(replaced_config, np.zeros(5))
