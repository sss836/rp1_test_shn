"""Offline regressions for launcher/API/process shutdown admission."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import Mock
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.lifecycle import LIFECYCLE_PATH, ShutdownCoordinator

HMI = Path(__file__).resolve().parents[1]


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("condition did not become true")


def launcher():
    spec = importlib.util.spec_from_file_location("handshake_launcher", HMI / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Controller:
    def __init__(self):
        self.closes = 0
        self.fail_close = False

    def snapshot(self):
        return {"state": "disconnected"}

    def disconnect(self):
        pass

    def close(self):
        self.closes += 1
        if self.fail_close:
            raise RuntimeError("synthetic record worker still pending")


def test_prepare_drains_existing_writes_and_serializes_repeated_requests():
    calls = []
    gate = ShutdownCoordinator(lambda: calls.append("closed"))
    assert gate.enter("/api/v1/configure")
    assert gate.request()["state"] == "preparing"
    for _ in range(5):
        assert gate.request()["attempt"] == 1
    assert not gate.enter("/api/v1/plc/start")
    assert not calls
    gate.leave()
    eventually(lambda: gate.snapshot()["ready"])
    assert calls == ["closed"]
    assert gate.request()["attempt"] == 1
    assert not gate.enter("/api/v1/plc/all-stop")


def test_drain_timeout_does_not_close_an_active_request_and_retry_is_explicit():
    close = Mock()
    gate = ShutdownCoordinator(close, drain_timeout_s=.01)
    assert gate.enter("/write")
    gate.request()
    eventually(lambda: gate.snapshot()["state"] == "blocked")
    close.assert_not_called()
    assert not gate.enter("/api/v1/plc/start")
    assert gate.enter("/api/v1/plc/all-stop")
    gate.leave()
    gate.leave()
    assert gate.snapshot()["state"] == "blocked"
    gate.request()
    eventually(lambda: gate.snapshot()["ready"])
    close.assert_called_once()


def test_failed_prepare_keeps_resources_reachable_and_allows_retry(tmp_path):
    controller = Controller()
    app = create_app(controller, data_root=tmp_path)
    hub = app.state.runtime
    hub.start()
    controller.fail_close = True
    try:
        with pytest.raises(RuntimeError, match="record worker"):
            hub.close()
        assert hub.session("same", create=False).controller is controller
        assert hub.plc.shutdown_status["state"] == "not_requested"
        controller.fail_close = False
        hub.close()
        hub.close()
        assert controller.closes == 2
    finally:
        controller.fail_close = False
        hub.close()


def test_api_auth_gate_and_unverified_external_power_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_SHUTDOWN_TOKEN", "isolated-test-token")
    app = create_app(Controller(), data_root=tmp_path)
    hub = app.state.runtime
    # This fixture deliberately has no connected PLC; shutdown is resource
    # completion only, never evidence of external electrical power OFF.
    monkeypatch.setattr(hub.plc, "start", lambda: None)
    headers = {"X-RP1-Shutdown-Token": "isolated-test-token"}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.post(LIFECYCLE_PATH).status_code == 403
        assert hub.shutdown.snapshot()["state"] == "running"
        assert client.post(LIFECYCLE_PATH, headers=headers).status_code in {200, 202}
        eventually(lambda: client.get(LIFECYCLE_PATH, headers=headers).json()["ready"])
        status = client.get(LIFECYCLE_PATH, headers=headers).json()
        assert status["plc"]["state"] == "unverified"
        assert "not proof" in status["detail"]
        assert client.post("/api/v1/plc/start", json={}).status_code == 409
        assert client.post("/api/v1/configure", json={}).status_code == 409
        assert client.get("/api/v1/health").json()["shutdown"]["ready"] is True
        assert client.get("/api/v1/snapshot", headers={"X-RP1-Client-ID": "new"}).status_code == 200
        assert hub.shutdown.snapshot()["attempt"] == 1


def test_api_rejects_non_loopback_prepare(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_SHUTDOWN_TOKEN", "isolated-test-token")
    with TestClient(create_app(Controller(), data_root=tmp_path), client=("198.51.100.1", 50000)) as client:
        reply = client.post(LIFECYCLE_PATH, headers={"X-RP1-Shutdown-Token": "isolated-test-token"})
        assert reply.status_code == 403


def test_finish_never_escalates_to_kill_even_old_allow_kill_callers():
    module = launcher()
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = subprocess.TimeoutExpired("inert", 8)
    assert module.finish(process, allow_kill=True) is False
    process.terminate.assert_called_once()
    process.kill.assert_not_called()


def test_handshake_network_error_never_signals_gateway(monkeypatch):
    module = launcher()
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(module, "open_gateway", Mock(side_effect=URLError("injected unavailable")))
    assert module.prepare_gateway(process, "http://127.0.0.1:1", "test")["state"] == "blocked"
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def test_supervisor_waits_for_explicit_retry_and_keeps_lock_identity(tmp_path, monkeypatch):
    module = launcher()
    state = tmp_path / "launcher.json"
    state.write_text(json.dumps({"pid": 123, "start": "test"}))
    gateway, desktop = Mock(), Mock()
    gateway.poll.return_value = None
    desktop.poll.return_value = None
    calls = []
    results = iter([{"state": "blocked", "ready": False, "detail": "injected failure"},
                    {"state": "ready", "ready": True}])
    def prepare(*args):
        calls.append("prepare")
        return next(results)
    def sleep(_duration):
        gateway.terminate.assert_not_called()
        desktop.terminate.assert_not_called()
        assert json.loads(state.read_text())["shutdown"]["state"] == "blocked"
        module.request_stop()
    def finish(process, **kwargs):
        calls.append("finish")
        process.poll.return_value = 0
        return True
    monkeypatch.setattr(module, "prepare_gateway", prepare)
    monkeypatch.setattr(module.time, "sleep", sleep)
    monkeypatch.setattr(module, "finish", finish)
    module.supervised_shutdown(gateway, desktop, "http://127.0.0.1:1", "test", state)
    assert calls == ["prepare", "prepare", "finish", "finish"]
    assert json.loads(state.read_text())["pid"] == 123


def test_direct_gateway_signals_cannot_exit_after_failed_prepare(tmp_path):
    """Actual inert subprocess, real Uvicorn: failure must keep HTTP alive."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    marker = tmp_path / "allow-close"
    script = tmp_path / "inert_gateway.py"
    script.write_text('''from pathlib import Path
import sys
sys.path.extend(sys.argv[4:])
import uvicorn
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.__main__ import PreparedShutdownServer
class Controller:
    def snapshot(self): return {"state": "disconnected"}
    def disconnect(self): pass
app = create_app(Controller(), data_root=Path(sys.argv[1]))
hub = app.state.runtime
original = hub.close
def close():
    if not Path(sys.argv[2]).exists(): raise RuntimeError("injected shutdown failure")
    original()
hub.shutdown._close = close
PreparedShutdownServer(uvicorn.Config(app, host="127.0.0.1", port=int(sys.argv[3]), log_level="error"), hub.shutdown).run()
''')
    output = (tmp_path / "gateway.log").open("w+")
    process = subprocess.Popen([sys.executable, str(script), str(tmp_path / "data"), str(marker), str(port), str(HMI), str(HMI / ".local-runtime")],
                               env=dict(os.environ), stdout=output, stderr=subprocess.STDOUT)
    def health():
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=.4) as response:
                return json.load(response)
        except OSError:
            return {}
    try:
        eventually(lambda: health().get("ok"), timeout=10)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGINT):
            process.send_signal(sig)
            eventually(lambda: health().get("shutdown", {}).get("state") == "blocked")
            assert process.poll() is None
            time.sleep(.15)
        # The previous launcher escalated after eight seconds. Stay alive
        # past that deadline, not merely until the first health response.
        time.sleep(8.2)
        assert process.poll() is None
        assert health()["shutdown"]["state"] == "blocked"
        marker.write_text("allow isolated resource close")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            # Only this inert, test-owned subprocess, never a workstation HMI.
            process.kill()
            process.wait(timeout=3)
        output.close()


