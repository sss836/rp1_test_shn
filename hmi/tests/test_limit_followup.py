"""Limit inspection regressions using virtual time and simulated motors only."""
import json
import math
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.limit_block_check import FeedbackError, ProbeSoftwareFault
from factory_hmi.core.live_limit_probe import ProbeCycle
from factory_hmi.core.module_limit_check import ModuleCycle, ModuleWorker, validate_module_plan
from factory_hmi.core.service import FactoryService
from factory_hmi.models import MotorSample, StationState
from test_module_limit_check import commissioned
from test_protective_damping import until


@pytest.mark.parametrize("mode", ["soft", "hard", "disable_during_handoff", "feedback_lost", "hot", "excess_damping_torque", "revoke_before_write"])
def test_fault_damping_via_station_start(tmp_path, monkeypatch, mode):
    coords, profile, _ = commissioned("waist_hip")
    backend = FakeMotorBackend(coords.entries, initial_positions=coords.motor_target(np.deg2rad(profile.home_deg)))
    controller = FactoryController(coords.station, backend)
    service = FactoryService(data_root=tmp_path)
    service._controller, service._backend_name = controller, "motors_py"
    backend.open_transport()
    backend.enable_motors()
    controller.state_machine.transition(StationState.CONNECTED, expected=StationState.DISCONNECTED)
    monkeypatch.setattr(service, "can_status", lambda: {"all_up": True})
    monkeypatch.setattr(backend, "limit_probe_samples", backend.read_samples, raising=False)
    monkeypatch.setattr("factory_hmi.core.module_limit_check.load_module_plan",
                        lambda config, serial, rev: validate_module_plan(config, profile, serial, rev))
    def fault(*_):
        if mode == "hard":
            raise FeedbackError("力矩超过门限")
        if mode == "excess_damping_torque":
            backend.speeds[0] = 10000.
        if mode == "hot":
            backend.temperatures[0] = 105.
        if mode == "disable_during_handoff":
            controller.request_motion_stop()
        raise ProbeSoftwareFault("模拟检查软件故障")
    if mode == "revoke_before_write":
        begin = backend.begin_protective_damping
        def revoke(*args, **kwargs):
            owner = begin(*args, **kwargs)
            kwargs["limit_stop"].set()
            return owner
        monkeypatch.setattr(backend, "begin_protective_damping", revoke)
    monkeypatch.setattr(ModuleCycle, "step", fault)
    try:
        plan = service.limit_module_preview("MOCK")
        service.limit_module_start("MOCK", plan["digest"], True)
        worker = service._limit_probe
        worker.thread.join(3)
        assert not worker.thread.is_alive()
        report = json.loads(worker.destination.read_text())
        assert report["phase"] == "aborted" and report["stop_phase"] != "aborted"
        records = [c for c in backend.command_history if c.get("mode") == "protective_damping"]
        if mode in {"soft", "feedback_lost"}:
            assert records and all(np.all(c["kd"] == 2) and np.all(c["kp"] == 0)
                                   and np.all(c["velocity"] == 0) and np.all(c["feedforward_torque"] == 0)
                                   for c in records)
            assert report["fault_protection"]["active"] and report["disable"]["request"] == "not_requested"
            assert controller.state is StationState.FAULT and backend.motors_enabled
            if mode == "feedback_lost":
                monkeypatch.setattr(backend, "read_samples", lambda: [])
                until(lambda: not backend.motors_enabled)
                assert controller._protective_damping.snapshot()["phase"] == "failed"
        else:
            assert not records and not backend.motors_enabled
        controller.disable_motors()
        assert not backend.motors_enabled
    finally:
        controller.disconnect()


def test_long_direction_keeps_start_and_recent_full_resolution():
    coords, profile, _ = commissioned("waist_hip")
    p = profile.windows[0].point
    cycle = ProbeCycle(coords, p, np.deg2rad(p.staging_pose_deg), 1.)
    for i in range(30000):
        cycle.record_sample({"n": i})
    history = cycle.history.snapshot()
    old = [row["n"] for row in history["earlier_samples"]]
    assert old == list(range(0, 25000, history["earlier_stride"]))
    assert len(old) < 512 and [r["n"] for r in cycle.samples] == list(range(25000, 30000))
    assert cycle.sample_count == 30000


