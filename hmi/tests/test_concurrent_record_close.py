"""Concurrent CSV closure uses fake file owners only; no PLC/CAN is created."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch

import pytest

from factory_hmi.core import playback as playback_module
from factory_hmi.core.playback import PlaybackController
from factory_hmi.core.service import FactoryService
from scripts.log_motor_telemetry import MotorTelemetryRecorder


def recorder_owner(release):
    recorder = MotorTelemetryRecorder.__new__(MotorTelemetryRecorder)
    recorder._stop = threading.Event()
    recorder._closed = threading.Event()
    recorder._stopped_elapsed_s = 0.0
    recorder._writer = None
    recorder.local_only = True
    recorder.blocked_reason = None
    recorder.row_count = 0
    entered = threading.Event()

    def owner():
        entered.set()
        release.wait(3)
        recorder._closed.set()

    recorder._thread = threading.Thread(target=owner, daemon=True)
    recorder._thread.start()
    assert entered.wait(1)
    return recorder


def playback_for(recorder):
    playback = PlaybackController.__new__(PlaybackController)
    playback._thread = None
    playback._recorder = recorder
    playback._lock = threading.RLock()
    playback._record_finalize_lock = threading.Lock()
    playback._record_done = threading.Event()
    playback._record_finished = False
    playback._record_error = None
    playback._fault = None
    playback._completion = {"alarm": None}
    playback._handle_fault = Mock()
    return playback


def test_record_close_timeout_preserves_live_owner_and_never_changes_motor_state(monkeypatch):
    monkeypatch.setattr(playback_module, "RECORD_CLOSE_TIMEOUT_S", .03)
    release = threading.Event()
    recorder = recorder_owner(release)
    playback = playback_for(recorder)
    try:
        playback.finalize_recording()
        assert playback._record_done.is_set()  # Attempt finished, not CSV ownership.
        assert not playback._record_finished
        assert "still closing" in playback._record_error
        assert recorder._thread is not None and recorder._thread.is_alive()
        assert playback.recording_owner_alive()
        assert playback.recording_close_pending()
        assert not playback.wait_recording(.01)
        assert not recorder.wait_closed(0)
        playback._handle_fault.assert_not_called()
    finally:
        release.set()
        recorder._thread.join(1)
    assert playback.wait_recording(.1)
    assert not playback.recording_close_pending()
    assert not playback._record_finished  # Late exit never converts the failed seal to success.


def test_second_window_closes_while_global_finalizer_and_first_csv_are_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(playback_module, "RECORD_CLOSE_TIMEOUT_S", .04)
    queue_entered, release_queue, release_first, release_second = [threading.Event() for _ in range(4)]
    source = tmp_path / "original.csv"
    original = b"time_s,joint_pos_rad\n0,0\n"
    source.write_bytes(original)

    def blocked_queue_job():
        queue_entered.set()
        assert release_queue.wait(3)

    queued = playback_module._RECORD_FINALIZER.submit(blocked_queue_job)
    assert queue_entered.wait(1)
    first_recorder = recorder_owner(release_first)
    second_recorder = recorder_owner(release_second)
    release_second.set()
    second_recorder._thread.join(1)
    first, second = playback_for(first_recorder), playback_for(second_recorder)
    try:
        with ThreadPoolExecutor(max_workers=2) as existing_report_waiters:
            a = existing_report_waiters.submit(first.finalize_recording)
            b = existing_report_waiters.submit(second.finalize_recording)
            b.result(timeout=.3)
            assert second.wait_recording(.01)
            assert second._record_finished and second._record_error is None
            assert not release_queue.is_set() and not release_first.is_set()
            a.result(timeout=.3)
            assert not first.wait_recording(.01) and not first._record_finished
        assert source.read_bytes() == original
        first._handle_fault.assert_not_called()
        second._handle_fault.assert_not_called()
    finally:
        release_first.set()
        first_recorder._thread.join(1)
        release_queue.set()
        queued.result(timeout=1)


def test_competing_finalizers_close_one_execution_exactly_once(monkeypatch):
    monkeypatch.setattr(playback_module, "RECORD_CLOSE_TIMEOUT_S", .04)
    release = threading.Event()
    recorder = recorder_owner(release)
    playback = playback_for(recorder)
    entered = threading.Event()
    original = recorder.finalize
    calls = []

    def finalize(**kwargs):
        calls.append(kwargs)
        entered.set()
        original(**kwargs)

    recorder.finalize = finalize
    try:
        with ThreadPoolExecutor(max_workers=2) as waiters:
            one = waiters.submit(playback.finalize_recording)
            assert entered.wait(1)
            waiters.submit(playback.finalize_recording).result(timeout=.2)
            one.result(timeout=.3)
        playback.finalize_recording()
        assert len(calls) == 1
        assert not playback._record_finished and playback._record_error
    finally:
        release.set()
        recorder._thread.join(1)


def test_segment_writer_owner_survives_sampling_thread_exit():
    release, entered = threading.Event(), threading.Event()
    writer = threading.Thread(target=lambda: (entered.set(), release.wait(3)), daemon=True)
    writer.start()
    assert entered.wait(1)
    recorder = MotorTelemetryRecorder.__new__(MotorTelemetryRecorder)
    recorder._thread = None
    recorder._writer = SimpleNamespace(_thread=writer)
    recorder._stop = threading.Event()
    recorder._closed = threading.Event()
    recorder._closed.set()
    recorder._stopped_elapsed_s = 0.0
    recorder.local_only = True
    recorder.blocked_reason = None
    recorder.row_count = 0
    try:
        assert recorder.owner_alive() and not recorder.wait_closed(0)
        with pytest.raises(TimeoutError, match="writer is still closing"):
            recorder.finalize(timeout=.01)
        assert recorder._writer._thread is writer
    finally:
        release.set()
        writer.join(1)
    assert recorder.wait_closed(.1)


def test_queued_previous_recorder_never_marks_a_reused_playback_complete():
    release_old, release_new = threading.Event(), threading.Event()
    old = recorder_owner(release_old)
    release_old.set()
    old._thread.join(1)
    playback = playback_for(old)
    playback.finalize_recording()
    assert playback._record_finished
    current = recorder_owner(release_new)
    # PlaybackController.start resets these same fields for its next recorder.
    with playback._lock:
        playback._recorder = current
        playback._record_done.clear()
        playback._record_finished = False
        playback._record_error = None
    try:
        playback._finish_recording(old)
        assert not playback._record_done.is_set()
        assert not playback._record_finished and playback._record_error is None
        assert playback.recording_owner_alive()
        assert not current._stop.is_set()
    finally:
        release_new.set()
        current._thread.join(1)
        playback.finalize_recording()


def test_service_retains_owner_and_rejects_reconfiguration_release_and_next_run(tmp_path):
    release = threading.Event()
    recorder = recorder_owner(release)
    service = FactoryService(data_root=tmp_path)
    service._recording_session = playback_for(recorder)
    try:
        for operation in [
            lambda: service.configure(config_path="unused", limb="left_arm", backend="fake"),
            service.can_disconnect,
            service.prepare_next_run,
            lambda: service._start_once(),
        ]:
            with pytest.raises(RuntimeError, match="记录文件仍在收尾"):
                operation()
        with pytest.raises(RuntimeError, match="记录文件仍在收尾"):
            service.close(timeout=.05)
        assert not service._closed
        assert service._recording_session is not None
    finally:
        release.set()
        recorder._thread.join(1)
        service._recording_session.finalize_recording()
        service.close()


def test_report_waiter_records_close_failure_without_archiving_or_releasing_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(playback_module, "RECORD_CLOSE_TIMEOUT_S", .03)
    release = threading.Event()
    recorder = recorder_owner(release)
    playback = playback_for(recorder)
    playback.wait = lambda: True
    playback.snapshot = lambda: {"progress": {"outcome": "stopped"}, "record_error": playback._record_error}
    service = FactoryService(data_root=tmp_path)
    service._recording_session = playback
    capacity = service.capacity_reservations.complete = Mock()
    source = tmp_path / "retained.csv"
    source.write_bytes(b"original evidence")
    try:
        with patch("factory_hmi.core.report_jobs.enqueue_report_job") as report:
            service._finalize_after_run(SimpleNamespace(), source, {"test_id": "synthetic-timeout"}, playback)
            report.assert_not_called()
        assert "收尾超时" in service._report_errors["synthetic-timeout"]
        capacity.assert_not_called()
        assert source.read_bytes() == b"original evidence"
        assert not (tmp_path / "executions/synthetic-timeout/manifest.json").exists()
    finally:
        release.set()
        recorder._thread.join(1)
        service.close()
