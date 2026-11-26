"""Feedback display regressions using Qt with network and device I/O disabled."""
import math

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.app import FactoryMainWindow


@pytest.fixture
def window(monkeypatch):
    application = QApplication.instance() or QApplication([])

    def setup_without_io(instance):
        instance.reconnect_timer = QTimer(instance)
        instance.snapshot_timer = QTimer(instance)
        instance.lease_timer = QTimer(instance)
        instance.websocket = QWebSocket()

    monkeypatch.setattr(FactoryMainWindow, "_setup_network", setup_without_io)
    monkeypatch.setattr(FactoryMainWindow, "_run_rest", lambda *args, **kwargs: None)
    result = FactoryMainWindow("http://127.0.0.1:65534")
    result.plc_page.stop()
    yield result
    result.plc_page.stop()
    result.deleteLater()
    assert application is not None


def receive(window, **overrides):
    motor = {
        "joint": "left_shoulder_pitch_joint", "motor_id": 1, "can": "can0",
        "timestamp_s": 123.0, "feedback_age_s": 0.01,
        "position_rad": 0.1, "speed_rad_s": 0.2, "torque_nm": 0.3,
        "temperature_c": 25.0, "bus_voltage_v": 48.0, "bus_current_a": 0.5,
        "error_id": 0,
    }
    motor.update(overrides)
    window._render_snapshot({
        "state": "connected", "connected": True,
        "station": {"state": "connected", "motors_discovered": True,
                    "motors_enabled": False},
        "motors": [motor],
    })


def cell(window, column):
    # The four columns after CAN show firmware, status, 0x1E data and confirmation.
    if column >= 3:
        column += 4
    return window.motor_table_model.index(0, column).data(Qt.DisplayRole)


@pytest.mark.parametrize("timestamp", [0.0, math.nan])
def test_no_feedback_is_not_normal_or_uptime_since_boot(window, timestamp):
    receive(window, timestamp_s=timestamp, feedback_age_s=952699.344)
    assert cell(window, 12) == "未收到电机反馈"
    assert cell(window, 13) == "—"
    assert cell(window, 11) == "—"
    for column in range(4, 11):
        assert cell(window, column) == "—"


def test_fresh_feedback_retains_readings_and_normal_status(window):
    receive(window)
    assert cell(window, 12) == "正常"
    assert cell(window, 4) != "—"
    assert cell(window, 11) == "0"
    assert cell(window, 13) != "—"


def test_lost_feedback_reports_timeout_and_retains_observed_motor_error(window):
    receive(window, feedback_age_s=0.3, error_id=7)
    assert cell(window, 12) == "通信反馈超时"
    assert cell(window, 4) == "—"
    assert cell(window, 11) == "7（上次读数）"
    assert cell(window, 13) != "—"


def test_nonzero_error_is_not_hidden_by_missing_timestamp(window):
    receive(window, timestamp_s=0.0, error_id=7)
    assert cell(window, 11) == "7（上次读数）"
    assert cell(window, 12) == "未收到电机反馈"


def test_existing_software_alarm_is_not_replaced(window):
    receive(window, timestamp_s=0.0, software_diagnosis="位置跟踪超限")
    assert cell(window, 12) == "位置跟踪超限"
    assert cell(window, 13) == "—"


def test_fresh_driver_fault_warns_temperature_can_freeze_without_rewriting_value(window):
    receive(window, error_id=7, temperature_c=32.0, lro_protocol_profile="legacy")
    assert cell(window, 8) == "32.000"
    assert "DRV_ERROR" in cell(window, 12)
    assert "温度为上报值" in cell(window, 12)
    assert "可能冻结" in cell(window, 12)
    assert cell(window, 11) == "7"


def test_scan_pending_blocks_duplicate_submission_even_if_snapshot_arrives(window, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "_run_rest", lambda *args, **kwargs: calls.append((args, kwargs)))
    window._configured = True
    window._can_connected = True
    window._discover_motors()
    window._discover_motors()
    assert len(calls) == 1
    assert window._motor_discovery_pending
    assert "扫描中" in window.motor_discovery_label.text()
    window._render_snapshot({"configured": True, "connected": False,
                             "can": {"all_up": True}, "motors": []})
    assert not window.discover_button.isEnabled()
    assert not window.enable_motors_button.isEnabled()
    assert "扫描中" in window.motor_discovery_label.text()
    calls[0][1]["on_finished"]()
    assert not window._motor_discovery_pending
    assert window.discover_button.isEnabled()


def test_scan_timeout_message_preserves_bus_and_joint_evidence(window, monkeypatch):
    from factory_hmi.client import GatewayHTTPError
    messages = []
    monkeypatch.setattr("factory_hmi.desktop.app.QMessageBox.critical", lambda *args: messages.append(args[-1]))
    window._rest_failed("连接/扫描电机", GatewayHTTPError("failed", status_code=409, detail=(
        "motor discovery: fresh feedback timeout: motor_ids=3; elapsed_s=5.000; "
        "pending=can3#3[left_shoulder_yaw_joint](0/2,age_s=none)")), False)
    assert "can3#3" in messages[-1]
    assert "left_shoulder_yaw_joint" in messages[-1]
    assert "未自动清错或使能" in messages[-1]


