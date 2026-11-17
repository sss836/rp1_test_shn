"""One-start, station-owned execution of individually commissioned stop searches."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from collections import deque
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from factory_hmi.limit_replay_assets import PRESET_KEYS, RESOURCE_ROOT, read_manifest, verified_asset
from .limit_block_check import Coordinates, FeedbackError, SETTINGS
from .live_limit_probe import (POS_VEC, PROFILE_ROOT, VEC, PositionMove, PositionSettling, ProbeCycle, ProbePoint, ProbeWorker,
                               _sha, check_torque, point_description, read_site_profile, station_sha)


class ScheduledPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    point: ProbePoint
    via_deg: Annotated[tuple[VEC, ...], Field(max_length=12)] = ()


class ModuleProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    limb: str
    model: Literal["RP1.3.4"]
    power_source: Literal["external", "existing_station"]
    fixture: Annotated[str, Field(min_length=1, max_length=120)]
    revision: Annotated[str, Field(min_length=1, max_length=80)]
    approved_by: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    source: Literal["site", "builtin_reference"] = "site"
    torque_limit_source: str = "site_profile"
    station_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    home_deg: VEC
    entry_via_deg: Annotated[tuple[VEC, ...], Field(max_length=12)] = ()
    return_via_deg: Annotated[tuple[VEC, ...], Field(max_length=12)] = ()
    waist_hip_hold_bounds_deg: tuple[tuple[float, float], tuple[float, float]] | None = None
    windows: Annotated[tuple[ScheduledPoint, ...], Field(min_length=4, max_length=14)]
    joint_peak_nm: POS_VEC | None = None
    transfer_speed_deg_s: Annotated[float, Field(gt=0, le=20)] | None = None
    use_builtin_trajectory: bool = False
    use_builtin_order: bool = False

    @model_validator(mode="before")
    @classmethod
    def default_left_arm_zero_pose(cls, value):
        """An arm reset means moving to logical zero, never setting encoder zero.

        Missing home coordinates do not require an operator to position the
        arm or supply measured starting angles. Existing explicit fixture
        homes remain supported; entry always starts from fresh feedback.
        """
        if isinstance(value, dict) and value.get("home_deg") is None:
            count = {"left_short_arm": 5, "left_arm": 7}.get(value.get("limb"))
            if count is not None:
                value = {**value, "home_deg": [0.0] * count}
        return value

    @model_validator(mode="after")
    def require_site_author(self):
        if self.source == "site" and self.approved_by is None:
            raise ValueError("外置工位方案须保留核定人")
        if (self.transfer_speed_deg_s or 0) > 10 and not (
                self.source == "builtin_reference" and self.limb in {"left_arm", "left_short_arm"}
                and all(w.point.full_range_search for w in self.windows)):
            raise ValueError("transfer_speed_deg_s：非内置左臂回零仍使用原有 10°/s 上限")
        return self

    @property
    def transfer_speed(self) -> float:
        if self.transfer_speed_deg_s is not None:
            return self.transfer_speed_deg_s
        return min(window.point.positioning_speed_deg_s for window in self.windows)


def load_module_plan(config, serial, config_revision):
    if config.limb not in PRESET_KEYS:
        raise ValueError("当前模块不支持整模块真机限位检查")
    try:
        payload = read_site_profile(PROFILE_ROOT / (config.limb + ".sequence.json"))
    except ValueError as exc:
        if config.limb not in {"left_arm", "left_short_arm"} or not isinstance(exc.__cause__, FileNotFoundError):
            raise
        from .arm_limit_defaults import builtin_arm_profile
        profile = builtin_arm_profile(config)
    else:
        profile = ModuleProfile.model_validate_json(payload)
    return validate_module_plan(config, profile, serial, config_revision)


def apply_builtin_trajectory(coords, profile):
    route_asset = read_manifest(RESOURCE_ROOT)[profile.limb].get("route")
    if route_asset is None:
        raise ValueError("当前模块没有内置的真机转场方案")
    _, raw = verified_asset(RESOURCE_ROOT, route_asset)
    route = json.loads(raw)
    if route["limb"] != profile.limb or tuple(route["joint_names"]) != coords.names:
        raise ValueError("内置转场与工位的具名关节不一致")
    site = {(window.point.joint, window.point.direction): window for window in profile.windows}
    route_keys = [(row["joint"], row["direction"]) for row in route["windows"]]
    if set(route_keys) != set(site) or len(route_keys) != len(site):
        raise ValueError("内置转场须覆盖该模块的全部上下限")
    if profile.use_builtin_order:
        # The old visualization is an order reference, not motion input.
        # Do not import any held angles, waypoints, timestamps or simulated
        # contact angles from it. ProbeCycle terminates on real feedback.
        return profile.model_copy(update={"windows": tuple(site[key] for key in route_keys)})
    windows = []
    for key, row in zip(route_keys, route["windows"], strict=True):
        window = site[key]
        held = row["held_pose_deg"]
        if len(held) != len(coords.names) or tuple(i for i, angle in enumerate(held) if angle is None) != (
            coords.names.index(window.point.joint),
        ):
            raise ValueError("内置转场的固定关节设置无效")
        via = row["via_deg"]
        if profile.limb == "waist_hip":
            held = [*held[:2], *profile.home_deg[2:]]
            via = [(*pose[:2], *profile.home_deg[2:]) for pose in via]
        if window.via_deg and window.via_deg != tuple(tuple(pose) for pose in via):
            raise ValueError("启用内置转场时，检查方向不能同时指定其他 via_deg")
        data = window.point.model_dump()
        for field in ("staging_pose_deg", "search_pose_deg"):
            data[field] = tuple(angle if fixed is None else fixed
                                for angle, fixed in zip(data[field], held, strict=True))
        windows.append(ScheduledPoint(point=ProbePoint.model_validate(data), via_deg=via))
    return profile.model_copy(update={"windows": tuple(windows)})


def validate_module_plan(config, profile, serial, config_revision):
    serial = serial.strip()
    if not serial or len(serial) > 120:
        raise ValueError("请填写实际样机编号")
    if config.limb not in PRESET_KEYS or profile.limb != config.limb or profile.station_sha256 != station_sha(config):
        raise ValueError("模块或电机映射与核定的工位方案不一致")
    coords = Coordinates(config, include_waist_holds=True)
    expected = {(d.joint, d.sign) for d in coords.directions}
    submitted = {(row.point.joint, row.point.direction) for row in profile.windows}
    if submitted != expected or len(profile.windows) != len(expected):
        raise ValueError("整模块方案须覆盖该模块的全部上下限，每个方向一次")
    if profile.use_builtin_trajectory and profile.use_builtin_order:
        raise ValueError("检查顺序与轨迹转场不能同时启用")
    if profile.use_builtin_trajectory or profile.use_builtin_order:
        profile = apply_builtin_trajectory(coords, profile)
    descriptions = [point_description(config, profile, w.point, coords=coords) for w in profile.windows]
    hips = profile.waist_hip_hold_bounds_deg
    if hips is not None and (coords.station.limb != "waist_hip" or len(profile.home_deg) != 4 or not all(
            -164. < low < high < 164. and low <= home <= high
            for (low, high), home in zip(hips, profile.home_deg[2:], strict=True))):
        raise ValueError("腰部保持位范围须包含核定髋角，并位于髋轴参考行程内")
    waypoints = [profile.home_deg, *profile.entry_via_deg, *profile.return_via_deg]
    for window in profile.windows:
        waypoints.extend((*window.via_deg, window.point.staging_pose_deg, window.point.search_pose_deg))
    for pose in waypoints:
        if len(pose) != len(coords.names) or not np.all(np.isfinite(pose)):
            raise ValueError("转场姿态的关节数量或角度无效")
        for d in coords.directions:
            if d.sign * (pose[d.index] - d.reference_deg) >= 0:
                raise ValueError("转场姿态超出量产参考区间")
        if config.limb == "waist_hip" and (pose[2:] != profile.home_deg[2:] or any(abs(v) >= 164. for v in pose[2:])):
            raise ValueError("腰部检查的所有转场必须保持两个髋轴固定")
    context = {"source_kind": "physical", "mode": "module", "serial": serial, "profile": profile.model_dump(),
               "limb": config.limb, "station_sha256": profile.station_sha256, "config_revision": config_revision,
               "windows": descriptions, "joint_names": list(coords.names),
               "transfer_speed_deg_s": profile.transfer_speed,
               "execution_mode": "feedback_limit_search",
               "order_source": "builtin_order_only" if profile.use_builtin_order else "profile_or_legacy_route",
               "startup_action": "move_to_joint_zero" if all(v == 0 for v in profile.home_deg)
               else "move_to_profile_home"}
    context["digest"] = _sha(context)
    return coords, profile, context


def path_segments(profile, names):
    previous = profile.home_deg
    for window in profile.windows:
        point = window.point
        route = (previous, *window.via_deg, point.staging_pose_deg, point.search_pose_deg)
        yield from zip(route, route[1:])
        search_end = list(point.search_pose_deg)
        search_end[names.index(point.joint)] += point.direction * point.permitted_travel_deg
        yield point.search_pose_deg, search_end
        previous = point.search_pose_deg
    route = (previous, *profile.return_via_deg, profile.home_deg)
    yield from zip(route, route[1:])


def entry_poses(coords, profile, q):
    """Build the first return from feedback before acquiring motion ownership."""
    measured = np.rad2deg(q)
    if measured.shape != (len(coords.names),) or not np.all(np.isfinite(measured)):
        raise FeedbackError("自动回轨迹起点缺少有效的当前姿态")
    for direction in coords.directions:
        if direction.sign * (measured[direction.index] - direction.reference_deg) > 1e-8:
            raise FeedbackError("当前姿态超出模块参考行程，无法自动回轨迹起点")
    poses = tuple(np.array(pose, dtype=float) for pose in (profile.entry_via_deg + (profile.home_deg,)))
    if coords.station.limb == "waist_hip":
        hips = profile.waist_hip_hold_bounds_deg or tuple((p-.12, p+.12) for p in profile.home_deg[2:])
        if not all(low <= actual <= high for actual, (low, high) in zip(measured[2:], hips, strict=True)):
            raise FeedbackError("当前髋轴超出工装核定的保持范围")
        for pose in poses:
            pose[2:] = measured[2:]
    return (measured, *poses)


class ModuleCycle:
    """Transfer only between acknowledged poses. Any unexpected stop ends the run."""
    def __init__(self, coords, profile, q, now):
        self.coords, self.profile = coords, profile
        self.index = 0
        self.phase = "positioning"
        self.actual = q.copy()
        self.target = q.copy()
        self.last_time = now
        self.gains = (1., 1.)  # The existing position hold was at station gains.
        self.results = []
        self.result = None
        self.probe = None
        self.last_packet = []
        self.samples = []
        self.last_transfer = None
        self.transfer_history = deque(maxlen=5000)
        self.transfer_sample_count = 0
        self.last_transfer_recorded = -math.inf
        self.direction_evidence = []
        self.point = profile.windows[0].point
        self.fixed_hips = q[2:].copy() if coords.station.limb == "waist_hip" else None
        poses = entry_poses(coords, profile, q)
        # Use the most conservative commissioned limits across transfers.
        self.transfer_motor = np.min([w.point.motor_abort_nm for w in profile.windows], axis=0)
        self.transfer_joint = np.min([w.point.joint_abort_nm for w in profile.windows], axis=0)
        self.transfer_error = min(w.point.following_error_deg for w in profile.windows)
        self.transfer_speed = profile.transfer_speed
        self.homing = True
        self.via = iter(poses[1:])
        self._move("gain_transition", q, (self.point.reset_kp_scale, self.point.reset_kd_scale))

    def _fixed(self, pose):
        q = np.deg2rad(pose)
        if self.fixed_hips is not None:
            q[2:] = self.fixed_hips
        return q

    def _prepare_window(self, q, now):
        window = self.profile.windows[self.index]
        self.point = window.point
        if self.fixed_hips is not None:
            self.point = self.point.model_copy(update={key: tuple(np.rad2deg(self._fixed(getattr(self.point, key))))
                for key in ("staging_pose_deg", "search_pose_deg")})
        self.via = iter((*window.via_deg, self.point.staging_pose_deg))
        scales = (self.point.reset_kp_scale, self.point.reset_kd_scale)
        if self.gains == scales:
            self._next_waypoint(q, now)
        else:
            self._move("gain_transition", self.target, scales)

    def _move(self, phase, goal, scales=None):
        self.phase = phase
        self.goal = goal.copy()
        self.gains_from = self.gains
        self.gains_to = self.gains if scales is None else scales
        speed = min(self.transfer_speed, self.point.positioning_speed_deg_s) if phase == "clearance" else self.transfer_speed
        self.move = PositionMove(self.target, goal, speed,
                                 gain_transition=self.gains_from != self.gains_to,
                                 settling=PositionSettling.for_point(self.point))

    def _next_waypoint(self, q, now):
        for waypoint in self.via:
            goal = self._fixed(waypoint)
            if np.allclose(goal, self.target, rtol=0., atol=1.e-12):
                continue  # The previous phase already confirmed this position.
            phase = ("homing" if self.homing else "returning"
                     if self.index == len(self.profile.windows) else "positioning")
            self._move(phase, goal)
            return
        if self.homing:
            self.homing = False
            self._prepare_window(q, now)
        elif self.index == len(self.profile.windows):
            self.phase = "finished"
        else:
            self.probe = ProbeCycle(self.coords, self.point, q, now, target=self.target)
            self.phase = self.probe.phase

    def _verify_load(self, q, joint_tau, motor_tau, now):
        if not 0 < now-self.last_time <= SETTINGS.max_feedback_age_s:
            raise FeedbackError("检查中逐电机反馈不连续")
        if self.fixed_hips is not None and np.max(abs(q[2:] - self.fixed_hips)) > math.radians(.20):
            raise FeedbackError("腰部检查时髋轴离开保持位")
        if self.probe is not None:
            return
        check_torque(self.coords, joint_tau, motor_tau, self.transfer_joint, self.transfer_motor,
                     "转场中超过核定中止力矩")
        if np.max(abs(np.rad2deg(q - self.target))) > self.transfer_error:
            raise FeedbackError("转场跟踪偏差超过核定值")

    def step(self, now, q, joint_tau, motor_tau):
        # Save the current measurement even when a verification raises below.
        self.actual = q.copy()
        if self.probe is None:
            self.last_transfer = {"phase": self.phase, "time_s": now,
                "actual_deg": np.rad2deg(q).tolist(), "target_deg": np.rad2deg(self.target).tolist(),
                "goal_deg": np.rad2deg(self.goal).tolist(), "joint_nm": joint_tau.tolist(),
                "arrival_tolerance_deg": self.point.arrival_tolerance_deg,
                "command_progress": self.move.progress / self.move.duration if self.move.duration else 1.}
            if now - self.last_transfer_recorded >= .1:
                self.transfer_history.append(dict(self.last_transfer))
                self.transfer_sample_count += 1
                self.last_transfer_recorded = now
        self._verify_load(q, joint_tau, motor_tau, now)
        if self.probe is not None:
            probe = self.probe
            probe.step(now, q, joint_tau, motor_tau)
            if probe.phase == "ready":
                probe.start_search(now)
            self.phase, self.target, self.gains = probe.phase, probe.target.copy(), probe.gains
            if probe.result is not None:
                self._complete_window(probe, q, joint_tau, motor_tau)
        else:
            self._position(q, now)
        if self.fixed_hips is not None:
            self.target[2:] = self.fixed_hips
        self.actual, self.last_time = q.copy(), now

    def _complete_window(self, probe, q, joint_tau, motor_tau):
        self.result = {**probe.result, "joint": self.point.joint, "sign": self.point.direction,
                       "reference_deg": probe.direction.reference_deg, "joint_nm_after_retreat": joint_tau.tolist(),
                       "motor_nm_after_retreat": motor_tau.tolist(), "retreat_completed": True}
        self.results.append(self.result)
        self.direction_evidence.append({"joint": self.point.joint, "sign": self.point.direction,
            "samples": list(probe.samples), "samples_truncated": probe.sample_count > len(probe.samples)})
        self.probe = None
        if self.result["outcome"] != "within_reference":
            raise FeedbackError(f"{probe.direction.label} {probe.direction.label_direction}结果异常，整模块检查已中止")
        # Confirm the next move leads away from the stop and preserves the held axes.
        if self.point.direction * (probe.start[probe.axis] - q[probe.axis]) > math.radians(.02):
            raise FeedbackError("缓退位置已超出核定退路，停止后续转场")
        self._move("clearance", probe.start)

    def _position(self, q, now):
        self.target, ratio, settled = self.move.step(
            self.coords, self.point, now, now-self.last_time, q, "转场命令已发完，未确认到位")
        curve = ratio**3 * (10 - 15*ratio + 6*ratio*ratio)
        self.gains = (self.gains_to if ratio == 1. else
                      tuple(a + curve*(b-a) for a,b in zip(self.gains_from, self.gains_to)))
        if not settled:
            return
        if self.phase == "clearance":
            self.index += 1
            if self.index < len(self.profile.windows):
                self._prepare_window(q, now)
                return
            self.via = iter((*self.profile.return_via_deg, self.profile.home_deg))
            self._move("gain_transition", self.target, (self.point.reset_kp_scale, self.point.reset_kd_scale))
            return
        self._next_waypoint(q, now)


class ModuleWorker(ProbeWorker):
    def __init__(self, backend, coords, profile, context, read, limits, publish, destination, entry_position):
        super().__init__(backend, coords, profile.windows[0].point, context, read, limits, publish)
        self.profile = profile
        self.destination = Path(destination)
        self.entry_position = entry_position.copy()
        self.report_file = None
        self.record_error = None
        self.checks = [{"joint": w["point"]["joint"], "sign": w["point"]["direction"],
                       "label": w["label"], "reference_deg": w["reference_deg"]}
                      for w in context["windows"]]

    def _new_cycle(self, q, now):
        # The station was holding during preflight. Reject a changed entrance,
        # independently of distance from the programmed home point.
        if np.max(abs(np.rad2deg(q - self.entry_position))) > min(p.point.following_error_deg for p in self.profile.windows):
            raise FeedbackError("预检后工位姿态已变化，自动回位未开始")
        return ModuleCycle(self.coords, self.profile, q, now)

    def begin(self):
        raise ValueError("整模块任务已自动执行")

    def _record_feedback(self, samples):
        cycle = self.cycle
        cycle.last_packet = [row.to_dict() for row in samples]
        # Preserve the raw Iq and torque provenance alongside the logical
        # angle/load trace, without sending the whole trace on every UI poll.
        new_result = len(cycle.samples) < len(cycle.results)
        trace = cycle.probe.samples if cycle.probe else (
            cycle.direction_evidence[-1]["samples"] if new_result else ())
        if trace:
            trace[-1]["drive_load"] = [
                {key: row.get(key) for key in ("motor_id", "bus", "joint_name", "iq_current_a",
                  "torque_nm", "torque_kt_nm_a", "torque_source", "torque_is_estimated")}
                for row in cycle.last_packet]
        if new_result:
            cycle.samples.append(cycle.last_packet)
            row = cycle.results[-1]
            drive = samples[self.coords.names.index(row["joint"])]
            row.update(torque_is_estimated=drive.torque_is_estimated, torque_source=drive.torque_source,
                       drive_feedback=cycle.last_packet)

    def snapshot(self, *, include_samples=False):
        result = super().snapshot(include_samples=include_samples)
        with self.lock:
            cycle = self.cycle
            probe = cycle.probe if cycle else None
            candidate = ({**probe.candidate, "joint": probe.point.joint, "sign": probe.point.direction,
                          "reference_deg": probe.direction.reference_deg,
                          "deviation_deg": probe.candidate["actual_deg"] - probe.direction.reference_deg,
                          "retreat_completed": False} if probe and probe.candidate else None)
            result.update(mode="module", direction_index=min(cycle.index+1, len(self.profile.windows)) if cycle else 1,
                          direction_total=len(self.profile.windows), results=list(cycle.results) if cycle else [],
                          current_candidate=candidate,
                          checks=self.checks, report_file=self.report_file, record_error=self.record_error)
            if cycle:
                result["last_transfer"] = cycle.last_transfer
            if cycle and probe:
                axis = self.coords.names.index(probe.point.joint)
                result["current_check"] = {"joint": probe.point.joint, "sign": probe.point.direction,
                    "actual_deg": float(np.rad2deg(cycle.actual[axis]))}
                if cycle.last_packet:
                    drive = cycle.last_packet[axis]
                    result["current_check"].update(torque_nm=(probe.samples[-1]["joint_nm"][axis]
                        if probe.samples else None),
                        torque_is_estimated=drive["torque_is_estimated"])
            if candidate and cycle.last_packet:
                drive = cycle.last_packet[self.coords.names.index(candidate["joint"])]
                candidate.update(torque_is_estimated=drive["torque_is_estimated"],
                                 drive_feedback=cycle.last_packet)
            if include_samples and cycle:
                result["transfer_history"] = list(cycle.transfer_history)
                result["transfer_history_truncated"] = cycle.transfer_sample_count > len(cycle.transfer_history)
                result["last_drive_feedback"] = list(cycle.last_packet)
                result["direction_evidence"] = list(cycle.direction_evidence)
                result["unfinished_search"] = ({"joint": probe.point.joint, "sign": probe.point.direction,
                    "samples": list(probe.samples), "samples_truncated": probe.sample_count > len(probe.samples)}
                    if probe else None)
        return result

    def _record_end(self):
        # The drive has already been disabled or a failed request recorded.
        try:
            report = self.snapshot(include_samples=True)
            report.update(active=False, moving=False)
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.destination.parent, encoding="utf-8", delete=False) as tmp:
                temp = Path(tmp.name)
                try:
                    json.dump(report, tmp, ensure_ascii=False, indent=2, allow_nan=False)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                    os.replace(temp, self.destination)
                finally:
                    temp.unlink(missing_ok=True)
            self.report_file = str(self.destination)
        except (OSError, ValueError, TypeError) as exc:
            self.record_error = str(exc)
