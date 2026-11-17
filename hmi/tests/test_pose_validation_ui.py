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


def test_homing_abort_shows_reason_above_results_and_preserves_actual_error(window):
    panel = window.module_limit_panel
    transfer = dict(phase="homing", actual_deg=[2.81,0,0,-2.83,0,0,0], goal_deg=[0]*7,
                    arrival_tolerance_deg=3., command_progress=.5)
    state = dict(limb="left_arm", mode="module", active=True, phase="homing", last_transfer=transfer)
    panel._take_result({"result": state})
    assert panel.progress_text.text() == "自动回零 · 50%"
    assert "2.83°" in panel.measurement.text() and "±3°" in panel.measurement.text()
    assert panel.fault_banner.isHidden()
    reason = "转场命令已发完，未确认到位：left_elbow_joint 偏差 3.40°"
    panel._take_result({"result": dict(state, active=False, phase="aborted", fault=reason)})
    assert not panel.fault_banner.isHidden() and reason in panel.fault_banner.text()
    assert "中止" in panel.current_value.text()
    assert panel.export.isEnabled()


def test_mit_homing_displays_error_as_measurement_without_arrival_cutoff(window):
    panel = window.module_limit_panel
    transfer = dict(phase="homing", actual_deg=[8.27,0,0,0,0,0,0], goal_deg=[0]*7,
                    arrival_tolerance_deg=None, command_progress=1., waiting_for_settle=True)
    panel._take_result({"result": dict(limb="left_arm", mode="module", active=True,
                                     phase="homing", last_transfer=transfer)})
    assert "8.27°" in panel.measurement.text()
    assert "角差仅记录" in panel.measurement.text()
    assert "±5" not in panel.measurement.text()


def test_recorded_items_and_final_return_are_distinct_and_rates_visible(window):
    from factory_hmi.desktop.pose_validation_view import reference_rows
    panel = window.module_limit_panel
    checks = reference_rows("left_arm")
    results = [dict(row, outcome="within_reference") for row in checks]
    transfer = dict(phase="clearance", actual_deg=[0,0,0,0,0,0,-6.61], goal_deg=[0]*7,
                    arrival_tolerance_deg=None, command_progress=.78)
    state = dict(limb="left_arm", active=True, phase="clearance", checks=checks, results=results,
                 last_transfer=transfer, motion_rates=dict(search_deg_s=10.,near_limit_deg_s=2.,return_deg_s=20.))
    panel._take_result({"result":state})
    assert "回零收尾中" in panel.progress_text.text()
    assert "10°/s" in panel.motion_rates.text() and "20°/s" in panel.motion_rates.text()
    panel._take_result({"result":dict(state,active=False,phase="aborted",fault="逐电机反馈已过期")})
    assert panel.current_value.text() == "回零收尾中止"
    assert "反馈已过期" in panel.fault_banner.text()


def test_contact_baseline_and_bounded_hold_are_visible_without_hiding_anomalies(window):
    panel = window.module_limit_panel
    panel._take_result({"result": dict(limb="left_arm", mode="module", active=True, phase="search",
        current_check=dict(joint="left_shoulder_pitch_joint", sign=1, actual_deg=160., torque_nm=4.,
            torque_is_estimated=True, baseline_nm=2., contact_threshold_nm=2.5,
            baseline_frozen=True, search_hold="command_or_lead"),
        results=[dict(joint="left_elbow_joint", sign=-1, actual_deg=-80., reference_deg=-91.481,
            deviation_deg=11.481, torque_floor_nm=0., torque_median_nm=4., torque_is_estimated=True,
            outcome="early")])})
    assert "2.00" in panel.contact_detail.text() and "2.50" in panel.contact_detail.text()
    assert "推进已停止" in panel.contact_detail.text()
    assert "估算力矩" in panel.measurement.text()
    assert "1 异常" in panel.counts.text()
    text = " ".join(panel.results.item(r, c).text() for r in range(panel.results.rowCount())
                    for c in range(panel.results.columnCount()))
    assert "提前阻挡" in text and "4.00（估算）" in text