def test_open_transport_after_failed_scan_is_not_displayed_as_discovered(window):
    window._render_snapshot({"configured": True, "can": {"all_up": True},
                             "station": {"state": "fault", "backend_connected": True,
                                         "motors_discovered": False, "motors_enabled": False},
                             "motors": []})
    assert not window._motors_discovered
    assert not window.enable_motors_button.isEnabled()


def test_scan_success_uses_confirmed_snapshot_and_ends_waiting_notice(window):
    window._motor_discovery_pending = True
    window._motors_discovered_ok({"snapshot": {
        "configured": True, "connected": True,
        "station": {"state": "connected", "backend_connected": True,
                    "motors_discovered": True, "motors_enabled": False},
        "configured_motors": [{"index": 0, "motor_id": 3, "bus": "can3"}],
        "motors": [],
    }})
    assert not window._motor_discovery_pending
    assert window._motors_discovered
    assert "扫描中" not in window.motor_discovery_label.text()


def test_enable_waits_for_protocol_readback_and_ignores_duplicate_clicks(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    snapshot = {
        "configured": True,
        "connected": True,
        "station": {"state": "connected", "motors_discovered": True,
                    "motors_enabled": False, "motor_protocols_pending": True},
        "motors": [],
    }
    calls = []
    confirmations = []

    def confirm(*args):
        confirmations.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr(window, "_run_rest", lambda *args, **kwargs: calls.append((args, kwargs)))
    window._motors_discovered_ok(snapshot)
    assert window._motors_discovered
    assert not window.enable_motors_button.isEnabled()
    assert "读取固件与协议" in window.feedback_read_label.text()
    window._enable_motors()
    assert not confirmations and not calls

    snapshot["station"]["motor_protocols_pending"] = False
    window._render_snapshot(snapshot)
    assert window.enable_motors_button.isEnabled()
    assert not confirmations and not calls

    window._enable_motors()
    assert len(confirmations) == len(calls) == 1
    window._render_snapshot(snapshot)
    assert not window.enable_motors_button.isEnabled()
    window._enable_motors()
    assert len(confirmations) == len(calls) == 1
    calls[0][1]["on_finished"]()
    assert window.enable_motors_button.isEnabled()


def test_multi_axis_popup_keeps_codes_and_fault_action_together(window, monkeypatch):
    from factory_hmi.client import GatewayHTTPError
    from factory_hmi.core.fault_response import fault_error_detail
    dialogs = []
    monkeypatch.setattr("factory_hmi.desktop.app.QMessageBox.critical", lambda *args: dialogs.append(args[-1]))
    error = RuntimeError("id=7:error_id=2, id=3:error_id=7")
    error.motor_fault_response = {"disable_request": "failed", "disable_feedback": "unknown", "error": "CAN-FD TX can1 CAN ID 0x8080: timeout"}
    window._rest_failed("进入 Armed", GatewayHTTPError("failed", status_code=409, detail=fault_error_detail(error)), False)
    assert "ID 7（error_id=2）" in dialogs[-1] and "ID 3（error_id=7）" in dialogs[-1]
    assert "失能请求失败" in dialogs[-1] and "实际失能反馈未知" in dialogs[-1] and "CAN-FD TX can1" in dialogs[-1]
    assert "主机 CAN 写入未完成" not in dialogs[-1]
    window._rest_failed("进入 Armed", GatewayHTTPError("failed", status_code=409, detail="can1#3 报警 error_id=7"), False)
    assert "can1#3" in dialogs[-1] and "此次请求未提供失能结果" in dialogs[-1]


def test_snapshot_503_keeps_operator_error_and_backs_off(window, monkeypatch):
    from factory_hmi.client import GatewayHTTPError
    window.statusBar().showMessage("首次关闭失败")
    window._rest_failed("刷新状态", GatewayHTTPError("failed", status_code=503, detail="尚无新鲜工位快照"), True)
    assert window.statusBar().currentMessage() == "首次关闭失败"
    calls = []
    monkeypatch.setattr(window, "_run_rest", lambda *args, **kwargs: calls.append(args))
    window._poll_snapshot()
    assert not calls and "等待新鲜" in window.connection_badge.text()
    window._receive_ws_status({"state": "closing", "fault": {"message": "首次关闭失败"}})
    assert window._snapshot_retry_at == 0 and "首次关闭失败" in window.global_fault.text()
    assert "失能未确认" in window.motor_enable_label.text()
    assert "未知" in window.position_hold_label.text()
    assert not window.enable_motors_button.isEnabled()


def test_multiple_motor_faults_retain_address_code_pairs(window):
    from factory_hmi.client import GatewayError
    detail = "cannot arm: bus=can0:id=7:error_id=2, bus=can0:id=3:error_id=7"
    window._rest_failed("进入 Armed", GatewayError(detail, status_code=409, detail=detail), quiet=True)
    message = window.statusBar().currentMessage()
    assert detail in message
    assert "id=7:error_id=7" not in message and "已撤销" not in message


def test_unknown_code_and_no_frame_do_not_display_zero(window):
    receive(window, error_id=None, timestamp_s=0.)
    assert cell(window, 11) == "—"


@pytest.mark.parametrize("profile,label", [(None, "协议待核实"), ("v1.03", "OTHER_ERROR")])
def test_fault_seven_label_follows_verified_profile(window, profile, label):
    receive(window, error_id=7, lro_protocol_profile=profile)
    assert label in cell(window, 12) and "DRV_ERROR" not in cell(window, 12)
