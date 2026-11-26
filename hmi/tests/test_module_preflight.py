"""Exercise the production preparation path with isolated station state and fake motors."""
import threading
import time
import uuid
from dataclasses import replace

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.limit_block_check import nominal_station
from factory_hmi.core.protective_damping import ProtectiveDamping
from factory_hmi.core.service import FactoryService
from factory_hmi.models import StationState


def station(limb, root, monkeypatch):
    config = nominal_station(limb)
    if limb == "left_arm":
        config = replace(config, entries=tuple(replace(e, interface="can1") for e in config.entries))
    backend = FakeMotorBackend(config.entries, initial_positions=np.zeros(len(config.entries)))
    service = FactoryService(data_root=root)
    controller = service._controller = FactoryController(config, backend)
    service._backend_name = "motors_py"
    monkeypatch.setattr(service, "can_status", lambda: {"all_up": True})
    def fresh():
        backend.refresh_feedback()
        return backend.read_samples()
    monkeypatch.setattr(backend, "limit_probe_samples", fresh, raising=False)
    backend.open_transport()
    backend.enable_motors()
    controller.state_machine.transition(StationState.CONNECTED, expected=StationState.DISCONNECTED)
    controller._start_position_hold(np.zeros(len(config.entries)))
    return service, backend


def test_two_workstations_keep_dispatching_while_preparing(tmp_path, monkeypatch, record_property):
    stations = []
    try:
        for limb in ("left_short_leg", "left_arm"):
            service, backend = station(limb, tmp_path / limb, monkeypatch)
            stations.append((service, backend))
            ticket = uuid.uuid4().hex
            initial = service.limit_module_prepare("MOCK", ticket, 3)
            assert initial["preparing"]
            job = service._module_preflight.job
            assert service.limit_module_prepare("MOCK", ticket, 3)["preparation_id"] == ticket
            assert service._module_preflight.job is job
        for service, backend in stations:
            job = service._module_preflight.job
            job.future.result(timeout=30)
            # Inject 2 s of queued work on the same compute process.
            service._module_preflight.pool.submit(time.sleep, 2)
            ticket = uuid.uuid4().hex
            assert service.limit_module_prepare("MOCK", ticket, 3)["preparing"]
        before = [len(backend.command_history) for _, backend in stations]
        time.sleep(.4)
        for i, (service, backend) in enumerate(stations):
            assert len(backend.command_history) > before[i]
            assert service.controller.state is StationState.CONNECTED
            assert service.limit_probe_status()["active"] is False
        for service, backend in stations:
            job = service._module_preflight.job
            plan, _ = job.future.result(timeout=30)
            stamps = [cmd["timestamp_s"] for cmd in tuple(backend.command_history)]
            assert stamps[-1] - stamps[0] > 2
            gap = float(np.max(np.diff(stamps)))
            record_property(service.controller.config.limb + "_max_gap_ms", round(gap * 1000, 1))
            assert gap < .5
            assert service.limit_module_preparation(job.id)["digest"] == plan["digest"]
            with pytest.raises(ValueError, match="方案已变化"):
                service.limit_module_start("MOCK", plan["digest"], True, job.id, 4)
            if service.controller.config.limb == "left_short_leg":
                service.request_motion_stop()
                with pytest.raises(ValueError, match="已取消"):
                    service.limit_module_start("MOCK", plan["digest"], True, job.id, 3)
            else:
                service.limit_module_start("MOCK", plan["digest"], True, job.id, 3)
                assert service._limit_probe._prepared_arm_completion.config is service.controller.config
                with pytest.raises(ValueError, match="已取消"):
                    service.limit_module_start("MOCK", plan["digest"], True, job.id, 3)
                service.disable_motors()
    finally:
        for service, _ in stations:
            service.close()


def test_damping_identifies_each_drive_and_reason(tmp_path, monkeypatch):
    service, backend = station("left_arm", tmp_path, monkeypatch)
    try:
        samples = backend.read_samples()
        rows = [replace(samples[1], error_id=3, temp_c=34), replace(samples[3], error_id=0, temp_c=102)]
        with pytest.raises(RuntimeError) as err:
            ProtectiveDamping(backend, overtemperature_c=100)._check_drives(rows)
        assert "#2 left_shoulder_roll_joint 驱动故障 error_id=3" in str(err.value)
        assert "#4 left_elbow_joint 温度 102 °C 达到阈值 100 °C" in str(err.value)
    finally:
        service.close()


def test_initial_feedback_failure_distinguishes_expired_frame_and_drive_alarm():
    from types import SimpleNamespace
    from factory_hmi.core.playback import PlaybackController, PlaybackError
    from factory_hmi.models import MotorSample
    row = MotorSample(pos_rad=0., timestamp_s=1., feedback_age_s=.95,
                      motor_id=2, bus="can1", joint_name="left_shoulder_roll_joint")
    rig = SimpleNamespace(backend=SimpleNamespace(read_samples=lambda: [row]),
                          config=SimpleNamespace(entries=[1]),
                          _parameters=SimpleNamespace(feedback_stale_seconds=.5))
    with pytest.raises(PlaybackError, match="can1#2.*反馈延迟 0.950 s") as stale:
        PlaybackController._sample_feedback(rig, None, 0, update_statistics=False)
    assert "error_id=" not in str(stale.value)
    row = replace(row, feedback_age_s=.001, error_id=7)
    with pytest.raises(PlaybackError, match="can1#2.*error_id=7") as fault:
        PlaybackController._sample_feedback(rig, None, 0, update_statistics=False)
    assert "反馈延迟" not in str(fault.value)

