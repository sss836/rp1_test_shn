"""Restore the main window on explicit local relaunch, without device commands."""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import QApplication, QWidget


def restore_window(window: QWidget) -> None:
    if window.isMinimized():
        window.showNormal()
    else:
        window.show()
    frame = window.frameGeometry()
    screens = QApplication.screens()
    if screens and not any(screen.availableGeometry().contains(frame.center()) for screen in screens):
        frame.moveCenter(screens[0].availableGeometry().center())
        window.move(frame.topLeft())
    window.raise_()
    window.activateWindow()


def install_activation(window: QWidget, address: str) -> QLocalServer:
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(address):
        raise RuntimeError(f"窗口恢复入口无法创建：{server.errorString()}")

    def connected() -> None:
        while server.hasPendingConnections():
            client = server.nextPendingConnection()
            client.setParent(server)
            client.setReadBufferSize(32)
            buffer = bytearray()

            def read(client=client, buffer=buffer) -> None:
                buffer.extend(bytes(client.readAll()))
                if bytes(buffer) == b"raise\n":
                    restore_window(window)
                    client.disconnectFromServer()
                elif len(buffer) >= 16 or b"\n" in buffer:
                    client.disconnectFromServer()

            client.readyRead.connect(read)
            client.disconnected.connect(client.deleteLater)
            QTimer.singleShot(1500, client, client.disconnectFromServer)
            read()

    server.newConnection.connect(connected)
    return server
