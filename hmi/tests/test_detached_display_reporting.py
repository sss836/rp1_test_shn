"""No device access. Validate bounded display readers and detached report evidence."""
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from factory_hmi.core import report_jobs
from factory_hmi.core.service import FactoryService
from factory_hmi.core.telemetry_hub import TelemetryHub
from factory_hmi.gateway.display import DisplayPublisher
from factory_hmi.models import StationState
from factory_hmi.models import MotorSample


def wait_for(get, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = get()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("timed out waiting for isolated fixture")


def test_display_readers_share_one_immutable_latest_frame_and_never_trigger_capture():
    next_frame = threading.Event()
    second_capture = threading.Event()
    samples = []

    def collect():
        samples.append(threading.current_thread().name)
        if len(samples) > 1:
            second_capture.set()
            next_frame.wait(2)
        return {"motor_source_timestamp": 123.45}

    publisher = DisplayPublisher(collect, rate_hz=10)
    publisher.start()
    publisher.start()
    try:
        first = wait_for(publisher.latest)
        assert second_capture.wait(1)
        assert all(publisher.latest() is first for _ in range(100))
        assert publisher.latest(max_age_s=0) is None
        assert samples == ["factory-display", "factory-display"]
        assert json.loads(first.text) == {
            "motor_source_timestamp": 123.45,
            "_display": {"sequence": 1, "sampled_at": first.sampled_at},
        }
        assert publisher.stop(timeout=0) is False
        assert publisher.latest() is None
    finally:
        next_frame.set()
        assert publisher.stop()


def test_oversize_utf8_frame_cannot_replace_last_display_frame():
    samples = iter([{"state": "ok"}, {"state": "中" * 1000}, {"state": "recovered"}])
    second = threading.Event()
    allow_recovery = threading.Event()

    def collect():
        payload = next(samples, {"state": "recovered"})
        if payload["state"] == "recovered":
            second.set()
            allow_recovery.wait(2)
        return payload

    publisher = DisplayPublisher(collect, rate_hz=20, max_bytes=200)
    publisher.start()
    try:
        assert second.wait(1)
        first = publisher.latest()
        assert first.sequence == 1
        assert json.loads(first.text)["state"] == "ok"
        allow_recovery.set()
        newest = wait_for(lambda: (frame if (frame := publisher.latest()).sequence > 1 else None))
        assert json.loads(newest.text)["state"] == "recovered"
    finally:
        allow_recovery.set()
        assert publisher.stop()


def test_read_only_display_uses_timestamped_can_cache_without_feedback_io(tmp_path):
    manager = Mock()
    manager.status.return_value = []
    service = FactoryService(data_root=tmp_path, can_manager=manager)
    controller = SimpleNamespace(
        sample_display_feedback=Mock(),
        snapshot=Mock(return_value={"station": {}}),
        config=SimpleNamespace(entries=[]),
        state=StationState.CONNECTED,
        backend=SimpleNamespace(motor_versions=lambda: []),
    )
    service._controller = controller
    service._backend_name = "motors_py"
    service._can_interfaces = ("can0",)
    assert service.display_snapshot()["can"]["stale"] is True
    manager.status.assert_not_called()
    controller.sample_display_feedback.assert_not_called()

    first = service.collect_display_snapshot()["can"]
    for _ in range(5):
        assert service.display_snapshot()["can"] == first
    assert first["stale"] is False
    assert isinstance(first["sampled_monotonic_s"], float)
    assert service.collect_display_snapshot()["can"] == first
    manager.status.assert_called_once_with(("can0",))
    assert controller.sample_display_feedback.call_count == 2
    assert all(call.kwargs == {"refresh_feedback": False} for call in controller.snapshot.call_args_list)

    service._config_revision = "changed"
    assert service.display_snapshot()["can"]["stale"] is True
    assert manager.status.call_count == 1


def test_preview_returns_latest_suffix_with_visible_gap_under_budget():
    hub = TelemetryHub()
    hub.begin("run-1")
    for index in range(90):
        hub.publish([MotorSample(1, joint_name="关节")], now=10+index*.06)
    payload = hub.read(after=0, limit=64, max_response_bytes=3000, now=15.4)
    frames = payload["frames"]
    assert 1 < len(frames) < 64
    assert frames[-1]["sequence"] == payload["last_sequence"] == 90
    assert payload["sequence_gap"] is True
    assert len(json.dumps(frames, separators=(",", ":")).encode()) <= 3000
    assert not hub.read(after=90, limit=64)["sequence_gap"]


@pytest.fixture
def report_source(tmp_path):
    source = tmp_path / "record.csv"
    source.write_text("time_s,hip_pos_rad,hip_cmd_pos_rad,hip_torque_nm,hip_temp_c\n0,0,0,1,25\n1,0.1,0.2,2,26\n")
    context = {
        "execution_uuid": str(uuid4()),
        "test_id": "synthetic-report",
        "recording_scope": "local",
        "started_at": "2026-10-15T00:00:00Z",
        "ended_at": "2026-10-15T00:00:01Z",
        "active_seconds": 1,
        "outcome": "completed",
        "record_rate_hz": 1,
    }
    return source, context


def test_real_renderer_detaches_from_service_and_retains_original(tmp_path, report_source, monkeypatch):
    source, context = report_source
    original = source.read_bytes()
    sidecar = source.with_suffix(".context.json")
    sidecar.write_text(json.dumps(context))
    entered, release = threading.Event(), threading.Event()
    execute = report_jobs._run_enqueued

    def controlled(*args):
        entered.set()
        assert release.wait(2)
        execute(*args)

    monkeypatch.setattr(report_jobs, "_run_enqueued", controlled)
    service = FactoryService(data_root=tmp_path)
    service.capacity_reservations.reserve(context["execution_uuid"], 100, 1000)
    key = report_jobs.enqueue_report_job(tmp_path, source, context)
    service._queued_report_ids.add(key)
    try:
        assert entered.wait(1)
        assert report_jobs.enqueue_report_job(tmp_path, source, context) == key
        assert report_jobs.report_job_status(tmp_path, key)["state"] == "pending"
        assert service.close(timeout=.01)["closed"]
        assert source.read_bytes() == original and sidecar.is_file()
    finally:
        release.set()
    service.wait_for_reports(timeout=10)
    assert not service._report_errors
    assert report_jobs.report_job_status(tmp_path, key)["state"] == "completed"
    assert source.read_bytes() == original and not sidecar.exists()
    assert service.report_manager.validate(key)
    assert service.execution_history_page()["total"] == 1
    assert service.outbox.list() == []


def test_cancel_pending_report_never_publishes_but_keeps_source(tmp_path, report_source, monkeypatch):
    source, context = report_source
    entered, release = threading.Event(), threading.Event()
    execute = report_jobs._run_enqueued

    def controlled(*args):
        entered.set()
        assert release.wait(2)
        execute(*args)

    monkeypatch.setattr(report_jobs, "_run_enqueued", controlled)
    key = report_jobs.enqueue_report_job(tmp_path, source, context)
    try:
        assert entered.wait(1)
        assert report_jobs.cancel_report_job(tmp_path, key)["state"] == "cancelled"
    finally:
        release.set()
    wait_for(lambda: tmp_path not in report_jobs._dispatchers)
    assert report_jobs.report_job_status(tmp_path, key)["state"] == "cancelled"
    assert source.is_file()
    assert not (tmp_path / "executions" / key).exists()


def test_empty_window_restart_resumes_durable_job_from_another_station(tmp_path, report_source):
    source, context = report_source
    root = tmp_path / "shared-reports"
    key = report_jobs._queue(root, source, context)
    assert report_jobs.report_job_status(root, key)["state"] == "pending"
    service = FactoryService(data_root=tmp_path / "fresh-window", report_root=root)
    receipt = wait_for(lambda: (
        receipt if (receipt := report_jobs.report_job_status(root, key))["state"] in {"completed", "failed"} else None
    ))
    assert receipt["state"] == "completed", receipt
    assert service.report_manager.validate(key)
    service.close()


def test_station_close_keeps_can_ownership_until_every_link_reports_down(tmp_path, monkeypatch):
    manager = Mock()
    service = FactoryService(data_root=tmp_path, can_manager=manager)
    backend = SimpleNamespace(connected=True)
    disconnected = Mock(side_effect=lambda: setattr(backend, "connected", False))
    service._controller = SimpleNamespace(backend=backend, disconnect=disconnected)
    service._backend_name = "motors_py"
    service._can_interfaces = ("can0", "can1")
    archive = Mock()
    monkeypatch.setattr("factory_hmi.core.motor_parameter_transport.archive_pn_quarantine", archive)
    manager.status.side_effect = [
        [SimpleNamespace(interface="can0", up=False)],
        [SimpleNamespace(interface="can0", up=False), SimpleNamespace(interface="can1", up=True)],
        [SimpleNamespace(interface="can0", up=False), SimpleNamespace(interface="can1", up=False)],
    ]
    manager.disconnect.side_effect = lambda _interfaces: backend.connected is False or pytest.fail("motor still connected")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="CAN关闭状态未确认"):
            service.close(disconnect_can=True)
        assert service._closing.is_set() and not service._closed
        archive.assert_not_called()
    assert service.close(disconnect_can=True)["closed"]
    assert disconnected.call_count == manager.disconnect.call_count == manager.status.call_count == 3
    archive.assert_called_once()


