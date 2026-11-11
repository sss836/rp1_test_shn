"""Offscreen local record controls. Network and file-manager calls are mocked."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QDate, QEvent, QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QTextEdit
from factory_hmi.desktop.app import FactoryMainWindow


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
        value = FactoryMainWindow("http://invalid.test")
        value.quick_page.timer.stop()
        value.sample_timer.stop()
        yield value
        value._history_search_timer.stop()
        value.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_presets_custom_dates_and_sn_query(window):
    today = QDate.currentDate()
    window.history_period.setCurrentIndex(window.history_period.findData("week"))
    assert window.history_from.date() == today.addDays(-6)
    query = window._history_date_query()
    assert datetime.fromisoformat(query["start_at"]).date() == today.addDays(-6).toPython()
    assert datetime.fromisoformat(query["end_at"]).date() == today.addDays(1).toPython()
    window.history_period.setCurrentIndex(window.history_period.findData("month"))
    assert window.history_from.date() == today.addMonths(-1)
    window.history_period.setCurrentIndex(window.history_period.findData("today"))
    assert window.history_from.date() == today
    window.history_period.setCurrentIndex(window.history_period.findData("custom"))
    assert window.history_from.isEnabled() and window.history_to.isEnabled()
    window.history_sn.setText("000012345678901234567890")
    assert window._history_date_query()["sn"] == "000012345678901234567890"
    window.history_period.setCurrentIndex(0)
    assert window._history_date_query() == {"sn": "000012345678901234567890"}


def test_late_reply_cannot_replace_new_filter_and_export_uses_all_matching_rows(window, tmp_path):
    window._run_rest.reset_mock()
    window._refresh_execution_history()
    old_request = window._run_rest.call_args
    window.history_sn.setText("000123")
    window._refresh_execution_history()
    latest = window._run_rest.call_args
    old_request.args[2]({"items": [{"test_id": "stale"}], "total": 1})
    assert not window._execution_history
    latest.args[2]({"items": [{"test_id": "fresh", "sample_id": "000123", "started_at": "2026-10-28T01:00:00Z"}], "total": 1007})
    latest.kwargs["on_finished"]()
    assert window._execution_history[0]["test_id"] == "fresh"
    assert window.execution_history_table.item(0, 1).text() == "000123"
    assert "1007" in window.history_count_label.text()
    assert window.history_next_button.isEnabled()
    path = tmp_path / "list.csv"
    with patch("factory_hmi.desktop.app.QFileDialog.getSaveFileName", return_value=(str(path), "CSV")), patch.object(window.client, "export_execution_history") as export:
        window._export_history_csv()
        call = window._run_rest.call_args
        call.args[1]()
        export.assert_called_once_with(str(path), sn="000123")
        assert "offset" not in export.call_args.kwargs and "limit" not in export.call_args.kwargs
        call.kwargs["on_finished"]()


def test_open_directory_uses_selected_test_and_os_file_manager(window, tmp_path):
    window._execution_history_received({"items": [{"test_id": "TEST-0001", "sample_id": "000123"}]})
    window.execution_history_table.selectRow(0)
    window._open_execution_directory()
    call = window._run_rest.call_args
    with patch.object(window.client, "execution_location") as location:
        call.args[1]()
        location.assert_called_once_with("TEST-0001")
    with patch("factory_hmi.desktop.app.QDesktopServices.openUrl", return_value=True) as open_url:
        call.args[2]({"result": {"directory": str(tmp_path)}})
        assert open_url.call_args.args[0].toLocalFile() == str(tmp_path)


def test_invalid_range_invalidates_outstanding_reply(window):
    window._refresh_execution_history()
    old = window._run_rest.call_args
    window.history_period.setCurrentIndex(window.history_period.findData("custom"))
    window.history_from.setDate(window.history_to.date().addDays(1))
    old.args[2]({"items": [{"test_id": "stale"}], "total": 1})
    assert not window._execution_history
    assert "开始日期不能晚于" in window.history_count_label.text()
    assert not window.export_history_csv_button.isEnabled()


def test_filtered_empty_list_never_targets_hidden_current_execution(window):
    window.test_id.setText("hidden-current-run")
    window._history_filters = {"sn": "different-module"}
    window._execution_history_received({"items": [], "total": 0})
    assert window._selected_test_id() == ""
    assert not window.export_bundle_button.isEnabled()
    assert not window.submit_review_button.isEnabled()


def test_factory_history_and_summary_show_trial_result_and_measured_excess(window):
    test_id = "LOCAL_UNIT-AGING-006_synthetic"
    window._execution_history_received({"items": [{
        "test_id": test_id, "data_domain": "FACTORY", "factory_quality_status": "failed",
        "factory_quality_basis": "trial",
    }]})
    assert window.execution_history_table.item(0, 8).text() == "试行不通过"
    summary = {"timing": {}, "factory_quality": {
        "status": "failed", "schema_version": "factory-quality-v2",
        "profile": {"basis_kind": "trial", "label": "UNIT-AGING-006 左臂（7轴）"},
        "execution_checks": [{"key": "sample_completeness", "label": "采样完整率",
                              "value": .99, "limit": .95, "unit": "ratio", "passed": True}],
        "metrics": [{"motor_label": "腕偏航", "motor_id": 6, "sample_count": 250,
                     "valid_metric_sample_count": 250, "checks": [{
                         "label": "绝对力矩 P99.9", "value": 8.25, "limit": 7.0,
                         "unit": "N·m", "passed": False, "exceedance": 1.25,
                     }]}],
    }}
    rendered = []

    def capture(dialog):
        rendered.append(dialog.findChild(QTextEdit).toPlainText())

    with patch("factory_hmi.desktop.app.QDialog.exec", new=capture):
        window._show_execution_summary(test_id, {"result": summary})
    text, = rendered
    assert "工厂质量筛查：试行不通过" in text
    assert "采样完整率：99.00%；要求 ≥ 95.00%；满足" in text
    assert "腕偏航（ID 6） · 有效测量 250/250" in text
    assert "实测 8.25 N·m；上限 7 N·m；超限（超出 1.25 N·m）" in text
