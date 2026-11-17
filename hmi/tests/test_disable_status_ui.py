"""Per-drive disable evidence in the desktop, without network or devices."""
import pytest
from PySide6.QtWidgets import QMessageBox

from test_motor_version_monitor_ui import window


def disable_rows(*, pending=(5, 6, 7), legacy=False):
    return [dict(bus="can0", motor_id=i, requires_confirmation=not legacy,
                 protocol_profile="legacy" if legacy else "v1.03",
                 state="unknown" if legacy else "unconfirmed" if i in pending else "confirmed")
            for i in range(1, 8)]


def snapshot(*, report=None, enabled=True, state="armed"):
    result = dict(configured=True, connected=True, trajectory={},
        configured_motors=[dict(joint_name=f"joint_{i}", motor_id=i, interface="can0") for i in range(1, 8)],
        joint_positions_rad=[0.]*7,
        station=dict(limb="left_arm", state=state, motors_enabled=enabled,
                     motors_discovered=True, backend_connected=True, position_hold_active=False))
    if report is not None:
        result["motor_disable"] = report
    return result


def test_partial_109_disable_uses_per_motor_evidence_instead_of_enabled_flag(window):
    window._render_snapshot(snapshot(report=dict(state="unconfirmed", motors=disable_rows())))
    assert window._motors_enabled is True  # Conservative backend flag is retained.
    assert window.motor_enable_label.text() == "使能\n失能待确认"
    assert window.feedback_read_label.text() == "状态读取\n失能待确认 · 控制下发已阻止"
    for label in (window.motor_enable_label, window.feedback_read_label):
        for i in (5, 6, 7):
            assert f"can0#{i}" in label.toolTip()
        assert "can0#1：" not in label.toolTip()
    for button in (window.preflight_button, window.start_button, window.reset_button,
                   window.enable_motors_button, window.manual_move_button):
        assert not button.isEnabled()
    assert window.disable_button.isEnabled() and window.manual_disable_button.isEnabled()
    assert not window._preflight_ok
    assert "失能待确认" in window.preflight_result.text()


@pytest.mark.parametrize("enabled", [True, False])
def test_retry_disable_remains_available_even_when_software_flag_is_false(window, monkeypatch, enabled):
    window._render_snapshot(snapshot(enabled=enabled, report=dict(state="unconfirmed", motors=disable_rows())))
    calls = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(window, "_run_rest", lambda name, operation, *args, **kwargs: calls.append(name))
    window.disable_button.click()
    window.manual_disable_button.click()
    assert calls == ["软件失能", "软件失能"]


def test_sending_failure_blocks_motion_even_for_legacy_protocol(window):
    rows = disable_rows(legacy=True)
    rows[2].update(state="request_failed", request_error="CAN write failed")
    window._render_snapshot(snapshot(enabled=False, report=dict(state="unconfirmed", motors=rows)))
    assert window._motor_disable_pending
    assert "can0#3：发送失败" in window.motor_enable_label.toolTip()
    assert "CAN write failed" in window.feedback_read_label.toolTip()
    assert window.disable_button.isEnabled() and not window.enable_motors_button.isEnabled()


def test_pending_status_does_not_mix_motor_ids_between_buses(window):
    rows = [dict(bus="can0", motor_id=1, requires_confirmation=True, state="confirmed"),
            dict(bus="can1", motor_id=1, requires_confirmation=True, state="unconfirmed")]
    window._render_snapshot(snapshot(report=dict(state="unconfirmed", motors=rows)))
    assert "can1#1" in window.motor_enable_label.toolTip()
    assert "can0#1" not in window.motor_enable_label.toolTip()


def test_new_motion_and_queued_manual_targets_do_not_dispatch_until_resolved(window, monkeypatch):
    window._render_snapshot(snapshot(report=dict(state="unconfirmed", motors=disable_rows())))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: pytest.fail("unexpected motion dialog"))
    window._confirm_preflight()
    window._return_default()
    window._start_default_return(recheck=True)
    window._manual_move()
    window._start_playback()
    window._reset_motor_fault()
    window._enable_motors()
    window._manual_mode_active = True
    window._manual_command_queued = True
    window._schedule_manual_command()
    assert not window.manual_command_timer.isActive()
    window._dispatch_manual_command()
    assert not window._manual_command_queued
    window._manual_mode_active = False
    window._manual_start_finished()
    window._manual_stopped(None)
    assert not window.manual_move_button.isEnabled()


def test_late_manual_start_callback_cannot_reenable_inputs_during_pending_disable(window):
    window._render_snapshot(snapshot(report=dict(state="unconfirmed", motors=disable_rows())))
    window._manual_started(None)
    assert not window.manual_speed_spin.isEnabled() and not window.manual_speed_slider.isEnabled()
    assert all(not window.manual_joint_table.cellWidget(i, 3).isEnabled()
               for i in range(window.manual_joint_table.rowCount()))


def test_successful_retry_shows_all_confirmed_and_clears_pending_controls(window):
    window._render_snapshot(snapshot(report=dict(state="unconfirmed", motors=disable_rows())))
    window._render_snapshot(snapshot(enabled=False, state="connected",
        report=dict(state="confirmed", motors=disable_rows(pending=()))))
    assert not window._motor_disable_pending
    assert "已逐台确认" in window.motor_enable_label.text()
    assert window.feedback_read_label.text() == "状态读取\n失能已逐台确认"
    assert "can0#7" in window.motor_enable_label.toolTip()
    assert window.enable_motors_button.isEnabled()
    assert not window.start_button.isEnabled() and not window.reset_button.isEnabled()