def test_prepared_gateway_cannot_create_a_new_station(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_SHUTDOWN_TOKEN", "isolated-test-token")
    app = create_app(data_root=tmp_path)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/v1/snapshot", headers={"X-RP1-Client-ID": "first"}).status_code == 200
        app.state.runtime.shutdown.request()
        eventually(lambda: app.state.runtime.shutdown.snapshot()["ready"])
        assert client.get("/api/v1/snapshot", headers={"X-RP1-Client-ID": "new"}).status_code == 409
        assert set(app.state.runtime._sessions) == {"first"}


def test_actual_offline_launcher_completes_private_handshake(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, http_proxy="http://127.0.0.1:1", no_proxy="", NO_PROXY="")
    process = subprocess.Popen([
        sys.executable, str(HMI / "launch.py"), "--offline-check", "--gateway-only",
        "--duration", ".3", "--port", str(port), "--state-dir", str(tmp_path),
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
    try:
        output, error = process.communicate(timeout=30)
        assert process.returncode == 0, (output, error)
        assert "HMI ready: PLC mock" in output
        assert not (tmp_path / "offline-check/run/launcher.json").exists()
        log = (tmp_path / "offline-check/logs/gateway.log").read_text()
        assert "Application shutdown complete" in log
        assert "Application shutdown failed" not in log
    finally:
        if process.poll() is None:
            # This isolated group was created above with mandatory mock-only
            # configuration and disabled CAN. Never target workstation PIDs.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)


def test_asgi_gate_drains_accepted_control_before_close(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_SHUTDOWN_TOKEN", "isolated-test-token")
    app = create_app(Controller(), data_root=tmp_path)
    hub = app.state.runtime
    admitted, release = threading.Event(), threading.Event()
    original = hub.shutdown._close
    closed = threading.Event()
    def close():
        closed.set()
        original()
    hub.shutdown._close = close
    @app.post("/isolated-blocking-command")
    def command():
        admitted.set()
        assert release.wait(3)
        return {"ok": True}
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        replies = []
        worker = threading.Thread(target=lambda: replies.append(client.post("/isolated-blocking-command")))
        worker.start()
        try:
            assert admitted.wait(2)
            assert client.post(LIFECYCLE_PATH, headers={"X-RP1-Shutdown-Token": "isolated-test-token"}).status_code == 202
            assert client.post("/isolated-blocking-command").status_code == 409
            assert not closed.is_set()
            assert hub.shutdown.snapshot()["active_requests"] == 1
        finally:
            release.set()
            worker.join(3)
        assert replies[0].status_code == 200
        eventually(lambda: hub.shutdown.snapshot()["ready"])


def test_plc_retry_does_not_reclose_completed_station(tmp_path, monkeypatch):
    controller = Controller()
    app = create_app(controller, data_root=tmp_path)
    hub = app.state.runtime
    hub.start()
    original = hub.plc.close
    calls = []
    def flaky_close():
        calls.append("close")
        if len(calls) == 1:
            raise RuntimeError("synthetic PLC stop unconfirmed")
        original()
    monkeypatch.setattr(hub.plc, "close", flaky_close)
    try:
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["state"] == "blocked")
        assert controller.closes == 1
        assert hub.session("shared", create=False).controller is controller
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["ready"])
        assert controller.closes == 1
        assert calls == ["close", "close"]
    finally:
        hub.close()


@pytest.mark.parametrize("path", [
    "/api/v1/motors/disable", "/api/v1/playback/disable",
    "/api/v1/manual/stop", "/api/v1/disconnect", "/api/v1/quick/abort",
])
@pytest.mark.parametrize("state", ["preparing", "blocked"])
def test_shutdown_keeps_authenticated_existing_safety_controls(tmp_path, monkeypatch, path, state):
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "operator-test-token")
    controller = Controller()
    safety = Mock(return_value={})
    controller.disable_motors = safety
    controller.disable = safety
    controller.manual_stop = safety
    controller.disconnect = safety
    controller.request_motion_stop = Mock()
    app = create_app(controller, data_root=tmp_path)
    hub = app.state.runtime
    with TestClient(app) as client:
        runtime = hub.session("owner")
        runtime.lease.claim("owner")
        if path == "/api/v1/quick/abort":
            runtime.quick = Mock()
            runtime.quick.abort = safety
            runtime.quick.snapshot.return_value = {}
        if state == "preparing":
            assert hub.shutdown.enter("/already-accepted-write")
        else:
            hub.shutdown._close = Mock(side_effect=RuntimeError("injected close failure"))
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["state"] == state)
        # A close failure's quarantine must not disable the old stop button.
        runtime._cleanup_error = "injected incomplete resource close"
        headers = {"X-RP1-Operator-Token": "operator-test-token", "X-RP1-Client-ID": "owner"}
        try:
            assert client.post(path, headers={"X-RP1-Client-ID": "owner"}).status_code == 401
            assert client.post(path, headers=dict(headers, **{"X-RP1-Client-ID": "other"})).status_code == 409
            controller.request_motion_stop.assert_not_called()
            safety.assert_not_called()
            reply = client.post(path, headers=headers)
            assert reply.status_code == 200, reply.text
            safety.assert_called_once()
            controller.request_motion_stop.assert_called_once()
            for forbidden in ("/api/v1/arm", "/api/v1/playback/start", "/api/v1/motors/enable", "/api/v1/configure"):
                assert client.post(forbidden, headers=headers, json={}).status_code == 409
        finally:
            runtime._cleanup_error = None
            if state == "preparing":
                hub.shutdown.leave()
                eventually(lambda: hub.shutdown.snapshot()["state"] != "preparing")


