"""Operator-selected model trajectory for normal arm completion.

Model geometry is not a physical calibration. Selection requires an explicit
per-run confirmation; no station YAML is marked verified by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import hashlib
import json
import math

import numpy as np

from .manual_control import joint_positions_to_motor, motor_positions_to_joint

MODE = 'mujoco_hanging'
# Software arrival criteria, not mechanical specifications. These are exposed
# before confirmation and in the completion evidence.
POSITION_TOLERANCE = .03
SPEED_TOLERANCE = .03
STABLE_SECONDS = 1.
TIMEOUT_SECONDS = 30.

ARM_LIMBS = frozenset({'left_arm', 'right_arm', 'left_short_arm', 'right_short_arm'})


def pose_description(limb):
    data, _ = _resource()
    preset = data['presets'].get(limb)
    if preset is None:
        raise ValueError('当前部位没有 MuJoCo 下垂预设')
    angle = math.degrees(preset['joint_positions'][-1][3])
    return (f"肘关节目标 {angle:+.2f}°，其余关节回模型零位；预览轨迹 5 秒。\n"
            "请确认当前设备的方向、零偏、安装及线缆适用，停放姿态可安全撤力。\n"
            "到位误差不超过 0.03 rad、速度不超过 0.03 rad/s，持续新鲜反馈稳定 1 秒后软件失能。")


@lru_cache(maxsize=1)
def _resource():
    path = resources.files('factory_hmi').joinpath('resources/completion/mujoco_hanging.json')
    raw = path.read_bytes()
    value = json.loads(raw)
    if value.get('schema') != 'rp1-mujoco-completion-v1':
        raise ValueError('MuJoCo 收尾资源格式无效')
    return value, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ReturnPlan:
    config: object
    joint_positions: np.ndarray
    timestamps: np.ndarray
    bounds: np.ndarray
    revision: str
    resource_sha256: str
    source: dict
    bridge_seconds: float
    start_motor: np.ndarray
    initial_velocity: np.ndarray

    @property
    def duration(self):
        return self.bridge_seconds + float(self.timestamps[-1])

    @property
    def target_motor(self):
        return joint_positions_to_motor(self.config, self.joint_positions[-1])

    def check_target(self, motor, *, actual=False):
        joint = motor_positions_to_joint(self.config, np.asarray(motor, dtype=float))
        tolerance = POSITION_TOLERANCE if actual else 1e-8
        if (not np.all(np.isfinite(joint)) or np.any(joint < self.bounds[:, 0] - tolerance)
                or np.any(joint > self.bounds[:, 1] + tolerance)):
            raise ValueError('MuJoCo 收尾位置超出所选模型范围')
        limits = self.config.safety_limits
        for position, lower, upper in ((motor, limits.motor_min_rad, limits.motor_max_rad),
                                       (joint, limits.joint_min_rad, limits.joint_max_rad)):
            if ((lower is not None and np.any(position < np.asarray(lower) - tolerance))
                    or (upper is not None and np.any(position > np.asarray(upper) + tolerance))):
                raise ValueError('MuJoCo 收尾位置超出本机已配置限位')

    def at(self, elapsed):
        if self.bridge_seconds and elapsed < self.bridge_seconds:
            u = max(0., elapsed / self.bridge_seconds)
            blend = u**3 * (10 + u * (-15 + 6*u))
            velocity_blend = u - 6*u**3 + 8*u**4 - 3*u**5
            first = joint_positions_to_motor(self.config, self.joint_positions[0])
            return (self.start_motor + (first-self.start_motor)*blend
                    + self.initial_velocity*self.bridge_seconds*velocity_blend)
        t = max(0., elapsed-self.bridge_seconds)
        joint = np.array([np.interp(t, self.timestamps, self.joint_positions[:, index])
                          for index in range(self.joint_positions.shape[1])])
        return joint_positions_to_motor(self.config, joint)

    def evidence(self):
        from .motion_completion import configuration_sha256
        return {'source': MODE, 'preset_revision': self.revision,
                'configuration_sha256': configuration_sha256(self.config),
                'preset_sha256': self.resource_sha256, 'model_source': self.source,
                'planned_return_seconds': self.duration,
                'connection_seconds': self.bridge_seconds,
                'parking_joint_rad': self.joint_positions[-1].tolist(),
                'parking_motor_rad': self.target_motor.tolist(),
                'position_tolerance_rad': POSITION_TOLERANCE,
                'speed_tolerance_rad_s': SPEED_TOLERANCE,
                'stable_seconds': STABLE_SECONDS, 'timeout_seconds': TIMEOUT_SECONDS,
                'confirmation_scope': 'operator_confirmed_selected_model_pose_not_product_calibration'}


def preset_info(config):
    data, digest = _resource()
    preset = data['presets'].get(config.limb)
    names = [entry.joint_name for entry in config.entries]
    if not preset or names != preset['joint_names']:
        return {'available': False, 'reason': '当前部位或关节映射不适用双臂 MuJoCo 下垂预设'}
    return {'available': True, 'mode': MODE, 'revision': data['revision'],
            'preset_sha256': digest,
            'target_joint_deg': np.rad2deg(preset['joint_positions'][-1]).tolist(),
            'duration_seconds': float(data['timestamps'][-1]),
            'position_tolerance_rad': POSITION_TOLERANCE, 'speed_tolerance_rad_s': SPEED_TOLERANCE,
            'stable_seconds': STABLE_SECONDS, 'requires_pose_confirmation': True}


def plan_return(config, start, velocity=None):
    data, digest = _resource()
    if not preset_info(config)['available']:
        raise ValueError('当前部位或关节映射不适用 MuJoCo 下垂预设')
    preset = data['presets'][config.limb]
    positions = np.asarray(preset['joint_positions'], dtype=float)
    times = np.asarray(data['timestamps'], dtype=float)
    bounds = np.asarray(preset['model_joint_bounds_rad'], dtype=float)
    count = len(config.entries)
    if (positions.shape != (len(times), count) or bounds.shape != (count, 2)
            or not np.all(np.isfinite(positions)) or not np.all(np.isfinite(times))
            or not np.all(np.isfinite(bounds)) or times[0] != 0 or np.any(np.diff(times) <= 0)
            or np.any(bounds[:, 0] >= bounds[:, 1])):
        raise ValueError('MuJoCo 收尾资源参数无效')
    start = np.asarray(start, dtype=float)
    velocity = np.zeros(count) if velocity is None else np.asarray(velocity, dtype=float)
    if start.shape != (count,) or velocity.shape != (count,) or not np.all(np.isfinite(start)) or not np.all(np.isfinite(velocity)):
        raise ValueError('MuJoCo 收尾起始位置或速度无效')
    first = joint_positions_to_motor(config, positions[0])
    peak = float(np.max(np.abs(np.diff(positions, axis=0)/np.diff(times)[:, None])))
    if not math.isfinite(peak) or peak <= 0:
        raise ValueError('MuJoCo 收尾资源缺少有效运动')
    configured_speed = config.safety_limits.max_velocity_rad_s
    speed_limit = np.full(count, peak) if configured_speed is None else np.asarray(configured_speed, dtype=float)
    frame_limit = config.safety_limits.max_frame_delta_rad
    if frame_limit is not None:
        speed_limit = np.minimum(speed_limit, np.asarray(frame_limit,dtype=float)*config.control_rate_hz)
    if not np.all(np.isfinite(speed_limit)) or np.any(speed_limit <= 0):
        raise ValueError('当前配置速度上限无效')
    bridge = 0. if np.allclose(start, first, atol=1e-8, rtol=0) and np.max(abs(velocity)) < 1e-8 else max(
        3., float(np.max(1.875*np.abs(first-start)/np.minimum(speed_limit, peak))))
    plan = ReturnPlan(config, positions, times, bounds, data['revision'], digest,
                      data['source'], bridge, start.copy(), velocity.copy())
    plan.check_target(start)
    if plan.duration + STABLE_SECONDS >= TIMEOUT_SECONDS:
        raise ValueError('MuJoCo 收尾接续所需时间超出预算，请先安全回默认位')
    # Validate the connection and the selected trajectory before sending any
    # new command. A fast manual finish with no bounded connection stays held.
    samples = np.array([plan.at(t) for t in np.linspace(0., plan.duration, max(1001, int(plan.duration*200)+1))])
    for target in samples:
        plan.check_target(target)
    actual_dt = plan.duration/(len(samples)-1)
    if np.any(np.abs(np.diff(samples,axis=0)/actual_dt) > speed_limit + 1e-6):
        raise ValueError('MuJoCo 收尾接续速度超出本机配置上限')
    return plan
