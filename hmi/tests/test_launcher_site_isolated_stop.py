"""Launcher-side regressions for the explicit site-isolation stop."""
import importlib.util
import io
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import select
import socket
import sys
from unittest.mock import Mock

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[1] / "launch.py"
    spec = importlib.util.spec_from_file_location("isolated_stop_launcher", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_launcher_is_not_signalled(launcher, tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "running", lambda _state: {"pid": 123, "start": "456"})
    monkeypatch.setattr(sys, "argv", ["rp1-test-hmi", "stop", "--site-isolated", "--state-dir", str(tmp_path)])
    send_signal = Mock()
    monkeypatch.setattr(launcher.os, "kill", send_signal)
    with pytest.raises(RuntimeError, match="没有现场隔离退出端点"):
        launcher.main()
    send_signal.assert_not_called()


def test_socket_checks_live_process_identity_and_same_user(launcher, tmp_path, monkeypatch):
    (tmp_path / "run").mkdir()
    pid = os.getpid()
    start = launcher.process_start(pid)
    data = {"pid": pid, "start": start, "site_isolated_stop": True}
    control = launcher.SiteIsolationControl(tmp_path, pid, start)
    try:
        assert control.address.stat().st_mode & 0o777 == 0o600
        with pytest.raises(RuntimeError, match="无法验证.*身份"):
            launcher.request_site_isolated_stop(tmp_path, dict(data, start="0"))
        assert control.receive() is False

        wrong_process = launcher.SiteIsolationControl(tmp_path, pid + 1, start)
        try:
            with pytest.raises(RuntimeError, match="无法验证.*身份"):
                launcher.request_site_isolated_stop(tmp_path, dict(data, pid=pid + 1))
            assert wrong_process.receive() is False
        finally:
            wrong_process.close()

        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(1)
            client.connect(str(control.address))
            client.sendall(launcher.site_stop_message(pid, "0"))
            assert control.receive() is False
            assert client.recv(128) == b""

        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
            client.settimeout(1)
            client.connect(str(control.address))
            client.sendall(launcher.site_stop_message(pid, start))
            with monkeypatch.context() as patch:
                patch.setattr(launcher, "socket_peer", lambda _connection: (pid, os.geteuid() + 1))
                assert control.receive() is False
            with pytest.raises(ConnectionResetError):
                client.recv(128)

        with ThreadPoolExecutor(max_workers=1) as executor:
            request = executor.submit(launcher.request_site_isolated_stop, tmp_path, data)
            assert select.select([control.listener], [], [], 1)[0]
            assert control.receive() is True
            request.result(timeout=1)
        assert control.receive() is False
    finally:
        control.close()
    assert not control.address.exists()


def test_isolation_header_only_sent_on_post_and_requires_gateway_ack(launcher, monkeypatch):
    gateway = Mock()
    gateway.poll.return_value = None
    requests = []
    replies = iter([
        {"state": "preparing", "site_isolated": True},
        {"state": "ready", "ready": True, "site_isolated": True},
        {"state": "ready", "ready": True},
    ])

    def open_gateway(request, timeout):
        requests.append((request.get_method(), {key.lower(): value for key, value in request.header_items()}))
        return io.StringIO(json.dumps(next(replies)))

    monkeypatch.setattr(launcher, "open_gateway", open_gateway)
    assert launcher.prepare_gateway(gateway, "http://127.0.0.1:1", "token", True)["ready"]
    assert launcher.prepare_gateway(gateway, "http://127.0.0.1:1", "token", True)["state"] == "blocked"
    assert requests == [
        ("POST", {"x-rp1-shutdown-token": "token", "x-rp1-site-isolated": "1"}),
        ("GET", {"x-rp1-shutdown-token": "token"}),
        ("POST", {"x-rp1-shutdown-token": "token", "x-rp1-site-isolated": "1"}),
    ]


def test_blocked_site_isolated_attempt_does_not_authorize_ordinary_retry(launcher, tmp_path, monkeypatch):
    gateway = Mock()
    gateway.poll.return_value = None
    state = tmp_path / "launcher.json"
    state.write_text("{}")
    prepares = Mock(side_effect=[
        {"state": "blocked", "ready": False, "detail": "Station worker still active"},
        {"state": "ready", "ready": True},
    ])
    monkeypatch.setattr(launcher, "prepare_gateway", prepares)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: launcher.request_stop())
    gateway.wait.side_effect = lambda **_kw: setattr(gateway.poll, "return_value", 0)
    launcher.supervised_shutdown(gateway, None, "http://127.0.0.1:1", "token", state,
                                 site_isolated=True)
    assert [call.args[3] for call in prepares.call_args_list] == [True, False]
    gateway.terminate.assert_called_once()
    gateway.kill.assert_not_called()


def test_site_isolated_request_can_retry_a_blocked_ordinary_stop(launcher, tmp_path, monkeypatch):
    gateway = Mock()
    gateway.poll.return_value = None
    state = tmp_path / "launcher.json"
    state.write_text("{}")
    prepares = Mock(side_effect=[
        {"state": "blocked", "ready": False, "detail": "PLC feedback unavailable"},
        {"state": "ready", "ready": True, "site_isolated": True},
    ])
    control = Mock()
    control.receive.return_value = True
    monkeypatch.setattr(launcher, "prepare_gateway", prepares)
    gateway.wait.side_effect = lambda **_kw: setattr(gateway.poll, "return_value", 0)
    launcher.supervised_shutdown(gateway, None, "http://127.0.0.1:1", "token", state,
                                 isolation_control=control)
    assert [call.args[3] for call in prepares.call_args_list] == [False, True]
    control.receive.assert_called_once()
    gateway.terminate.assert_called_once()