def test_ready_waits_for_safety_admitted_during_close():
    entered, release = threading.Event(), threading.Event()
    def close():
        entered.set()
        assert release.wait(2)
    gate = ShutdownCoordinator(close)
    gate.request()
    assert entered.wait(1)
    assert gate.enter("/api/v1/motors/disable")
    release.set()
    time.sleep(.03)
    assert gate.snapshot()["state"] == "preparing"
    assert gate.snapshot()["active_requests"] == 1
    gate.leave()
    eventually(lambda: gate.snapshot()["ready"])
    assert not gate.enter("/api/v1/motors/disable")


def test_motor_safety_preempts_then_serializes_with_close(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "operator-test-token")
    entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()
    controller = Controller()
    controller.request_motion_stop = cancelled.set
    calls = []
    def close():
        entered.set()
        assert release.wait(3)
        calls.append("close")
    def disable():
        calls.append("disable")
    controller.close = close
    controller.disable_motors = disable
    app = create_app(controller, data_root=tmp_path)
    hub = app.state.runtime
    with TestClient(app) as client:
        hub.session("owner").lease.claim("owner")
        hub.shutdown.request()
        assert entered.wait(2)
        replies = []
        worker = threading.Thread(target=lambda: replies.append(client.post(
            "/api/v1/motors/disable", headers={"X-RP1-Operator-Token": "operator-test-token", "X-RP1-Client-ID": "owner"})))
        worker.start()
        try:
            assert cancelled.wait(1), "cancel must not wait behind close's lease guard"
            assert calls == []
            assert not hub.shutdown.snapshot()["ready"]
        finally:
            release.set()
            worker.join(3)
        assert replies[0].status_code == 200, replies[0].text
        assert calls == ["close", "disable"]
        eventually(lambda: hub.shutdown.snapshot()["ready"])


