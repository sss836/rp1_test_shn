"""Frozen, trajectory-specific factory quality assessment from recorded metrics."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from typing import Any


POLICY_SCHEMA = "factory-quality-policy-v1"
CRITERIA_VERSION = "factory-quality-v1"
_SCOPE_NOTE = (
    "阈值来自约四小时参考记录，适用于相同部位、轨迹、倍率和20 Hz记录。"
    "装夹、负载和散热条件需现场保持一致，温升按相同运行时长比较。"
)
_METRICS = (
    ("tracking_rms_rad", "电机位置误差 RMS", "rad"),
    ("tracking_p99_abs_rad", "电机绝对位置误差 P99", "rad"),
    ("temperature_peak_c", "最高温度", "°C"),
    ("temperature_peak_rise_c", "峰值温升", "°C"),
    ("torque_p999_abs_nm", "绝对力矩 P99.9", "N·m"),
)
# Rounded margins from the supplied normal records. The tuple order is RMS,
# absolute tracking P99 and absolute torque P99.9, in rad, rad and N.m.
_PROFILES = {
    "right_short_arm": {
        "id": "right-short-arm-reference-v1",
        "label": "右短臂参考轨迹（用户确认1倍，执行节拍待核对）",
        "trajectory_sha256": "6b5b447399d48233c5c27571c67cbc7c0ef6a984a954f34e27b1cd1434945a84",
        "speed": 1.0,
        "timing_verified": False,
        "reference_run_count": 1,
        "limits": {
            "right_shoulder_pitch_joint": (0.10, 0.30, 11.0),
            "right_shoulder_roll_joint": (0.07, 0.20, 9.0),
            "right_shoulder_yaw_joint": (0.07, 0.25, 2.0),
            "right_elbow_joint": (0.05, 0.20, 2.0),
            "right_wrist_roll_joint": (0.11, 0.35, 2.0),
        },
    },
    "right_short_leg": {
        "id": "right-short-leg-reference-v1",
        "label": "右短腿参考轨迹（用户确认1倍，执行节拍待核对）",
        "trajectory_sha256": "8a45ed836d1557fd149cf15b65b812d8d1cbd935189a5e1c88ef01a405d70d3b",
        "speed": 1.0,
        "timing_verified": False,
        "reference_run_count": 1,
        "limits": {
            "right_hip_roll_joint": (0.22, 0.35, 32.0),
            "right_hip_yaw_joint": (0.05, 0.10, 20.0),
            "right_knee_joint": (0.08, 0.20, 11.0),
            "right_ankle_pitch_joint": (0.06, 0.15, 2.0),
            "right_ankle_roll_joint": (0.07, 0.15, 2.0),
        },
    },
    "waist_hip": {
        "id": "waist-hip-reference-v1",
        "label": "腰胯参考轨迹 1 倍",
        "trajectory_sha256": "cf68d2c8b21e65ea89d75b1017d81237a3ee09c15acf60a256cac2ab5d68aaf0",
        "speed": 1.0,
        "reference_run_count": 2,
        "limits": {
            "waist_roll_joint": (0.07, 0.25, 3.0),
            "waist_yaw_joint": (0.11, 0.40, 8.0),
            "right_hip_pitch_joint": (0.13, 0.45, 5.0),
            "left_hip_pitch_joint": (0.15, 0.45, 6.0),
        },
    },
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _identity(context: Mapping[str, Any]) -> dict[str, Any]:
    trajectory = context.get("trajectory")
    request = context.get("run_request")
    entries = context.get("entries")
    names = []
    if isinstance(entries, list):
        names = [str(entry.get("joint_name") or "") for entry in entries if isinstance(entry, Mapping)]
    return {
        "limb": str(context.get("limb") or context.get("module_name") or ""),
        "trajectory_sha256": str(trajectory.get("sha256") or "") if isinstance(trajectory, Mapping) else "",
        "speed": _number(request.get("speed")) if isinstance(request, Mapping) else None,
        "record_rate_hz": _number(context.get("record_rate_hz")),
        "joint_names": sorted(names),
    }


def freeze_factory_quality(context: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze reference thresholds and matching evidence before a factory run."""
    identity = _identity(context)
    result = {
        "schema_version": POLICY_SCHEMA,
        "criteria_version": CRITERIA_VERSION,
        "matched": False,
        "reason": "",
        "identity": identity,
        "profile": None,
    }
    reference = _PROFILES.get(identity["limb"])
    reasons = []
    if context.get("data_domain") != "FACTORY":
        reasons.append("本次记录未选择工厂模式")
    if reference is None:
        reasons.append("当前部位尚无工厂参考判据")
    else:
        if reference.get("timing_verified") is False:
            reasons.append("参考记录按用户确认的1倍设定，但实际指令周期与文件周期不一致，执行节拍待核对")
        if identity["trajectory_sha256"] != reference["trajectory_sha256"]:
            reasons.append("轨迹哈希与参考记录不一致")
        if identity["speed"] is None or not math.isclose(identity["speed"], reference["speed"], rel_tol=0, abs_tol=1e-9):
            reasons.append("运行倍率与参考记录不一致")
        if identity["joint_names"] != sorted(reference["limits"]):
            reasons.append("电机通道集与参考记录不一致")
    if identity["record_rate_hz"] is None or not math.isclose(identity["record_rate_hz"], 20.0, rel_tol=0, abs_tol=1e-9):
        reasons.append("参考判据要求20 Hz记录")
    if reasons:
        result["reason"] = "；".join(reasons)
        return result

    limits = {
        name: {
            "tracking_rms_rad": values[0],
            "tracking_p99_abs_rad": values[1],
            "torque_p999_abs_nm": values[2],
            "temperature_peak_c": 60.0,
            "temperature_peak_rise_c": 20.0,
        }
        for name, values in reference["limits"].items()
    }
    result.update(matched=True, reason="工况字段匹配参考判据", profile={
        "id": reference["id"], "label": reference["label"],
        "reference_duration_s": 14400.0,
        "reference_run_count": reference["reference_run_count"],
        "scope_note": _SCOPE_NOTE,
        "joint_limits": limits,
    })
    return result