def test_waist_guard_names_axis_and_respects_site_and_absolute_limits():
    coords, profile, _ = commissioned("waist_hip")
    home = np.deg2rad(profile.home_deg)
    tau = np.zeros_like(home)
    cycle = ModuleCycle(coords, profile, home, 1.)
    q = home.copy()
    q[2] += math.radians(.21)
    with pytest.raises(FeedbackError, match=r"\+0\.210°.*0\.200°"):
        cycle._verify_load(q, tau, tau, 1.025)
    raw = profile.model_dump()
    raw.update(source="site", approved_by="mock", waist_hip_hold_drift_deg=.3,
               waist_hip_hold_bounds_deg=[(v-.5, v+.5) for v in profile.home_deg[2:]])
    custom = type(profile).model_validate(raw)
    cycle = ModuleCycle(coords, custom, home, 1.)
    cycle._verify_load(q, tau, tau, 1.025)
    q[2] += math.radians(.31)
    with pytest.raises(FeedbackError, match="核定保持范围"):
        cycle._verify_load(q, tau, tau, 1.025)


def test_wait_alarm_uses_phase_time_only_and_clears_without_disabling():
    coords, profile, _ = commissioned("waist_hip")
    cycle = ModuleCycle(coords, profile, np.deg2rad(profile.home_deg), 1.)
    cycle.move.commanded_at = 5.
    cycle._update_wait_warning(34.99)
    assert cycle.wait_warning is None
    cycle._update_wait_warning(35.)
    assert cycle.wait_warning["phase"] == cycle.phase and cycle.wait_warning["elapsed_s"] == 30.
    cycle.move.commanded_at = None
    cycle._update_wait_warning(36.)
    assert cycle.wait_warning is None and cycle.last_wait_warning["elapsed_s"] == 30.


def test_observe_and_archive_share_one_serialization(tmp_path, monkeypatch):
    coords, profile, context = commissioned("waist_hip")
    worker = ModuleWorker(None, coords, profile, context, None, None, None, tmp_path/"unused", coords.home)
    worker.cycle = SimpleNamespace(samples=[], results=[], probe=SimpleNamespace(samples=[{}], samples_started=1))
    rows = [MotorSample(pos_rad=0., index=i) for i in range(len(coords.entries))]
    original = MotorSample.to_dict
    calls = []
    def count(row):
        calls.append(row.index)
        return original(row)
    monkeypatch.setattr(MotorSample, "to_dict", count)
    packet = worker._observe_feedback(rows, 1.)
    worker._record_feedback(rows, packet)
    assert calls == list(range(len(rows))) and worker.cycle.last_packet is packet
    assert len(worker.cycle.probe.samples[-1]["drive_load"]) == len(rows)


def test_known_station_node_refresh_uses_positive_numeric_identity():
    from factory_hmi.core.motor_versions import MotorVersions
    from test_lro_local_evidence import RAW_1E, entries
    e = entries()[0]
    matched = (e.interface,e.motor_id,e.motor_model,"0.1.8","0.1.0","0.0.1.17")
    complete = threading.Event()
    rows = []
    def observed(_, row):
        rows.append(row)
        complete.set()
    class Motor:
        def __init__(self):
            self.numeric = bytes.fromhex(RAW_1E)
            self.requests = []
        def request_version(self, code):
            self.requests.append(code)
            return True
        def version_payload(self, code):
            return self.numeric if code == 0x1e else b""
    m = Motor()
    reader = MotorVersions([e],[m],timeout=.01,on_observation=observed,numeric_nodes={matched})
    def refresh():
        complete.clear()
        reader.request_refresh()
        assert complete.wait(1)
        return rows[-1]
    reader.start()
    try:
        assert complete.wait(1) and m.requests == [0x31,0x1e]
        row = refresh()
        assert row["firmware_read_state"] == "station_numeric" and row["errors"] == {}
        assert row["raw_responses"] == {"0x1e": RAW_1E} and m.requests == [0x31,0x1e,0x1e]
        when = row["_monotonic"]
        m.numeric = b""
        row = refresh()
        assert row["_monotonic"] == when and row["last_refresh_errors"].get("0x1e")
        m.numeric = bytes.fromhex(RAW_1E)[:-1] + bytes([18])
        assert refresh()["status"] == "conflict"
        assert m.requests.count(0x31) == 1
    finally:
        reader.close()
