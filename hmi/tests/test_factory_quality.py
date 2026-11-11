"""Factory quality evidence uses synthetic CSVs and never operates devices."""
import copy
import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from factory_hmi.core.config import load_station_config
from factory_hmi.core.factory_quality import CRITERIA_VERSION, freeze_factory_quality
from factory_hmi.core.report import ReportManager
from factory_hmi.core.trajectory import import_trajectory
from factory_hmi.quick_profiles import catalog, preset


JOINTS = (
    "waist_roll_joint", "waist_yaw_joint", "right_hip_pitch_joint", "left_hip_pitch_joint",
)
SUFFIXES = ("cmd_pos_rad", "pos_rad", "spd_rad_s", "torque_nm", "temp_c", "error_id")
SUMMARY_SCHEMA = Path(__file__).resolve().parents[1] / "factory_hmi/contracts/aging-summary-v1.schema.json"


def context_for_factory(limb="waist_hip"):
    profile = preset(limb)
    path = Path(__file__).resolve().parents[1] / "scripts/config" / profile["config_path"]
    config = load_station_config(limb, path)
    trajectory = import_trajectory(profile["trajectory_path"], limb=limb, motor_count=len(config.entries))
    start = datetime(2026, 11, 11, tzinfo=timezone.utc)
    return {
        "test_id": f"LOCAL_{profile['test_case_id']}_factory_quality",
        "execution_uuid": "11111111-1111-4111-8111-111111111111",
        "test_case_id": profile["test_case_id"],
        "sample_id": "SYNTHETIC-001",
        "bench_id": "MOCK-BENCH",
        "recording_scope": "local",
        "data_domain": "FACTORY",
        "data_domain_source": "EXPLICIT",
        "limb": limb,
        "module_name": limb,
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(seconds=trajectory.duration_s)).isoformat(),
        "active_seconds": trajectory.duration_s,
        "completed_cycles": 1,
        "record_rate_hz": 20.0,
        "outcome": "duration_complete",
        "run_request": {
            "speed": 1.0, "loop": True, "cycles": 0, "duration_s": 5.0,
            "completion_mode": "configured", "power_source": "external",
        },
        "motion_completion": {"phase": "holding", "safe_disabled": False},
        "entries": [{"joint_name": entry.joint_name, "motor_id": entry.motor_id,
                     "motor_type": entry.motor_type, "motor_model": entry.motor_model,
                     "module_name": config.limb} for entry in config.entries],
        "trajectory": trajectory.to_dict(),
        "config": config.raw,
        "acceptance": config.raw["factory_hmi_acceptance"],
    }


def write_measurements(path, *, joints=JOINTS, active_seconds=6.58, overrides=None, missing=None):
    overrides = overrides or {}
    fieldnames = ["time_s", "sample"] + [f"{name}_{suffix}" for name in joints for suffix in SUFFIXES]
    if missing:
        fieldnames.remove(missing)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for index in range(int(active_seconds * 20) + 1):
            row = {"time_s": index / 20.0, "sample": index}
            for name in joints:
                for suffix, value in zip(SUFFIXES, (.1, .08, .0, 1.0, 30.0, 0)):
                    row[f"{name}_{suffix}"] = value
            row.update(overrides)
            if missing:
                row.pop(missing)
            writer.writerow(row)


def finalize_synthetic(tmp_path, *, limb="waist_hip", changes=None, overrides=None, missing=None):
    context = context_for_factory(limb)
    active_seconds = context["active_seconds"]
    if changes:
        changes(context)
    context["factory_quality_policy"] = freeze_factory_quality(context)
    source = tmp_path / "acquired.csv"
    write_measurements(source, joints=tuple(entry["joint_name"] for entry in context["entries"]),
                       active_seconds=active_seconds, overrides=overrides, missing=missing)
    before = source.read_bytes()
    manager = ReportManager(tmp_path / "reports")
    manager.finalize(source, context)
    summary = manager.summary(context["test_id"])
    assert source.read_bytes() == before
    assert (manager.execution_dir(context["test_id"]) / "telemetry/raw.csv").read_bytes() == before
    return manager, context, summary


