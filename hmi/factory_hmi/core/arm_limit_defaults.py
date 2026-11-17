"""Executable left-arm defaults: production references and feedback, not NPZ playback."""
import math

from .limit_block_check import Coordinates
from .live_limit_probe import ProbePoint, station_sha
from .production_parameters import load_script


def builtin_arm_profile(config):
    from .module_limit_check import ModuleProfile, ScheduledPoint

    if config.limb not in {"left_arm", "left_short_arm"}:
        raise ValueError("内置从零限位搜索仅支持左臂 5／7 轴")
    coords = Coordinates(config)
    count = len(coords.names)
    zero = (0.,) * count
    hmi = config.raw.get("factory_hmi", {})
    options = hmi.get("limit_check", {})
    # Inherit the station protection and the user's production-script torque
    # range. These are software caps, not fabricated mechanical peak ratings.
    cap = float(hmi.get("faults", {}).get("high_torque_nm", 35.))
    peaks = options.get("joint_peak_nm")
    if peaks is not None:
        if set(peaks) != set(coords.names):
            raise ValueError("关节峰值力矩须按名称完整覆盖当前模块")
        peaks = tuple(float(peaks[name]) for name in coords.names)
        if not all(math.isfinite(x) and x > 0 for x in peaks):
            raise ValueError("关节峰值力矩须为有限正数")
    script = load_script()
    limits = []
    for i, entry in enumerate(coords.entries):
        if entry.motor_type != "LRO" or entry.motor_model not in script.MODEL_PARAMS:
            raise ValueError(f"{entry.joint_name} 没有产线型号参数")
        if not math.isfinite(entry.kp) or entry.kp <= 0 or not math.isfinite(entry.kd) or entry.kd < 0:
            raise ValueError(f"{entry.joint_name} 的 Kp/Kd 无效")
        limit = min(cap, script.MODEL_PARAMS[entry.motor_model]["tau_max"])
        if peaks is not None:
            limit = min(limit, peaks[i])
        if not math.isfinite(limit) or limit <= .5:
            raise ValueError(f"{entry.joint_name} 的中止门限不能容纳接触判定")
        limits.append(limit)
    windows = []
    for d in coords.directions:
        point = ProbePoint(
            joint=d.joint, direction=d.sign, full_range_search=True,
            staging_pose_deg=zero, search_pose_deg=zero,
            permitted_travel_deg=abs(d.reference_deg) + 2.9,
            search_speed_deg_s=5., positioning_speed_deg_s=5.,
            reset_kp_scale=1., reset_kd_scale=1., search_kp_scale=1., search_kd_scale=1.,
            motor_abort_nm=limits, joint_abort_nm=limits,
            contact_floor_nm=.5, contact_rise_nm=.5, following_error_deg=5.,
            motor_step_deg=.5, retreat_deg=1., comparison_tolerance_deg=3.)
        windows.append(ScheduledPoint(point=point))
    return ModuleProfile(schema_version=1, limb=config.limb, model="RP1.3.4", power_source="existing_station",
        fixture="当前模块台架", revision="arm-zero-feedback-v1", source="builtin_reference",
        torque_limit_source="station_cap_and_production_range" + ("_and_joint_peak" if peaks else ""),
        station_sha256=station_sha(config), home_deg=zero, joint_peak_nm=peaks,
        windows=windows, transfer_speed_deg_s=10., use_builtin_order=True)
