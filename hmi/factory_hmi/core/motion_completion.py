"""Verified motion limits and bounded normal-completion primitives.

No product return defaults live here. Missing optional completion data retains
normal playback and end-position hold; an invalid verified profile still blocks.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

import numpy as np

from factory_hmi.models import StationConfig, TrajectoryData
from .manual_control import joint_positions_to_motor, motor_positions_to_joint


class MotionParameterError(ValueError):
    pass


def configuration_sha256(config: StationConfig) -> str:
    raw = deepcopy(config.raw)
    hmi = raw.get('factory_hmi')
    if isinstance(hmi, dict):
        hmi.pop('motion_completion', None)
        if not hmi:
            raw.pop('factory_hmi', None)
    # Effective entries bind GUI sign/zero-offset overrides, not only source YAML.
    entries = [asdict(entry) for entry in config.entries]
    for entry in entries:
        if entry.get('lro_protocol') is None:
            entry.pop('lro_protocol', None)
    payload = {'limb': config.limb, 'raw': raw,
               'entries': entries,
               'control_rate_hz': config.control_rate_hz}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class MotionParameters:
    parameter_revision: str
    configuration_sha256: str
    values: dict[str, np.ndarray]
    stable_seconds: float
    timeout_seconds: float
    feedback_stale_seconds: float
    parking_motor_rad: np.ndarray

    @classmethod
    def load(cls, config: StationConfig) -> 'MotionParameters':
        raw = config.raw.get('factory_hmi', {}).get('motion_completion', {})
        if not isinstance(raw, dict) or raw.get('verified') is not True:
            raise MotionParameterError('运动参数尚未核实：禁止 arm/start')
        if raw.get('limb') != config.limb:
            raise MotionParameterError('运动参数部位与当前配置不一致')
        if raw.get('safe_to_disable') is not True:
            raise MotionParameterError('停放姿态尚未确认可安全撤力')
        revision = str(raw.get('parameter_revision') or '').strip()
        if not revision or not str(raw.get('verification_evidence') or '').strip():
            raise MotionParameterError('缺少参数版本或核实依据')
        digest = configuration_sha256(config)
        if raw.get('configuration_sha256') != digest:
            raise MotionParameterError('运动参数配置哈希不符：重新核实映射、零偏及配置')
        count = len(config.entries)
        names = ('joint_min_rad', 'joint_max_rad', 'motor_min_rad', 'motor_max_rad',
                 'max_velocity_rad_s', 'max_acceleration_rad_s2',
                 'return_velocity_rad_s', 'return_acceleration_rad_s2',
                 'parking_joint_rad', 'position_tolerance_rad',
                 'speed_tolerance_rad_s', 'max_first_frame_delta_rad')
        values = {}
        for name in names:
            try:
                value = np.asarray(raw[name], dtype=float)
                if value.ndim == 0:
                    value = np.full(count, float(value))
                if value.shape != (count,) or not np.all(np.isfinite(value)):
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise MotionParameterError(f'{name} 必须显式提供 {count} 个有限值或有限标量') from None
            values[name] = value
        for name in names[4:8] + names[9:]:
            if np.any(values[name] <= 0):
                raise MotionParameterError(f'{name} 必须大于零')
        for prefix in ('joint', 'motor'):
            if np.any(values[prefix+'_min_rad'] >= values[prefix+'_max_rad']):
                raise MotionParameterError(f'{prefix} 上下限无效')
        if np.any(values['return_velocity_rad_s'] > values['max_velocity_rad_s']) or np.any(
                values['return_acceleration_rad_s2'] > values['max_acceleration_rad_s2']):
            raise MotionParameterError('回位速度/加速度不能超过已核实运动上限')
        timing = []
        for name in ('stable_seconds', 'timeout_seconds', 'feedback_stale_seconds'):
            try:
                value = float(raw[name])
            except (KeyError, ValueError, TypeError):
                raise MotionParameterError(f'缺少有效 {name}') from None
            if not math.isfinite(value) or value <= 0:
                raise MotionParameterError(f'{name} 必须为正有限值')
            timing.append(value)
        if timing[1] <= timing[0]:
            raise MotionParameterError('timeout_seconds 必须大于 stable_seconds')
        parking = joint_positions_to_motor(config, values['parking_joint_rad'])
        result = cls(revision, digest, values, *timing, parking)
        result.check_target(config, parking)
        return result

    def check_target(self, config: StationConfig, motor: np.ndarray) -> None:
        motor = np.asarray(motor, dtype=float)
        if motor.shape != self.parking_motor_rad.shape or not np.all(np.isfinite(motor)):
            raise MotionParameterError('运动目标无效')
        joint = motor_positions_to_joint(config, motor)
        for prefix, value in (('motor', motor), ('joint', joint)):
            if np.any(value < self.values[prefix+'_min_rad'] - 1e-10) or np.any(
                    value > self.values[prefix+'_max_rad'] + 1e-10):
                raise MotionParameterError(f'运动目标超过已核实 {prefix} 限位')

    def check_trajectory(self, config: StationConfig, trajectory: TrajectoryData,
                         current: np.ndarray | None = None, speed: float = 1.0,
                         loop: bool = False, check_dynamics: bool = True, *,
                         check_velocity: bool = True) -> None:
        for row in trajectory.motor_pos:
            self.check_target(config, row)
        if current is not None:
            if np.any(np.abs(trajectory.motor_pos[0] - current) > self.values['max_first_frame_delta_rad']):
                raise MotionParameterError('首帧与实际位置差超过已核实限值')
        dt = np.diff(trajectory.timestamps)
        if dt.size and check_dynamics:
            velocity = np.diff(trajectory.motor_pos, axis=0) / dt[:, None] * speed
            if check_velocity and np.any(np.abs(velocity) > self.values['max_velocity_rad_s'] + 1e-9):
                raise MotionParameterError('轨迹速度超过已核实上限')
            # Reserve a verified braking distance for operator finish at any
            # point of every piecewise-linear segment. Never discover an
            # outward stopping target only after a real run has begun.
            stopping_offset = velocity * np.abs(velocity) / (2 * self.values['return_acceleration_rad_s2'])
            for endpoints in (trajectory.motor_pos[:-1], trajectory.motor_pos[1:]):
                for row in endpoints + stopping_offset:
                    self.check_target(config, row)
            if len(velocity) > 1:
                acceleration = np.diff(velocity, axis=0) / ((dt[:-1] + dt[1:])[:, None] / 2) * speed
                if np.any(np.abs(acceleration) > self.values['max_acceleration_rad_s2'] + 1e-9):
                    raise MotionParameterError('轨迹加速度超过已核实上限')
        if loop and not np.allclose(trajectory.motor_pos[0], trajectory.motor_pos[-1], atol=1e-8, rtol=0):
            raise MotionParameterError('循环轨迹首尾不连续，不能跳转开启下一周期')

    def return_duration(self, start: np.ndarray) -> float:
        distance = np.abs(self.parking_motor_rad - start)
        # Quintic smoothstep maxima: 1.875 for velocity, 5.774 for acceleration.
        return max(float(np.max(1.875 * distance / self.values['return_velocity_rad_s'])),
                   float(np.max(np.sqrt(5.774 * distance / self.values['return_acceleration_rad_s2']))), 1e-6)


def automatic_completion_parameters(config: StationConfig) -> MotionParameters | None:
    """Old station files retain normal playback; missing return data cannot disable torque.

    An explicitly verified profile must still pass every check. Never silently
    fall back when that profile is stale, malformed or outside verified limits.
    """
    raw = config.raw.get('factory_hmi', {}).get('motion_completion')
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MotionParameterError('自动收尾参数必须为配置对象')
    verified = raw.get('verified')
    if verified is None or verified is False:
        return None
    if verified is not True:
        raise MotionParameterError('自动收尾 verified 必须为布尔值')
    return MotionParameters.load(config)


def motion_readiness(config: StationConfig) -> dict[str, Any]:
    try:
        params = automatic_completion_parameters(config)
        if params is None:
            return {'ready': False, 'test_allowed': True,
                    'reason': '自动回位/失能未配置：保留原有启动预检，测试结束后保持位置，不自动失能',
                    'parameter_revision': None, 'configuration_sha256': configuration_sha256(config)}
        return {'ready': True, 'test_allowed': True, 'reason': None, 'parameter_revision': params.parameter_revision,
                'configuration_sha256': params.configuration_sha256}
    except (ValueError, TypeError, KeyError) as exc:
        return {'ready': False, 'test_allowed': False, 'reason': str(exc), 'parameter_revision': None,
                'configuration_sha256': configuration_sha256(config)}