class DelayedLegFeedback(FakeMotorBackend):
    """ID 6 can keep its pre-enable frame while the other four reply."""
    def __init__(self, entries, delay, handshake=False):
        super().__init__(entries)
        self.delay = delay
        self.handshake = handshake
        self.enabled_at = None
        self.post_enable_reads = 0

    def _enable_motors_impl(self):
        super()._enable_motors_impl()
        self.enabled_at = time.monotonic()
        if self.handshake:
            self._refresh_feedback_impl()
            return self._read_samples_impl()

    def _refresh_feedback_impl(self):
        if self._enabled and self.enabled_at is not None:
            self.post_enable_reads += 1
            self.stale[3] = time.monotonic() - self.enabled_at < self.delay
        super()._refresh_feedback_impl()


@pytest.mark.parametrize("delay, handshake", [(.06, False), (.65, False), (.65, True)])
def test_first_hold_uses_current_enable_budget(delay, handshake, record_property):
    config = nominal_station("left_short_leg")
    backend = DelayedLegFeedback(config.entries, delay, handshake)
    assert backend.entries[3].motor_id == 6
    controller = FactoryController(config, backend)
    try:
        controller.discover_motors()
        if not handshake and delay > .5:
            with pytest.raises(RuntimeError, match=r"after motor enable: fresh feedback timeout: motor_ids=6;.*can0#6"):
                controller.enable_motors()
            assert controller.state is StationState.FAULT
            assert not backend.command_history and not backend.motors_enabled
            assert controller._fault.kind != "ControlTimeout"
            return
        controller.enable_motors()
        assert backend.wait_for_commands(2)
        elapsed = backend.command_history[0]["timestamp_s"] - backend.enabled_at
        record_property("first_hold_ms", round(elapsed * 1000, 2))
        assert 0 <= elapsed < .5
        assert np.array_equal(backend.command_history[0]["target"], backend.positions)
        if handshake:
            assert backend.post_enable_reads == 0
        else:
            assert elapsed >= delay
    finally:
        controller.disconnect()


@pytest.mark.parametrize("cancel", [False, True])
def test_delayed_leg_worker_keeps_one_command_owner(tmp_path, monkeypatch, record_property, cancel):
    from factory_hmi.core.module_limit_check import ModuleWorker
    service, backend = station("left_short_leg", tmp_path, monkeypatch)
    gate, entered, dispatched = threading.Event(), threading.Event(), threading.Event()
    owners = []
    original_send = backend._command_mit_impl

    def observe(*args, **kwargs):
        original_send(*args, **kwargs)
        owner = threading.current_thread().name
        owners.append(owner)
        if owner == "limit-probe":
            dispatched.set()

    original_start = ModuleWorker.start

    def slow_start(self):
        original_read = self.read

        def delayed_read():
            entered.set()
            gate.wait(3)
            return original_read()

        self.read = delayed_read
        original_start(self)

    monkeypatch.setattr(ModuleWorker, "start", slow_start)
    monkeypatch.setattr(backend, "_command_mit_impl", observe)
    try:
        job_id = uuid.uuid4().hex
        service.limit_module_prepare("MOCK", job_id, 1)
        service._module_preflight.job.future.result(timeout=30)
        plan = service.limit_module_preparation(job_id)
        service.limit_module_start("MOCK", plan["digest"], True, job_id, 1)
        assert entered.wait(1)
        start_count = len(owners)
        time.sleep(.65)
        assert len(owners) > start_count and "limit-probe" not in owners
        assert backend.motors_enabled and service.controller.state is StationState.CONNECTED
        if cancel:
            service.request_motion_stop()
        gate.set()
        if cancel:
            service._limit_probe.thread.join(1)
            assert not dispatched.is_set() and not backend.motors_enabled
        else:
            assert dispatched.wait(1)
            service._limit_probe.request_stop()
            service._limit_probe.thread.join(1)
            assert set(owners[owners.index("limit-probe"):]) == {"limit-probe"}
            diag = service._limit_probe.snapshot()["feedback_diagnostics"]
            record_property("handoff_gap_ms", diag["handoff_gap_ms"])
            assert diag["handoff_gap_ms"] < 500
            assert not backend.motors_enabled
    finally:
        gate.set()
        service.close()


