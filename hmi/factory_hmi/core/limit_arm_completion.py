"""Shared aging-arm parking trajectory for normal limit-check completion.

No motor handles or bus operations live here. A normal module worker keeps
dispatching bounded targets until fresh measured feedback is settled; only
then may its existing shutdown path request disable.
"""
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
import math
import time

import numpy as np

from .live_limit_probe import advance_motion
from .mujoco_completion import ARM_LIMBS, SPEED_TOLERANCE, STABLE_SECONDS, plan_return


def _retiming_scale(coords, plan, speed_deg_s):
    """Expensive preflight sampling; never run this in a control frame."""
    times = np.linspace(0., plan.duration, max(1001, int(plan.duration*200)+1))
    motors = np.array([plan.at(t) for t in times])
    joints = np.array([coords.joint_position(row) for row in motors])
    dt = np.diff(times)[:, None]
    motor_peak = np.max(abs(np.diff(motors, axis=0)/dt), axis=0)
    joint_peak = float(np.max(abs(np.diff(joints, axis=0)/dt)))
    limit = math.radians(speed_deg_s)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError("手臂缓降目标速度无效")
    return max(1., float(np.max(motor_peak))/limit, joint_peak/limit)


def _readonly(values):
    value = np.array(values, dtype=float, copy=True)
    value.setflags(write=False)
    return value


@dataclass(frozen=True)
class PreparedArmCompletion:
    config: object
    names: tuple
    identity: tuple
    command: np.ndarray
    goal: np.ndarray
    plan: object
    scale: float
    duration: float
    speed_deg_s: float
    evidence: dict
    alarm: str | None

    def matches(self, coords, command):
        return (coords.station is self.config and coords.names == self.names
                and coords.identity == self.identity and np.shape(command) == self.command.shape
                and np.allclose(command, self.command, rtol=0., atol=1.e-10))

    def activate(self, coords, command, actual, now):
        return ArmCompletion(coords, command, actual, now, prepared=self)


def prepare_arm_completion(coords, command, *, speed_deg_s=15.):
    """Compile the immutable shared path while the old hold is still active."""
    if coords.station.limb not in ARM_LIMBS:
        raise ValueError("当前模块没有手臂下垂收尾")
    began = time.perf_counter()
    goal = np.array(command, dtype=float, copy=True)
    plan, scale, duration, alarm = None, 1., 0., None
    evidence = {}
    try:
        start_motor = coords.motor_target(command)
        plan = plan_return(coords.station, start_motor, np.zeros(len(command)))
        goal = coords.joint_position(plan.target_motor)
        # Aging's resource is sampled at 200Hz; the limit worker is slower.
        scale = _retiming_scale(coords, plan, speed_deg_s)
        duration = plan.duration*scale
        evidence = deepcopy(plan.evidence())
        evidence.update(
            original_aging_timeout_seconds=evidence.get("timeout_seconds"),
            original_aging_confirmation_scope=evidence.get("confirmation_scope"),
            confirmation_scope="operator_requested_shared_aging_completion_for_limit_tests",
            timeout_seconds=None, planned_return_seconds=duration,
            original_aging_return_seconds=plan.duration,
            retiming_scale=scale, target_speed_deg_s=speed_deg_s,
            position_error_policy="record_only_mit_load_deflection",
            settling_policy="fresh_position_velocity_and_one_second_stationary_window",
            stable_angle_span_deg=.2, prepared_command_joint_rad=np.asarray(command).tolist())
        # ReturnPlan.at() only reads these arrays. Active instances own their
        # mutable progress/targets/windows independently of the compiled path.
        for value in (plan.joint_positions, plan.timestamps, plan.bounds,
                      plan.start_motor, plan.initial_velocity):
            value.setflags(write=False)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        plan = None
        alarm = "手臂下垂收尾无法接续，保持最近目标，未自动失能："+str(exc)
        evidence.update(source="mujoco_hanging", connection_error=str(exc),
                        automatic_disable_requested=False)
    evidence["preparation_seconds"] = time.perf_counter()-began
    evidence["preparation_phase"] = "before_motion_execution"
    return PreparedArmCompletion(coords.station, coords.names, coords.identity,
        _readonly(command), _readonly(goal), plan, scale, duration, speed_deg_s,
        evidence, alarm)


