import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from factory_hmi.client import GatewayClient, GatewayError
from factory_hmi.desktop.device_tools_page import DeviceToolsPage


@pytest.fixture
def page():
    app = QApplication.instance() or QApplication([])
    calls = []
    def run(label, operation, success=None, **options):
        calls.append((operation, success, options))
    widget = DeviceToolsPage("dexhand", GatewayClient("http://127.0.0.1:1", client_id="ui-test"),
                             run, lambda: {"configured_motors": [{"bus": "can1"}]})
    widget.show()
    yield widget, calls
    widget.shutdown()
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_page_opens_without_device_commands_and_bounds_polling(page):
    widget, calls = page
    assert not calls
    widget.poll()
    widget.poll()
    assert len(calls) == 1
    calls[0][1]({"owner": None, "detail": "未连接"})
    calls[0][2]["on_finished"]()
    assert widget.bus_label.text() == "can1"
    assert {b.text() for b in widget.findChildren(QPushButton)} == {"读取状态", "开始运动"}
    widget.poll()
    assert len(calls) == 2


def test_late_poll_cannot_discard_new_connection_generation(page):
    widget, calls = page
    widget.apply({"owner": None})
    widget.poll()
    widget.command("read-state")
    calls[1][1]({"owner": "ui-test", "generation": "new", "connected": True})
    calls[0][1]({"owner": None, "generation": "old"})
    assert widget.state["generation"] == "new"


def test_unknown_connect_result_keeps_close_blocked_until_status(page):
    widget, calls = page
    widget.apply({"owner": None})
    widget.command("read-state")
    calls[0][2]["on_failure"](GatewayError("response lost"))
    calls[0][2]["on_finished"]()
    assert widget.active() and widget.claim_unknown
    calls[-1][1]({"owner": None, "generation": None})
    assert not widget.active()


def test_imu_buttons_dispatch_separate_commands_to_edited_interface(monkeypatch):
    app = QApplication.instance() or QApplication([])
    client = GatewayClient("http://127.0.0.1:1")
    calls = []
    monkeypatch.setattr(client, "device_action", lambda kind, action, settings: calls.append((action, settings["interface"])))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    # Run queued HTTP work against the stub without opening the network.
    def submit(label, operation, success, **kwargs):
        operation()
        success({"owner": None})
        kwargs["on_finished"]()
    page = DeviceToolsPage("imu", client, submit, lambda: {})
    try:
        page.apply({"owner": None})
        assert page.interface.text() == "can2"
        page.interface.setText("can4")
        for action in ("check", "initialize"):
            button = page.buttons[action]
            assert button.isEnabled()
            button.click()
        assert calls == [("check", "can4"), ("initialize", "can4")]
    finally:
        page.shutdown()
        page.deleteLater()
        app.processEvents()


def test_same_button_stops_wave_and_retries_disconnect(page, monkeypatch):
    w, pending = page
    actions = []
    monkeypatch.setattr(w.client, "device_action", lambda k, a, settings: actions.append((a, settings)))
    w.arm_snapshot = lambda: {"backend": "fake", "configured_motors": [{"bus":"can3"}]}
    w.apply({"owner": None})
    w.buttons["wave"].click()
    pending[0][0]()
    pending[0][1]({"owner": "ui-test", "generation": "demo", "phase": "waving", "config": {"operation": "wave"}})
    pending[0][2]["on_finished"]()
    assert w.buttons["wave"].text() == "停止运动"
    w.buttons["wave"].click()
    pending[-1][0]()
    assert [a for a, _ in actions] == ["wave", "finish-wave"]
    assert actions[0][1]["backend"] == "mock" and actions[1][1]["generation"] == "demo"


def test_read_while_arm_runs_uses_only_matching_timestamped_cache(page, monkeypatch):
    widget, calls = page
    arm = {"state": "running", "config_revision": 3, "configured_motors": [{"bus": "can1"}]}
    monkeypatch.setattr(widget, "arm_snapshot", lambda: arm)
    report = {"owner": "ui-test", "config_revision": 3, "node_id": 1, "interface": "can1",
              "created_at": "2026-11-26T04:00:00+00:00", "result": {"feedback": {"positions": [7]*6}}}
    widget.apply({"owner": None, "last_report": report})
    assert report["created_at"] in widget.status_label.text() and widget.feedback.item(0, 1).text() == "7"
    assert not widget.buttons["wave"].isEnabled() and widget.buttons["read-state"].isEnabled()
    widget.command("read-state")
    assert len(calls) == 1 and widget.poll_pending and not widget.action_pending
    for changed in ({"owner": "other"}, {"interface": "can3"}, {"config_revision": 4}, {"node_id": 2}):
        widget.apply({"owner": None, "last_report": {**report, **changed}})
        assert "暂无匹配" in widget.status_label.text() and widget.feedback.item(0, 1).text() == "—"
