"""Built-in whole-module feedback searches, using named route geometry only.

Route resources supply order and clearance poses, never simulated stop angles
or NPZ playback. Torque caps are software limits, not mechanical peak ratings.
"""
import json
import math
from importlib import resources

import numpy as np

from factory_hmi.limit_replay_assets import PRESET_KEYS, RESOURCE_ROOT, read_manifest, verified_asset
from .arm_limit_defaults import builtin_arm_profile
from .limit_block_check import Coordinates
from .live_limit_probe import ProbePoint, station_sha
from .production_parameters import load_script


def _pose(values, count, *, axis=None):
    if not isinstance(values, (tuple, list)) or len(values) != count:
        raise ValueError("内置避让姿态与当前模块关节数量不一致")
    if axis is not None and tuple(i for i, x in enumerate(values) if x is None) != (axis,):
        raise ValueError("内置保持姿态须仅留空当前受检关节")
    try:
        result = tuple(0. if i == axis else float(x) for i, x in enumerate(values))
    except (TypeError, ValueError) as exc:
        raise ValueError("内置避让姿态包含无效角度") from exc
    if not all(math.isfinite(x) for x in result):
        raise ValueError("内置避让姿态包含无效角度")
    return result


def _route(coords):
    try:
        asset = read_manifest(RESOURCE_ROOT)[coords.station.limb]["route"]
        _, raw = verified_asset(RESOURCE_ROOT, asset)
        route = json.loads(raw)
        if route["limb"] != coords.station.limb or tuple(route["joint_names"]) != coords.names:
            raise ValueError("内置检查顺序与当前模块具名关节映射不一致")
        expected = {(d.joint, d.sign) for d in coords.directions}
        keys = [(r["joint"], r["direction"]) for r in route["windows"]]
        if set(keys) != expected or len(keys) != len(expected):
            raise ValueError("内置检查顺序须完整覆盖当前模块全部上下限")
        for row in route["windows"]:
            _pose(row["held_pose_deg"], len(coords.names), axis=coords.names.index(row["joint"]))
            if not isinstance(row["via_deg"], (tuple, list)) or len(row["via_deg"]) > 12:
                raise ValueError("内置转场航点无效")
            for p in row["via_deg"]:
                _pose(p, len(coords.names))
        return route["windows"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("内置模块检查顺序资源无效") from exc


def _torque_caps(coords):
    hmi = coords.station.raw.get("factory_hmi", {})
    cap = float(hmi.get("faults", {}).get("high_torque_nm", 35.))
    if not math.isfinite(cap) or cap <= .5:
        raise ValueError("工位中止力矩须为能容纳接触判定的有限正数")
    supplied = hmi.get("limit_check", {}).get("joint_peak_nm")
    peaks = None
    if supplied is not None:
        if not isinstance(supplied, dict) or set(supplied) != set(coords.names):
            raise ValueError("关节峰值力矩须按名称完整覆盖当前模块")
        peaks = tuple(float(supplied[name]) for name in coords.names)
        if not all(math.isfinite(x) and x > 0 for x in peaks):
            raise ValueError("关节峰值力矩须为有限正数")
    script = load_script()
    motors, joints = [], []
    for i, entry in enumerate(coords.entries):
        if entry.motor_type != "LRO" or entry.motor_model not in script.MODEL_PARAMS:
            raise ValueError(f"{entry.joint_name} 没有产线型号参数")
        if not math.isfinite(entry.kp) or entry.kp <= 0 or not math.isfinite(entry.kd) or entry.kd < 0:
            raise ValueError(f"{entry.joint_name} 的 Kp/Kd 无效")
        motor = min(cap, float(script.MODEL_PARAMS[entry.motor_model]["tau_max"]))
        # A coupled joint does not get an invented, larger sum of motor ratings.
        # Keep the conservative station/model cap and honour explicit joint peaks.
        joint = min(motor, peaks[i]) if peaks is not None else motor
        if min(motor, joint) <= .5 or not all(math.isfinite(x) for x in (motor, joint)):
            raise ValueError(f"{entry.joint_name} 的中止门限不能容纳接触判定")
        motors.append(motor)
        joints.append(joint)
    return tuple(motors), tuple(joints), peaks


def _hanging_elbow(coords):
    try:
        data = json.loads(resources.files("factory_hmi").joinpath(
            "resources/completion/mujoco_hanging.json").read_text())
        preset = data["presets"][coords.station.limb]
        if tuple(preset["joint_names"]) != coords.names:
            raise ValueError("肩 yaw 下垂姿态与当前模块具名关节映射不一致")
        index = coords.names.index("right_elbow_joint")
        value = math.degrees(float(preset["joint_positions"][-1][index]))
        if not math.isfinite(value):
            raise ValueError("肩 yaw 下垂肘角无效")
        return index, value
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("肩 yaw 下垂姿态资源无效") from exc


def builtin_module_profile(config):
    """Return an executable fallback; callers must prefer valid site profiles."""
    from .module_limit_check import ModuleProfile, ScheduledPoint

    if config.limb in {"left_arm", "left_short_arm"}:
        return builtin_arm_profile(config)
    if config.limb not in PRESET_KEYS:
        raise ValueError("当前模块没有内置整模块限位方案")
    coords = Coordinates(config, include_waist_holds=True)
    count = len(coords.names)
    home = tuple(float(x) for x in np.rad2deg(coords.home))
    if len(home) != count or not all(math.isfinite(x) for x in home):
        raise ValueError("当前配置缺少完整有效的默认关节姿态")
    motor_caps, joint_caps, peaks = _torque_caps(coords)
    directions = {(d.joint, d.sign): d for d in coords.directions}
    hanging = _hanging_elbow(coords) if config.limb in {"right_arm", "right_short_arm"} else None
    windows = []
    for row in _route(coords):
        d = directions[(row["joint"], row["direction"])]
        pose = list(_pose(row["held_pose_deg"], count, axis=d.index))
        via = [_pose(p, count) for p in row["via_deg"]]
        if config.limb == "waist_hip":
            # Only waist roll/yaw are inspected; configured hips remain fixed.
            pose[2:] = home[2:]
            via = [(*p[:2], *home[2:]) for p in via]
        if hanging is not None and d.joint == "right_shoulder_yaw_joint":
            elbow, angle = hanging
            pose[elbow] = angle
            via = [tuple(angle if i == elbow else x for i, x in enumerate(p)) for p in via]
        pose = tuple(pose)
        travel = d.sign * (d.reference_deg - pose[d.index])
        if travel <= 0:
            raise ValueError("内置搜索起点须位于量产参考角内侧")
        point = ProbePoint(
            joint=d.joint, direction=d.sign, full_range_search=True,
            staging_pose_deg=pose, search_pose_deg=pose,
            permitted_travel_deg=travel+2.9,
            search_speed_deg_s=10., positioning_speed_deg_s=20.,
            reset_kp_scale=1., reset_kd_scale=1., search_kp_scale=1., search_kd_scale=1.,
            motor_abort_nm=motor_caps, joint_abort_nm=joint_caps,
            contact_floor_nm=.5, contact_rise_nm=.5, following_error_deg=5.,
            motor_step_deg=.5, retreat_deg=1., comparison_tolerance_deg=3.)
        windows.append(ScheduledPoint(point=point, via_deg=via))
    return ModuleProfile(
        schema_version=1, limb=config.limb, model="RP1.3.4", power_source="existing_station",
        fixture="当前模块台架", revision="all-module-feedback-v1", source="builtin_reference",
        torque_limit_source="station_cap_and_production_range" + ("_and_joint_peak" if peaks else ""),
        station_sha256=station_sha(config), home_deg=home, joint_peak_nm=peaks,
        windows=windows, transfer_speed_deg_s=20.)