@pytest.mark.parametrize("bad", ["old", "wrong_motor", "disabled_109", "drive_fault"])
def test_unusable_handshake_never_supplies_a_hold_target(bad, monkeypatch):
    config = nominal_station("left_short_leg")
    backend = DelayedLegFeedback(config.entries, .65, handshake=True)
    controller = FactoryController(config, backend)
    original = backend._enable_motors_impl

    def bad_reply():
        previous_stamp = backend.feedback_timestamps[3]
        rows = original()
        if bad in ("old", "disabled_109"):
            # No later genuine reply from ID 6 available to rescue this read.
            backend.feedback_timestamps[3] = previous_stamp
        changes = {
            "old": {"timestamp_s": previous_stamp},
            "wrong_motor": {"motor_id": 7},
            "disabled_109": {"lro_protocol_profile": "v1.03", "drive_enabled": False},
            "drive_fault": {"error_id": 3},
        }
        rows[3] = replace(rows[3], **changes[bad])
        return rows

    monkeypatch.setattr(backend, "_enable_motors_impl", bad_reply)
    try:
        controller.discover_motors()
        with pytest.raises(RuntimeError, match="after motor enable"):
            controller.enable_motors()
        assert not backend.command_history and not backend.motors_enabled
    finally:
        controller.disconnect()


def test_slow_controller_snapshot_does_not_break_position_hold(tmp_path, monkeypatch):
    service, backend = station("left_short_leg", tmp_path, monkeypatch)
    try:
        with service.controller._lock:
            before = len(backend.command_history)
            time.sleep(.65)
            assert len(backend.command_history) > before
            assert service.controller.state is StationState.CONNECTED
        assert backend.motors_enabled
    finally:
        service.close()


def test_blocked_first_dispatch_still_trips_station_watchdog(monkeypatch):
    config = nominal_station("left_short_leg")
    backend = FakeMotorBackend(config.entries)
    controller = FactoryController(config, backend)
    entered, release = threading.Event(), threading.Event()
    original = backend._command_mit_impl

    def blocked(*args, **kwargs):
        entered.set()
        release.wait(3)
        original(*args, **kwargs)

    monkeypatch.setattr(backend, "_command_mit_impl", blocked)
    try:
        controller.discover_motors()
        controller.enable_motors()
        assert entered.wait(1)
        deadline = time.monotonic() + 1.5
        while controller.state is not StationState.FAULT and time.monotonic() < deadline:
            time.sleep(.01)
        assert controller.state is StationState.FAULT and backend.commands_inhibited
        message = controller._fault.message
        assert "(limit 0.500s)" in message and "dispatch_count=0 " in message
        assert "调用电机后端" in message and "sender=factory-position-hold-left_short_leg" in message
        assert backend.last_mit_success_s is None
    finally:
        release.set()
        controller.disconnect()


@pytest.mark.parametrize("changes,reason", [
    ({"timestamp_s": 0.}, "时间戳无效"),
    ({"feedback_age_s": .16}, "反馈超时 160.0 ms"),
    ({"error_id": 7}, "驱动故障 error_id=7"),
    ({"fault_code_available": False}, "故障码不可用"),
    ({"drive_enabled": False}, "未使能"),
    ({"torque_nm": float("nan")}, "力矩反馈无效"),
    ({"pos_rad": float("inf")}, "位置反馈无效"),
])
def test_pose_error_identifies_axis_and_reason(changes, reason):
    from factory_hmi.core.limit_block_check import Coordinates, FeedbackError
    from factory_hmi.models import MotorSample
    coords = Coordinates(nominal_station("left_arm"))
    rows = [MotorSample(pos_rad=0., bus=b, motor_id=i, joint_name=j, timestamp_s=1.) for b, i, j in coords.identity]
    rows[-1] = replace(rows[-1], **changes)
    with pytest.raises(FeedbackError) as fault:
        coords.decode(rows, 1.01, ())
    assert f"{rows[-1].bus}#7" in str(fault.value) and reason in str(fault.value)


def test_one_empty_batch_can_recover_for_both_pose_entry_points(tmp_path, monkeypatch):
    service, backend = station("left_arm", tmp_path, monkeypatch)
    try:
        fresh = backend.limit_probe_samples
        reads = []
        def read():
            reads.append(None)
            return [] if len(reads) == 1 else fresh()
        monkeypatch.setattr(backend, "limit_probe_samples", read)
        ticket = uuid.uuid4().hex
        service.limit_module_prepare("MOCK", ticket, 1)
        assert len(reads) == 2
        job = service._module_preflight.job
        plan, _ = job.future.result(timeout=30)
        reads.clear()
        service.limit_module_start("MOCK", plan["digest"], True, ticket, 1)
        assert len(reads) >= 2
    finally:
        service.close()


def test_pose_empty_retry_expires_and_driver_exceptions_propagate(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from factory_hmi.core import service as module
    now = [100.]
    def wait(dt):
        now[0] += dt
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=wait))
    with pytest.raises(ValueError, match="150 ms"):
        FactoryService._read_probe_entry(lambda: [])
    assert now[0] == pytest.approx(100.15)
    read = Mock(side_effect=RuntimeError("driver fault"))
    with pytest.raises(RuntimeError, match="driver fault"):
        FactoryService._read_probe_entry(read)
    read.assert_called_once()
