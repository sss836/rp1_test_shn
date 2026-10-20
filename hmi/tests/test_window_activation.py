"""Window activation is local IPC only; these tests never create device services."""
import importlib.util
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


def test_launcher_only_targets_socket_of_verified_instance(tmp_path):
    spec = importlib.util.spec_from_file_location("activation_launcher", Path(__file__).parents[1] / "launch.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    with mock.patch.object(launcher.socket, "socket") as connect:
        assert not launcher.raise_existing(tmp_path, {})
        assert not launcher.raise_existing(tmp_path, {"pid": 123, "activation_socket": "/tmp/other.sock"})
        connect.assert_not_called()
