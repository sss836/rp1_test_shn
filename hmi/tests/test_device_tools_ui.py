import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QTabWidget

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
    assert widget.backend.currentData() == "hardware"
    assert widget.amplitude.minimum() == 1
    widget.poll()
    assert len(calls) == 2


def test_late_poll_cannot_discard_new_connection_generation(page):
    widget, calls = page
    widget.apply({"owner": None})
    widget.poll()
    widget.command("connect")
    calls[1][1]({"owner": "ui-test", "generation": "new", "connected": True})
    calls[0][1]({"owner": None, "generation": "old"})
    assert widget.state["generation"] == "new"


def test_unknown_connect_result_keeps_close_blocked_until_status(page):
    widget, calls = page
    widget.apply({"owner": None})
    widget.command("connect")
    calls[0][2]["on_failure"](GatewayError("response lost"))
    calls[0][2]["on_finished"]()
    assert widget.active() and widget.claim_unknown
    calls[-1][1]({"owner": None, "generation": None})
    assert not widget.active()


def test_imu_tabs_dispatch_separate_commands_to_edited_interface(monkeypatch):
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
        tabs = page.findChild(QTabWidget)
        page.apply({"owner": None})
        assert page.interface.text() == "can2"
        page.interface.setText("can4")
        for index, action in ((1, "check"), (0, "initialize")):
            tabs.setCurrentIndex(index)
            button = page.buttons[action]
            assert tabs.currentWidget().isAncestorOf(button) and button.isEnabled()
            button.click()
        assert calls == [("check", "can4"), ("initialize", "can4")]
    finally:
        page.shutdown()
        page.deleteLater()
        app.processEvents()