def test_authenticated_plc_all_stop_interrupts_long_prepare(tmp_path, monkeypatch):
    import uuid
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "operator-test-token")
    app = create_app(Controller(), data_root=tmp_path)
    hub = app.state.runtime
    with TestClient(app) as client:
        hub.plc.connect(user="explicit-offline-test")
        context = hub.plc.snapshot()["coordination"]
        entered = threading.Event()
        with monkeypatch.context() as patcher:
            patcher.setattr(hub.plc.controller, "tick", lambda *args: entered.set())
            patcher.setattr(hub.plc.controller, "request_stop", lambda **kwargs: True)
            patcher.setattr(hub.plc, "_shutdown_feedback_confirmed", lambda: False)
            hub.shutdown.request()
            eventually(lambda: hub.plc.shutdown_status["state"] == "stopping")
            assert entered.wait(1)
            body = {"user": "offline", "request_id": str(uuid.uuid4()),
                    "expected_epoch": context["epoch"], "expected_revision": context["revision"]}
            headers = {"X-RP1-Client-ID": "owner", "X-RP1-Operator-Token": "operator-test-token"}
            assert client.post("/api/v1/plc/all-stop", json=body,
                               headers={"X-RP1-Client-ID": "owner"}).status_code == 401
            assert not hub.plc._shutdown_interrupt.is_set()
            started = time.monotonic()
            reply = client.post("/api/v1/plc/all-stop", json=body, headers=headers)
            assert reply.status_code == 200, reply.text
            assert time.monotonic() - started < 1
            eventually(lambda: hub.shutdown.snapshot()["state"] == "blocked")
            assert "interrupted by safety command" in hub.shutdown.snapshot()["detail"]
        eventually(lambda: hub.plc.controller._pending is None)
        # AllStop intentionally latches the software FAULT phase. An explicit
        # reset is required before normal exit can verify OFF.
        context = hub.plc.snapshot()["coordination"]
        body.update(request_id=str(uuid.uuid4()), expected_epoch=context["epoch"], expected_revision=context["revision"])
        reset = client.post("/api/v1/plc/reset-fault", json=body, headers=headers)
        assert reset.status_code == 200, reset.text
        eventually(lambda: hub.plc.snapshot()["phase"] == "OFF")
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["ready"])


