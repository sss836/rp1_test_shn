"""Cold user provisioning and all-module maintenance, without device I/O."""
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import shutil
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.bundled_motor_assets import seed_motor_assets, verify_bundle, MODELS
from factory_hmi.core.config import CONFIG_FILENAMES, load_station_config
from factory_hmi.core.motor_maintenance import effective_targets, ModuleFirmwareMaintenance, MODULE_109, SELECTED_109
from factory_hmi.gateway.motor_maintenance import cached_releases, cached_109_releases, artifact
from factory_hmi.gateway.motor_firmware import MotorFirmwareDownloads
from factory_hmi.core.backend import MotorsPyBackend
from factory_hmi.core.production_parameters import entries_for
from scripts.log_motor_telemetry import csv_fieldnames, sample_row
from test_lro_runtime_profiles import OfflineLro
from test_torque_estimation import ActualKtMotor

SOURCE = Path(__file__).resolve().parents[1]
BUNDLE = SOURCE / "factory_hmi/resources/motor-maintenance"


@pytest.fixture
def cold(tmp_path, monkeypatch):
    state = tmp_path / "a08" / ".local/state/rp1-test-hmi"
    result = seed_motor_assets(state, BUNDLE)
    assert result["status"] == "ready" and result["imported"] == 4
    monkeypatch.setenv("RP1_MOTOR_FIRMWARE_PUBLIC_KEY", str(state / "config/motor-firmware-public-key.pem"))
    monkeypatch.setenv("RP1_MOTOR_OTA_POLICY", str(state / "config/motor-ota-policy.json"))
    return state, MotorFirmwareDownloads(None, state / "data")


def observation(target, generation=108):
    return {**target, "status": "known", "raw_firmware": None if generation == 108 else "V5.0.0.109",
        "software": f"0.1.{generation-100}", "numeric_software": f"0.1.{generation-100}",
        "hardware": "0.1.0", "protocol": "0.0.1.17",
        "raw_responses": {"0x1e": "a01e0001" + ("08" if generation == 108 else "09") + "00010000000111"},
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "errors": {"0x31": "版本查询超时"} if generation == 108 else {}}


def test_cold_user_launcher_initializes_public_key_policy_and_firmware(tmp_path, monkeypatch):
    launcher = runpy.run_path(str(SOURCE / "launch.py"))
    launcher["seed_configs"](tmp_path, offline=True, offline_access="control")
    env = launcher["gateway_environment"](tmp_path, None, offline=True, driver="mock", mode="control")
    assert env["RP1_MOTOR_FIRMWARE_PUBLIC_KEY"] == str(tmp_path / "config/motor-firmware-public-key.pem")
    assert env["RP1_MOTOR_OTA_POLICY"] == str(tmp_path / "config/motor-ota-policy.json")
    assert len(list((tmp_path / "data/motor-firmware/releases").glob("*/release.json"))) == 4
    assert {r["model"] for r in verify_bundle(BUNDLE)["releases"]} == MODELS


def test_second_start_preserves_configs_and_local_records(cold):
    state, _ = cold
    original = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file() and p.name != "bundle-import.json"}
    (state / "data/result.csv").write_text("existing acquisition\n")
    assert seed_motor_assets(state, BUNDLE)["imported"] == 0
    assert all((state / name).read_bytes() == data for name, data in original.items())
    assert (state / "data/result.csv").read_text() == "existing acquisition\n"


@pytest.mark.parametrize("changed", ["motor-firmware-public-key.pem", "motor-ota-policy.json"])
def test_custom_trust_and_policy_are_not_overwritten(tmp_path, changed):
    config = tmp_path / "config"; config.mkdir()
    (config / changed).write_text("existing custom value")
    result = seed_motor_assets(tmp_path, BUNDLE)
    assert result["status"].startswith("custom_")
    assert (config / changed).read_text() == "existing custom value"


@pytest.mark.parametrize("fault", ["missing-key", "bad-firmware", "extra-private-file"])
def test_incomplete_or_tampered_package_is_rejected_before_provisioning(tmp_path, fault):
    bundle = tmp_path / "bundle"; shutil.copytree(BUNDLE, bundle)
    if fault == "missing-key": (bundle / "motor-firmware-public-key.pem").unlink()
    elif fault == "bad-firmware": next(bundle.glob("releases/*/*.lsepbin")).write_bytes(b"invalid")
    else: (bundle / "private.pem").write_text("should never be distributed")
    state = tmp_path / "state"
    with pytest.raises((ValueError, OSError)): seed_motor_assets(state, bundle)
    assert not state.exists()