def test_matching_factory_run_seals_passed_quality_separately_from_legacy_verdict(tmp_path):
    manager, context, summary = finalize_synthetic(tmp_path)
    result = summary["factory_quality"]
    assert result["status"] == "passed"
    assert summary["verdict"]["status"] == "passed"
    assert {metric["joint_name"] for metric in result["metrics"]} == set(JOINTS)
    for metric in result["metrics"]:
        checks = {check["key"]: check for check in metric["checks"]}
        assert {"tracking_rms_rad", "tracking_p99_abs_rad", "torque_p999_abs_nm",
                "temperature_peak_c", "temperature_peak_rise_c"} <= checks.keys()
        assert all(check["passed"] for check in checks.values())
    frozen = json.loads((manager.execution_dir(context["test_id"]) / "config/execution.json").read_text())
    assert frozen["factory_quality_policy"] == context["factory_quality_policy"]


@pytest.mark.parametrize("limb", [item["id"] for item in catalog()])
def test_each_builtin_factory_profile_seals_an_explicit_result(tmp_path, limb):
    manager, context, summary = finalize_synthetic(tmp_path, limb=limb)
    result = summary["factory_quality"]
    assert result["schema_version"] == CRITERIA_VERSION
    assert result["status"] == "passed", result["reason"]
    assert result["profile"]["test_case_id"] == context["test_case_id"]
    assert result["profile"]["basis_kind"] == ("reference" if limb == "waist_hip" else "trial")
    assert len(result["metrics"]) == preset(limb)["motor_count"]
    assert all(len(motor["checks"]) == 5 and motor["valid_metric_sample_count"] == summary["quality"]["sample_count"]
               for motor in result["metrics"])
    text = (manager.execution_dir(context["test_id"]) / "analysis/report.md").read_text()
    assert ("工厂质量筛查：通过" if limb == "waist_hip" else "工厂质量筛查：试行通过") in text
    assert result["profile"]["basis"]
    assert "| 判定前提 | 实测或状态 | 要求 | 结果 |" in text
    assert "| 电机通道 | 指标 | 实测 | 上限 | 对照结果 | 超出量 |" in text
    assert "99.9" in text and "20 °C" in text
    Draft202012Validator(json.loads(SUMMARY_SCHEMA.read_text())).validate(summary)


def test_old_right_short_arm_trajectory_does_not_inherit_the_current_profile():
    context = context_for_factory("right_short_arm")
    context["trajectory"]["sha256"] = "6b5b447399d48233c5c27571c67cbc7c0ef6a984a954f34e27b1cd1434945a84"
    policy = freeze_factory_quality(context)
    assert policy["matched"] is False
    assert "轨迹哈希" in policy["reason"]


def test_long_arm_report_identifies_the_failed_new_wrist_axis(tmp_path):
    manager, context, summary = finalize_synthetic(
        tmp_path, limb="left_arm", overrides={"left_wrist_yaw_joint_torque_nm": 8.25},
    )
    result = summary["factory_quality"]
    assert result["status"] == "failed"
    assert "腕偏航（ID 6）绝对力矩 P99.9 8.25 N·m > 7 N·m" in result["reason"]
    wrist = next(row for row in result["metrics"] if row["motor_id"] == 6)
    torque = next(check for check in wrist["checks"] if check["key"] == "torque_p999_abs_nm")
    assert torque["value"] == 8.25 and torque["limit"] == 7 and torque["exceedance"] == 1.25
    text = (manager.execution_dir(context["test_id"]) / "analysis/report.md").read_text()
    assert "腕偏航（ID 6） | 绝对力矩 P99.9 | 8.25 N·m | 7 N·m | 超限 | 1.25 N·m" in text


@pytest.mark.parametrize(("column", "value", "failed_key"), [
    ("waist_roll_joint_temp_c", 80.0, "temperature_peak_c"),
    ("waist_roll_joint_pos_rad", 1.0, "tracking_rms_rad"),
    ("waist_roll_joint_torque_nm", 100.0, "torque_p999_abs_nm"),
])
def test_measurement_exceedance_fails_factory_quality_without_rewriting_legacy_verdict(
        tmp_path, column, value, failed_key):
    _, _, summary = finalize_synthetic(tmp_path, overrides={column: value})
    result = summary["factory_quality"]
    assert result["status"] == "failed"
    assert summary["verdict"]["status"] == "passed"
    checks = next(item for item in result["metrics"] if item["joint_name"] == JOINTS[0])["checks"]
    assert any(check["key"] == failed_key and check["passed"] is False for check in checks)


