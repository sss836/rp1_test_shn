"""Window activation is local IPC only; these tests never create device services."""
import importlib.util
import os
import socket
import tempfile
import time
from pathlib import Path
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from factory_hmi.desktop.activation import install_activation, restore_window


def test_explicit_relaunch_restores_minimized_window_and_releases_clients():
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    with tempfile.TemporaryDirectory(prefix="rp1-raise-") as directory:
        address = str(Path(directory) / "desktop.sock")
        server = install_activation(window, address)
        window.showMinimized()
        for _ in range(12):
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(address)
                client.sendall(b"raise\n")
            deadline = time.monotonic() + .15
            while time.monotonic() < deadline:
                app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            assert not window.isMinimized()
            assert window.isVisible()
        assert not server.hasPendingConnections()
        server.close()
    window.close()
    window.deleteLater()


def test_restore_moves_offscreen_window_into_available_screen():
    app = QApplication.instance() or QApplication([])
    window = QWidget()
    window.move(-40000, -40000)
    restore_window(window)
    assert any(s.availableGeometry().contains(window.frameGeometry().center()) for s in app.screens())
    window.close()


def load_launcher():
    spec = importlib.util.spec_from_file_location("activation_launcher", Path(__file__).parents[1] / "launch.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


def test_launcher_only_targets_socket_of_verified_instance(tmp_path):
    launcher = load_launcher()
    with mock.patch.object(launcher.socket, "socket") as connect:
        assert not launcher.raise_existing(tmp_path, {})
        assert not launcher.raise_existing(tmp_path, {"pid": 123, "activation_socket": "/tmp/other.sock"})
        connect.assert_not_called()


def test_supervisor_starts_and_tracks_independent_desktops_on_the_same_gateway(tmp_path, monkeypatch):
    launcher = load_launcher()
    children = []

    class DesktopProcess:
        def __init__(self):
            self.pid = 20_000 + len(children)
            self.stdout = object()
            self.returncode = None

        def poll(self):
            return self.returncode

    def start(argv, **options):
        child = DesktopProcess()
        children.append((child, argv, options))
        return child

    def log(_path):
        return mock.Mock(**{"close.return_value": {"close_timed_out": False}})

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    monkeypatch.setattr(launcher, "BoundedProcessLog", log)
    gateway = "http://127.0.0.1:19200"
    group = launcher.DesktopChildren(tmp_path, gateway, {})
    try:
        assert group.spawn()
        os.write(group.write_fd, b"+")
        assert group.spawn_requested()
        assert group.snapshot()["desktop_pids"] == [20_000, 20_001]
        first, second = children
        for _, argv, options in children:
            assert argv[1:] == ["-m", "factory_hmi.desktop", "--gateway-url", gateway]
            assert options["pass_fds"] == (group.write_fd,)
            assert options["env"]["RP1_HMI_NEW_WINDOW_FD"] == str(group.write_fd)
        assert first[2]["env"]["RP1_HMI_ACTIVATION_SOCKET"] != second[2]["env"]["RP1_HMI_ACTIVATION_SOCKET"]
        os.write(group.write_fd, b"+")
        assert not group.spawn_requested()

        first[0].returncode = 0
        assert group.reap() == (True, False)
        state = {"pid": os.getpid(), **group.snapshot()}
        assert state["desktop_pids"] == [20_001]
        assert second[0].poll() is None
        with mock.patch.object(launcher.socket, "socket") as socket_factory:
            assert launcher.raise_existing(tmp_path, state)
            socket_factory.return_value.__enter__.return_value.connect.assert_called_once_with(
                second[2]["env"]["RP1_HMI_ACTIVATION_SOCKET"])
        os.write(group.write_fd, b"+")
        assert group.spawn_requested()
        assert sorted(group.snapshot()["desktop_pids"]) == [20_001, 20_002]
    finally:
        group.stop_requests()
        group.close_logs()