@pytest.mark.parametrize("limb", sorted(CONFIG_FILENAMES))
@pytest.mark.parametrize("selection", ["one", "all"])
def test_all_modules_select_batch_write_then_one_power_cycle_and_readback(cold, limb, selection):
    state, downloads = cold
    config = load_station_config(limb, SOURCE / "scripts/config" / CONFIG_FILENAMES[limb])
    targets = effective_targets(config)
    # Exercise both CAN buses on combined/waist modules, with their actual node IDs.
    selected = targets[:1] if selection == "one" else targets
    selections = []
    for target in selected:
        obs = observation(target)
        choices = cached_109_releases(downloads, obs, cached_releases(downloads, obs))
        assert len(choices) == 1 and choices[0]["model"] == target["model"]
        selections.append({"index": target["index"], "release_id": choices[0]["release_id"]})
        assert entries_for(target["model"], 109)  # per-model production parameters
    written = []
    def writer(row, data, stop, progress):
        assert len(data) == 501316
        written.append(row["index"]); progress(len(data), len(data))
        return {"selfcheck_response": "ff020200bcf53ec2", "reset_sent": False}
    task = ModuleFirmwareMaintenance(state / "jobs", lambda: targets,
        lambda: [observation(t) for t in targets], None,
        lambda release_id, row: artifact(downloads, release_id, row), lambda: nullcontext(), writer,
        lambda row, stop: observation(row, 109 if row["index"] in written else 108),
        authorize_local=lambda: {"username": "offline-fixture", "role": "LOCAL_MAINTENANCE"})
    def wait(expected):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            row = task.snapshot()
            if row["status"] == expected: return row
            if row["status"] in {"failed", "version_unknown"}: pytest.fail(str(row))
            time.sleep(.01)
        pytest.fail(str(task.snapshot()))
    try:
        preview = task.preview(selections, mode=SELECTED_109 if selection == "one" else MODULE_109, local=True)
        job = task.start(preview["preview_id"], True, local=True)
        pending = wait("awaiting_power")
        assert written == [r["index"] for r in selected]
        assert all(not r["actual_version_verified"] for r in pending["targets"])
        task.confirm_power_and_readback(job["job_id"], True, local=True)
        done = wait("verified")
        assert all(r["actual_version_verified"] for r in done["targets"])
    finally: task.cancel()


@pytest.mark.parametrize("limb", sorted(CONFIG_FILENAMES))
@pytest.mark.parametrize("versions", ["108", "109", "mixed"])
def test_existing_enable_command_feedback_record_chain_for_every_module(monkeypatch, limb, versions):
    config = load_station_config(limb, SOURCE / "scripts/config" / CONFIG_FILENAMES[limb])
    expected = [108 if versions == "108" or versions == "mixed" and i % 2 == 0 else 109 for i in range(len(config.entries))]
    motors = [OfflineLro(None, (0,1,8,0,1,0,0,0,1,17)) if v == 108 else ActualKtMotor([159,218,308,432][e.motor_model])
              for e, v in zip(config.entries, expected)]
    monkeypatch.setitem(sys.modules, "motors_py", SimpleNamespace(MotorControlMode=SimpleNamespace(MIT="MIT")))
    monkeypatch.setattr("factory_hmi.core.backend.create_motor_driver", lambda e, _: motors[e.index])
    commands = []
    monkeypatch.setattr("factory_hmi.core.backend.legacy_command_mit", lambda motors, entries, target, **kw: commands.append(target.copy()))
    backend = MotorsPyBackend(config.entries)
    try:
        backend.open_transport(); backend.prepare_motor_protocols(); backend.enable_motors()
        assert backend._selected_protocols == ["legacy" if v == 108 else "v1.03" for v in expected]
        backend.command_mit(np.zeros(len(motors)))
        assert len(commands) == 1
        samples = backend.read_samples()
        row = sample_row(config.entries, samples, elapsed_s=.1, sample=1)
        assert set(row) == set(csv_fieldnames(config.entries))
        for version, sample in zip(expected, samples):
            if version == 108: assert sample.torque_nm == 3 and not sample.torque_is_estimated
            else: assert sample.drive_enabled and sample.iq_current_a == 10 and sample.torque_is_estimated
        backend.disable()
        assert not any(m.enabled for m in motors)
    finally: backend.close_transport()
