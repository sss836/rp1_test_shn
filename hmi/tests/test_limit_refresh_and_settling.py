"""No bus handles. Regressions for retained evidence, expiry and arm settling."""

from dataclasses import replace
import math

import numpy as np
import pytest

from factory_hmi.core.backend import MotorSafetyError
from factory_hmi.core.limit_arm_completion import ArmCompletion
from factory_hmi.core.limit_block_check import Coordinates, FeedbackError, nominal_station
from factory_hmi.core.live_limit_probe import PositionSettling
from factory_hmi.core.module_limit_check import entry_poses
from factory_hmi.core.motor_versions import MotorVersions
from factory_hmi.core.torque_estimation import estimate
from factory_hmi.models import MotorSample
from test_limit_arm_completion import setup
from test_lro_local_evidence import (
    backend,
    config,
    entries,
    observation,
    payload,
    policy_file,
    read_local_legacy_evidence,
)
from test_motor_versions import entry


def test_failed_query_keeps_numeric_confirmation_until_original_expiry(monkeypatch, tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)))
    b.prepare_motor_protocols()
    old = observation()
    failed = MotorVersions._empty(cfg.entries[0])
    failed.update(status="timeout", errors={"0x31": "版本查询超时", "0x1e": "版本查询超时"})
    merged = MotorVersions._merge_refresh(old, failed)
    assert merged["raw_responses"] == old["raw_responses"] and merged["_monotonic"] == old["_monotonic"]
    assert merged["refresh_failures"] == 1 and merged["last_refresh_errors"] == failed["errors"]
    b._observe_motor_protocol(0, merged)
    b._check_protocol_conflicts()
    monkeypatch.setattr("time.monotonic", lambda: old["_monotonic"] + 181)
    with pytest.raises(MotorSafetyError, match="过期"):
        b._check_protocol_conflicts()


def test_changed_identity_malformed_reply_and_kt_expiry():
    motor = entry()
    old = dict(
        MotorVersions._empty(motor),
        status="known",
        raw_firmware="109",
        _monotonic=10.0,
        torque_kt_nm_a=2.0,
        kt_raw_response="a01600c8",
        kt_monotonic=10.0,
        kt_observed_at="t",
    )
    new = dict(
        MotorVersions._empty(motor),
        status="known",
        raw_firmware="109",
        _monotonic=70.0,
        kt_error="Kt读取失败：版本查询超时",
    )
    merged = MotorVersions._merge_refresh(old, new.copy())
    assert estimate(motor, merged, 3.0, now=70.0) == (6.0, 2.0, "device_kt_times_iq")
    assert estimate(motor, merged, 3.0, now=191.0)[-1] == "Kt读回已过期"
    for changes in (
        {"raw_firmware": "108"},
        {"errors": {"0x31": "版本响应头无效"}},
        {"kt_error": "Kt必须为正值"},
    ):
        assert "torque_kt_nm_a" not in MotorVersions._merge_refresh(old, {**new, **changes})


def test_query_events_are_bounded_and_identify_timed_out_command():
    motor = type(
        "Motor", (), {"request_version": lambda self, code: True, "version_payload": lambda self, code: b""}
    )()
    v = MotorVersions([entry()], [motor], timeout=0.0)
    with pytest.raises(TimeoutError):
        v._query(motor, 0x31, 0, MotorVersions._empty(entry()))
    (event,) = v.events_snapshot()
    assert event["index"] == 0 and event["outcome"] == "timeout" and event["code"] == "0x31"
    assert event["start"] <= event["end"] and not v.events_snapshot(event["end"] + 1)


def test_two_fresh_drive_faults_and_whole_bus_expiry():
    coords = Coordinates(nominal_station("left_arm"))
    rows = [
        MotorSample(
            pos_rad=0.0,
            bus=e.interface,
            motor_id=e.motor_id,
            joint_name=e.joint_name,
            timestamp_s=1.0,
            error_id={2: 3, 7: 7}.get(e.motor_id, 0),
            lro_protocol_profile="v1.03",
        )
        for e in coords.entries
    ]
    with pytest.raises(FeedbackError) as fresh:
        coords.decode(rows, 1.02, ())
    assert all(s in str(fresh.value) for s in ("2/7 台", "#2 ", "#7 ", "error_id=3", "OTHER_ERROR"))
    with pytest.raises(FeedbackError) as stale:
        coords.decode(rows, 1.2, ())
    assert "全部 7 台同时" in str(stale.value) and "error_id=" not in str(stale.value)


def test_windowed_settling_survives_quantization():
    coords, profile = setup("left_arm")
    c = ArmCompletion(coords, np.zeros(7), np.zeros(7), 1.0)
    now = 1.0
    point = profile.windows[0].point
    for _ in range(2000):
        now += 0.025
        c.step(now, c.target.copy(), point)
        if c.commanded_complete:
            break
    assert c.commanded_complete
    for n in range(200):
        now += 0.025
        q = c.target.copy()
        q[-1] += 0.00038 if n % 2 else -0.00038
        _, done = c.step(now, q, point)
        if done:
            break
    assert done and c.snapshot()["waiting_seconds"] >= 1.0


def test_settling_exposes_joint_and_reason():
    s = PositionSettling()
    assert not s.settled(1.0, np.array([0.0, 0.1]), np.zeros(2))
    assert s.blocking_joint(["a", "b"])["joint"] == "b"
    for t in range(11):
        ok = s.settled(2.0 + t * 0.025, np.zeros(2), np.zeros(2))
    assert ok and s.blocking is None


@pytest.mark.parametrize(
    "margin,excess,accepted",
    [(0, 3, False), (5, 3, True), (5, 6, False), (11, 3, False), (float("nan"), 3, False)],
)
def test_entry_reference_margin_requires_opt_in(margin, excess, accepted):
    coords, profile = setup("left_arm")
    coords = Coordinates(
        replace(
            coords.station,
            raw={
                **coords.station.raw,
                "factory_hmi": {"limit_check": {"entry_beyond_reference_margin_deg": margin}},
            },
        )
    )
    q = np.zeros(7)
    q[-1] = math.radians(70 + excess)
    evidence = []
    if accepted:
        entry_poses(coords, profile, q, evidence=evidence)
        assert evidence[0]["joint"] == coords.names[-1] and evidence[0]["excess_deg"] == pytest.approx(
            excess
        )
    else:
        with pytest.raises((ValueError, FeedbackError)):
            entry_poses(coords, profile, q, evidence=evidence)
