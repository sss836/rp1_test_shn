"""Arm defaults and actual-stop-driven execution. No PLC/CAN/device writes."""
from dataclasses import replace
import json
import math

import numpy as np
import pytest

from factory_hmi.core.limit_block_check import FeedbackError, nominal_station
from factory_hmi.core.module_limit_check import ModuleCycle, load_module_plan


@pytest.fixture(autouse=True)
def absent_site_profiles(tmp_path, monkeypatch):
    monkeypatch.setattr("factory_hmi.core.module_limit_check.PROFILE_ROOT", tmp_path / "no-site-files")


def plan(limb="left_arm", config=None):
    return load_module_plan(config or nominal_station(limb), "OFFLINE-ARM", "fixture1")


def simulate(coords, profile, *, offset=0., missing=False):
    initial = np.deg2rad([20., 12., -8., -40., 9., *([4., -6.] if len(coords.names) == 7 else [])])
    cycle = ModuleCycle(coords, profile, initial, 1.)
    homed = False
    searches = set()
    failure = None
    for i in range(1, 70000):
        q = cycle.target.copy()
        torque = np.full(len(q), .7)
        if cycle.phase == "homing" and np.max(abs(q)) < 1.e-8:
            homed = True
        probe = cycle.probe
        if probe is not None and probe.phase in {"search", "unload"}:
            assert homed
            searches.add((probe.point.joint, probe.point.direction))
            stop = math.radians(probe.direction.reference_deg + offset * probe.point.direction)
            if not missing and probe.point.direction * (q[probe.axis] - stop) >= -1.e-9:
                q[probe.axis] = stop
                torque[probe.axis] = 2.  # Baseline + rise, below inherited caps.
        try:
            cycle.step(1 + i*.05, q, torque, torque)
        except FeedbackError as exc:
            failure = str(exc)
            break
        if cycle.phase == "finished":
            break
    else:
        pytest.fail(f"sequence did not terminate: {cycle.phase}")
    return cycle, failure, searches, i*.05