def test_108_unknown_does_not_claim_confirmation_or_break_reenable_compatibility(window):
    window._render_snapshot(snapshot(enabled=False, state="connected",
        report=dict(state="unknown", motors=disable_rows(legacy=True))))
    assert not window._motor_disable_pending
    assert window.motor_enable_label.text() == "使能\n失能指令已发出"
    assert "旧协议无使能位" in window.feedback_read_label.text()
    assert "已逐台确认" not in window.feedback_read_label.text()
    assert "can0#7" in window.motor_enable_label.toolTip()
    assert window.enable_motors_button.isEnabled()


def test_mixed_108_and_confirmed_109_does_not_claim_every_drive_confirmed(window):
    rows = disable_rows(pending=())
    rows[-1].update(requires_confirmation=False, protocol_profile="legacy", state="unknown")
    window._render_snapshot(snapshot(enabled=False, state="connected", report=dict(state="unknown", motors=rows)))
    assert not window._motor_disable_pending
    assert "已逐台确认" not in window.motor_enable_label.text()
    assert "旧协议无使能位" in window.feedback_read_label.text()


def test_older_snapshot_without_disable_evidence_retains_normal_controls(window):
    window._render_snapshot(snapshot())
    assert window.motor_enable_label.text() == "使能\n是"
    assert window.start_button.isEnabled() and window.reset_button.isEnabled()
    window._render_snapshot(snapshot(state="connected"))
    assert window.manual_move_button.isEnabled()


def test_top_level_pending_with_no_motor_rows_still_explains_unknown_status(window):
    window._render_snapshot(snapshot(enabled=False, report=dict(state="pending", motors=None)))
    assert window._motor_disable_pending
    assert "工位失能反馈待确认" in window.motor_enable_label.toolTip()
    assert window.disable_button.isEnabled()


def drive_feedback(**changes):
    return dict(bus="can1", motor_id=7, joint_name="wrist_pitch", lro_protocol_profile="v1.03",
                drive_enabled=True, timestamp_s=99.99, feedback_age_s=.01, **changes)


@pytest.mark.parametrize("saved", [None, "confirmed"])
def test_fresh_actual_enabled_overrides_missing_or_old_confirmed_stop_result(window, saved):
    report = dict(state=saved, motors=disable_rows(pending=())) if saved else None
    data = snapshot(enabled=False, state="connected", report=report)
    data.update(timestamp_s=100., motors=[drive_feedback()])
    window._render_snapshot(data)
    assert window._motor_disable_pending
    assert window.motor_enable_label.text() == "使能\n失能待确认"
    assert "can1#7：仍使能（新鲜实测反馈）" in window.motor_enable_label.toolTip()
    assert "已逐台确认" not in window.motor_enable_label.text()
    assert window.feedback_read_label.text() == "状态读取\n失能待确认 · 控制下发已阻止"
    assert window.disable_button.isEnabled() and window.manual_disable_button.isEnabled()
    assert not window.enable_motors_button.isEnabled() and not window.reset_button.isEnabled()
    assert not window.manual_move_button.isEnabled()


@pytest.mark.parametrize("changes,server_time", [
    ({"feedback_age_s": .101}, 100.),
    ({"feedback_age_s": None}, 100.),
    ({"feedback_age_s": -1.}, 100.),
    ({"timestamp_s": 0.}, 100.),
    ({"timestamp_s": None}, 100.),
    ({"timestamp_s": 100.01}, 100.),
    ({"timestamp_s": 99.}, 100.),  # Reported age cannot hide an old timestamp.
    ({"drive_enabled": None}, 100.),
])
def test_expired_or_unknown_feedback_does_not_override_confirmed_result(window, changes, server_time):
    data = snapshot(enabled=False, state="connected", report=dict(state="confirmed", motors=disable_rows(pending=())))
    motor = drive_feedback()
    motor.update(changes)
    data.update(timestamp_s=server_time, motors=[motor])
    window._render_snapshot(data)
    assert not window._motor_disable_pending
    assert "已逐台确认" in window.motor_enable_label.text()
    assert "仍使能" not in window.motor_enable_label.toolTip()
    assert window.enable_motors_button.isEnabled()


def test_normal_enabled_station_is_not_mistaken_for_failed_disable(window):
    data = snapshot(enabled=True, state="connected")
    data.update(timestamp_s=100., motors=[drive_feedback()])
    window._render_snapshot(data)
    assert not window._motor_disable_pending
    assert window.motor_enable_label.text() == "使能\n是"
    assert window.manual_move_button.isEnabled() and window.reset_button.isEnabled()


def test_freshness_boundary_accepts_one_hundred_milliseconds_and_keeps_can_identity(window):
    data = snapshot(enabled=False, state="connected")
    motor = drive_feedback()
    motor.update(timestamp_s=99.9, feedback_age_s=.1)
    data.update(timestamp_s=100., motors=[motor])
    assert window._motor_disable_view(data)["fresh_enabled"] == ["can1#7"]
