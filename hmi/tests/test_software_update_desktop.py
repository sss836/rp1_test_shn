"""Offscreen GUI checks. All gateway calls are captured and never executed."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from unittest.mock import Mock, patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox, QPushButton
from factory_hmi.desktop.app import FactoryMainWindow
from factory_hmi.desktop.update_commands import package_install_command


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    def no_network(window):
        window.reconnect_timer = QTimer(window)
        window.snapshot_timer = QTimer(window)
        window.lease_timer = QTimer(window)
        window.websocket = QWebSocket()
    with patch.object(FactoryMainWindow, "_setup_network", no_network), patch.object(FactoryMainWindow, "_run_rest"):
        window = FactoryMainWindow("http://invalid.test")
        for timer in window.findChildren(QTimer):
            timer.stop()
        yield window
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_login_only_queues_check_and_optional_failure_is_quiet(window):
    window._run_rest.reset_mock()
    with patch.object(window.client, "software_update_download") as download, patch("factory_hmi.desktop.app.QMessageBox") as dialog:
        window._platform_login_ok({"result": {"operator": "viewer"}})
        calls = [call for call in window._run_rest.call_args_list if call.args[0] == "检查软件更新"]
        assert len(calls) == 1
        call = calls[0]
        assert call.args[1] == window.client.software_update_check
        assert call.kwargs["quiet"] is True
        call.args[2]({"ok": False, "result": {"available": False, "reason": "未部署可信公钥"}})
        dialog.assert_not_called()
        download.assert_not_called()
        call.kwargs["on_finished"]()
        assert window.software_update_button.isEnabled()


def test_logout_invalidates_pending_check_and_cannot_open_download_prompt(window):
    window._platform_operator = "executor"
    window._check_software_update(manual=False)
    call = window._run_rest.call_args
    window._platform_logout()
    with patch("factory_hmi.desktop.app.QMessageBox") as dialog:
        call.args[2]({"result": {"available": True, "can_update": True,
                                 "release": {"manifest": {"release_id": "a" * 32, "version": "99"}}}})
        dialog.assert_not_called()


def test_viewer_can_read_release_notes_but_has_no_download_action(window):
    window._platform_operator = "viewer"
    window._check_software_update(manual=False)
    call = window._run_rest.call_args
    with patch("factory_hmi.desktop.app.QMessageBox") as dialog, patch.object(window, "_download_software_update") as download:
        call.args[2]({"result": {"available": True, "can_update": False,
                                 "release": {"manifest": {"version": "2.0.14", "notes": "release notes"}}}})
        assert dialog.return_value.addButton.call_count == 1
        assert dialog.return_value.addButton.call_args.args[0] == "稍后"
        download.assert_not_called()


def test_returning_phase_is_visible_without_changing_runtime_or_enabling_hardware(window):
    snapshot = {"playback": {"completion": {"phase": "returning", "active": True,
                                               "return_seconds": 1.2, "evidence_path": "/local/evidence.json"},
                              "progress": {}, "recording": {}}}
    window._render_aging_status(snapshot)
    assert "慢回安全停放位" in window.completion_label.text()
    assert window.completion_label.toolTip() == "/local/evidence.json"


def test_download_receipt_shows_path_and_copy_keeps_dialog_open(window, tmp_path):
    package = tmp_path / "下载 包" / "rp1's-test.deb"
    package.parent.mkdir()
    package.write_bytes(b"test receipt only")
    release_id = "a" * 32
    observed = []

    def inspect_dialog():
        dialog = window.findChild(QDialog, "softwareDownloadDialog")
        path = dialog.findChild(QLineEdit, "softwareDownloadPath")
        observed.append((path.text(), path.isReadOnly()))
        button = dialog.findChild(QPushButton, "copySoftwareInstallCommand")
        assert button.text() == "复制安装命令"
        assert dialog.findChild(QPushButton, "copySoftwareDownloadCommand") is None
        button.click()
        observed.append(dialog.isVisible())
        dialog.findChild(QPushButton, "openSoftwareDownloadDirectory").click()
        dialog.reject()

    window._run_rest.reset_mock()
    QTimer.singleShot(0, inspect_dialog)
    with patch("factory_hmi.desktop.app.QDesktopServices.openUrl", return_value=True) as opened:
        window._show_software_download({"status": "staged", "package_path": str(package)}, release_id)
        assert opened.call_args.args[0].toLocalFile() == str(package.parent)
    assert observed == [(str(package), True), True]
    assert QApplication.clipboard().text() == package_install_command(str(package))
    # The normal queued PLC status refresh may run during the modal event loop;
    # copying/opening must not issue a software download or installation call.
    assert not any("下载" in call.args[0] or "安装" in call.args[0]
                   for call in window._run_rest.call_args_list)


def test_available_update_offers_download_then_waits_for_receipt(window):
    window._platform_operator = "executor"
    window._check_software_update(manual=False)
    call = window._run_rest.call_args
    observed = []

    def click_download():
        dialog = next(item for item in window.findChildren(QMessageBox) if item.isVisible())
        observed.extend(button.text() for button in dialog.buttons())
        next(button for button in dialog.buttons() if button.text() == "下载更新").click()

    QTimer.singleShot(0, click_download)
    with patch.object(window, "_download_software_update") as download, patch.object(window, "_show_software_download") as staged:
        call.args[2]({"result": {"available": True, "can_update": True,
                                 "release": {"manifest": {"release_id": "b" * 32, "version": "99"}}}})
        download.assert_called_once_with("b" * 32)
        staged.assert_not_called()
    assert set(observed) == {"下载更新", "稍后"}


def test_download_failure_or_invalid_receipt_cannot_copy_install_command(window):
    QApplication.clipboard().setText("keep previous clipboard")
    with patch("factory_hmi.desktop.app.QMessageBox.warning") as warning:
        for receipt in ({"status": "failed", "package_path": "/tmp/test.deb"},
                        {"status": "staged", "package_path": "relative.deb"},
                        {"status": "staged", "package_path": ""}):
            window._show_software_download(receipt, "c" * 32)
        assert warning.call_count == 3
    assert QApplication.clipboard().text() == "keep previous clipboard"
    assert window.findChild(QDialog, "softwareDownloadDialog") is None


@pytest.mark.parametrize(("authentication", "expected"), [
    ("authenticated_https_platform", "平台已验证发布签名；下载文件SHA256和DEB信息已校验，尚未安装。"),
    (None, "安装包签名和 SHA256 已校验，尚未安装。"),
])
def test_download_receipt_describes_actual_authentication(window, tmp_path, authentication, expected):
    package = tmp_path / "rp1-test.deb"
    package.write_bytes(b"test receipt only")
    observed = []

    def inspect_dialog():
        dialog = window.findChild(QDialog, "softwareDownloadDialog")
        observed.extend(label.text() for label in dialog.findChildren(QLabel))
        dialog.reject()

    receipt = {"status": "staged", "package_path": str(package)}
    if authentication:
        receipt["release_authentication"] = authentication
    QTimer.singleShot(0, inspect_dialog)
    window._show_software_download(receipt, "d" * 32)
    assert expected in observed
    other = ("安装包签名和 SHA256 已校验，尚未安装。"
             if authentication else "平台已验证发布签名；下载文件SHA256和DEB信息已校验，尚未安装。")
    assert other not in observed
