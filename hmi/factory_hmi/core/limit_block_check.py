"""RP1.3.4 limit block plans, feedback checks and offline commissioning simulator."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import hashlib
from importlib import resources
import json
import math
import statistics

import numpy as np
import yaml

from factory_hmi.models import MotorSample, StationConfig
from scripts.rp1_limb_common import close_chain_motor_indices, joint_default_positions, limb_joint_names

from .config import CONFIG_FILENAMES, build_station_config
from .manual_control import joint_positions_to_motor, motor_positions_to_joint

MODULES = (
    ("left_short_leg", "左腿（5 自由度）"), ("right_short_leg", "右腿（5 自由度）"),
    ("left_leg", "左腿（6 自由度）"), ("right_leg", "右腿（6 自由度）"),
    ("left_short_arm", "左臂（5 自由度）"), ("right_short_arm", "右臂（5 自由度）"),
    ("left_arm", "左臂（7 自由度）"), ("right_arm", "右臂（7 自由度）"),
    ("waist_hip", "腰部（2 自由度）"),
)
NAMES = {
    "hip_pitch": "髋俯仰", "hip_roll": "髋侧摆", "hip_yaw": "髋旋转",
    "knee": "膝屈伸", "ankle_pitch": "踝俯仰", "ankle_roll": "踝侧摆",
    "shoulder_pitch": "肩俯仰", "shoulder_roll": "肩侧摆", "shoulder_yaw": "肩旋转",
    "elbow": "肘屈伸", "wrist_roll": "腕侧摆", "wrist_yaw": "腕旋转",
    "wrist_pitch": "腕俯仰", "waist_roll": "腰侧摆", "waist_yaw": "腰旋转",
}
# RP1.3.4 production table dated 2026-09-16. These are reference angles only.
COMMON = {
    "hip_pitch": (-164, 164), "hip_yaw": (-157.8, 157.8),
    "ankle_pitch": (-45, 35), "ankle_roll": (-15, 15),
    "shoulder_pitch": (-165.553, 165.553), "shoulder_yaw": (-165, 165),
    "wrist_roll": (-148.278, 148.278), "wrist_yaw": (-85, 85), "wrist_pitch": (-70, 70),
    "waist_roll": (-40, 40), "waist_yaw": (-166.74, 166.74),
}
SIDE = {
    "left": {"hip_roll": (-42.73, 149.87), "knee": (-162.86, 2.14),
             "shoulder_roll": (-27.51, 171.49), "elbow": (-91.481, 71)},
    "right": {"hip_roll": (-149.87, 42.73), "knee": (-2.14, 162.86),
              "shoulder_roll": (-171.49, 27.51), "elbow": (-71, 91.481)},
}


@dataclass(frozen=True)
class Settings:
    # Simulation settings. The position reference does not validate real gain/load budgets.
    search_deg_s: float = 1.0
    stall_s: float = 1.0
    stall_deg: float = 0.2
    min_torque_nm: float = 0.5
    rise_nm: float = 3.0
    abort_nm: float = 12.0
    reference_tolerance_deg: float = 3.0
    max_feedback_age_s: float = 0.15
    approach_margin_deg: float = 6.0
    retreat_deg: float = 1.0
    reset_seconds: float = 4.0
    positioning_speed_deg_s: float = 20.0


SETTINGS = Settings()


@dataclass(frozen=True)
class Direction:
    index: int
    joint: str
    label: str
    sign: int
    reference_deg: float
    motor_labels: tuple[str, ...]

    @property
    def label_direction(self) -> str:
        return "下限" if self.sign < 0 else "上限"


class FeedbackError(RuntimeError):
    pass


def nominal_station(limb: str) -> StationConfig:
    if limb not in dict(MODULES):
        raise ValueError("限位块检查仅支持独立单腿、单臂和腰部")
    asset = resources.files("scripts").joinpath("config", CONFIG_FILENAMES[limb])
    return build_station_config(yaml.safe_load(asset.read_text(encoding="utf-8")), limb)


class Coordinates:
    """Bind logical names to the configured order of CAN buses, IDs and signs."""

    def __init__(self, station: StationConfig, *, include_waist_holds=False):
        if station.limb not in dict(MODULES):
            raise ValueError("限位块检查仅支持独立单腿、单臂和腰部")
        self.station = station
        names = tuple(limb_joint_names(station.limb))
        if names != tuple(e.joint_name for e in station.entries):
            raise ValueError("模块电机配置与具名关节顺序不一致")
        waist = station.limb == "waist_hip"
        self.waist_only = waist and not include_waist_holds
        # Use the names from the actual waist configuration. Never use its hip IDs.
        self.names = names[:2] if self.waist_only else names
        self.entries = station.entries[:len(self.names)]
        if waist and (self.names[:2] != ("waist_roll_joint", "waist_yaw_joint")
                      or any(e.motor_id != 1 for e in self.entries[:2])):
            raise ValueError("腰部限位检查需要两条总线上各一台 ID 1 电机")
        self.identity = tuple((e.interface, e.motor_id, e.joint_name) for e in self.entries)
        if len({(bus, node) for bus, node, _ in self.identity}) != len(self.identity):
            raise ValueError("CAN 接口和电机 ID 组合有重复")
        self.home = joint_default_positions(station.raw, station.limb)[:len(self.names)]
        self.signs = np.asarray([e.sign for e in self.entries], dtype=float)
        if np.any(np.abs(self.signs) != 1):
            raise ValueError("限位检查的电机方向须为 ±1")
        self.pair = (tuple(close_chain_motor_indices(station.raw, station.limb, len(self.names)))
                     if station.limb.endswith("_leg") else ())
        specs = []
        for axis, name in enumerate(self.names[:2] if waist else self.names):
            stem = name.removesuffix("_joint")
            side = stem.split("_", 1)[0]
            key = stem.split("_", 1)[1] if side in SIDE else stem
            lower, upper = SIDE.get(side, {}).get(key, COMMON.get(key, (None, None)))
            if lower is None:
                raise ValueError(f"缺少具名关节参考角：{name}")
            indices = self.pair if key in {"ankle_pitch", "ankle_roll"} else (axis,)
            labels = tuple(f"{self.entries[i].interface}#{self.entries[i].motor_id}" for i in indices)
            specs.extend((Direction(axis, name, NAMES[key], -1, lower, labels),
                          Direction(axis, name, NAMES[key], 1, upper, labels)))
        self.directions = tuple(specs)

    def motor_target(self, q: np.ndarray) -> np.ndarray:
        return q * self.signs if self.waist_only else joint_positions_to_motor(self.station, q)

    def joint_position(self, q_motor: np.ndarray) -> np.ndarray:
        return q_motor / self.signs if self.waist_only else motor_positions_to_joint(self.station, q_motor)

    def jacobian(self, q: np.ndarray) -> np.ndarray:
        matrix = np.diag(self.signs)
        if not self.pair:
            return matrix
        rows = list(self.pair)
        for axis in range(len(q) - 2, len(q)):
            delta = np.zeros(len(q))
            delta[axis] = 1e-5
            matrix[rows, axis] = (self.motor_target(q + delta)[rows] -
                                  self.motor_target(q - delta)[rows]) / (2e-5)
        if not np.all(np.isfinite(matrix)) or np.linalg.cond(matrix) > 30:
            raise FeedbackError("踝部闭链映射接近奇异位形")
        return matrix

    def decode(self, samples: list[MotorSample], now: float, last_stamps: tuple[float, ...]):
        identity = tuple((s.bus, s.motor_id, s.joint_name) for s in samples)
        if identity != self.identity:
            raise FeedbackError("反馈的接口、ID 或具名关节与方案不一致")
        stamps = tuple(s.timestamp_s for s in samples)
        for sample in samples:
            node = f"{sample.bus}#{sample.motor_id} {sample.joint_name}"
            issue = sample.feedback_issue(SETTINGS.max_feedback_age_s, now)
            if issue:
                raise FeedbackError(f"电机 {node} {issue}")
            if sample.error_id:
                raise FeedbackError(f"电机 {node} 驱动故障 error_id={sample.error_id}")
            if sample.drive_enabled is False:
                raise FeedbackError(f"电机 {node} 反馈为未使能")
            if not math.isfinite(sample.pos_rad) or not math.isfinite(sample.torque_nm):
                raise FeedbackError(f"电机 {node} 位置或力矩读数无效")
        # Each motor must be fresh and advance independently. CAN responses
        # need not share a 35 ms group deadline; group skew is diagnostic only.
        if last_stamps and not all(a > b for a, b in zip(stamps, last_stamps)):
            raise FeedbackError("需要一组全部更新的逐电机反馈")
        motor = np.array([s.pos_rad for s in samples])
        q = self.joint_position(motor)
        if np.max(np.abs(self.motor_target(q) - motor)) > 1e-5:
            raise FeedbackError("反馈闭链反解与电机角不一致")
        tau = self.jacobian(q).T @ np.array([s.torque_nm for s in samples])
        if not np.all(np.isfinite(q)) or not np.all(np.isfinite(tau)):
            raise FeedbackError("逻辑关节反馈不完整")
        return q, tau, stamps


class ContactWindow:
    def __init__(self, direction: Direction, baseline: float, *, settings=SETTINGS):
        self.direction = direction
        self.baseline = baseline
        self.settings = settings
        self.threshold_nm = max(settings.min_torque_nm, baseline + settings.rise_nm)
        self.samples: deque[tuple[float, float, float, float]] = deque()

    def observe(self, time_s: float, actual_deg: float, torque_nm: float, target_deg: float):
        window = self.samples
        if window and time_s <= window[-1][0]:
            raise FeedbackError("限位判定窗口收到重复或倒序反馈")
        if window and time_s - window[-1][0] > self.settings.max_feedback_age_s:
            window.clear()
        window.append((time_s, actual_deg, abs(torque_nm), target_deg))
        cutoff = time_s - self.settings.stall_s
        while len(window) > 2 and window[1][0] <= cutoff + 1e-9:
            window.popleft()
        if len(window) < 3 or time_s - window[0][0] < self.settings.stall_s - 1e-9:
            return None
        span = max(row[1] for row in window) - min(row[1] for row in window)
        min_load = min(row[2] for row in window)
        progress = self.direction.sign * (window[-1][3] - window[0][3])
        if (span > self.settings.stall_deg or progress < 0.02 or
                min_load < self.threshold_nm):
            return None
        return {"actual_deg": actual_deg, "angle_span_deg": span, "torque_floor_nm": min_load,
                "baseline_nm": self.baseline, "target_progress_deg": progress,
                "fresh_samples": len(window)}


class LimitCheckSimulation:
    """A commissioning workflow that feeds synthetic motor packets back through the real mappings."""

    STEP = 0.05
    SCENARIOS = {"normal": "正常限位", "early": "提前阻挡", "late": "超出参考",
                 "missing": "未检出", "stale": "反馈中断", "overload": "超力矩"}

    def __init__(self, limb: str, serial: str, selected: list[int], scenario: str,
                 *, station: StationConfig | None = None):
        if not serial.strip() or scenario not in self.SCENARIOS:
            raise ValueError("请填写样机编号并选择有效的模拟工况")
        active_config = station or nominal_station(limb)
        if active_config.limb != limb:
            raise ValueError("所选模块与电机配置机型不一致")
        self.coords = Coordinates(active_config)
        if len(set(selected)) != len(selected) or not selected or not all(
            0 <= i < len(self.coords.directions) for i in selected
        ):
            raise ValueError("请选择有效的关节与方向")
        self.serial, self.scenario = serial.strip(), scenario
        self.plan = [self.coords.directions[i] for i in selected]
        plan_data = {
            "source_kind": "simulation", "reference": "RP1.3.4 / 20260916",
            "limb": limb, "serial": self.serial, "scenario": scenario,
            "targets": [asdict(item) for item in self.plan], "settings": asdict(SETTINGS),
            "mapping": [asdict(entry) for entry in self.coords.entries],
            "close_chain": self.coords.pair, "home_rad": self.coords.home.tolist(),
        }
        self.plan_data = plan_data
        self.digest = hashlib.sha256(json.dumps(plan_data, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.phase = "prepared"
        self.time = 1.0
        self.phase_since = self.time
        self.enabled = False
        self.target = self.coords.home.copy()
        self.target[0] += math.radians(1)
        self.actual = self.target.copy()
        self.torque = np.zeros(len(self.target))
        self.last_stamps: tuple[float, ...] = ()
        self.index = 0
        self.results: list[dict] = []
        self.samples: list[dict] = []
        self.contact = None
        self.reason = ""

    @property
    def task(self):
        return self.plan[min(self.index, len(self.plan) - 1)]

    def _phase(self, value: str):
        self.phase = value
        self.phase_since = self.time

    def _verify(self, digest: str, phases: set[str]):
        if digest != self.digest or self.phase not in phases:
            raise ValueError("方案摘要或动作状态已变化，请核对当前页")

    def connect(self, digest: str):
        self._verify(digest, {"prepared"})
        self._feedback()
        self._phase("connected")

    def enable(self, digest: str):
        self._verify(digest, {"connected"})
        self._feedback()
        self.enabled = True
        self._phase("enabled")

    def reset(self, digest: str):
        self._verify(digest, {"enabled", "ready"})
        self._move(self.coords.home, "resetting", SETTINGS.reset_seconds)

    def start(self, digest: str):
        self._verify(digest, {"ready", "review"})
        if self.phase == "review":
            self.index += 1
            phase = "returning" if self.index == len(self.plan) else "between"
            self._move(self.coords.home, phase, SETTINGS.reset_seconds)
            return
        if self.phase == "ready" and np.max(np.abs(np.rad2deg(self.actual - self.coords.home))) > .25:
            raise ValueError("当前起点角差超出 0.25°，请先复位")
        self._approach_next()

    def _approach_next(self):
        task = self.task
        near = self.coords.home.copy()
        near[task.index] = math.radians(task.reference_deg - task.sign * SETTINGS.approach_margin_deg)
        self._move(near, "approach", 2.0)

    def abort(self, reason: str = "操作员中止"):
        if self.phase in {"completed", "aborted"}:
            return
        self.enabled = False
        self.reason = reason
        self._phase("aborted")

    def _move(self, target, phase, seconds):
        self.move_from = self.actual.copy()
        self.move_to = np.asarray(target).copy()
        distance_deg = np.max(np.abs(np.rad2deg(self.move_to - self.move_from)))
        self.move_seconds = max(seconds, 1.875 * distance_deg / SETTINGS.positioning_speed_deg_s)
        self._phase(phase)

    def _feedback(self):
        self.time = round(self.time + self.STEP, 3)
        q = self.target.copy()
        tau = np.full(len(q), .6)
        stamp = self.time
        if self.phase == "search":
            task = self.task
            offset = {"early": -4, "late": 4}.get(self.scenario, 0)
            stop = math.radians(task.reference_deg + task.sign * offset)
            if self.scenario != "missing" and task.sign * (q[task.index] - stop) >= 0:
                q[task.index] = stop
                tau[task.index] = 5.4
            if self.scenario == "stale":
                stamp -= .3
            if self.scenario == "overload":
                tau[task.index] = SETTINGS.abort_nm + 1
        motor = self.coords.motor_target(q)
        motor_torque = np.linalg.solve(self.coords.jacobian(q).T, tau)
        packets = [MotorSample(pos_rad=float(motor[i]), torque_nm=float(motor_torque[i]),
                               bus=entry.interface, motor_id=entry.motor_id, joint_name=entry.joint_name,
                               timestamp_s=stamp, feedback_age_s=self.time - stamp)
                   for i, entry in enumerate(self.coords.entries)]
        self.actual, self.torque, self.last_stamps = self.coords.decode(packets, self.time, self.last_stamps)
        return motor, motor_torque

    def _result(self, outcome, **values):
        self.results.append({**asdict(self.task), "outcome": outcome, **values})

    def tick(self):
        if self.phase not in {"resetting", "approach", "baseline", "search", "unload", "retreat", "returning", "between"}:
            return
        try:
            motor, motor_torque = self._feedback()
            if np.max(np.abs(self.torque)) >= SETTINGS.abort_nm:
                raise FeedbackError("力矩超过模拟中止门限，已停止推进")
            elapsed = self.time - self.phase_since
            self.samples.append({
                "time_s": self.time, "phase": self.phase, "axis": self.task.joint, "direction": self.task.sign,
                "position_deg": np.rad2deg(self.actual).tolist(), "target_deg": np.rad2deg(self.target).tolist(),
                "torque_nm": self.torque.tolist(), "motor_pos_rad": motor.tolist(),
                "motor_torque_nm": motor_torque.tolist(), "motor_feedback_s": list(self.last_stamps),
            })
            if self.phase in {"resetting", "approach", "returning", "between"}:
                self._tick_move(elapsed)
            elif self.phase == "baseline":
                self.baseline.append(abs(self.torque[self.task.index]))
                if elapsed >= .6 and len(self.baseline) >= 10:
                    self.contact = ContactWindow(self.task, statistics.median(self.baseline))
                    self.search_from = self.target.copy()
                    self.trace = []
                    self._phase("search")
            elif self.phase == "search":
                self._tick_search(elapsed)
            elif self.phase == "unload":
                self._prepare_retreat()
            elif self.phase == "retreat":
                self._tick_retreat(elapsed)
        except (FeedbackError, ValueError, np.linalg.LinAlgError) as exc:
            if self.phase in {"approach", "baseline", "search", "unload", "retreat"}:
                self._result("aborted", reason=str(exc))
            self.abort(str(exc))

    def _tick_move(self, elapsed):
        u = min(elapsed / self.move_seconds, 1)
        blend = u**3 * (10 - 15 * u + 6 * u*u)
        self.target = self.move_from + blend * (self.move_to - self.move_from)
        # After the target time, wait for measured feedback at the destination.
        if elapsed < self.move_seconds or np.max(np.abs(self.actual - self.move_to)) > math.radians(.10):
            return
        if self.phase == "approach":
            self.baseline = []
            self._phase("baseline")
        elif self.phase == "returning":
            self.enabled = False
            self._phase("completed")
        elif self.phase == "between":
            self._approach_next()
        else:
            self._phase("ready")

    def _tick_search(self, elapsed):
        task = self.task
        angle = math.degrees(self.actual[task.index])
        self.trace.append(self.actual.copy())
        candidate = self.contact.observe(self.time, angle, self.torque[task.index],
                                         math.degrees(self.target[task.index]))
        if candidate is not None or elapsed >= 12.0:
            self.candidate = candidate
            self.target = self.actual.copy()  # unload advancing command before retreat
            self._phase("unload")
            return
        travel = min(elapsed * SETTINGS.search_deg_s, 12.0)
        self.target = self.search_from.copy()
        self.target[task.index] += task.sign * math.radians(travel)

    def _prepare_retreat(self):
        axis, sign = self.task.index, self.task.sign
        trace = [self.actual.copy(), *reversed(self.trace)]
        distance = math.radians(SETTINGS.retreat_deg)
        for i in range(1, len(trace)):
            if sign * (trace[0][axis] - trace[i][axis]) >= distance:
                a, b = trace[i - 1], trace[i]
                exact = trace[0][axis] - sign * distance
                end = a + (b - a) * ((exact - a[axis]) / (b[axis] - a[axis]))
                self.retreat_path = np.array([*trace[:i], end])
                self.retreat_start = trace[0][axis]
                self._phase("retreat")
                return
        raise FeedbackError("没有足够的实测退路，已中止")

    def _tick_retreat(self, elapsed):
        # Replay the recorded path, with speed no faster than the forward search.
        point = min(elapsed / self.STEP, len(self.retreat_path) - 1)
        i = int(point)
        a, b = self.retreat_path[i], self.retreat_path[min(i + 1, len(self.retreat_path) - 1)]
        self.target = a + (point - i) * (b - a)
        if point < len(self.retreat_path) - 1:
            return
        task = self.task
        retreat = task.sign * math.degrees(self.retreat_start - self.actual[task.index])
        if retreat < SETTINGS.retreat_deg - .03:
            return
        if self.candidate is None:
            self._result("not_detected", retreat_deg=retreat)
        else:
            delta = self.candidate["actual_deg"] - task.reference_deg
            directed = task.sign * delta
            outcome = ("early" if directed < -SETTINGS.reference_tolerance_deg else
                       "late" if directed > SETTINGS.reference_tolerance_deg else "within_reference")
            self._result(outcome, deviation_deg=delta, retreat_deg=retreat, **self.candidate)
        self._phase("review")

    def report(self):
        recorded = len(self.results)
        return {"schema": "rp1-limit-block-check-v1", **self.plan_data, "plan_sha256": self.digest,
                "phase": self.phase, "end_reason": self.reason, "calibration_passed": None,
                "results": [*self.results, *({**asdict(t), "outcome": "uncovered"} for t in self.plan[recorded:])],
                "samples": list(self.samples)}