def test_wrist_retry_progress_and_missed_endpoints_are_distinct_from_measured_stop(window):
    panel = window.module_limit_panel
    ref = dict(joint="left_wrist_yaw_joint", sign=-1, reference_deg=-85.)
    initial = dict(ref, search_attempt=1, search_attempt_limit=2)
    status = dict(limb="left_arm", mode="module", active=True, checks=[ref])
    panel._take_result({"result": dict(status, phase="retreat", current_check=initial)})
    assert "首轮搜索 1/2" in panel.current_value.text()
    assert "确认退让后低速复搜" in panel.contact_detail.text()

    retry = dict(initial, search_attempt=2)
    panel._take_result({"result": dict(status, phase="baseline", current_check=retry)})
    assert "复搜 2/2" in panel.current_value.text()
    assert "重新采集复搜负载基线" in panel.contact_detail.text()
    assert panel.results.item(0, 5).text() == "复搜中"

    attempts = [
        dict(attempt=1, end_reason="actual_travel", search_end_actual_deg=-88.03,
             search_end_target_deg=-88.95, search_end_torque_nm=.68, threshold_nm=1.18),
        dict(attempt=2, end_reason="command_travel", search_end_actual_deg=-87.91,
             search_end_target_deg=-88.11, search_end_torque_nm=.70, threshold_nm=1.20),
    ]
    result = dict(ref, outcome="not_detected", search_attempts=attempts)
    panel._take_result({"result": dict(status, active=False, phase="finished", results=[result])})
    assert panel.results.item(0, 2).text() == "—"
    outcome = panel.results.item(0, 5)
    assert outcome.text() == "未检出"
    assert "实测到达行程边界" in outcome.toolTip() and "-88.03°" in outcome.toolTip()
    assert "指令到达行程边界" in outcome.toolTip() and "-87.91°" in outcome.toolTip()


def test_wrist_extension_shows_progress_and_distinguishes_travel_boundary(window):
    panel = window.module_limit_panel
    ref = dict(joint="left_wrist_yaw_joint", sign=-1, reference_deg=-85.)
    extension = dict(actual_limit_deg=-94., command_limit_deg=-95.)
    state = dict(limb="left_arm", mode="module", checks=[ref])
    panel._take_result({"result": dict(state, active=True, phase="search",
        current_check=dict(ref, search_extension_active=True, search_extension=extension))})
    assert "延伸搜索" in panel.current_value.text()
    assert panel.results.item(0, 5).text() == "延伸搜索中"

    panel._take_result({"result": dict(state, active=False, phase="finished",
        results=[dict(ref, outcome="not_detected", search_extension=extension)])})
    assert panel.results.item(0, 2).text() == "—"
    assert panel.results.item(0, 5).text() == "行程到界 · 止挡待确认"


@pytest.mark.parametrize("phase,title", [
    ("arm_hanging", "肘部缓慢下垂收尾"),
    ("arm_settling", "下垂姿态 · 等待实测停稳"),
    ("arm_holding", "下垂收尾需处理 · 保持目标"),
])
def test_arm_completion_has_its_own_live_stage(window, phase, title):
    panel = window.module_limit_panel
    panel._take_result({"result": dict(limb="left_arm", mode="module", active=True,
        phase=phase, completion=dict(actual_joint_deg=[0,0,0,-72,0,0,0],
            target_joint_deg=[0,0,0,-80.8,0,0,0], stable_seconds=.4))})
    assert panel.current_value.text() == title
    assert "-72.00°" in panel.measurement.text() and "-80.80°" in panel.measurement.text()
    assert "逐台确认失能" in panel.contact_detail.text()
    assert not panel.export.isEnabled()


def test_partial_disable_names_drives_and_stopped_worker_can_export(window):
    panel = window.module_limit_panel
    details = dict(state="unconfirmed", motors=[
        dict(bus="can0",motor_id=i,requires_confirmation=True,
             state="confirmed" if i<5 else "unconfirmed") for i in range(1,8)])
    panel._take_result({"result": dict(limb="left_arm",mode="module",active=True,
        motors_enabled=True,control_thread_running=False,phase="aborted",
        completion=dict(normal_completed=True), motor_disable=details,
        disable=dict(request="failed",feedback="unconfirmed",details=details),
        fault="收尾失能尚未确认")})
    assert panel.link_value.text() == "失能未确认 · can0#5、can0#6、can0#7"
    assert panel.progress_text.text() == "检查已记录 · 电机失能未全部确认"
    assert panel.current_value.text() == "肘部缓降完成 · 失能待确认"
    assert "尚有电机未确认失能" in panel.state.text()
    assert panel.export.isEnabled() and panel.abort.isEnabled()
    assert not panel.start.isEnabled()


def test_prior_disable_confirmation_is_labelled_as_reused(window):
    panel = window.module_limit_panel
    panel._take_result({"result": dict(limb="left_arm",mode="module",active=False,
        phase="finished",disable=dict(request="sent",feedback="confirmed",
                                      details=dict(reused_confirmation=True)))})
    assert "复用本次控制的逐台失能确认" in panel.state.text()
