"""Two-window status regressions with real Qt, mock PLC and no network sockets."""
from collections import deque
from dataclasses import replace
import json
from threading import Event
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from factory_hmi.client import GatewayClient
from factory_hmi.desktop import app as desktop
from factory_hmi.gateway.app import create_app
from factory_hmi.plc.client import MockPlcClient
from factory_hmi.plc.models import FeedbackState, OperationLogEntry


class SocketStub(QObject):
    disconnected = Signal()

    def close(self):
        self.disconnected.emit()


def network_without_io(window):
    for name in ("reconnect_timer", "snapshot_timer", "feedback_freshness_timer", "lease_timer"):
        setattr(window, name, QTimer(window))
    window.websocket = SocketStub(window)
    window.websocket.disconnected.connect(window._ws_disconnected_slot)


def discard_rest(_window, _label, _operation, _success=None, **kwargs):
    if (finish := kwargs.get("on_finished")) is not None:
        finish()


@pytest.fixture
def desktop_pair(monkeypatch, tmp_path):
    qapp = QApplication.instance() or QApplication([])
    monkeypatch.setenv("RP1_HMI_STATION_CONFIG_DIR", str(tmp_path / "station-configs"))
    monkeypatch.setattr(desktop.FactoryMainWindow, "_setup_network", network_without_io)

    def unexpected_http(*_args, **_kwargs):
        raise AssertionError("desktop regression attempted a socket request")

    monkeypatch.setattr(GatewayClient, "_open", unexpected_http, raising=False)
    monkeypatch.setattr("factory_hmi.client.request.urlopen", unexpected_http)
    with monkeypatch.context() as initial:
        initial.setattr(desktop.FactoryMainWindow, "_run_rest", discard_rest)
        windows = tuple(desktop.FactoryMainWindow("http://127.0.0.1:1") for _ in range(2))
        for window in windows:
            window.sample_timer.stop()
            window.plc_page.stop()
            window.quick_page.stop()
            window.show()
        qapp.processEvents()
    yield qapp, windows
    for window in windows:
        window.reconnect_timer.stop()
        window.hide()
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def synchronous_rest(_label, operation, success, **kwargs):
    try:
        success(operation())
    finally:
        kwargs["on_finished"]()