def test_blocked_plc_fault_can_be_explicitly_reset_before_retry(tmp_path, monkeypatch):
    import uuid
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "operator-test-token")
    app = create_app(Controller(), data_root=tmp_path)
    hub = app.state.runtime
    with TestClient(app) as client:
        hub.plc.connect(user="explicit-offline-test")
        hub.plc.client.set_status(fault_latched=True, fault_code=7)
        eventually(lambda: hub.plc.snapshot()["phase"] == "FAULT")
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["state"] == "blocked")
        eventually(lambda: hub.plc.controller._pending is None)
        context = hub.plc.snapshot()["coordination"]
        body = {"user": "offline", "request_id": str(uuid.uuid4()),
                "expected_epoch": context["epoch"], "expected_revision": context["revision"]}
        headers = {"X-RP1-Client-ID": "owner", "X-RP1-Operator-Token": "operator-test-token"}
        reply = client.post("/api/v1/plc/reset-fault", json=body, headers=headers)
        assert reply.status_code == 200, reply.text
        eventually(lambda: hub.plc.snapshot()["phase"] == "OFF")
        assert client.post("/api/v1/plc/start", json={}, headers=headers).status_code == 409
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["ready"])


def test_plc_safety_intent_before_close_cannot_miss_preemption(tmp_path, monkeypatch):
    """Deterministic race: initial drain passed, PLC close not entered yet."""
    import uuid
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "operator-test-token")
    app = create_app(Controller(), data_root=tmp_path)
    hub = app.state.runtime
    close_waiting, allow_close = threading.Event(), threading.Event()
    safety_waiting, allow_safety = threading.Event(), threading.Event()
    original_close = hub.close
    def delayed_close():
        close_waiting.set()
        assert allow_close.wait(3)
        original_close()
    hub.shutdown._close = delayed_close
    with TestClient(app) as client:
        hub.plc.connect(user="explicit-offline-test")
        context = hub.plc.snapshot()["coordination"]
        body = {"user": "offline", "request_id": str(uuid.uuid4()),
                "expected_epoch": context["epoch"], "expected_revision": context["revision"]}
        headers = {"X-RP1-Client-ID": "owner", "X-RP1-Operator-Token": "operator-test-token"}
        original_execute = hub.plc.coordination.execute
        def delayed_execute(*args, **kwargs):
            safety_waiting.set()
            assert allow_safety.wait(3)
            return original_execute(*args, **kwargs)
        with monkeypatch.context() as patcher:
            patcher.setattr(hub.plc.coordination, "execute", delayed_execute)
            hub.shutdown.request()
            assert close_waiting.wait(1)
            replies = []
            worker = threading.Thread(target=lambda: replies.append(client.post(
                "/api/v1/plc/all-stop", json=body, headers=headers)))
            worker.start()
            try:
                assert safety_waiting.wait(1)
                allow_close.set()
                eventually(lambda: hub.shutdown.snapshot()["state"] == "blocked", timeout=2)
                assert "safety request pending" in hub.shutdown.snapshot()["detail"].lower()
                assert hub.plc.client.connected
                assert hub.plc._thread.is_alive()
            finally:
                allow_safety.set()
                allow_close.set()
                worker.join(3)
            assert replies[0].status_code == 200, replies[0].text
        eventually(lambda: hub.plc.controller._pending is None)
        context = hub.plc.snapshot()["coordination"]
        body.update(request_id=str(uuid.uuid4()), expected_epoch=context["epoch"], expected_revision=context["revision"])
        reset = client.post("/api/v1/plc/reset-fault", json=body, headers=headers)
        assert reset.status_code == 200, reset.text
        eventually(lambda: hub.plc.snapshot()["phase"] == "OFF")
        hub.shutdown.request()
        eventually(lambda: hub.shutdown.snapshot()["ready"])
