"""Bounded, previewable initial motion; never enables or opens a device."""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from factory_hmi.models import StationConfig
from .manual_control import motor_positions_to_joint
from .fault_diagnosis import DiagnosisThresholds


@dataclass(frozen=True)
class FirstFrameApproach:
    origin: np.ndarray
    target: np.ndarray
    duration_s: float
    position_tolerance_rad: float
    speed_tolerance_rad_s: float
    stable_seconds: float
    settling_timeout_s: float
    speed_limits: np.ndarray
    position_tolerance_source: str = "station_config"

    @property
    def required(self) -> bool:
        return self.duration_s > 0.

    def at(self, elapsed_s: float) -> np.ndarray:
        x = min(1., max(0., elapsed_s / self.duration_s)) if self.required else 1.
        blend = x ** 3 * (10. + x * (-15. + 6. * x))
        return self.origin + (self.target - self.origin) * blend

    def to_dict(self) -> dict:
        return {"required": self.required, "duration_s": self.duration_s,
            "max_distance_rad": float(np.max(np.abs(self.target - self.origin))),
            "origin_rad": self.origin.tolist(), "target_rad": self.target.tolist(),
            "speed_limits_rad_s": self.speed_limits.tolist(),
            "position_tolerance_rad": np.asarray(self.position_tolerance_rad).tolist(),
            "position_tolerance_source": self.position_tolerance_source,
            "precise_position_required": self.position_tolerance_source != "runtime_tracking_limit",
            "speed_tolerance_rad_s": self.speed_tolerance_rad_s,
            "stable_seconds": self.stable_seconds, "settling_timeout_s": self.settling_timeout_s}


def plan_first_frame_approach(config: StationConfig, origin, target, *, parameters=None) -> FirstFrameApproach:
    count = len(config.entries)
    origin, target = np.asarray(origin, dtype=float), np.asarray(target, dtype=float)
    if origin.shape != (count,) or target.shape != (count,) or not np.all(np.isfinite([origin, target])):
        raise ValueError("首帧接近需要完整、有限的当前位置和目标")
    raw = config.raw.get("factory_hmi", {}).get("first_frame_approach", {})
    if not isinstance(raw, dict):
        raise ValueError("first_frame_approach 必须为配置对象")

    def positive(name, default):
        value = raw.get(name, default)
        if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError(f"first_frame_approach.{name} 必须为正有限值")
        return float(value)

    def vector(value):
        result = np.broadcast_to(np.asarray(value, dtype=float), (count,)).copy()
        if not np.all(np.isfinite(result)) or np.any(result <= 0):
            raise ValueError("首帧接近的速度/加速度上限必须为正有限值")
        return result

    speed = vector(positive("speed_rad_s", math.radians(10.)))
    limits = config.safety_limits
    if limits.max_velocity_rad_s is not None:
        speed = np.minimum(speed, vector(limits.max_velocity_rad_s))
    if limits.max_frame_delta_rad is not None:
        speed = np.minimum(speed, vector(limits.max_frame_delta_rad) * config.control_rate_hz)
    if parameters is not None:
        speed = np.minimum(speed, vector(parameters.values["max_velocity_rad_s"]))
    # Validate the complete path in motor and joint coordinates. This does not
    # invent absent mechanical limits, and never wraps angles across +/-pi.
    for u in np.linspace(0., 1., 101):
        motor = origin + (target - origin) * u
        if parameters is not None:
            parameters.check_target(config, motor)
        joint = (motor_positions_to_joint(config, motor)
            if limits.joint_min_rad is not None or limits.joint_max_rad is not None else motor)
        for values, low, high in ((motor, limits.motor_min_rad, limits.motor_max_rad),
                                 (joint, limits.joint_min_rad, limits.joint_max_rad)):
            if ((low is not None and np.any(values < np.asarray(low)))
                    or (high is not None and np.any(values > np.asarray(high)))):
                raise ValueError("首帧接近路径超过已配置位置限位")
    distance = np.abs(target - origin)
    duration = 0.
    if np.max(distance) > 1e-6:
        duration = max(positive("min_duration_s", 1.), float(np.max(1.875 * distance / speed)))
        if parameters is not None:
            duration = max(duration, float(np.max(np.sqrt(5.774 * distance / vector(
                parameters.values["max_acceleration_rad_s2"])))))
    # Ordinary MIT playback can have a persistent load-dependent position
    # offset. Reuse its configured running tracking envelope unless the station
    # explicitly supplies a precise first-frame tolerance; do not invent a
    # calibration requirement from a generic 0.03-rad default.
    if raw.get("position_tolerance_rad") is not None:
        tolerance = positive("position_tolerance_rad", .03)
        tolerance_source = "station_config"
    else:
        tolerance = float(DiagnosisThresholds.from_config(config.raw).tracking_error_rad)
        tolerance_source = "runtime_tracking_limit"
    if not np.all(np.isfinite(tolerance)) or np.any(np.asarray(tolerance) <= 0):
        raise ValueError("首帧过渡需要有效的工位运行跟踪范围或明确到位误差")
    return FirstFrameApproach(origin.copy(), target.copy(), duration,
        tolerance, positive("speed_tolerance_rad_s", .03),
        positive("stable_seconds", .2), positive("settling_timeout_s", 5.), speed, tolerance_source)
