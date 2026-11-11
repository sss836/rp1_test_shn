"""Factory quality evidence uses synthetic CSVs and never operates devices."""
import copy
import csv
import hashlib
import json

import pytest

from factory_hmi.core.config import load_station_config
from factory_hmi.core.factory_quality import freeze_factory_quality
from factory_hmi.core.report import ReportManager
from factory_hmi.quick_profiles import preset


JOINTS = (
    "waist_roll_joint", "waist_yaw_joint", "right_hip_pitch_joint", "left_hip_pitch_joint",
)
SUFFIXES = ("cmd_pos_rad", "pos_rad", "spd_rad_s", "torque_nm", "temp_c", "error_id")


def context_for_factory():
    config = load_station_config("waist_hip")
    return {
        "test_id": "LOCAL_UNIT-AGING-003_factory_quality",
        "execution_uuid": "11111111-1111-4111-8111-111111111111",
        "test_case_id": "UNIT-AGING-003",
        "sample_id": "SYNTHETIC-WAIST-001",
        "bench_id": "MOCK-BENCH",
        "recording_scope": "local",
        "data_domain": "FACTORY",
        "data_domain_source": "EXPLICIT",
        "limb": "waist_hip",
        "module_name": "waist_hip",
        "started_at": "2026-11-11T00:00:00Z",
        "ended_at": "2026-11-11T00:00:06.580000Z",
        "active_seconds": 6.58,
        "completed_cycles": 1,
        "record_rate_hz": 20.0,
        "outcome": "duration_complete",
        "run_request": {
            "speed": 1.0, "loop": True, "cycles": 0, "duration_s": 5.0,
            "completion_mode": "configured", "power_source": "external",
        },
        "motion_completion": {"phase": "holding", "safe_disabled": False},
        "entries": [{"joint_name": entry.joint_name, "motor_id": entry.motor_id,
                     "module_name": config.limb} for entry in config.entries],
        "trajectory": {"sha256": preset("waist_hip")["sha256"],
                       "filename": "waist_hip_joint_inspect.npz", "duration_s": 6.58},
        "config": config.raw,
        "acceptance": config.raw["factory_hmi_acceptance"],
    }


def write_measurements(path, *, overrides=None, missing=None):
    overrides = overrides or {}
    fieldnames = ["time_s", "sample"] + [f"{name}_{suffix}" for name in JOINTS for suffix in SUFFIXES]
    if missing:
        fieldnames.remove(missing)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        for index in range(132):
            row = {"time_s": index / 20.0, "sample": index}
            for name in JOINTS:
                for suffix, value in zip(SUFFIXES, (.1, .08, .0, 1.0, 30.0, 0)):
                    row[f"{name}_{suffix}"] = value
            row.update(overrides)
            if missing:
                row.pop(missing)
            writer.writerow(row)


def finalize_synthetic(tmp_path, *, changes=None, overrides=None, missing=None):
    context = context_for_factory()
    if changes:
        changes(context)
    context["factory_quality_policy"] = freeze_factory_quality(context)
    source = tmp_path / "acquired.csv"
    write_measurements(source, overrides=overrides, missing=missing)
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


@pytest.mark.parametrize(("limb", "trajectory_sha"), [
    ("right_short_arm", "6b5b447399d48233c5c27571c67cbc7c0ef6a984a954f34e27b1cd1434945a84"),
    ("right_short_leg", "8a45ed836d1557fd149cf15b65b812d8d1cbd935189a5e1c88ef01a405d70d3b"),
])
def test_nominal_one_times_reference_does_not_treat_inferred_pace_as_selected_speed(limb, trajectory_sha):
    context = context_for_factory()
    config = load_station_config(limb)
    context.update(limb=limb, trajectory={"sha256": trajectory_sha},
                   entries=[{"joint_name": entry.joint_name} for entry in config.entries])
    policy = freeze_factory_quality(context)
    assert policy["identity"]["speed"] == 1.0
    assert policy["matched"] is False
    assert "执行节拍待核对" in policy["reason"]
    assert "运行倍率与参考记录不一致" not in policy["reason"]


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
    lambda c: c.update(outcome="stopped", manual_stop=True, completed_cycles=0, active_seconds=2.0),
], ids=("missing-speed", "different-speed", "different-trajectory", "different-sample-rate", "interrupted"))
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
