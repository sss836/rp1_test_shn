"""Station-owned, one-direction limit block inspection for 5-DoF legs."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, replace
import hashlib
import gc
import json
import logging
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

from .limit_block_check import ContactWindow, Coordinates, FeedbackError, ProbeFeedbackGap, ProbeSoftwareFault, SETTINGS, check_feedback_interval

PILOT_LIMBS = {"left_short_leg", "right_short_leg"}
PROFILE_ROOT = Path("/etc/rp1-factory-hmi/limit-probe.d")
POS = Annotated[float, Field(gt=0, allow_inf_nan=False)]
VEC = Annotated[tuple[Annotated[float, Field(allow_inf_nan=False)], ...], Field(min_length=2, max_length=7)]
POS_VEC = Annotated[tuple[POS, ...], Field(min_length=2, max_length=7)]
MEASURED_SEARCH_MARGIN_DEG = 10.
REPOLL_LIMIT_PER_GAP = 3
REPOLL_WINDOW_S = 10.
REPOLL_MAX_IN_WINDOW = 20


class SearchExtension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    actual_limit_deg: float
    # Explicit endpoints remain readable for already commissioned v3.0.35 profiles.
    command_limit_deg: float | None = None


class ProbePoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    joint: str
    direction: Literal[-1, 1]
    staging_pose_deg: VEC
    search_pose_deg: VEC
    full_range_search: bool = False
    permitted_travel_deg: Annotated[float, Field(gt=0, le=360)]
    search_extension: SearchExtension | None = None
    search_speed_deg_s: Annotated[float, Field(gt=0, le=10)]
    positioning_speed_deg_s: Annotated[float, Field(gt=0, le=20)]
    reset_kp_scale: Annotated[float, Field(gt=0, le=1)]
    reset_kd_scale: Annotated[float, Field(ge=0, le=1)]
    search_kp_scale: Annotated[float, Field(gt=0, le=1)]
    search_kd_scale: Annotated[float, Field(ge=0, le=1)]
    motor_abort_nm: POS_VEC
    joint_abort_nm: POS_VEC
    contact_floor_nm: POS
    contact_rise_nm: POS
    following_error_deg: Annotated[float, Field(gt=.2, le=8)]
    motor_step_deg: Annotated[float, Field(gt=0, le=.5)]
    retreat_deg: Annotated[float, Field(ge=.5, le=2)]
    comparison_tolerance_deg: Annotated[float, Field(gt=0, le=3)]

    @property
    def arrival_tolerance_deg(self):
        # Full-range MIT preparation confirms completion of the commanded path
        # and actual settling. Static load error is measured, not an arrival
        # fault. The legacy one-direction pilot keeps its commissioned gate.
        return None if self.full_range_search else .12

    @property
    def contact_speed_deg_s(self):
        return min(self.search_speed_deg_s, 2.) if self.full_range_search else self.search_speed_deg_s

    @model_validator(mode="after")
    def bounded(self):
        if self.search_extension is not None and not self.full_range_search:
            raise ValueError("延伸搜索仅用于整模块限位检查")
        if not self.full_range_search and (self.permitted_travel_deg > 12 or
                self.search_speed_deg_s > 1 or self.following_error_deg > 2):
            raise ValueError("单方向试点仍使用原有行程、速度和跟踪范围")
        contact_speed = self.contact_speed_deg_s
        if not self.full_range_search and self.following_error_deg <= contact_speed * SETTINGS.stall_s + .05:
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
    contact_speed = point.contact_speed_deg_s
    min_beyond = 0. if point.full_range_search else contact_speed * SETTINGS.stall_s + .05
    if reference_travel <= 0 or not min_beyond < beyond <= 3.0 + 1e-8:
        raise ValueError("搜索起点须在参考位置内侧，参考外指令预算须可容纳判定窗口且不超过 3°")
    if point.search_extension is not None:
        extension = point.search_extension
        actual_travel = sign * (extension.actual_limit_deg - start[index])
        if not point.permitted_travel_deg + point.motor_step_deg < actual_travel <= 360.:
            raise ValueError("延伸搜索实测边界须沿本方向越过普通行程")
        if extension.command_limit_deg is None:
            if not math.isclose(actual_travel-reference_travel, MEASURED_SEARCH_MARGIN_DEG, abs_tol=1e-8):
                raise ValueError("自动延伸的实测边界须为参考角沿搜索方向外移 10°")
        else:
            if getattr(profile, "source", None) != "site" or not profile.approved_by:
                raise ValueError("固定延伸目标只适用于已核定的整模块工位方案")
            command_travel = sign * (extension.command_limit_deg - start[index])
            if not point.permitted_travel_deg < command_travel <= 360.:
                raise ValueError("延伸搜索目标边界须沿本方向越过普通行程")
    if not point.full_range_search and reference_travel + 1e-8 < point.retreat_deg:
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
    def __init__(self, tolerance_deg=.12, jitter_deg=0.):
        self.window = deque()
        self.tolerance_deg, self.jitter_deg = tolerance_deg, jitter_deg
        self.blocking = None

    def blocking_joint(self, names):
        if self.blocking is None:
            return None
        axis, reason, value = self.blocking
        return {"joint": names[axis], "reason": reason, "value": value, "unit": "°"}

    @classmethod
    def for_point(cls, point):
        return cls(point.arrival_tolerance_deg, SETTINGS.stall_deg if point.full_range_search else 0.)

    def settled(self, now, q, target):
        self.blocking = None
        if self.tolerance_deg is not None and np.max(abs(q - target)) > math.radians(self.tolerance_deg):
            axis = int(np.argmax(abs(q - target)))
            self.blocking = (axis, "tolerance", math.degrees(abs(q[axis] - target[axis])))
            self.window.clear()
            return False
        self.window.append((now, q.copy()))
        while len(self.window) > 1 and now - self.window[1][0] >= .20:
            self.window.popleft()
        duration = now - self.window[0][0]
        if duration < .20:
            return False
        span = np.ptp([row[1] for row in self.window], axis=0)
        axis = int(np.argmax(span))
        if span[axis] > math.radians(max(self.jitter_deg, .15 * duration)):
            self.blocking = (axis, "span", math.degrees(span[axis]))
            return False
        return True


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
        delta = abs(coords.motor_target(pose(high)) - last)
        axis = int(np.argmax(delta-cap))
        bound = np.broadcast_to(cap, delta.shape)[axis]
        raise FeedbackError(f"按工位核定的电机单步和速度限制，本段无法继续推进，{coords.names[axis]} "
                           f"单步 {math.degrees(delta[axis]):g}°，上限 {math.degrees(bound):g}°")
    return low


class PositionMove:
    def __init__(self, origin, goal, speed, *, gain_transition=False, settling=None, acceleration=None):
        self.origin, self.goal = origin.copy(), goal.copy()
        self.duration = max(.5 if gain_transition else 0.,
                            1.875 * np.max(abs(np.rad2deg(goal - origin))) / speed)
        self.ramp = 0.
        if acceleration is not None and not gain_transition:
            travel = float(np.max(abs(np.rad2deg(goal-origin))))
            if travel > 0:
                # Half-cosine velocity ramps join a constant-speed middle segment.
                # Their integrated area is v*ramp and peak acceleration is pi*v/(2*ramp).
                peak = min(speed, math.sqrt(2.*travel*acceleration/math.pi))
                self.ramp = math.pi*peak/(2.*acceleration)
                self.duration = travel/peak + self.ramp
        self.progress = 0.
        self.commanded_at = None
        self.settling = settling or PositionSettling()

    def pose(self, elapsed):
        x = elapsed / self.duration if self.duration else 1.
        if self.ramp:
            ramp = self.ramp
            edge = min(elapsed, self.duration-elapsed)
            if edge < ramp:
                area = .5*(edge-ramp/math.pi*math.sin(math.pi*edge/ramp))
            else:
                area = edge - ramp/2.
            curve = area/(self.duration-ramp)
            if elapsed > self.duration/2:
                curve = 1.-curve
        else:
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
        # Full-range MIT preparation waits for actual settling, without a
        # fixed angle-error or 3-second gate. The caller continues checking
        # torque, feedback, drive faults and configured motion bounds.
        if not settled and not point.full_range_search and now - self.commanded_at > 3.:
            error = abs(np.rad2deg(q-self.goal))
            axis = int(np.argmax(error))
            raise ProbeSoftwareFault(f"{timeout_message}：{coords.names[axis]} 实测 {math.degrees(q[axis]):.2f}°，"
                                f"目标 {math.degrees(self.goal[axis]):.2f}°，偏差 {error[axis]:.2f}°，"
                                f"到位范围 ±{self.settling.tolerance_deg:g}°；须同时停稳")
        return target, ratio, settled


class DirectionEvidence:
    """Keep a bounded overview of records evicted from the full-resolution tail."""
    def __init__(self):
        self.earlier = []
        self.stride = 1
        self.evicted = 0

    def retain(self, sample):
        if self.evicted % self.stride == 0:
            self.earlier.append(sample)
            if len(self.earlier) == 512:
                self.earlier = self.earlier[::2]
                self.stride *= 2
        self.evicted += 1

    def snapshot(self):
        return {"earlier_samples": list(self.earlier), "earlier_stride": self.stride,
                "evicted_from_full_resolution": self.evicted}


class ProbeCycle:
    """Pure state machine shared by the live executor and simulated evidence."""
    def __init__(self, coords, point, q, now, *, target=None, far_speed=None, fixture_cap=None):
        self.coords, self.point = coords, point
        self.axis = coords.names.index(point.joint)
        self.direction = next(d for d in coords.directions if d.joint == point.joint and d.sign == point.direction)
        self.start = np.deg2rad(point.search_pose_deg)
        staging = np.deg2rad(point.staging_pose_deg)
        arrival = point.arrival_tolerance_deg if point.full_range_search else .25
        if not point.full_range_search and min(np.max(abs(q - staging)), np.max(abs(q - self.start))) > math.radians(arrival):
            raise FeedbackError("实测姿态不在核定复位起点或搜索起点范围内")
        self.actual = q.copy()
        self.target = q.copy() if target is None else target.copy()
        self.reset = PositionMove(self.target, self.start, point.positioning_speed_deg_s,
                                  settling=PositionSettling.for_point(point))
        self.held_actual = q.copy()
        self.phase = "reset"
        self.since = now
        self.previous = now
        self.gain_blend = 0.
        self.settling = PositionSettling.for_point(point)
        self.progress = 0.
        self.commanded_at = None
        self.recent_load = []
        self.moving_load = deque(maxlen=25)
        self.trace = []
        self.retreat = None
        self.candidate = None
        self.result = None
        self.samples = deque(maxlen=5000)
        self.history = DirectionEvidence()
        self.sample_count = 0
        self.samples_started = now
        self.far_speed = point.search_speed_deg_s if far_speed is None else far_speed
        self.fixture_cap = fixture_cap
        self.feedback_search = None
        if point.full_range_search:
            from .feedback_limit_search import FeedbackLimitSearch
            self.feedback_search = FeedbackLimitSearch(self)

    def record_sample(self, sample):
        if len(self.samples) == self.samples.maxlen:
            self.history.retain(self.samples[0])
        self.samples.append(sample)
        self.sample_count += 1

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
        if self.feedback_search is not None:
            return self.feedback_search.step(now, q, joint_tau, motor_tau)
        p, a, s = self.point, self.axis, self.point.direction
        elapsed, dt = now - self.since, now - self.previous
        check_feedback_interval(dt, self.phase, source="单方向检查")
        check_torque(self.coords, joint_tau, motor_tau, p.joint_abort_nm, p.motor_abort_nm,
                     "实测力矩达到工位中止门限")
        if np.max(abs(np.rad2deg(q - self.target))) > p.following_error_deg:
            raise ProbeSoftwareFault("实际姿态与最近指令偏差超过核定值")
        held = self.held_actual if p.full_range_search else self.start
        held_tolerance = p.arrival_tolerance_deg if p.full_range_search else .35
        if np.max(abs(np.rad2deg(np.delete(q - held, a)))) > held_tolerance:
            raise FeedbackError("非搜索轴离开核定保持位")
        self.record_sample({"time_s": now - self.samples_started, "phase": self.phase,
            "actual_deg": np.rad2deg(q).tolist(), "target_deg": np.rad2deg(self.target).tolist(),
            "joint_nm": joint_tau.tolist(), "motor_nm": motor_tau.tolist(), "kp_kd_scale": self.gains})
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
                raise ProbeSoftwareFault("搜索增益切换后未取得稳定基线")
        elif self.phase == "search":
            self.trace.append((now, q.copy()))
            if p.full_range_search:
                # Refresh gravity/load baseline only while actually following
                # forward motion. Freeze it as soon as lag/stall begins.
                moving = s * math.degrees(q[a] - self.actual[a]) > .002
                if moving and abs(math.degrees(q[a] - self.target[a])) < .3:
                    self.moving_load.append(abs(joint_tau[a]))
                    self.window.baseline = statistics.median(self.moving_load)
                    self.window.threshold_nm = max(p.contact_floor_nm, self.window.baseline + p.contact_rise_nm)
            self.candidate = self.window.observe(now, math.degrees(q[a]), joint_tau[a], math.degrees(self.target[a]))
            cap = math.radians(p.permitted_travel_deg)
            displacement = s * (q[a] - self.start[a])
            if displacement > cap + math.radians(.12):
                raise FeedbackError(f"实际搜索姿态超过核定行程，{p.joint} 实测 {math.degrees(displacement):g}°，"
                                   f"上限 {math.degrees(cap)+.12:g}°")
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
                speed = p.search_speed_deg_s
                if p.full_range_search and (s * (self.direction.reference_deg - math.degrees(q[a])) <= 6.
                        or abs(math.degrees(q[a] - self.target[a])) > .35):
                    speed = min(speed, 1.)
                proposed = min(cap, self.progress + math.radians(dt * speed))
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
            raise ProbeSoftwareFault(timeout_message)
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
        self.command_interval_s = .025
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self._handoff_lock = None
        self._hold_stop = None
        self._handoff_generation = None
        self.thread = threading.Thread(target=self._run, daemon=True, name="limit-probe")
        self.cycle = None
        self.reason = ""
        self.stop_phase = None
        self.stop_reason = None
        self.fault_handoff = None
        self.protection = None
        self._damping_after_stamps = ()
        self.disable = {"request": "pending", "feedback": "unknown"}
        self.started = False
        self._started_at = time.monotonic()
        self._sent = None
        self._sent_at = 0.
        self._gap_repolls = 0
        self._repoll_times = deque()
        self._gap_noted = False
        self.reply_misses = deque(maxlen=256)
        self.gc_pauses = deque(maxlen=256)
        self._gc_start = None
        self.feedback_diagnostics = {"received_groups": 0, "accepted_groups": 0,
            "repeated_groups": 0, "empty_reads": 0, "recovery_count": 0,
            "repoll_count": 0, "missing_events": 0, "missed_by_motor": {},
            "whole_bus_missed": 0, "max_accept_gap_ms": 0.,
            "accept_gap_buckets": dict.fromkeys(("<=40", "<=80", "<=120", "<=150", ">150"), 0),
            "last_group_skew_ms": None, "max_group_skew_ms": 0.,
            "latest_status": "waiting_feedback", "last_error": None}
        self.latest_feedback_group = None
        self.last_rejected_feedback_group = None
        self.feedback_events = deque(maxlen=64)

    def take_over_hold(self, dispatch_lock, hold_stop, generation):
        self._handoff_lock = dispatch_lock
        self._hold_stop = hold_stop
        self._handoff_generation = generation

    def _send_first_target(self, target, kp_scale, kd_scale, stamps):
        # The old owner checks its stop event under the same lock before each
        # write. Its thread can finish later without sending another hold.
        with self._handoff_lock:
            if (self.stop_event.is_set() or self.backend.control_generation != self._handoff_generation
                    or (self._hold_stop is not None and self._hold_stop.is_set())):
                raise FeedbackError("控制交接已取消")
            if time.monotonic() - min(stamps) >= SETTINGS.max_feedback_age_s:
                raise FeedbackError("控制交接时逐电机反馈已过期")
            if self._hold_stop is not None:
                self._hold_stop.set()
            previous = self.backend.last_mit_success_s
            self.backend.command_mit(target, kp_scale=kp_scale, kd_scale=kd_scale,
                                     expected_generation=self._handoff_generation,
                                     dispatch_before=min(stamps) + SETTINGS.max_feedback_age_s,
                                     cancelled=self.stop_event)
            first = self.backend.last_mit_success_s
        self._handoff_lock = None
        gap_ms = round((first - previous) * 1000, 2) if first is not None and previous is not None else None
        with self.lock:
            self.feedback_diagnostics["first_dispatch_monotonic_s"] = first
            self.feedback_diagnostics["handoff_gap_ms"] = gap_ms
        logging.getLogger(__name__).info("Pose check %s first dispatch: limb=%s handoff_gap_ms=%s",
                                         self.context.get("check_id"), self.coords.station.limb, gap_ms)

    def start(self):
        self.thread.start()
        self.started = True

    @property
    def active(self):
        return self.thread.is_alive() or (self.started and self.disable["request"] == "failed")

    def request_stop(self, reason=None):
        if reason and not self.stop_reason:
            self.stop_reason = reason
        self.stop_event.set()
        if self._hold_stop is not None:
            self._hold_stop.set()

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
            base.update(stop_phase=self.stop_phase, fault_protection=self.protection)
            base["control_thread_running"] = self.thread.is_alive()
            base["feedback_diagnostics"] = {**self.feedback_diagnostics,
                "missed_by_motor": dict(self.feedback_diagnostics["missed_by_motor"]),
                "accept_gap_buckets": dict(self.feedback_diagnostics["accept_gap_buckets"])}
            if include_samples:
                base.update(reply_misses=list(self.reply_misses), gc_pauses=list(self.gc_pauses))
                base.update(context=self.context, calibration_passed=None,
                            samples=list(c.samples) if c else [])
                base.update(latest_feedback_group=self.latest_feedback_group,
                            last_rejected_feedback_group=self.last_rejected_feedback_group,
                            feedback_events=list(self.feedback_events))
                if isinstance(c, ProbeCycle):
                    base.update(candidate=c.candidate, samples_truncated=c.sample_count > len(c.samples), **c.history.snapshot())
            return base

    def _disable(self):
        if self._hold_stop is not None:
            self._hold_stop.set()
        try:
            self.backend.disable_motors()
            self.disable = {"request": "sent", "feedback": self.backend.disable_feedback()}
        except Exception as exc:
            self.disable = {"request": "failed", "feedback": "unknown", "reason": str(exc)}
            if not self.reason:
                self.reason = "收尾失能尚未确认：" + str(exc)
        status = getattr(self.backend, "disable_status", None)
        if callable(status):
            try:
                self.disable["details"] = status()
                self.disable["feedback"] = self.backend.disable_feedback()
            except Exception as exc:
                self.disable["status_error"] = str(exc)

    def _new_cycle(self, q, now):
        return ProbeCycle(self.coords, self.point, q, now)

    def _record_feedback(self, samples, packet=None, now=None):
        if self.cycle.samples and (now is None or self.cycle.samples[-1]["time_s"] == now-self.cycle.samples_started):
            self.cycle.samples[-1]["drive_feedback"] = packet if packet is not None else [row.to_dict() for row in samples]

    def _record_end(self):
        pass

    def _observe_feedback(self, samples, now):
        """Capture the received group before validation can reject its facts."""
        stamps = [row.timestamp_s for row in samples]
        skew = (1000.*(max(stamps)-min(stamps)) if stamps and
                all(math.isfinite(value) for value in stamps) else None)
        group = {"received_at_monotonic_s": now, "skew_ms": skew,
                 "samples": [row.to_dict() for row in samples]}
        with self.lock:
            self.latest_feedback_group = group
            diag = self.feedback_diagnostics
            diag["received_groups" if samples else "empty_reads"] += 1
            diag["last_group_skew_ms"] = skew
            if skew is not None and skew > diag["max_group_skew_ms"]:
                diag["max_group_skew_ms"] = skew
                self.feedback_events.append({"event": "observed_skew", "time_s": now, "skew_ms": skew})

        return group["samples"]

    def _feedback_state(self, state, now):
        with self.lock:
            diag = self.feedback_diagnostics
            previous = diag["latest_status"]
            if state == "accepted":
                diag["accepted_groups"] += 1
                if previous.startswith("waiting_") and previous != "waiting_feedback":
                    diag["recovery_count"] += 1
                    self.feedback_events.append({"event": "recovered", "time_s": now,
                                                 "previous_status": previous})
            elif state == "waiting_repeated":
                diag["repeated_groups"] += 1
            if state != previous and state.startswith("waiting_"):
                self.feedback_events.append({"event": state, "time_s": now})
            diag["latest_status"] = state

    def _feedback_failure(self, message, now, received):
        with self.lock:
            diag = self.feedback_diagnostics
            diag.update(latest_status="failed", last_error=message)
            group = self.latest_feedback_group if received else None
            self.last_rejected_feedback_group = {"reason": message, "failed_at_monotonic_s": now,
                "received": received, "skew_ms": group["skew_ms"] if group else None,
                "samples": group["samples"] if group else []}
            self.feedback_events.append({"event": "failed", "time_s": now, "reason": message,
                                         "received": received, "skew_ms": group["skew_ms"] if group else None})

    def _check_received_loads(self, tau, motor_tau, q=None):
        """A repeated group must not hide new drive faults or an overload."""
        cycle = self.cycle
        point = cycle.point if cycle is not None else self.point
        in_transfer = hasattr(cycle, "transfer_joint") and getattr(cycle, "probe", None) is None
        joint = cycle.transfer_joint if in_transfer else point.joint_abort_nm
        motor = cycle.transfer_motor if in_transfer else point.motor_abort_nm
        check_torque(self.coords, tau, motor_tau, joint, motor, "实测力矩达到工位中止门限")

    def check_damping_feedback(self, samples, kd):
        if self.stop_event.is_set():
            raise FeedbackError("限位检查已收到停止请求")
        q, torque, _ = self.coords.decode(samples, time.monotonic(), self._damping_after_stamps)
        if any(not math.isfinite(row.temp_c) or not math.isfinite(row.spd_rad_s) for row in samples):
            raise FeedbackError("阻尼交接缺少温度或速度反馈")
        self._check_received_loads(torque, np.asarray([s.torque_nm for s in samples]), q)
        damping_torque = -kd * np.asarray([s.spd_rad_s for s in samples])
        self._check_received_loads(self.coords.jacobian(q).T @ damping_torque, damping_torque, q)
        self.limits(q, np.asarray([s.pos_rad for s in samples]))
        return min(s.timestamp_s for s in samples) + SETTINGS.max_feedback_age_s

    def _observe_gc(self, phase, info):
        # GC can run on a thread already holding the worker lock.
        if phase == "start":
            self._gc_start = time.monotonic()
        elif self._gc_start is not None:
            end = time.monotonic()
            ms = (end - self._gc_start) * 1000.
            if info["generation"] == 2 or ms > 20:
                self.gc_pauses.append({"time_s": end, "generation": info["generation"],
                                       "duration_ms": ms, "collected": info["collected"]})
            self._gc_start = None

    @property
    def repoll_after_s(self):
        return max(self.command_interval_s, .015)

    def _wait_for_reply(self, now, last_cohort, stamp, samples, q=None):
        deadline = last_cohort + SETTINGS.max_feedback_age_s
        if stamp:
            oldest = min(s.timestamp_s for s in samples) if samples else min(stamp)
            deadline = min(deadline, oldest + SETTINGS.max_feedback_age_s)
        missing = [(row.bus, row.motor_id, round((now-row.timestamp_s)*1000, 1))
                   for row, old in zip(samples, stamp) if row.timestamp_s <= old]
        if missing and not self._gap_noted:
            labels = [f"{bus}#{node}" for bus, node, age in missing]
            whole = [bus for bus in dict.fromkeys(s.bus for s in samples)
                     if sum(b == bus for b, _, _ in missing) == sum(s.bus == bus for s in samples)]
            with self.lock:
                diag = self.feedback_diagnostics
                diag["missing_events"] += 1
                diag["whole_bus_missed"] += len(whole)
                for label in labels:
                    diag["missed_by_motor"][label] = diag["missed_by_motor"].get(label, 0) + 1
                self.reply_misses.append({"time_s": now, "missing": labels,
                    "age_ms": [age for _, _, age in missing], "whole_buses": whole,
                    "phase": self.cycle.phase if self.cycle else None,
                    "joint_deg": np.round(np.rad2deg(q), 2).tolist() if q is not None else None})
            self._gap_noted = True
        if now >= deadline:
            detail = "，".join(f"{bus}#{node} {age:g} ms" for bus, node, age in missing)
            raise ProbeFeedbackGap(f"没有收到完整的新鲜逐电机反馈，间隔 {(now-last_cohort)*1000:.1f} ms " + detail)
        if (self._sent is not None and self._handoff_lock is None and not self.stop_event.is_set()
                and self._gap_repolls < REPOLL_LIMIT_PER_GAP and now - self._sent_at >= self.repoll_after_s):
            while self._repoll_times and now - self._repoll_times[0] >= REPOLL_WINDOW_S:
                self._repoll_times.popleft()
            if len(self._repoll_times) >= REPOLL_MAX_IN_WINDOW:
                raise FeedbackError(f"反馈补询过于频繁（链路劣化），缺帧计数 {self.feedback_diagnostics['missed_by_motor']}")
            target, kp, kd = self._sent
            self.backend.command_mit(target, kp_scale=kp, kd_scale=kd, dispatch_before=deadline,
                                     expected_generation=self._handoff_generation, cancelled=self.stop_event)
            self._sent_at = time.monotonic()
            self._repoll_times.append(self._sent_at)
            self._gap_repolls += 1
            with self.lock:
                self.feedback_diagnostics["repoll_count"] += 1
                self.feedback_events.append({"event": "repoll", "time_s": self._sent_at})
        self.stop_event.wait(max(0., min(.008, self.command_interval_s, deadline - time.monotonic())))

    def _run(self):
        stamp = ()
        self._started_at = last_cohort = time.monotonic()
        last_motor = None
        last_command = None
        received = False
        gc.callbacks.append(self._observe_gc)
        try:
            while not self.stop_event.is_set():
                received = False
                samples = self.read()
                if self.stop_event.is_set():
                    break
                now = time.monotonic()
                received = True
                packet = self._observe_feedback(samples, now)
                if not samples:
                    self._feedback_state("waiting_empty", now)
                    self._wait_for_reply(now, last_cohort, stamp, samples)
                    continue
                # Validate current identities, individual freshness and faults
                # before the repeated-group wait; no stale/fault/overload is
                # swallowed by that wait. Skew is recorded but never a gate.
                q, tau, received_stamp = self.coords.decode(samples, now, ())
                motor_tau = np.asarray([s.torque_nm for s in samples])
                self._check_received_loads(tau, motor_tau, q)
                if stamp and not all(a > old for a, old in zip(received_stamp, stamp, strict=True)):
                    self._feedback_state("waiting_repeated", now)
                    self._wait_for_reply(now, last_cohort, stamp, samples, q)
                    continue
                # Before takeover the hold owner is servicing the drives. A
                # delayed first read may still return a fully fresh group.
                holding_before_takeover = not stamp and self._hold_stop is not None and not self._hold_stop.is_set()
                if not holding_before_takeover and now - last_cohort >= SETTINGS.max_feedback_age_s:
                    check_feedback_interval(now-last_cohort, self.cycle.phase if self.cycle else "waiting_feedback",
                                            source="整组反馈超过允许间隔", inclusive=True)
                if stamp:
                    gap_ms = (now - last_cohort) * 1000
                    with self.lock:
                        diag = self.feedback_diagnostics
                        diag["max_accept_gap_ms"] = max(gap_ms, diag["max_accept_gap_ms"])
                        bucket = next((f"<={ms}" for ms in (40, 80, 120, 150) if gap_ms <= ms), ">150")
                        diag["accept_gap_buckets"][bucket] += 1
                self._gap_noted = False
                self._gap_repolls = 0
                stamp = received_stamp
                self._feedback_state("accepted", now)
                self.on_samples(samples)
                last_cohort = now
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
                            self._record_feedback(samples, packet, now)
                    if self.cycle.phase == "finished":
                        return
                    target = self.coords.motor_target(self.cycle.target)
                    change = abs(target - last_motor)
                    dt = None if last_command is None else now - last_command
                    cap = np.broadcast_to(motor_step_cap(self.coords, self.cycle.point, dt), change.shape)
                    if np.any(change > cap):
                        i = int(np.argmax(change-cap))
                        node = self.coords.entries[i]
                        raise FeedbackError(f"映射到电机的单步或速度超出工位核定值，{node.interface}#{node.motor_id} "
                            f"{node.joint_name} 变化 {math.degrees(change[i]):g}°，上限 {math.degrees(cap[i]):g}°，dt={dt} s")
                    self.limits(self.cycle.target, target)
                    kp_scale, kd_scale = self.cycle.gains
                if self.stop_event.is_set():
                    break
                if self._handoff_lock is None:
                    self.backend.command_mit(target, kp_scale=kp_scale, kd_scale=kd_scale)
                else:
                    self._send_first_target(target, kp_scale, kd_scale, stamp)
                self._sent = (target.copy(), kp_scale, kd_scale)
                self._sent_at = time.monotonic()
                if last_command is None:
                    with self.lock:
                        self.feedback_diagnostics.setdefault("first_dispatch_monotonic_s", self._sent_at)
                last_motor = target
                last_command = now
                self.stop_event.wait(self.command_interval_s)
            self.reason = self.stop_reason or "工位停止、租约撤销或操作员中止"
        except Exception as exc:
            self.reason = str(exc)
            self._feedback_failure(self.reason, time.monotonic(), received)
            if isinstance(exc, ProbeSoftwareFault) and self.fault_handoff and not self.stop_event.is_set():
                try:
                    if isinstance(exc, ProbeFeedbackGap):
                        self._damping_after_stamps = stamp
                    self.protection = self.fault_handoff(self, self.reason) or {
                        "active": False, "error": "限位控制权、控制代次或停止状态不允许交接"}
                    if self.protection.get("active"):
                        self.disable = {"request": "not_requested", "feedback": "unknown"}
                except Exception as error:
                    self.protection = {"active": False, "error": str(error)}
        finally:
            gc.callbacks.remove(self._observe_gc)
            if not self.protection or not self.protection.get("active"):
                self._disable()
            with self.lock:
                self.stop_phase = self.cycle.phase if self.cycle else "waiting_feedback"
                if self.cycle and (self.reason or self.cycle.phase != "finished"):
                    self.cycle.phase = "aborted"
            self._record_end()
