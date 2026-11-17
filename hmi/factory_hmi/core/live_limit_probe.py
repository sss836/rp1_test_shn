"""Station-owned, one-direction limit block inspection for 5-DoF legs."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import statistics
import threading
import time
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .limit_block_check import ContactWindow, Coordinates, FeedbackError, SETTINGS

PILOT_LIMBS = {"left_short_leg", "right_short_leg"}
PROFILE_ROOT = Path("/etc/rp1-factory-hmi/limit-probe.d")
POS = Annotated[float, Field(gt=0, allow_inf_nan=False)]
VEC = Annotated[tuple[Annotated[float, Field(allow_inf_nan=False)], ...], Field(min_length=2, max_length=7)]
POS_VEC = Annotated[tuple[POS, ...], Field(min_length=2, max_length=7)]


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    joint: str
    direction: Literal[-1, 1]
    staging_pose_deg: VEC
    search_pose_deg: VEC
    permitted_travel_deg: Annotated[float, Field(gt=0, le=12)]
    search_speed_deg_s: Annotated[float, Field(gt=0, le=1)]
    positioning_speed_deg_s: Annotated[float, Field(gt=0, le=5)]
    reset_kp_scale: Annotated[float, Field(gt=0, le=1)]
    reset_kd_scale: Annotated[float, Field(ge=0, le=1)]
    search_kp_scale: Annotated[float, Field(gt=0, le=1)]
    search_kd_scale: Annotated[float, Field(ge=0, le=1)]
    motor_abort_nm: POS_VEC
    joint_abort_nm: POS_VEC
    contact_floor_nm: POS
    contact_rise_nm: POS
    following_error_deg: Annotated[float, Field(gt=.2, le=2)]
    motor_step_deg: Annotated[float, Field(gt=0, le=.5)]
    retreat_deg: Annotated[float, Field(ge=.5, le=2)]
    comparison_tolerance_deg: Annotated[float, Field(gt=0, le=3)]

    @model_validator(mode="after")
    def bounded(self):
        if self.following_error_deg <= self.search_speed_deg_s * SETTINGS.stall_s + .05:
            raise ValueError("跟踪门限未留出一秒停滞窗口的最大指令推进量")
        return self


class ProbeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal[1]
    limb: Literal["left_short_leg", "right_short_leg"]
    model: Literal["RP1.3.4"]
    power_source: Literal["external"]
    fixture: Annotated[str, Field(min_length=1, max_length=120)]
    approved_by: Annotated[str, Field(min_length=1, max_length=120)]
    revision: Annotated[str, Field(min_length=1, max_length=80)]
    station_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    points: Annotated[tuple[ProbePoint, ...], Field(min_length=1, max_length=10)]
    joint_peak_nm: POS_VEC | None = None


def _sha(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def station_sha(config) -> str:
    # Effective mappings include live bus overrides, signs, zeros, models and MIT gains.
    return _sha({"limb": config.limb, "entries": [asdict(e) for e in config.entries],
                 "close_chain": config.raw.get("close_chain_motor_idx")})


def read_site_profile(path):
    try:
        directory = path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != 0 or directory.st_mode & 0o022:
            raise ValueError("真机限位方案目录须由管理员维护")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("真机限位方案须为管理员所有且不可被普通用户写入")
            payload = file.read(65537)
    except FileNotFoundError as exc:
        raise ValueError(f"当前模块尚未安装核定的工位方案：{path}") from exc
    if len(payload) > 65536:
        raise ValueError("工位限位方案过大")
    return payload


def load_site_profile(config) -> ProbeProfile:
    if config.limb not in PILOT_LIMBS:
        raise ValueError("单方向真机试点仅开放 5 自由度单腿")
    profile = ProbeProfile.model_validate_json(read_site_profile(PROFILE_ROOT / (config.limb + ".json")))
    if profile.limb != config.limb or profile.station_sha256 != station_sha(config):
        raise ValueError("工位方案的机型或电机映射摘要与当前配置不一致")
    if len({(p.joint, p.direction) for p in profile.points}) != len(profile.points):
        raise ValueError("同一关节方向在工位方案中重复")
    return profile


def point_description(config, profile, point, *, coords=None):
    coords = coords or Coordinates(config)
    direction = next((d for d in coords.directions if (d.joint, d.sign) == (point.joint, point.direction)), None)
    if direction is None:
        raise ValueError("核定方向不在当前模块的具名关节内")
    index, sign = direction.index, direction.sign
    staging, start = np.asarray(point.staging_pose_deg), np.asarray(point.search_pose_deg)
    count = len(coords.names)
    if any(len(v) != count for v in (staging, start, point.motor_abort_nm, point.joint_abort_nm)):
        raise ValueError("核定角度与力矩向量必须对应工位的具名关节顺序")
    if profile.joint_peak_nm is not None:
        if len(profile.joint_peak_nm) != count:
            raise ValueError("峰值力矩必须对应工位的具名关节顺序")
        for name, cutoff, peak in zip(coords.names, point.joint_abort_nm, profile.joint_peak_nm, strict=True):
            if cutoff > peak:
                raise ValueError(f"{name} 的中止力矩 {cutoff:g} N·m 超过该关节峰值 {peak:g} N·m")
    inactive = np.arange(count) != index
    if np.any(np.abs(staging[inactive] - start[inactive]) > 1e-8):
        raise ValueError("复位路径只允许当前逻辑关节改变")
    if abs(staging[index] - start[index]) > 12:
        raise ValueError("复位起点与搜索起点相隔超过试点的 12° 上限")
    for axis in range(count):
        bounds = tuple(d.reference_deg for d in coords.directions if d.index == axis)
        lower, upper = bounds or (-164., 164.)  # Fixed hip-pitch axes in the waist station.
        if not (lower < staging[axis] < upper and lower < start[axis] < upper):
            raise ValueError("复位起点和搜索姿态须位于量产参考区间内")
    reference_travel = sign * (direction.reference_deg - start[index])
    beyond = point.permitted_travel_deg - reference_travel
    if reference_travel <= 0 or not point.search_speed_deg_s * SETTINGS.stall_s + .05 < beyond <= 3.0 + 1e-8:
        raise ValueError("搜索起点须在参考位置内侧，参考外指令预算须可容纳判定窗口且不超过 3°")
    if reference_travel + 1e-8 < point.retreat_deg:
        raise ValueError(f"{direction.label} {direction.label_direction} 的搜索起点距参考角 "
                         f"{reference_travel:.3g}°，不足以缓退 {point.retreat_deg:g}°")
    # The absolute baseline is nonnegative. Its actual value is measured on site.
    if max(point.contact_floor_nm, point.contact_rise_nm) >= point.joint_abort_nm[index]:
        raise ValueError("配置的候选力矩下限已达到关节中止门限")
    return {"fixture": profile.fixture, "revision": profile.revision, "approved_by": profile.approved_by,
            "reference_deg": direction.reference_deg, "label": direction.label,
            "direction": direction.label_direction, "point": point.model_dump(),
            "joint_peak_nm": profile.joint_peak_nm,
            "motors": [{"joint": e.joint_name, "bus": e.interface, "id": e.motor_id,
                        "reset_kp": e.kp * point.reset_kp_scale, "reset_kd": e.kd * point.reset_kd_scale,
                        "search_kp": e.kp * point.search_kp_scale, "search_kd": e.kd * point.search_kd_scale}
                       for e in coords.entries]}


class PositionSettling:
    def __init__(self):
        self.window = deque()

    def settled(self, now, q, target):
        if np.max(abs(q - target)) > math.radians(.12):
            self.window.clear()
            return False
        self.window.append((now, q.copy()))
        while len(self.window) > 1 and now - self.window[1][0] >= .20:
            self.window.popleft()
        duration = now - self.window[0][0]
        if duration < .20:
            return False
        span = np.ptp([row[1] for row in self.window], axis=0)
        return bool(np.max(span) <= math.radians(.15) * duration)


def motor_step_cap(coords, point, dt):
    limits = coords.station.safety_limits
    cap = math.radians(point.motor_step_deg)
    if limits.max_frame_delta_rad is not None:
        cap = np.minimum(cap, limits.max_frame_delta_rad)
    if dt is not None and limits.max_velocity_rad_s is not None:
        cap = np.minimum(cap, np.asarray(limits.max_velocity_rad_s) * dt)
    return cap


def check_torque(coords, joint_tau, motor_tau, joint_limits, motor_limits, message):
    for values, limits, kind in ((motor_tau, motor_limits, "电机"), (joint_tau, joint_limits, "关节")):
        trips = np.flatnonzero(abs(values) >= limits)
        if len(trips):
            i = trips[0]
            raise FeedbackError(f"{message}，{coords.names[i]} {kind} "
                                f"实测 {values[i]:.3g} N·m，门限 {limits[i]:g} N·m")


def advance_motion(coords, point, dt, pose, progress, proposed):
    """Advance along the planned path as far as the current motor frame permits."""
    last = coords.motor_target(pose(progress))
    cap = motor_step_cap(coords, point, dt)
    if np.all(abs(coords.motor_target(pose(proposed)) - last) <= cap):
        return proposed
    low, high = progress, proposed
    for _ in range(24):
        middle = (low + high) / 2
        if np.all(abs(coords.motor_target(pose(middle)) - last) <= cap):
            low = middle
        else:
            high = middle
    if low == progress:
        raise FeedbackError("按工位核定的电机单步和速度限制，本段无法继续推进")
    return low


class PositionMove:
    def __init__(self, origin, goal, speed, *, gain_transition=False):
        self.origin, self.goal = origin.copy(), goal.copy()
        self.duration = max(.5 if gain_transition else 0.,
                            1.875 * np.max(abs(np.rad2deg(goal - origin))) / speed)
        self.progress = 0.
        self.commanded_at = None
        self.settling = PositionSettling()

    def pose(self, elapsed):
        x = elapsed / self.duration if self.duration else 1.
        curve = x**3 * (10 - 15*x + 6*x*x)
        return self.origin + (self.goal - self.origin) * curve

    def step(self, coords, point, now, dt, q, timeout_message):
        if self.commanded_at is None:
            self.progress = advance_motion(coords, point, dt, self.pose, self.progress,
                                           min(self.duration, self.progress + dt))
            if self.progress == self.duration:
                self.commanded_at = now
        target = self.pose(self.progress)
        ratio = self.progress / self.duration if self.duration else 1.
        if self.commanded_at is None:
            return target, ratio, False
        settled = self.settling.settled(now, q, self.goal)
        if not settled and now - self.commanded_at > 3.:
            raise FeedbackError(timeout_message)
        return target, ratio, settled


class ProbeCycle:
    """Pure state machine shared by the live executor and simulated evidence."""
    def __init__(self, coords, point, q, now, *, target=None):
        self.coords, self.point = coords, point
        self.axis = coords.names.index(point.joint)
        self.direction = next(d for d in coords.directions if d.joint == point.joint and d.sign == point.direction)
        self.start = np.deg2rad(point.search_pose_deg)
        staging = np.deg2rad(point.staging_pose_deg)
        if min(np.max(abs(q - staging)), np.max(abs(q - self.start))) > math.radians(.25):
            raise FeedbackError("实测姿态不在核定复位起点或搜索起点范围内")
        self.actual = q.copy()
        self.target = q.copy() if target is None else target.copy()
        self.reset = PositionMove(self.target, self.start, point.positioning_speed_deg_s)
        self.phase = "reset"
        self.since = now
        self.previous = now
        self.gain_blend = 0.
        self.settling = PositionSettling()
        self.progress = 0.
        self.commanded_at = None
        self.recent_load = []
        self.trace = []
        self.retreat = None
        self.candidate = None
        self.result = None
        self.samples = deque(maxlen=5000)
        self.sample_count = 0
        self.samples_started = now

    def _phase(self, name, now):
        self.phase, self.since = name, now
        self.settling.window.clear()
        self.progress = 0.
        self.commanded_at = None

    def start_search(self, now):
        if self.phase != "ready":
            raise ValueError("请先完成复位并确认到位，再开始搜索")
        self._phase("baseline", now)

    @property
    def gains(self):
        p, x = self.point, self.gain_blend
        return (p.reset_kp_scale * (1-x) + p.search_kp_scale * x,
                p.reset_kd_scale * (1-x) + p.search_kd_scale * x)

    def step(self, now, q, joint_tau, motor_tau):
        p, a, s = self.point, self.axis, self.point.direction
        elapsed, dt = now - self.since, now - self.previous
        if not (0 < dt <= SETTINGS.max_feedback_age_s):
            raise FeedbackError("检查中逐电机反馈不连续")
        check_torque(self.coords, joint_tau, motor_tau, p.joint_abort_nm, p.motor_abort_nm,
                     "实测力矩达到工位中止门限")
        if np.max(abs(np.rad2deg(q - self.target))) > p.following_error_deg:
            raise FeedbackError("实际姿态与最近指令偏差超过核定值")
        if np.max(abs(np.rad2deg(np.delete(q - self.start, a)))) > .35:
            raise FeedbackError("非搜索轴离开核定保持位")
        self.samples.append({"time_s": now - self.samples_started, "phase": self.phase,
            "actual_deg": np.rad2deg(q).tolist(), "target_deg": np.rad2deg(self.target).tolist(),
            "joint_nm": joint_tau.tolist(), "motor_nm": motor_tau.tolist(), "kp_kd_scale": self.gains})
        self.sample_count += 1
        if self.phase == "reset":
            self.target, _, settled = self.reset.step(
                self.coords, p, now, dt, q, "目标时程已发完，复位实测到位未确认")
            if settled:
                self._phase("ready", now)
        elif self.phase == "ready":
            if elapsed > 30:
                raise FeedbackError("等待人工开始超时")
        elif self.phase == "baseline":
            # Blend while holding the measured start, then acquire load at search gains.
            self.gain_blend = min(elapsed / .5, 1.)
            if elapsed >= .5 and self.settling.settled(now, q, self.start):
                self.recent_load.append((now, abs(joint_tau[a])))
                if now - self.recent_load[0][0] >= .5 and len(self.recent_load) >= 6:
                    baseline = statistics.median(row[1] for row in self.recent_load[-25:])
                    settings = replace(SETTINGS, min_torque_nm=p.contact_floor_nm, rise_nm=p.contact_rise_nm)
                    window = ContactWindow(self.direction, baseline, settings=settings)
                    if window.threshold_nm >= p.joint_abort_nm[a]:
                        raise FeedbackError(
                            f"实测候选力矩门限 {window.threshold_nm:.3g} N·m 达到关节中止门限 "
                            f"{p.joint_abort_nm[a]:.3g} N·m，搜索未开始"
                        )
                    self.window = window
                    self.trace = [(now, q.copy())]
                    self._phase("search", now)
            else:
                self.recent_load.clear()
            if elapsed > 3:
                raise FeedbackError("搜索增益切换后未取得稳定基线")
        elif self.phase == "search":
            self.trace.append((now, q.copy()))
            self.candidate = self.window.observe(now, math.degrees(q[a]), joint_tau[a], math.degrees(self.target[a]))
            cap = math.radians(p.permitted_travel_deg)
            displacement = s * (q[a] - self.start[a])
            if displacement > cap + math.radians(.12):
                raise FeedbackError("实际搜索姿态超过核定行程")
            if self.candidate is not None or self.progress >= cap:
                self.unload_from = self.target.copy()
                self.unload_to = self.start.copy()
                self.unload_to[a] = s * min(s * self.target[a], s * q[a])
                self._phase("unload", now)
            else:
                def search_pose(travel):
                    pose = self.start.copy()
                    pose[a] += s * travel
                    return pose
                proposed = min(cap, self.progress + math.radians(dt * p.search_speed_deg_s))
                self.progress = advance_motion(self.coords, p, dt, search_pose, self.progress, proposed)
                self.target = search_pose(self.progress)
        elif self.phase == "unload":
            # Freeze forward progression immediately. Unload the existing
            # tracking offset in bounded reverse steps before replaying feedback.
            self.trace.append((now, q.copy()))
            delta = self.unload_to - self.unload_from
            distance = np.max(abs(np.rad2deg(delta)))
            speed = min(p.positioning_speed_deg_s, 2*p.search_speed_deg_s)
            fraction = min(1., self.progress + dt * speed / distance) if distance else 1.
            def unload_pose(progress):
                return self.unload_from + delta * progress
            self.progress = advance_motion(self.coords, p, dt, unload_pose, self.progress, fraction)
            self.target = unload_pose(self.progress)
            if self._motion_settled(now, q, self.progress == 1., "撤掉推进后实际姿态未停稳"):
                self._plan_retreat(now, q)
        elif self.phase == "retreat":
            t, path = self.retreat
            def retreat_pose(progress):
                return np.array([np.interp(progress, t, col) for col in path.T])
            self.progress = advance_motion(self.coords, p, dt, retreat_pose, self.progress,
                                           min(t[-1], self.progress + dt))
            self.target = retreat_pose(self.progress)
            if self._motion_settled(now, q, self.progress == t[-1], "缓退终点反馈未确认"):
                distance = s * math.degrees(self.retreat_origin_angle - q[a])
                if distance < p.retreat_deg - .12:
                    raise FeedbackError("未确认实退距离")
                evidence = self.candidate or {}
                delta = evidence["actual_deg"] - self.direction.reference_deg if evidence else None
                verdict = ("not_detected" if delta is None else
                           "early" if s * delta < -p.comparison_tolerance_deg else
                           "late" if s * delta > p.comparison_tolerance_deg else "within_reference")
                self.result = {"outcome": verdict, "deviation_deg": delta, "retreat_deg": distance, **evidence}
                self._phase("finished", now)
        self.previous, self.actual = now, q.copy()

    def _motion_settled(self, now, q, complete, timeout_message):
        if not complete:
            return False
        if self.commanded_at is None:
            self.commanded_at = now
        settled = self.settling.settled(now, q, self.target)
        if not settled and now - self.commanded_at > 3.:
            raise FeedbackError(timeout_message)
        return settled

    def _plan_retreat(self, now, q):
        a, s = self.axis, self.point.direction
        distance = math.radians(self.point.retreat_deg)
        poses = [self.target.copy()]
        times = [0.]
        for _, prior in reversed(self.trace[:-1]):
            if self.coords.station.limb == "waist_hip":
                prior = prior.copy()
                prior[2:] = self.start[2:]
            delta = prior - poses[-1]
            if np.max(abs(delta)) < 1.e-10:
                continue
            if s * delta[a] > math.radians(.10):
                raise FeedbackError("实测退路含较大逆向变化")
            end_distance = s * (q[a] - prior[a])
            if end_distance >= distance:
                fraction = (s * (q[a] - poses[-1][a]) - distance) / (s * delta[a])
                prior = poses[-1] + np.clip(fraction, 0., 1.) * delta
            # Bound reverse speed by the approved search speed.
            times.append(times[-1] + max(.02, np.max(abs(np.rad2deg(prior - poses[-1]))) /
                                         self.point.search_speed_deg_s))
            poses.append(prior)
            if end_distance >= distance:
                self.retreat_origin_angle = q[a]
                self.retreat = (np.asarray(times), np.asarray(poses))
                self._phase("retreat", now)
                return
        raise FeedbackError("实测退路不足，已取消后续动作")



class ProbeWorker:
    """Exclusive command owner, with a distinct cancellation path for safety RPCs."""
    def __init__(self, backend, coords, point, report_context, read, limits, on_samples):
        self.backend, self.coords, self.point = backend, coords, point
        self.context, self.read, self.limits, self.on_samples = report_context, read, limits, on_samples
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True, name="limit-probe")
        self.cycle = None
        self.reason = ""
        self.disable = {"request": "pending", "feedback": "unknown"}
        self.started = False

    def start(self):
        self.thread.start()
        self.started = True

    @property
    def active(self):
        return self.thread.is_alive() or (self.started and self.disable["request"] == "failed")

    def request_stop(self):
        self.stop_event.set()

    def stop(self, timeout=2.0):
        self.request_stop()
        if self.thread.ident is not None and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("限位搜索线程尚未退出")
        if self.disable["request"] == "failed":
            self._disable()
            if self.disable["request"] == "failed":
                raise RuntimeError("限位搜索的软件失能命令尚未确认执行")
        return []

    def begin(self):
        with self.lock:
            if self.cycle is None or self.stop_event.is_set() or not self.thread.is_alive():
                raise ValueError("限位搜索复位未就绪")
            self.cycle.start_search(time.monotonic())

    def snapshot(self, *, include_samples=False):
        with self.lock:
            c = self.cycle
            base = {"active": self.active, "moving": self.active, "phase": c.phase if c else "aborted" if self.reason else "waiting_feedback",
                    "fault": self.reason or None, "disable": dict(self.disable),
                    "result": dict(c.result) if c and c.result else None,
                    "positions_rad": c.actual.tolist() if c else None}
            if include_samples:
                base.update(context=self.context, calibration_passed=None,
                            samples=list(c.samples) if c else [])
                if isinstance(c, ProbeCycle):
                    base.update(candidate=c.candidate, samples_truncated=c.sample_count > len(c.samples))
            return base

    def _disable(self):
        try:
            self.backend.disable_motors()
            self.disable = {"request": "sent", "feedback": self.backend.disable_feedback()}
        except Exception as exc:
            self.disable = {"request": "failed", "feedback": "unknown", "reason": str(exc)}

    def _new_cycle(self, q, now):
        return ProbeCycle(self.coords, self.point, q, now)

    def _record_feedback(self, samples):
        if self.cycle.samples:
            self.cycle.samples[-1]["drive_feedback"] = [row.to_dict() for row in samples]

    def _record_end(self):
        pass

    def _run(self):
        stamp = ()
        last_cohort = time.monotonic()
        last_motor = None
        last_command = None
        try:
            while not self.stop_event.is_set():
                samples = self.read()
                now = time.monotonic()
                if not samples or (stamp and not all(
                        s.timestamp_s > old for s, old in zip(samples, stamp, strict=True))):
                    if now - last_cohort >= SETTINGS.max_feedback_age_s:
                        raise FeedbackError("没有收到完整的新鲜逐电机反馈")
                    self.stop_event.wait(.008)
                    continue
                if now - last_cohort >= SETTINGS.max_feedback_age_s:
                    raise FeedbackError("逐电机反馈超过允许间隔")
                q, tau, stamp = self.coords.decode(samples, now, stamp)
                self.on_samples(samples)
                last_cohort = now
                motor_tau = np.asarray([s.torque_nm for s in samples])
                with self.lock:
                    if self.cycle is None:
                        check_torque(self.coords, tau, motor_tau, self.point.joint_abort_nm,
                                     self.point.motor_abort_nm, "复位前力矩达到中止门限")
                        self.cycle = self._new_cycle(q, now)
                        last_motor = np.asarray([s.pos_rad for s in samples])
                    else:
                        try:
                            self.cycle.step(now, q, tau, motor_tau)
                        finally:
                            self._record_feedback(samples)
                    if self.cycle.phase == "finished":
                        return
                    target = self.coords.motor_target(self.cycle.target)
                    change = abs(target - last_motor)
                    dt = None if last_command is None else now - last_command
                    if np.any(change > motor_step_cap(self.coords, self.cycle.point, dt)):
                        raise FeedbackError("映射到电机的单步或速度超出工位核定值")
                    self.limits(self.cycle.target, target)
                    kp_scale, kd_scale = self.cycle.gains
                if self.stop_event.is_set():
                    break
                self.backend.command_mit(target, kp_scale=kp_scale, kd_scale=kd_scale)
                last_motor = target
                last_command = now
                self.stop_event.wait(.025)
            self.reason = "工位停止、租约撤销或操作员中止"
        except Exception as exc:
            self.reason = str(exc)
        finally:
            self._disable()
            with self.lock:
                if self.cycle and (self.reason or self.cycle.phase != "finished"):
                    self.cycle.phase = "aborted"
            self._record_end()
