"""Frozen, trajectory-specific factory quality assessment from recorded metrics."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from typing import Any


POLICY_SCHEMA = "factory-quality-policy-v1"
CRITERIA_VERSION = "factory-quality-v2"
_LEGACY_CRITERIA_VERSION = "factory-quality-v1"
_QUALITY_LABELS = {"passed": "通过", "failed": "不通过", "not_evaluated": "未判定"}
_SCOPE_NOTE = (
    "仅用于工厂质量筛查，不作为运行保护或正式放行依据。"
    "装夹、负载、散热和运行时长不同，结果不能直接比较。"
)
_METRICS = (
    ("tracking_rms_rad", "电机位置误差 RMS", "rad"),
    ("tracking_p99_abs_rad", "电机绝对位置误差 P99", "rad"),
    ("temperature_peak_c", "最高温度", "°C"),
    ("temperature_peak_rise_c", "峰值温升", "°C"),
    ("torque_p999_abs_nm", "绝对力矩 P99.9", "N·m"),
)
# Each axis is (motor ID, LRO model index, Chinese label, RMS rad, P99 rad, P99.9 N·m).
_SHORT_ARM = {
    "shoulder_pitch": (1, 2, "肩俯仰", 0.10, 0.30, 11.0),
    "shoulder_roll": (2, 1, "肩侧摆", 0.07, 0.20, 9.0),
    "shoulder_yaw": (3, 1, "肩旋转", 0.07, 0.25, 2.0),
    "elbow": (4, 1, "肘", 0.05, 0.20, 2.0),
    "wrist_roll": (5, 1, "腕旋转", 0.11, 0.35, 2.0),
}
_LONG_ARM = {
    **_SHORT_ARM,
    "wrist_yaw": (6, 0, "腕偏航", 0.15, 0.30, 7.0),
    "wrist_pitch": (7, 0, "腕俯仰", 0.11, 0.20, 8.0),
}
_SHORT_LEG = {
    "hip_roll": (3, 3, "髋侧摆", 0.22, 0.35, 32.0),
    "hip_yaw": (4, 3, "髋旋转", 0.05, 0.10, 20.0),
    "knee": (5, 3, "膝", 0.08, 0.20, 11.0),
    "ankle_pitch": (6, 1, "踝俯仰电机", 0.06, 0.15, 2.0),
    "ankle_roll": (7, 1, "踝侧摆电机", 0.07, 0.15, 2.0),
}
_WAIST_HIP = {
    "waist_roll_joint": (1, 3, "腰侧摆", 0.07, 0.25, 3.0),
    "waist_yaw_joint": (1, 3, "腰旋转", 0.11, 0.40, 8.0),
    "right_hip_pitch_joint": (2, 3, "右髋俯仰", 0.13, 0.45, 5.0),
    "left_hip_pitch_joint": (2, 3, "左髋俯仰", 0.15, 0.45, 6.0),
}
_SHORT_ARM_BASIS = "沿用旧右短臂的1倍速参考，当前轨迹已换版；左右按同名轴映射，待当前轨迹复测。"
_LONG_ARM_BASIS = (
    "前五轴沿用旧右短臂同名轴参考；腕偏航、腕俯仰取7次七轴左臂正常历史记录的"
    "逐轴最大统计量加裕量。旧记录未保存当前轨迹哈希和倍率，左右七轴均为试行。"
)
_SHORT_LEG_BASIS = "沿用右短腿1倍速参考，左右按同名轴映射；原记录实际指令节拍与文件周期有差异。"
_WAIST_BASIS = "两条同轨迹腰胯1倍速参考记录，时长约四小时。"
# Hashes are pinned to the reviewed trajectory files; replacing an NPZ in the
# catalog must not silently transfer these thresholds to a different motion.
_PROFILES = {
    "left_short_leg": ("UNIT-AGING-001", "左短腿", "a3066437b09168955c4d5fa14c690772c14afcdc87ef2ce54c739e5f0da820e4", _SHORT_LEG, _SHORT_LEG_BASIS),
    "right_short_leg": ("UNIT-AGING-002", "右短腿", "8a45ed836d1557fd149cf15b65b812d8d1cbd935189a5e1c88ef01a405d70d3b", _SHORT_LEG, _SHORT_LEG_BASIS),
    "waist_hip": ("UNIT-AGING-003", "腰胯", "cf68d2c8b21e65ea89d75b1017d81237a3ee09c15acf60a256cac2ab5d68aaf0", _WAIST_HIP, _WAIST_BASIS),
    "left_short_arm": ("UNIT-AGING-004", "左短臂", "24f44bf9f112788c700c4f189379403476d76e4f43678ee0c6af934426f5227c", _SHORT_ARM, _SHORT_ARM_BASIS),
    "right_short_arm": ("UNIT-AGING-005", "右短臂", "d9bdeda272e073146b3b8597fadc9b90a05731c40e8470ad3226587d48a7e7be", _SHORT_ARM, _SHORT_ARM_BASIS),
    "left_arm": ("UNIT-AGING-006", "左臂（7轴）", "984cf5a6764113800edf764d9f779a83e5bf29da90b9f9349e7b771b1719a746", _LONG_ARM, _LONG_ARM_BASIS),
    "right_arm": ("UNIT-AGING-007", "右臂（7轴）", "b610359b4b2813900ac4293980ca832149200ed3d63fa44a7a4257e8dc5cdbba", _LONG_ARM, _LONG_ARM_BASIS),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def quality_status_label(status: str, basis_kind: str | None = None) -> str:
    label = _QUALITY_LABELS.get(status, "未判定")
    return "试行" + label if basis_kind == "trial" and status in {"passed", "failed"} else label


def format_quality_value(value: Any, unit: str = "") -> str:
    if value is None:
        return "未记录"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        if unit == "ratio":
            return f"{value:.2%}"
        return f"{value:.8g}" + (f" {unit}" if unit else "")
    if isinstance(value, list):
        return "、".join(str(item) for item in value) or "无"
    return str(value)


def execution_check_values(check: Mapping[str, Any]) -> tuple[str, str]:
    value, limit = check["value"], check["limit"]
    if check["key"] == "execution_target":
        actual = (f"有效时长 {format_quality_value(value.get('active_seconds'), 's')}；"
                  f"完成 {format_quality_value(value.get('completed_cycles'), '圈')}")
        targets = []
        if limit.get("duration_s"):
            targets.append(f"时长 ≥ {format_quality_value(limit['duration_s'], 's')}")
        if limit.get("cycles"):
            targets.append(f"圈数 ≥ {format_quality_value(limit['cycles'], '圈')}")
        return actual, " 或 ".join(targets) or "未设定时长或圈数目标"
    if check["key"] == "execution_completed":
        value = {"duration_complete": "达到时长", "cycles_complete": "达到圈数", "completed": "正常结束",
                 "stopped": "主动结束", "fault": "故障停测"}.get(value, value)
    unit = check.get("unit", "")
    if unit == "cycles":
        unit = "圈"
    comparators = {"sample_completeness": "≥ ", "max_gap_seconds": "≤ ", "minimum_cycle": "≥ "}
    return format_quality_value(value, unit), comparators.get(check["key"], "") + format_quality_value(limit, unit)


def _identity(context: Mapping[str, Any], *, include_motors: bool = False) -> dict[str, Any]:
    trajectory = context.get("trajectory")
    request = context.get("run_request")
    entries = context.get("entries")
    channels = [entry for entry in entries if isinstance(entry, Mapping)] if isinstance(entries, list) else []
    identity = {
        "limb": str(context.get("limb") or context.get("module_name") or ""),
        "trajectory_sha256": str(trajectory.get("sha256") or "") if isinstance(trajectory, Mapping) else "",
        "speed": _number(request.get("speed")) if isinstance(request, Mapping) else None,
        "record_rate_hz": _number(context.get("record_rate_hz")),
        "joint_names": sorted(str(entry.get("joint_name") or "") for entry in channels),
    }
    if include_motors:
        identity["motors"] = sorted(
            ({"joint_name": str(entry.get("joint_name") or ""),
              "motor_id": entry.get("motor_id") if type(entry.get("motor_id")) is int else None,
              "motor_type": str(entry.get("motor_type") or "").upper(),
              "motor_model": entry.get("motor_model") if type(entry.get("motor_model")) is int else None}
             for entry in channels), key=lambda entry: entry["joint_name"],
        )
    return identity


def freeze_factory_quality(context: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze reference thresholds and matching evidence before a factory run."""
    identity = _identity(context, include_motors=True)
    result = {
        "schema_version": POLICY_SCHEMA,
        "criteria_version": CRITERIA_VERSION,
        "matched": False,
        "reason": "",
        "identity": identity,
        "profile": None,
    }
    limb = identity["limb"]
    reference = _PROFILES.get(limb)
    reasons = []
    if context.get("data_domain") != "FACTORY":
        reasons.append("本次记录未选择工厂模式")
    if reference is None:
        reasons.append("当前部位尚无工厂参考判据")
    else:
        case_id, label, trajectory_sha, axes, basis = reference
        trial = limb != "waist_hip"
        if trial:
            side = "left" if limb.startswith("left_") else "right"
            axes = {f"{side}_{axis}_joint": spec for axis, spec in axes.items()}
        if identity["trajectory_sha256"] != trajectory_sha:
            reasons.append("轨迹哈希与本版工厂判据不一致")
        if identity["speed"] is None or not math.isclose(identity["speed"], 1.0, rel_tol=0, abs_tol=1e-9):
            reasons.append("本版工厂判据要求1倍速")
        expected_motors = [{"joint_name": name, "motor_id": spec[0], "motor_type": "LRO", "motor_model": spec[1]}
                           for name, spec in sorted(axes.items())]
        if identity["motors"] != expected_motors:
            reasons.append("电机通道、ID或型号与本版工厂判据不一致")
    if identity["record_rate_hz"] is None or not math.isclose(identity["record_rate_hz"], 20.0, rel_tol=0, abs_tol=1e-9):
        reasons.append("参考判据要求20 Hz记录")
    if reasons:
        result["reason"] = "；".join(reasons)
        return result

    limits = {
        name: {
            "tracking_rms_rad": values[3],
            "tracking_p99_abs_rad": values[4],
            "torque_p999_abs_nm": values[5],
            "temperature_peak_c": 60.0,
            "temperature_peak_rise_c": 20.0,
        }
        for name, values in axes.items()
    }
    result.update(matched=True, reason="工况字段匹配本版工厂判据", profile={
        "id": f"{case_id.lower()}-quality-v2", "label": f"{case_id} {label} · 1倍速",
        "test_case_id": case_id,
        "basis_kind": "trial" if trial else "reference",
        "basis": basis,
        "scope_note": _SCOPE_NOTE,
        "joint_labels": {name: spec[2] for name, spec in axes.items()},
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
    version = policy.get("criteria_version")
    current = version == CRITERIA_VERSION
    valid_policy = (
        policy.get("schema_version") == POLICY_SCHEMA
        and (current or version == _LEGACY_CRITERIA_VERSION)
        and policy.get("matched") is True
        and isinstance(profile, Mapping)
        and isinstance(profile.get("joint_limits"), Mapping)
        and policy.get("identity") == _identity(context, include_motors=current)
    )
    if valid_policy:
        valid_policy = bool(profile["joint_limits"]) and all(
            isinstance(limits, Mapping) and all(_number(limits.get(key)) is not None for key, _, _ in _METRICS)
            for limits in profile["joint_limits"].values()
        )
    limits_by_name = profile["joint_limits"] if valid_policy else {}
    labels = profile.get("joint_labels") if current and valid_policy else None
    labels = labels if isinstance(labels, Mapping) else {}
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
            check = dict(key=key, label=label, value=value, limit=limit, unit=unit, passed=passed)
            if current:
                check.update(comparison="<=", exceedance=value - limit if passed is False else None)
            checks.append(check)
        metric = dict(joint_name=name, motor_id=joint.get("motor_id"), checks=checks)
        if current:
            metric.update(motor_label=labels.get(name, name), sample_count=joint.get("sample_count"),
                          valid_metric_sample_count=joint.get("factory_metric_sample_count"))
        metrics.append(metric)
    result = {
        "schema_version": _LEGACY_CRITERIA_VERSION if version == _LEGACY_CRITERIA_VERSION else CRITERIA_VERSION,
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
    failed = []
    for metric in metrics:
        for item in metric["checks"]:
            if item["passed"] is False:
                if current:
                    failed.append(f"{metric['motor_label']}（ID {metric['motor_id']}）{item['label']} "
                                  f"{format_quality_value(item['value'], item['unit'])} > "
                                  f"{format_quality_value(item['limit'], item['unit'])}")
                else:
                    failed.append(metric["joint_name"] + " " + item["label"])
    result["status"] = "failed" if failed else "passed"
    result["reason"] = "超出参考阈值：" + "、".join(failed) if failed else "电机误差、温度及力矩均在本次冻结参考范围内"
    return result
