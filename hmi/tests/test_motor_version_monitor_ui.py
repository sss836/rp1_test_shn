"""Motor monitor firmware presentation; all network and device I/O disabled."""
import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.app import FactoryMainWindow


@pytest.fixture
def window(monkeypatch):
    app = QApplication.instance() or QApplication([])
    def setup_without_io(instance):
        instance.reconnect_timer = QTimer(instance)
        instance.snapshot_timer = QTimer(instance)
        instance.lease_timer = QTimer(instance)
        instance.websocket = QWebSocket()
    monkeypatch.setattr(FactoryMainWindow, "_setup_network", setup_without_io)
    monkeypatch.setattr(FactoryMainWindow, "_run_rest", lambda *args, **kwargs: None)
    result = FactoryMainWindow("http://127.0.0.1:65534")
    result.plc_page.stop()
    monkeypatch.setattr(result, "_run_rest", lambda *args, **kwargs: pytest.fail("unexpected device/network command"))
    yield result
    result.plc_page.stop()
    result.deleteLater()
    assert app is not None


def motor(interface, motor_id, index):
    return {"joint": f"motor_{index}", "index": index, "motor_id": motor_id,
            "can": interface, "timestamp_s": 10.0, "feedback_age_s": .01,
            "position_rad": .1, "error_id": 0}


def observed(interface, motor_id, index, firmware, status="known", **extra):
    return {"interface": interface, "motor_id": motor_id, "index": index,
            "raw_firmware": firmware, "status": status, "errors": {}, **extra}


def render(window, motors, versions, *, connected=True):
    window._render_snapshot({"state": "connected" if connected else "disconnected", "connected": connected,
        "station": {"state": "connected" if connected else "disconnected", "motors_enabled": False,
                    "motors_discovered": connected}, "motors": motors, "motor_versions": versions})


def read(window, row, header):
    column = window.MOTOR_COLUMNS.index(header)
    return window.motor_table_model.index(row, column).data(Qt.DisplayRole)


def test_versions_are_joined_by_can_and_motor_id_not_position_or_id_only(window):
    motors = [motor("can0", 1, 0), motor("can0", 2, 1), motor("can1", 1, 2)]
    # Deliberately reverse observations and mismatch indices. Neither row order nor
    # motor ID alone may attach can1#1's firmware to can0#1.
    versions = [observed("can1", 1, 0, "109-actual"), observed("can0", 2, 9, "108-b"),
                observed("can0", 1, 5, "108-a")]
    render(window, motors, versions)
    assert [read(window, i, "固件版本") for i in range(3)] == ["108-a", "108-b", "109-actual"]
    assert [read(window, i, "读取状态") for i in range(3)] == ["已读取"] * 3


@pytest.mark.parametrize("status,firmware,errors,expected", [
    ("known", "108", {}, "已读取"),
    ("known", None, {}, "部分读取"),
    ("known", "108", {"0x1e": "timeout"}, "部分读取"),
    ("timeout", None, {}, "读取超时"),
    ("unknown", None, {}, "未知"),
    ("unsupported", None, {}, "不支持"),
    ("stale", "108", {}, "已过期"),
])
def test_version_status_does_not_confuse_feedback_with_verified_version(window, status, firmware, errors, expected):
    render(window, [motor("can0", 1, 0)], [observed("can0", 1, 0, firmware, status, errors=errors)])
    assert read(window, 0, "读取状态") == expected
    assert read(window, 0, "固件版本") == (firmware or "—")


def test_missing_observation_and_reconfiguration_do_not_reuse_previous_version(window):
    render(window, [motor("can0", 1, 0)], [observed("can0", 1, 0, "108-old")])
    assert read(window, 0, "固件版本") == "108-old"
    # New configuration reuses index/ID on another bus. No observation exists yet.
    render(window, [motor("can1", 1, 0)], [])
    assert read(window, 0, "固件版本") == "—"
    assert read(window, 0, "读取状态") == "未读取"


def test_disconnect_does_not_present_last_version_as_current(window):
    m = [motor("can0", 1, 0)]
    v = [observed("can0", 1, 0, "108")]
    render(window, m, v)
    render(window, m, v, connected=False)
    assert read(window, 0, "读取状态") != "已读取"


@pytest.mark.parametrize("status,connected,error,expected", [
    ("known", True, "版本查询超时", "固件超时"),
    ("stale", False, "版本查询超时", "过期·固件超时"),
    ("stale", True, "SDK或驱动不支持版本查询", "过期·固件不支持"),
    ("stale", False, "固件字符串响应不足20字节", "过期·固件读取失败"),
])
def test_missing_firmware_reports_actual_error_even_when_disconnected(window, status, connected, error, expected):
    observation = observed("can0", 1, 0, None, status, errors={"0x31": error},
                           numeric_software="0.1.8", hardware="0.1.0", protocol="0.0.1.17")
    render(window, [motor("can0", 1, 0)], [observation], connected=connected)
    assert read(window, 0, "固件版本") == "—"
    assert read(window, 0, "读取状态") == expected


