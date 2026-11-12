import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from unittest.mock import Mock
from shiboken6 import isValid
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent
from factory_hmi.desktop.motor_firmware_dialog import MotorFirmwareDialog


@pytest.fixture
def dialog():
    app = QApplication.instance() or QApplication([])
    state = {"identity": ("executor", 1)}
    rows = [dict(index=0, interface="can2", motor_id=3, joint_name="关节1", status="known",
                 raw_firmware="1.0.8", software="1.0.8", hardware="1.0.2", protocol="1.0.0.0",
                 observed_at="2026-10-30T02:00:00+08:00", raw_responses={"0x31": "a031..."}, errors={})]
    run = Mock()
    d = MotorFirmwareDialog(Mock(), run, lambda: rows, lambda: state["identity"])
    d.timer.stop()
    yield d, run, state, app
    if isValid(d):
        d.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_actual_versions_visible_and_download_separate(dialog):
    d, run, state, app = dialog
    assert "1.0.8" in d.versions.toPlainText() and "读取时间" in d.versions.toPlainText()
    d.check()
    d.client.motor_firmware_releases.assert_not_called() # queued by the window worker
    callback = run.call_args.args[2]
    callback({"result": {"releases": [{"manifest": {"release_id": "a"*32, "version": "1.0.10", "model": "LRO_5550", "notes": "合成检查"}}]}})
    run.call_args.kwargs["on_finished"]()
    d.download()
    run.call_args.args[2]({"result": {"status": "downloaded", "installed": False}})
    assert "尚未刷写" in d.receipt.toPlainText()
    assert "1.0.8" in d.versions.toPlainText()


def test_account_change_or_dialog_close_drops_pending_callbacks(dialog):
    d, run, state, app = dialog
    d.check()
    call = run.call_args
    state["identity"] = ("executor", 2) # same username, new login generation
    call.args[2]({"result": {"releases": [{"manifest": {}}]}})
    assert not d.releases
    call.kwargs["on_finished"]()
    d.check()
    call = run.call_args
    d.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    call.args[2]({"result": {"releases": []}})
    call.kwargs["on_finished"]() # must not call destroyed Qt widgets


def test_partial_numeric_version_keeps_timeout_visible_without_raw_json(dialog):
    d, run, state, app = dialog
    rows = [dict(index=0, interface="can0", motor_id=1, joint_name="关节1", status="known",
                 raw_firmware=None, software="0.1.8", hardware="0.1.0", protocol="0.0.1.17",
                 configured_profile="v1.03", active_profile=None, profile_error="完整版本与配置冲突",
                 observed_at="2026-11-02T05:33:50.510507+00:00",
                 raw_responses={"0x1e": "a01e00010800010000000111"}, errors={"0x31": "版本查询超时"})]
    d.observations = lambda: rows
    d.refresh()
    text = d.versions.toPlainText()
    assert "已读取（部分）" in text and "软件：0.1.8" in text
    assert "原始固件字符串：未取得" in text and "0x31" in text and "版本查询超时" in text
    assert "驱动配置：v1.03" in text and "当前协议配置：未应用" in text and "完整版本与配置冲突" in text
    assert "原始响应" not in text and "{'" not in text
    d.version_details.setChecked(True)
    assert rows[0]["raw_responses"]["0x1e"] in d.versions.toPlainText()
    run.assert_not_called()


@pytest.mark.parametrize("axis", ["vertical", "horizontal"])
def test_refresh_preserves_scroll_and_defers_document_changes_while_dragging(dialog, axis):
    from PySide6.QtWidgets import QPlainTextEdit
    d, run, state, app = dialog
    base = d.rows[0]
    rows = [{**base, "index": i, "motor_id": i + 1, "joint_name": "关节" + str(i)} for i in range(50)]
    rows[0] = {**rows[0], "raw_responses": {"0x31": "a031" + "31" * 62}}
    d.observations = lambda: rows
    d.version_details.setChecked(True)
    d.versions.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    d.resize(550, 460)
    d.show()
    d.refresh();app.processEvents()
    if axis == "horizontal":
        assert d.versions.find("原始响应")
        app.processEvents()
    scrollbar = d.versions.verticalScrollBar() if axis == "vertical" else d.versions.horizontalScrollBar()
    assert scrollbar.maximum() > 0
    position = max(1, scrollbar.maximum() // 2)
    scrollbar.setValue(position)
    changes = Mock()
    d.versions.textChanged.connect(changes)
    d.refresh()
    assert scrollbar.value() == position
    changes.assert_not_called()  # identical observations never replace the document
    before = d.versions.toPlainText()
    scrollbar.setSliderDown(True)
    rows[0] = {**rows[0], "software": "1.0.9"}
    d.refresh();app.processEvents()
    assert d.versions.toPlainText() == before and scrollbar.value() == position
    changes.assert_not_called()
    scrollbar.setSliderDown(False);app.processEvents()
    assert "软件：1.0.9" in d.versions.toPlainText() and scrollbar.value() == position
    # A later observation changes the document without moving the user's view.
    rows[0] = {**rows[0], "software": "1.0.7"}
    d.refresh();app.processEvents()
    assert "软件：1.0.7" in d.versions.toPlainText() and scrollbar.value() == position
    run.assert_not_called()


def test_closing_dialog_stops_refresh_timer(dialog):
    d, run, state, app = dialog
    d.show();d.timer.start()
    assert d.timer.isActive()
    d.close();app.processEvents()
    assert not d.timer.isActive()


def test_current_motor_identity_is_passed_to_firmware_and_parameter_windows(dialog):
    from factory_hmi.desktop.motor_maintenance_dialog import MotorMaintenanceDialog, SELECTED_UPGRADE
    from factory_hmi.desktop.motor_parameters_dialog import MotorParametersDialog

    d, run, state, app = dialog
    first = dict(d.rows[0], model='LRO_5550')
    second = dict(first, index=1, interface='can7', joint_name='关节2')
    d.observations = lambda: [first, second]
    d.refresh()
    d.targets.setCurrentIndex(1)
    d.maintenance()
    d.parameters()
    firmware = d.findChild(MotorMaintenanceDialog)
    parameters = d.findChild(MotorParametersDialog)
    assert firmware.scope.currentData() == SELECTED_UPGRADE
    assert firmware.selected_motor == parameters.selected_motor == (1, 'can7', 3, 'LRO_5550')