@pytest.mark.parametrize("limb,count", [("left_short_arm", 10), ("left_arm", 14)])
def test_builtin_default_completes_from_nonzero_feedback(limb, count):
    coords, profile, context = plan(limb)
    assert profile.home_deg == (0.,) * (count//2)
    assert profile.source == "builtin_reference" and profile.approved_by is None
    assert context["execution_mode"] == "feedback_limit_search"
    assert context["order_source"] == "builtin_order_only"
    assert all(w.point.full_range_search and w.point.search_pose_deg == profile.home_deg for w in profile.windows)
    assert len(profile.windows) == count
    # Candidates deliberately offset from reference: copying references/NPZ
    # instead of measuring the simulated physical stops would fail this test.
    cycle, failure, searches, duration = simulate(coords, profile, offset=-.8)
    assert failure is None
    assert cycle.phase == "finished" and len(cycle.results) == count
    assert len(searches) == count
    assert len(cycle.direction_evidence) == count
    for row in cycle.results:
        assert row["actual_deg"] == pytest.approx(row["reference_deg"] - row["sign"]*.8)
        assert row["deviation_deg"] == pytest.approx(-row["sign"]*.8)
        assert row["outcome"] == "within_reference" and row["retreat_completed"]
        assert row["fresh_samples"] >= 3
        assert row["baseline_nm"] == pytest.approx(.7)
        assert row["torque_floor_nm"] == 2.
    np.testing.assert_allclose(cycle.actual, np.zeros(count//2), atol=1.e-8)
    assert duration < 1500  # Deterministic virtual time, not a wall-clock sleep.


@pytest.mark.parametrize("offset,missing,outcome", [(-8., False, "early"), (0., True, "not_detected")])
def test_missing_or_wrong_stop_is_not_fabricated_as_reference(offset, missing, outcome):
    coords, profile, _ = plan("left_short_arm")
    cycle, failure, searches, _ = simulate(coords, profile, offset=offset, missing=missing)
    assert failure and "结果异常" in failure
    assert len(searches) == len(cycle.results) == 1
    assert cycle.results[0]["outcome"] == outcome
    if missing:
        assert cycle.results[0].get("actual_deg") is None
    else:
        assert abs(cycle.results[0]["deviation_deg"]) == pytest.approx(8.)


def test_peak_override_and_bus_sign_mapping_are_bound_to_plan():
    config = nominal_station("left_arm")
    raw = dict(config.raw)
    peaks = {e.joint_name: 12. + i for i, e in enumerate(config.entries)}
    raw["factory_hmi"] = {"limit_check": {"joint_peak_nm": peaks}}
    changed = replace(config, raw=raw, entries=tuple(replace(e, interface="can2", sign=-1.) for e in config.entries))
    coords, profile, context = plan(config=changed)
    assert profile.joint_peak_nm == tuple(peaks.values())
    assert profile.windows[0].point.joint_abort_nm == tuple(peaks.values())
    assert all(m["bus"] == "can2" for m in context["windows"][0]["motors"])
    np.testing.assert_allclose(coords.motor_target(np.ones(7)), -np.ones(7))
    assert context["digest"] != plan()[2]["digest"]


def test_bad_external_profile_is_not_silently_replaced(monkeypatch):
    def bad(_):
        raise ValueError("方案文件损坏")
    monkeypatch.setattr("factory_hmi.core.module_limit_check.read_site_profile", bad)
    with pytest.raises(ValueError, match="损坏"):
        plan()


def test_other_modules_do_not_get_arm_defaults():
    with pytest.raises(ValueError, match="尚未安装"):
        plan("left_short_leg")


def test_zero_search_still_uses_fresh_feedback_and_station_load_caps():
    coords, profile, _ = plan()
    cycle = ModuleCycle(coords, profile, np.zeros(7), 1.)
    with pytest.raises(FeedbackError, match="反馈不连续"):
        cycle.step(1.2, np.zeros(7), np.zeros(7), np.zeros(7))
    with pytest.raises(FeedbackError, match="中止力矩"):
        cycle.step(1.05, np.zeros(7), np.ones(7)*40, np.ones(7)*40)


@pytest.mark.parametrize("limb", ["left_arm", "left_short_arm"])
def test_default_service_start_uses_current_pose_and_saves_on_cancel(tmp_path, monkeypatch, limb):
    import time
    from pathlib import Path
    from factory_hmi.core.backend import FakeMotorBackend
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.core.service import FactoryService
    from factory_hmi.models import StationState

    coords, _, _ = plan(limb)
    initial = np.zeros(len(coords.names)); initial[0] = math.radians(12)
    fake = FakeMotorBackend(coords.entries, initial_positions=initial)
    controller = FactoryController(coords.station, fake)
    service = FactoryService(data_root=tmp_path)
    service._controller, service._backend_name = controller, "motors_py"
    fake.open_transport(); fake.enable_motors()
    controller.state_machine.transition(StationState.CONNECTED, expected=StationState.DISCONNECTED)
    monkeypatch.setattr(service, "can_status", lambda: {"all_up": True})
    def fresh_samples():
        fake.refresh_feedback()
        return fake.read_samples()
    monkeypatch.setattr(fake, "limit_probe_samples", fresh_samples, raising=False)
    try:
        preview = service.limit_module_preview("OFFLINE-ARM")
        assert not fake.command_history
        service.limit_module_start("OFFLINE-ARM", preview["digest"], True)
        deadline = time.monotonic() + 2
        while (not any(c["target"][0] < initial[0] - 1.e-6 for c in fake.command_history)
               and time.monotonic() < deadline):
            time.sleep(.01)
        assert fake.command_history
        assert fake.command_history[0]["target"][0] == pytest.approx(initial[0])
        assert 0 < fake.command_history[-1]["target"][0] < initial[0]
        service.disable_motors()
        result = service.limit_probe_status(report=True)
        assert result["disable"]["feedback"] == "confirmed_mock"
        report = json.loads(Path(result["report_file"]).read_text())
        assert report["context"]["entry_pose_deg"][0] == pytest.approx(12.)
        assert report["context"]["entry_target_deg"] == [0.] * len(initial)
        assert report["phase"] == "aborted"
        assert fake.zeroed_indices == []  # Moving to zero never writes encoder zero.
    finally:
        service.close()


def test_candidate_and_iq_survive_retreat_failure_in_report(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from factory_hmi.models import MotorSample
    from factory_hmi.core.live_limit_probe import ProbeCycle
    from factory_hmi.core.module_limit_check import ModuleWorker

    coords, profile, context = plan("left_short_arm")
    def failed_retreat(*_):
        raise FeedbackError("模拟退让故障")
    monkeypatch.setattr(ProbeCycle, "_plan_retreat", failed_retreat)
    cycle, failure, _, _ = simulate(coords, profile, offset=-.8)
    assert failure == "模拟退让故障"
    assert cycle.probe.candidate and not cycle.results
    worker = ModuleWorker(Mock(), coords, profile, context, Mock(), Mock(), Mock(),
                          tmp_path / "candidate.json", np.zeros(5))
    worker.cycle = cycle
    samples = [MotorSample(pos_rad=float(cycle.actual[i]), cmd_pos_rad=0., spd_rad_s=0.,
        torque_nm=2., temp_c=25., error_id=0, index=i, motor_id=e.motor_id, bus=e.interface,
        joint_name=e.joint_name, timestamp_s=100., feedback_age_s=0., iq_current_a=1.,
        torque_kt_nm_a=2., torque_is_estimated=True, torque_source="iq_times_measured_kt")
        for i,e in enumerate(coords.entries)]
    worker._record_feedback(samples)
    cycle.phase = "aborted"; worker.reason = failure
    worker._record_end()
    report = json.loads((tmp_path / "candidate.json").read_text())
    assert report["current_candidate"]["retreat_completed"] is False
    assert report["current_candidate"]["torque_is_estimated"] is True
    assert abs(report["current_candidate"]["deviation_deg"]) == pytest.approx(.8)
    assert report["unfinished_search"]["samples"][-1]["drive_load"][0]["iq_current_a"] == 1.
    assert report["results"] == []
    worker.backend.disable_motors.assert_not_called()  # Test persists evidence only.