def test_active_saved_window_source_accepts_identity_only_from_station_boundary(tmp_path):
    from fastapi import HTTPException
    from factory_hmi.gateway.local_curves import _source

    service = FactoryService(data_root=tmp_path / "station", report_root=tmp_path / "shared")
    service._record_identity = {"execution_uuid": "run1", "test_id": "trial", "operator_id": "internal"}
    assert service.preview_record_identity("run1") == {"execution_uuid": "run1", "test_id": "trial"}
    assert service.preview_record_identity("other") is None
    remote = SimpleNamespace(data_root=service.data_root, report_manager=service.report_manager,
                             preview_record_identity=service.preview_record_identity)
    checkpoint = service.record_root / "trial.checkpoint.json"
    checkpoint.write_text(json.dumps({"last_time_s": 1.25}))
    source = _source(remote, "run1")
    assert source == {"sealed": False, "duration": 1.25,
                      "windows": service.record_root / "trial.segments/windows.jsonl"}
    with pytest.raises(HTTPException) as rejected:
        _source(remote, "other")
    assert rejected.value.status_code == 409
    checkpoint.unlink()
    service.close()


def test_clear_configuration_preserves_device_identity_until_can_down_is_confirmed(tmp_path, monkeypatch):
    manager = Mock()
    service = FactoryService(data_root=tmp_path, can_manager=manager)
    backend = SimpleNamespace(connected=False)
    controller = SimpleNamespace(backend=backend, disconnect=Mock())
    service._controller = controller
    service._backend_name = "motors_py"
    service._can_interfaces = ("can0", "can1")
    before = service._config_revision
    archive = Mock()
    monkeypatch.setattr("factory_hmi.core.motor_parameter_transport.archive_pn_quarantine", archive)
    manager.status.side_effect = [
        [SimpleNamespace(interface="can0", up=False)],
        [SimpleNamespace(interface="can0", up=False), SimpleNamespace(interface="can1", up=False)],
    ]
    with pytest.raises(RuntimeError, match="CAN关闭状态未确认"):
        service.clear_configuration()
    assert service._controller is controller and service._can_interfaces == ("can0", "can1")
    assert service._config_revision == before
    archive.assert_not_called()
    assert service.clear_configuration()["configured"] is False
    assert service._controller is None and service._can_interfaces == ()
    archive.assert_called_once()


def test_configure_uses_the_same_parsed_config_as_can_ownership(tmp_path, monkeypatch):
    from pathlib import Path
    from factory_hmi.core.config import load_station_config

    service = FactoryService(data_root=tmp_path)
    library = Path(__file__).resolve().parents[1] / "scripts/config/arm_motors.yaml"
    loaded = load_station_config("left_arm", library)
    config_path = tmp_path / "mutable.yaml"
    config_path.write_text("replacement data must not be loaded\n")
    reread = Mock(side_effect=AssertionError("configuration changed after CAN ownership was established"))
    monkeypatch.setattr("factory_hmi.core.service.load_station_config", reread)
    snapshot = service.configure(config_path=str(config_path), limb="left_arm", backend="fake", station_config=loaded)
    assert snapshot["configured"]
    assert service.controller.config is loaded
    reread.assert_not_called()
    assert service.close()["closed"]
