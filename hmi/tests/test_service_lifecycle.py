"""Deterministic writer ownership checks, using files/fake motors only."""
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch
import pytest

from factory_hmi.core.service import FactoryService
from factory_hmi.core.report import ReportManager
from factory_hmi.gateway.app import GatewayHub


def seed_pending(root):
    records = root / "records"
    records.mkdir(parents=True)
    # Recovery is intercepted below; this is only a synthetic pending marker.
    (records / "synthetic.csv").write_text("time_s,joint_1_pos_rad\n0,0\n")


def test_empty_service_has_no_unowned_recovery_io(tmp_path):
    with patch.object(FactoryService, "_recover_existing_records") as recovery:
        service = FactoryService(data_root=tmp_path)
        assert service._report_threads == set()
        recovery.assert_not_called()
        assert not (tmp_path / ".evidence.lock").exists()
        assert not (tmp_path / "executions/执行台账.csv").exists()
        # Explicit history queries still create the complete empty ledger.
        assert service.execution_history_page()["total"] == 0
        assert (tmp_path / "executions/执行台账.csv").is_file()
        assert service.close()["closed"]


def test_close_drains_recovery_before_filesystem_owner_exits(tmp_path):
    seed_pending(tmp_path)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    def recover(manager, record_root):
        entered.set()
        assert release.wait(3)
        (manager.data_root / "recovery-complete").write_text("done")
        return []
    with patch.object(ReportManager, "recover_orphans", recover):
        service = FactoryService(data_root=tmp_path)
        assert entered.wait(1)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: (service.close(), closed.set()))
            try:
                assert service._closing.wait(1)
                assert not closed.is_set()
                assert not future.done()
                # close must not hold _lock while joining: the recovering worker
                # acquires it to publish errors/completion.
                assert service._lock.acquire(timeout=.2)
                service._lock.release()
            finally:
                release.set()
            future.result(timeout=2)
        assert (tmp_path / "recovery-complete").is_file()
        assert service._report_threads == set()
        assert service.close()["closed"] # idempotent
        with pytest.raises(RuntimeError, match="closing or closed"):
            service.configure(config_path="unused", limb="left_arm", backend="fake")


def test_timeout_is_visible_and_writer_is_kept_for_retry(tmp_path):
    seed_pending(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def recover(manager, record_root):
        entered.set(); assert release.wait(3); return []
    with patch.object(ReportManager, "recover_orphans", recover):
        service = FactoryService(data_root=tmp_path)
        assert entered.wait(1)
        try:
            with pytest.raises(TimeoutError, match="factory-report-recovery"):
                service.close(timeout=.01)
            assert not service._closed
            assert any(t.is_alive() for t in service._report_threads)
            assert "shutdown" in service._report_errors
        finally:
            release.set()
            service.close()
        assert service._closed and not service._report_threads


def test_report_failure_is_retained_after_join(tmp_path):
    seed_pending(tmp_path)
    with patch.object(ReportManager, "recover_orphans", side_effect=OSError("synthetic recovery failure")):
        service = FactoryService(data_root=tmp_path)
        status = service.close()
    assert status["closed"]
    assert status["report_errors"]["startup-recovery"] == "synthetic recovery failure"


def test_gateway_drains_facades_even_when_one_close_fails(tmp_path):
    with patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        hub = GatewayHub(None, data_root=tmp_path, lease_timeout_s=2., status_rate_hz=10.)
        first, second = Mock(), Mock()
        first.close.side_effect = TimeoutError("synthetic writer blocked")
        hub._sessions = {"one": first, "two": second}
        hub._interface_owners = {"can-test": "one"}
        with patch.object(hub._platform, "close") as platform_close:
            with pytest.raises(RuntimeError, match="shutdown incomplete"):
                hub.close()
            first.close.assert_called_once()
            second.close.assert_called_once()
            plc.return_value.close.assert_called_once()
            platform_close.assert_called_once()
            assert hub._sessions == {"one": first}
            assert hub._interface_owners == {"can-test": "one"}
            first.close.side_effect = None
            hub.close()
            assert not hub._sessions and not hub._interface_owners


def test_close_waits_for_finalizer_record_boundary_and_preserves_partial_report(tmp_path, monkeypatch):
    """A finalizer holds its captured playback; close must wait for CSV finish."""
    from types import SimpleNamespace
    service = FactoryService(data_root=tmp_path)
    boundary, csv_closed = threading.Event(), threading.Event()
    report = Mock(return_value="synthetic-stop")
    monkeypatch.setattr("factory_hmi.core.report_jobs.enqueue_report_job", report)
    monkeypatch.setattr(service, "_reap_report_receipts", lambda: None)
    playback = SimpleNamespace(wait=lambda: boundary.wait(2),
        wait_recording=lambda **_: csv_closed.wait(2), snapshot=lambda: {"progress": {"outcome": "stopped"}})
    def disconnect():
        boundary.set()
    service._controller = SimpleNamespace(disconnect=disconnect)
    context = {"test_id": "synthetic-stop"}
    service._start_report_worker(service._finalize_after_run, "factory-report-synthetic",
        (service._controller, tmp_path / "synthetic.csv", context, playback))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.close)
        try:
            assert boundary.wait(1)
            report.assert_not_called()
            assert not future.done()
        finally:
            csv_closed.set()
        assert future.result(timeout=2)["closed"]
    report.assert_called_once()
    assert report.call_args.args[2]["outcome"] == "stopped"
    assert not service._report_threads