def test_numeric_version_readback_is_shown_separately_from_unread_firmware(window):
    observation = observed("can0", 1, 0, None, "known", errors={"0x31": "版本查询超时"},
                           numeric_software="0.1.8", hardware="0.1.0", protocol="0.0.1.17")
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "固件版本") == "—"
    assert read(window, 0, "读取状态") == "固件超时"
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "0.1.8 / 0.1.0 / 0.0.1.17"


def test_firmware_and_numeric_version_are_independent_even_when_both_read(window):
    observation = observed("can0", 1, 0, "5.0.0.109", numeric_software="0.1.8",
                           hardware="0.1.0", protocol="0.0.1.17")
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "固件版本") == "5.0.0.109"
    assert read(window, 0, "读取状态") == "已读取"
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "0.1.8 / 0.1.0 / 0.0.1.17"


def test_software_derived_from_firmware_is_not_used_as_numeric_readback(window):
    observation = observed("can0", 1, 0, "5.0.0.109", software="5.0.0.109")
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "—"


def test_numeric_identity_is_labeled_stale_after_disconnect(window):
    observation = observed("can0", 1, 0, None, "stale", errors={"0x31": "版本查询超时"},
                           numeric_software="0.1.8", hardware="0.1.0", protocol="0.0.1.17")
    render(window, [motor("can0", 1, 0)], [observation], connected=False)
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "过期 · 0.1.8 / 0.1.0 / 0.0.1.17"
    assert read(window, 0, "读取状态") == "过期·固件超时"


def test_numeric_versions_do_not_cross_can_buses_or_reconfigured_motors(window):
    motors = [motor("can0", 1, 0), motor("can1", 1, 1)]
    observations = [observed("can1", 1, 1, None, numeric_software="1.2.3", hardware="4.5.6"),
                    observed("can0", 1, 0, None, numeric_software="0.1.8", protocol="0.0.1.17")]
    render(window, motors, observations)
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "0.1.8 / — / 0.0.1.17"
    assert read(window, 1, "软件 / 硬件 / 协议（0x1E）") == "1.2.3 / 4.5.6 / —"
    render(window, [motor("can2", 1, 0)], [])
    assert read(window, 0, "软件 / 硬件 / 协议（0x1E）") == "—"


def test_confirmation_is_only_shown_when_backend_reports_active_local_evidence(window):
    observation = observed("can0", 1, 0, None, errors={"0x31": "版本查询超时"},
                           numeric_software="0.1.8", hardware="0.1.0", protocol="0.0.1.17",
                           active_profile="legacy", protocol_confirmation_source="local_numeric_evidence",
                           protocol_confirmation_evidence="现场逐台核实")
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "固件版本") == "—"
    assert read(window, 0, "读取状态") == "固件超时"
    assert read(window, 0, "协议确认") == "现场核实旧协议"


@pytest.mark.parametrize("extra", [
    {"numeric_software": "0.1.8", "hardware": "0.1.0", "protocol": "0.0.1.17"},
    {"active_profile": "legacy", "protocol_confirmation_source": "local_numeric_evidence"},
    {"active_profile": None, "protocol_confirmation_source": "local_numeric_evidence", "protocol_confirmation_evidence": "核实"},
    {"active_profile": "v1.03", "protocol_confirmation_source": "local_numeric_evidence", "protocol_confirmation_evidence": "核实"},
])
def test_numeric_readback_or_incomplete_confirmation_never_implies_verified(window, extra):
    render(window, [motor("can0", 1, 0)], [observed("can0", 1, 0, None, **extra)])
    assert read(window, 0, "协议确认") == "待核实"


def test_disconnected_local_confirmation_is_stale_and_not_current(window):
    observation = observed("can0", 1, 0, None, "stale", active_profile="legacy",
                           protocol_confirmation_source="local_numeric_evidence",
                           protocol_confirmation_evidence="现场逐台核实")
    render(window, [motor("can0", 1, 0)], [observation], connected=False)
    assert read(window, 0, "协议确认") == "待核实（已过期）"


def test_unknown_version_state_does_not_show_stale_confirmation(window):
    observation = observed("can0", 1, 0, None, "timeout", active_profile="legacy",
                           protocol_confirmation_source="local_numeric_evidence",
                           protocol_confirmation_evidence="现场逐台核实")
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "协议确认") == "待核实"


@pytest.mark.parametrize("source,label", [
    ("configured_identity", "配置身份已核实"),
    ("firmware_readback", "固件读回已核实"),
])
def test_other_confirmation_sources_require_backend_active_profile(window, source, label):
    observation = observed("can0", 1, 0, "108", active_profile="legacy", protocol_confirmation_source=source)
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "协议确认") == label
    observation["active_profile"] = None
    render(window, [motor("can0", 1, 0)], [observation])
    assert read(window, 0, "协议确认") == "待核实"