def evaluate_factory_quality(
    context: Mapping[str, Any],
    quality: Mapping[str, Any],
    joints: Sequence[Mapping[str, Any]],
    *,
    non_monotonic: bool = False,
) -> dict[str, Any] | None:
    """Evaluate recorded factory metrics with the run's frozen reference policy."""
    policy = context.get("factory_quality_policy")
    if context.get("data_domain") != "FACTORY" or not isinstance(policy, Mapping):
        return None

    profile = policy.get("profile")
    valid_policy = (
        policy.get("schema_version") == POLICY_SCHEMA
        and policy.get("criteria_version") == CRITERIA_VERSION
        and policy.get("matched") is True
        and isinstance(profile, Mapping)
        and isinstance(profile.get("joint_limits"), Mapping)
        and policy.get("identity") == _identity(context)
    )
    if valid_policy:
        valid_policy = bool(profile["joint_limits"]) and all(
            isinstance(limits, Mapping) and all(_number(limits.get(key)) is not None for key, _, _ in _METRICS)
            for limits in profile["joint_limits"].values()
        )
    limits_by_name = profile["joint_limits"] if valid_policy else {}
    metrics = []
    for joint in joints:
        name = str(joint.get("joint_name") or "")
        limits = limits_by_name.get(name, {})
        checks = []
        for key, label, unit in _METRICS:
            value, limit = _number(joint.get(key)), _number(limits.get(key))
            if value is not None and key in {"tracking_rms_rad", "tracking_p99_abs_rad", "torque_p999_abs_nm"} and value < 0:
                value = None
            passed = None if value is None or limit is None else value <= limit
            checks.append(dict(key=key, label=label, value=value, limit=limit, unit=unit, passed=passed))
        metrics.append(dict(joint_name=name, motor_id=joint.get("motor_id"), checks=checks))
    result = {
        "schema_version": CRITERIA_VERSION,
        "status": "not_evaluated",
        "reason": "",
        "profile": deepcopy(profile) if isinstance(profile, Mapping) else None,
        "metrics": metrics,
        "execution_checks": [],
    }
    if not valid_policy:
        result["reason"] = (str(policy.get("reason")) if policy.get("matched") is False and policy.get("reason")
                            else "冻结工厂判据无效或与本次执行不匹配")
        return result

    execution_checks = result["execution_checks"]

    def check(key: str, label: str, value: Any, limit: Any, unit: str, passed: bool) -> None:
        execution_checks.append(dict(key=key, label=label, value=value, limit=limit, unit=unit, passed=passed))

    completion = context.get("motion_completion")
    cycle_finish = isinstance(completion, Mapping) and completion.get("finish_cycle_completed") is True
    outcome = str(context.get("outcome") or "")
    completed = (outcome in {"completed", "duration_complete", "cycles_complete"}
                 and not context.get("manual_stop")) or (outcome == "stopped" and cycle_finish)
    check("execution_completed", "测试正常完成", outcome, "正常结束或补圈完成", "", completed)
    faults = [_number(joint.get("fault_count")) for joint in joints]
    no_faults = (not context.get("safety_failure") and not context.get("product_failure")
                 and not context.get("terminal_fault") and outcome != "fault"
                 and bool(faults) and all(count == 0 for count in faults))
    check("fault_free", "运行故障", "无故障" if no_faults else "故障或故障证据缺失", "无故障", "", no_faults)
    check("monotonic_time", "采样时间递增", not non_monotonic, True, "", not non_monotonic)

    completeness = _number(quality.get("sample_completeness"))
    gap = _number(quality.get("max_gap_seconds"))
    count = _number(quality.get("sample_count"))
    check("sample_completeness", "采样完整率", completeness, 0.95, "ratio", completeness is not None and completeness >= 0.95)
    check("max_gap_seconds", "最大采样间隔", gap, 0.2, "s", gap is not None and 0 <= gap <= 0.2)
    names = [str(joint.get("joint_name") or "") for joint in joints]
    expected_names = sorted(limits_by_name)
    check("motor_channels", "预期电机通道", sorted(names), expected_names, "", sorted(names) == expected_names)
    complete_fields = count is not None and count > 0 and count.is_integer()
    for joint in joints:
        valid_count = _number(joint.get("factory_metric_sample_count"))
        samples = _number(joint.get("sample_count"))
        complete_fields = complete_fields and valid_count == count and samples == count
    complete_fields = complete_fields and all(item["value"] is not None for metric in metrics for item in metric["checks"])
    check("metric_fields_complete", "电机质量字段齐全", bool(complete_fields), True, "", bool(complete_fields))

    request = context.get("run_request")
    request = request if isinstance(request, Mapping) else {}
    duration, cycles = _number(request.get("duration_s")), _number(request.get("cycles"))
    active, completed_cycles = _number(context.get("active_seconds")), _number(context.get("completed_cycles"))
    check("minimum_cycle", "至少完成一圈", completed_cycles, 1, "cycles",
          completed_cycles is not None and completed_cycles.is_integer() and completed_cycles >= 1)
    request_valid = (
        (request.get("duration_s") is None or (duration is not None and duration > 0))
        and (request.get("cycles") is None or (cycles is not None and cycles >= 0 and cycles.is_integer()))
    )
    reached_duration = duration is not None and duration > 0 and active is not None and active + 1e-6 >= duration
    reached_cycles = cycles is not None and cycles > 0 and completed_cycles is not None and completed_cycles >= cycles
    has_target = (duration is not None and duration > 0) or (cycles is not None and cycles > 0)
    if outcome == "duration_complete":
        target_completed = reached_duration
    elif outcome == "cycles_complete":
        target_completed = reached_cycles
    else:
        target_completed = reached_duration or reached_cycles or not has_target
    check("execution_target", "约定结束目标", dict(active_seconds=active, completed_cycles=completed_cycles),
          dict(duration_s=duration, cycles=cycles, either_target_completes=True), "", request_valid and target_completed)

    incomplete = [item["label"] for item in execution_checks if not item["passed"]]
    if incomplete:
        result["reason"] = "未完成质量判定：" + "、".join(incomplete)
        return result
    failed = [metric["joint_name"] + " " + item["label"]
              for metric in metrics for item in metric["checks"] if item["passed"] is False]
    result["status"] = "failed" if failed else "passed"
    result["reason"] = "超出参考阈值：" + "、".join(failed) if failed else "电机误差、温度及力矩均在本次冻结参考范围内"
    return result