def test_outbox_commit_rollback_and_connection_handles_are_closed(tmp_path):
    import sqlite3
    from factory_hmi.sync.outbox import Outbox
    outbox = Outbox(tmp_path / "queue.sqlite3")
    with outbox._connect() as database:
        database.execute("CREATE TABLE synthetic (value INTEGER)")
        database.execute("INSERT INTO synthetic VALUES (1)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with outbox._connect() as database:
            database.execute("INSERT INTO synthetic VALUES (2)")
            raise RuntimeError("synthetic failure")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        database.execute("SELECT 1")
    with outbox._connect() as database:
        assert [r[0] for r in database.execute("SELECT value FROM synthetic")] == [1]


def test_service_close_leaves_no_database_file_handles(tmp_path):
    import os
    from pathlib import Path
    service = FactoryService(data_root=tmp_path)
    service.execution_history_page()
    service.close()
    # Do not force GC: resource ownership must not depend on a later collection.
    open_files = []
    for descriptor in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(descriptor)
            if str(tmp_path) in target:
                open_files.append(target)
        except FileNotFoundError:
            pass
    assert open_files == []


def test_existing_orphan_is_recovered_on_startup_without_changing_raw_csv(tmp_path):
    import json
    records = tmp_path / "records"
    records.mkdir()
    raw = b"time_s,hip_pos_rad,hip_cmd_pos_rad,hip_torque_nm,hip_temp_c\n0,0,0,1,25\n1,0.1,0.2,2,26\n"
    csv = records / "synthetic-orphan.csv"
    csv.write_bytes(raw)
    service = FactoryService(data_root=tmp_path)
    status = service.close()
    assert status["report_errors"] == {}
    execution = tmp_path / "executions/synthetic-orphan"
    manifest = json.loads((execution / "manifest.json").read_text())
    assert manifest["origin"] == "HISTORY"
    assert manifest["timestamp_provenance"] == "missing"
    assert csv.read_bytes() == (execution / "telemetry/raw.csv").read_bytes() == raw
    assert service.outbox.list() == [] # recovery never submits/approves


def test_context_exit_aborts_fake_zeroing_and_disconnects_transport(tmp_path):
    from test_factory_hmi_core import fake_station
    from factory_hmi.core.controller import FactoryController
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.discover_motors(); controller.enable_motors()
    with FactoryService(data_root=tmp_path) as service:
        service._controller = controller
        service.zero_prepare(1)
        zeroing = service._zeroing
        assert zeroing.state.value == "reading"
    assert zeroing.state.value == "aborted"
    assert not backend.connected and not backend.motors_enabled
    assert service._closed and service._zeroing is None
    assert not service._report_threads