def synthetic_cabinet(tmp_path, monkeypatch):
    config = tmp_path / "synthetic-plc.yaml"
    config.write_text("""driver: mock
access_mode: control
dut_can_map: {DUT1: can0, DUT2: can1, DUT3: can2, DUT4: can3}
setpoints:
  enabled: true
  voltage_min_v: 1.0
  voltage_max_v: 60.0
  current_min_a: 0.1
  current_max_a: 20.0
""", encoding="utf-8")
    monkeypatch.setenv("RP1_FACTORY_PLC_CONFIG", str(config))
    monkeypatch.delenv("RP1_FACTORY_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("RP1_HMI_UPDATE_MONITOR_ONLY", raising=False)
    gateway = create_app(controller=SimpleNamespace(), data_root=tmp_path / "gateway")
    cabinet = gateway.state.runtime.plc
    assert isinstance(cabinet.client, MockPlcClient)
    assert not gateway.state.runtime._started
    # The fixture advances only the in-memory controller on explicit ticks.
    cabinet.controller.clock = lambda: 500.0
    cabinet.controller.connect(user="fixture")
    cabinet.communication_enabled = True
    cabinet._trend = deque(({
        "timestamp": index / 10, "Voltage": [24.0] * 4,
        "Current": [1.0] * 4, "Power": [24.0] * 4, "AnalogValidMask": 15,
    } for index in range(300)), maxlen=300)
    cabinet.controller.operation_log = [OperationLogEntry(
        timestamp=f"fixture-{index}", user="fixture", field="Setpoints", old_value=23.9,
        new_value=24.0, command_sequence=index, confirmation=FeedbackState.CONFIRMED,
    ) for index in range(200)]
    return gateway, cabinet


def test_hidden_plc_page_keeps_shared_faults_and_confirmations_in_two_windows(desktop_pair, tmp_path, monkeypatch):
    qapp, (left, right) = desktop_pair
    gateway, cabinet = synthetic_cabinet(tmp_path, monkeypatch)
    transport = TestClient(gateway)  # No context manager, so lifespan stays idle.
    snapshots = []

    def in_process(request, **_):
        url = urlsplit(request.full_url)
        assert url.hostname == "127.0.0.1" and url.port == 1
        assert url.path in {"/api/v1/plc/snapshot", "/api/v1/plc/start"}
        path = url.path + ("?" + url.query if url.query else "")
        response = transport.request(request.method, path, content=request.data, headers=dict(request.header_items()))
        assert response.status_code == 200, response.text
        if request.method == "GET":
            snapshots.append((path, response.json()))
        return InProcessReply(response.content)

    monkeypatch.setattr(GatewayClient, "_open", lambda _client, request, **kwargs: in_process(request, **kwargs))
    monkeypatch.setattr("factory_hmi.client.request.urlopen", in_process)
    left.plc_page.runner = right.plc_page.runner = synchronous_rest
    try:
        left._select_main_page(3)
        qapp.processEvents()
        assert left.plc_page.isVisible() and not right.plc_page.isVisible()
        snapshots.clear()
        left.plc_page.refresh()
        right.plc_page.refresh()
        assert [path for path, _ in snapshots] == [
            "/api/v1/plc/snapshot", "/api/v1/plc/snapshot?include_history=false",
        ]
        full, compact = [payload for _, payload in snapshots]
        assert len(full["trend"]) == 300 and len(full["operation_log"]) == 200
        assert "trend" not in compact and "operation_log" not in compact
        for field in ("status", "command", "feedback", "phase", "output_admission"):
            assert full[field] == compact[field]

        left.plc_page.voltage_setpoint.setValue(24.0)
        left.plc_page.current_setpoint.setValue(1.0)
        left.plc_page._start()
        for _ in range(30):
            cabinet.controller.tick()
            if not cabinet.snapshot()["coordination"]["busy"]:
                break
        left.plc_page.refresh()
        right.plc_page.refresh()
        for window in (left, right):
            assert window.plc_page._snapshot["feedback"]["Start"]["state"] == "confirmed"
            assert window.plc_page._snapshot["coordination"]["last_command"]["state"] == "confirmed"
        assert "本窗口" in left.plc_page.coordination_state.text()
        assert "其他窗口" in right.plc_page.coordination_state.text()

        cabinet.client._status = replace(cabinet.client._status, fault_latched=True, fault_code=17)
        cabinet.controller.tick()
        left.plc_page.refresh()
        right.plc_page.refresh()
        assert all("FaultCode=17" in window.global_fault.text() for window in (left, right))

        left._select_main_page(0)
        right._select_main_page(3)
        qapp.processEvents()
        left.plc_page.refresh()
        right.plc_page.refresh()
        assert left.plc_page.logs.rowCount() == right.plc_page.logs.rowCount() == 200
        assert len(right.plc_page.voltage_chart.samples) == 300
        assert all("FaultCode=17" in window.global_fault.text() for window in (left, right))
        assert not gateway.state.runtime._started and cabinet._thread is None
    finally:
        transport.close()
        cabinet.client.disconnect()


class InProcessReply:
    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.content


def idle_status():
    return {"configured": False, "connected": False, "state": "disconnected",
            "station": {"state": "disconnected", "motors_discovered": False}, "motors": []}


def test_two_idle_websockets_track_receipt_independently_and_only_silent_window_falls_back(desktop_pair, monkeypatch):
    _, (left, right) = desktop_pair
    clock = [500.0]
    monkeypatch.setattr(desktop, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    jobs = [[], []]
    for window, queue in zip((left, right), jobs):
        monkeypatch.setattr(window, "_run_rest", lambda *args, queue=queue, **kwargs: queue.append((args, kwargs)))
        window._ws_connected_slot()
    frame = json.dumps({"type": "snapshot", "snapshot": idle_status()})
    for step in range(12):
        clock[0] = 500.0 + step * desktop.STATUS_SILENCE_S / 4
        for window in (left, right):
            window._ws_message(frame)
            window._poll_snapshot()
    assert jobs == [[], []]

    last_good = clock[0]
    clock[0] += desktop.STATUS_SILENCE_S * .75
    left._ws_message(frame)
    right._ws_message('{"type":"snapshot","snapshot":[]}')
    right._ws_message('{"type":"snapshot","snapshot":{}}')
    clock[0] = last_good + desktop.STATUS_SILENCE_S + .01
    left._poll_snapshot()
    right._poll_snapshot()
    right._poll_snapshot()
    assert not jobs[0] and len(jobs[1]) == 1
    assert right._snapshot_pending and "静默" in right.connection_badge.text()

    right._ws_message(frame)
    jobs[1][0][1]["on_finished"]()
    right._poll_snapshot()
    assert not right._snapshot_pending and len(jobs[1]) == 1
    assert "已连接" in right.connection_badge.text()


def test_close_discards_late_status_and_plc_replies_without_restarting_timers(desktop_pair, monkeypatch):
    qapp, (closing, other) = desktop_pair
    closing.pool = QThreadPool(closing)
    began, release = Event(), Event()
    calls = []

    def delayed_snapshot():
        calls.append("status")
        began.set()
        assert release.wait(5)
        return {**idle_status(), "fault": "late-reply-marker"}

    monkeypatch.setattr(closing.client, "snapshot", delayed_snapshot)
    closing._render_snapshot(idle_status())
    plc_jobs = []
    closing.plc_page.runner = lambda *args, **kwargs: plc_jobs.append((args, kwargs))
    closing.plc_page.refresh()
    assert closing.plc_page._pending and len(plc_jobs) == 1
    try:
        closing._poll_snapshot()
        assert began.wait(2)
        closing._poll_snapshot()
        assert closing._snapshot_pending and calls == ["status"] and len(closing._workers) == 1
        worker = next(iter(closing._workers))
        closing.close()
        assert not closing.reconnect_timer.isActive()
        assert not closing.snapshot_timer.isActive() and not closing.plc_page.timer.isActive()

        plc_jobs[0][0][2]({"phase": "late-plc-reply"})
        plc_jobs[0][1]["on_finished"]()
        assert not closing.plc_page._pending and closing.plc_page._snapshot.get("phase") != "late-plc-reply"
        closing.plc_page.refresh()
        assert len(plc_jobs) == 1

        release.set()
        assert closing.pool.waitForDone(2000)
        deadline = time.monotonic() + 2
        while (closing._workers or closing._snapshot_pending or isValid(worker.signals)) and time.monotonic() < deadline:
            qapp.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not closing._workers and not closing._snapshot_pending and not isValid(worker.signals)
        assert "late-reply-marker" not in closing.global_fault.text()
        closing._poll_snapshot()
        assert calls == ["status"]

        other._ws_connected_slot()
        other._ws_message(json.dumps({"type": "snapshot", "snapshot": {**idle_status(), "fault": "other-window"}}))
        assert "other-window" in other.global_fault.text()
    finally:
        release.set()
        closing.pool.waitForDone(2000)
        qapp.processEvents()