class ArmCompletion:
    def __init__(self, coords, command, actual, now, *, speed_deg_s=15., prepared=None):
        prepared = prepared if prepared is not None else prepare_arm_completion(
            coords, command, speed_deg_s=speed_deg_s)
        self.coords = coords
        self.started_at = now
        self.previous_at = now
        self.previous_actual = actual.copy()
        self.actual = actual.copy()
        self.target = command.copy()
        self.goal = prepared.goal.copy()
        self.progress = 0.
        self.duration = prepared.duration
        self.scale = prepared.scale
        self.finished = False
        self.cancelled = False
        self.alarm = prepared.alarm
        self.plan = prepared.plan
        self.stable = deque()
        self.stable_seconds = 0.
        self.actual_speed = np.zeros(len(command))
        self.speed_deg_s = prepared.speed_deg_s
        self.evidence = deepcopy(prepared.evidence)
        self.actual_elapsed = 0.
        if not prepared.matches(coords, command):
            self.plan = None
            self.goal = command.copy()
            self.duration = 0.
            self.alarm = "手臂预计算收尾绑定已变化，保持最近目标，未自动失能"
            self.evidence.update(binding_error=self.alarm, automatic_disable_requested=False)
        self.phase = "arm_holding" if self.plan is None else "arm_hanging"
        self.evidence.update(initial_command_joint_rad=command.tolist(),
                             initial_actual_joint_rad=actual.tolist())

    @property
    def commanded_complete(self):
        return self.plan is not None and self.progress >= self.duration

    def _pose(self, elapsed):
        return self.coords.joint_position(self.plan.at(elapsed/self.scale))

    def step(self, now, actual, point):
        dt = now-self.previous_at
        self.actual_speed = (actual-self.previous_actual)/dt
        self.previous_actual = actual.copy()
        self.previous_at = now
        self.actual = actual.copy()
        self.actual_elapsed = now-self.started_at
        if self.plan is None or self.cancelled:
            return self.target.copy(), False
        if not self.commanded_complete:
            proposed = min(self.duration, self.progress+dt)
            # Uniform retiming bounds speed; adaptive time progress additionally
            # honours this station's motor frame and velocity constraints.
            speed_point = point.model_copy(update={
                "motor_step_deg": min(point.motor_step_deg, self.speed_deg_s*dt)})
            self.progress = advance_motion(self.coords, speed_point, dt, self._pose, self.progress, proposed)
            self.target = self._pose(self.progress)
            self.plan.check_target(self.coords.motor_target(self.target))
            self.stable.clear()
            self.stable_seconds = 0.
            if self.commanded_complete:
                self.phase = "arm_settling"
            return self.target.copy(), False
        speed_ok = bool(np.all(abs(self.actual_speed) <= SPEED_TOLERANCE))
        if not speed_ok:
            self.stable.clear()
            self.stable_seconds = 0.
            return self.target.copy(), False
        self.stable.append((now, actual.copy(), self.actual_speed.copy()))
        while len(self.stable)>1 and now-self.stable[1][0]>=STABLE_SECONDS:
            self.stable.popleft()
        self.stable_seconds = now-self.stable[0][0]
        span = np.ptp([row[1] for row in self.stable], axis=0)
        if self.stable_seconds+1e-9 < STABLE_SECONDS or np.max(span)>math.radians(.2):
            return self.target.copy(), False
        self.finished = True
        self.phase = "completed"
        self.evidence.update(
            final_command_joint_rad=self.target.tolist(),
            final_actual_joint_rad=actual.tolist(),
            final_error_joint_deg=np.rad2deg(actual-self.target).tolist(),
            actual_return_seconds=self.actual_elapsed,
            stable_seconds_observed=self.stable_seconds,
            stable_angle_span_deg_observed=np.rad2deg(span).tolist(),
            stable_speed_rad_s_observed=np.max(abs(np.array([row[2] for row in self.stable])), axis=0).tolist(),
            stable_fresh_samples=len(self.stable))
        return self.target.copy(), True

    def interrupt(self, reason):
        if self.finished:
            return
        self.cancelled = True
        self.phase = "interrupted"
        self.alarm = reason
        self.evidence.update(interruption_reason=reason, automatic_disable_requested=False)

    def snapshot(self):
        return {"required": True, "source": "mujoco_hanging", "phase": self.phase,
                "active": not self.finished and not self.cancelled,
                "normal_completed": self.finished, "alarm": self.alarm,
                "progress": self.progress/self.duration if self.duration else 0.,
                "planned_seconds": self.duration, "actual_seconds": self.actual_elapsed,
                "stable_seconds": self.stable_seconds,
                "target_speed_deg_s": self.speed_deg_s,
                "target_joint_deg": np.rad2deg(self.goal).tolist(),
                "actual_joint_deg": np.rad2deg(self.actual).tolist(),
                "actual_speed_rad_s": self.actual_speed.tolist(),
                "target_error_deg": np.rad2deg(self.actual-self.goal).tolist(),
                "evidence": dict(self.evidence)}
