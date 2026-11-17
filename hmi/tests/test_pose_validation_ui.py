"""Actual desktop wiring with all network/device I/O replaced in memory."""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel, QTabWidget
from test_motor_version_monitor_ui import window


@pytest.mark.parametrize("limb,count", [("left_arm", 14), ("left_short_arm", 10)])
def test_single_pose_page_starts_preview_then_live_job_without_replay(window, monkeypatch, limb, count):
    panel = window.module_limit_panel
    page = window.tabs.widget(6)
    assert window.limit_block_nav_button.text() == "姿态验证"
    assert page.widget() is panel
    assert not page.findChildren(QTabWidget)
    assert any("姿态验证" in label.text() for label in panel.findChildren(QLabel))
    for removed in ("limit_block_page", "continuous_limit_page", "live_limit_panel"):
        assert not hasattr(window, removed)
    monkeypatch.setattr(panel, "isVisible", lambda: True)
    monkeypatch.setattr(panel, "_poll", lambda: None)
    monkeypatch.setattr("factory_hmi.desktop.module_limit_panel.QMessageBox.question",
                        lambda *a: pytest.fail("builtin order needs no extra dialog"))
    calls = []
    ready = dict(limb=limb, backend="motors_py", discovered=True, motors_enabled=True, active=False)
    def post(path, payload):
        calls.append((path, payload))
        if path.endswith("preview"):
            return {"result": dict(limb=limb, profile={"use_builtin_order": True},
                                   windows=[{}]*count, digest="a"*64)}
        assert path == "/api/v1/limit-module/start"
        return {"result": dict(ready, active=True, mode="module", phase="homing",
                               direction_index=1, direction_total=count)}
    monkeypatch.setattr(window, "client", SimpleNamespace(post=post))
    def synchronous_rest(label, operation, success, **kwargs):
        success(operation())
        if finish := kwargs.get("on_finished"):
            finish()
    monkeypatch.setattr(window, "_run_rest", synchronous_rest)
    window.robot_id.setEditText("POSE-TEST")
    panel._take_result({"result": ready})
    assert panel.start.isEnabled()
    panel.start.click()
    assert [path for path, _ in calls] == ["/api/v1/limit-module/preview", "/api/v1/limit-module/start"]
    assert calls[1][1] == dict(serial="POSE-TEST", digest="a"*64, confirmed=True)
    assert "平滑复位" in panel.state.text()
    assert not panel.start.isEnabled() and panel.abort.isEnabled()
    panel.shutdown()


def test_unready_has_explanation_and_estimated_result_is_labelled(window):
    panel = window.module_limit_panel
    panel._take_result({"result": dict(limb="left_arm", backend="motors_py", discovered=False,
                                       motors_enabled=False, active=False)})
    assert not panel.start.isEnabled()
    assert "连接、扫描并使能" in panel.state.text()
    panel._take_result({"result": dict(limb="left_arm", mode="module", phase="finished", active=False,
        results=[dict(joint="left_shoulder_pitch_joint", sign=1, actual_deg=164.9,
                      reference_deg=165.553, deviation_deg=-.653, torque_floor_nm=2.5,
                      torque_is_estimated=True, outcome="within_reference")])})
    text = " ".join(panel.results.item(r, c).text() for r in range(panel.results.rowCount()) for c in range(panel.results.columnCount()))
    assert "+164.90" in text and "-0.65" in text and "（估算）" in text
    assert panel.export.isEnabled()