@pytest.mark.parametrize("change", [
    lambda c: c["run_request"].pop("speed"),
    lambda c: c["run_request"].update(speed=.8),
    lambda c: c["trajectory"].update(sha256="0" * 64),
    lambda c: c.update(record_rate_hz=10.0),
    lambda c: c["entries"][0].update(motor_id=9),
    lambda c: c["entries"][0].update(motor_model=1),
    lambda c: c.update(outcome="stopped", manual_stop=True, completed_cycles=0, active_seconds=2.0),
], ids=("missing-speed", "different-speed", "different-trajectory", "different-sample-rate",
        "different-motor-id", "different-motor-model", "interrupted"))
def test_unmatched_or_incomplete_factory_execution_remains_not_evaluated(tmp_path, change):
    _, _, summary = finalize_synthetic(tmp_path, changes=change)
    assert summary["factory_quality"]["status"] == "not_evaluated"
    assert summary["factory_quality"]["reason"]


@pytest.mark.parametrize("suffix", ["cmd_pos_rad", "torque_nm", "temp_c", "error_id"])
def test_missing_required_measurement_column_prevents_factory_quality_pass(tmp_path, suffix):
    _, _, summary = finalize_synthetic(tmp_path, missing=f"waist_roll_joint_{suffix}")
    assert summary["factory_quality"]["status"] == "not_evaluated"


def test_research_execution_retains_existing_verdict_without_factory_quality(tmp_path):
    _, _, summary = finalize_synthetic(tmp_path, changes=lambda c: c.update(data_domain="RESEARCH"),
                                       overrides={"waist_roll_joint_torque_nm": 100.0})
    assert summary["verdict"]["status"] == "passed"
    assert summary.get("factory_quality") is None


def test_legacy_execution_without_frozen_factory_policy_keeps_original_interpretation(tmp_path):
    context = context_for_factory()
    source = tmp_path / "legacy.csv"
    write_measurements(source)
    manager = ReportManager(tmp_path / "reports")
    manager.finalize(source, context)
    summary = manager.summary(context["test_id"])
    assert summary["verdict"]["status"] == "passed"
    assert summary.get("factory_quality") is None


def test_quality_policy_and_original_evidence_stay_frozen_on_reanalysis(tmp_path):
    manager, context, summary = finalize_synthetic(tmp_path)
    execution = manager.execution_dir(context["test_id"])
    original = {path.relative_to(execution): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in execution.rglob("*") if path.is_file()}
    policy = copy.deepcopy(context["factory_quality_policy"])
    rebuilt = manager.rebuild(context["test_id"], actor="synthetic-reviewer", reason="质量摘要封存回归")
    assert rebuilt.summary["factory_quality"] == summary["factory_quality"]
    assert {path: hashlib.sha256((execution / path).read_bytes()).hexdigest() for path in original} == original
    assert json.loads((execution / "config/execution.json").read_text())["factory_quality_policy"] == policy


def test_frozen_v1_policy_can_still_be_reanalyzed(tmp_path):
    context = context_for_factory()
    policy = freeze_factory_quality(context)
    policy["criteria_version"] = "factory-quality-v1"
    del policy["identity"]["motors"]
    policy["profile"] = {"id": "waist-hip-reference-v1", "label": "腰胯参考轨迹 1 倍",
                         "joint_limits": policy["profile"]["joint_limits"]}
    context["factory_quality_policy"] = policy
    for entry in context["entries"]:
        del entry["motor_type"], entry["motor_model"]
    source = tmp_path / "v1.csv"
    write_measurements(source)
    manager = ReportManager(tmp_path / "reports")
    manager.finalize(source, context)
    result = manager.rebuild(context["test_id"], actor="synthetic-reviewer", reason="核对历史判据")
    quality = result.summary["factory_quality"]
    assert quality["schema_version"] == "factory-quality-v1"
    assert quality["profile"]["id"] == "waist-hip-reference-v1"
    assert quality["status"] == "passed"
