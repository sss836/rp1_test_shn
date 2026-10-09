from __future__ import annotations

import os
import unittest
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication

from factory_hmi.desktop.app import FactoryMainWindow


def _setup_network_without_io(window: FactoryMainWindow) -> None:
    window.reconnect_timer = QTimer(window)
    window.snapshot_timer = QTimer(window)
    window.lease_timer = QTimer(window)
    window.websocket = QWebSocket()


class FactoryHmiResponsiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        patcher = mock.patch.object(
            FactoryMainWindow,
            "_setup_network",
            _setup_network_without_io,
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        self.window = FactoryMainWindow("http://127.0.0.1:65534")

    def tearDown(self) -> None:
        self.window.deleteLater()

    def test_logged_in_operator_can_start_local_test_without_upload_profile(self) -> None:
        self.window._platform_operator = "qa-operator"
        self.window.robot_id.setText("RP1.3-UPPER-01")
        self.window.platform_test_profile.setCurrentIndex(0)
        with mock.patch.object(self.window, "_run_rest") as run, mock.patch.object(self.window.client, "playback_start") as start:
            self.window._start_playback()
            run.assert_called_once()
            run.call_args.args[1]()
            self.assertIsNone(start.call_args.kwargs["campaign_id"])
            self.assertEqual(start.call_args.kwargs["operator_id"], "qa-operator")

    def test_incomplete_selected_upload_profile_still_blocks_start(self) -> None:
        self.window._platform_operator = "qa-operator"
        self.window.robot_id.setText("RP1.3-UPPER-01")
        self.window.platform_test_profile.addItem("Incomplete", {"ready": False, "missing": ["campaign_id"]})
        self.window.platform_test_profile.setCurrentIndex(1)
        with mock.patch.object(self.window, "_run_rest") as run, mock.patch("factory_hmi.desktop.app.QMessageBox.warning") as warning:
            self.window._start_playback()
            run.assert_not_called()
            warning.assert_called_once()

    def test_presence_label_and_logout_follow_actual_gateway_state(self) -> None:
        self.window._render_platform_presence({"operator": "qa-operator", "presence": {"online": True}})
        self.assertIn("监控在线", self.window.platform_identity_label.text())
        self.assertTrue(self.window.platform_logout_button.isEnabled())
        self.window._render_platform_presence({"operator": "qa-operator", "presence": {"online": False}})
        self.assertIn("重新登录", self.window.platform_identity_label.text())
        with mock.patch.object(self.window, "_run_rest") as run:
            self.window._platform_logout()
            self.assertEqual(run.call_args.args[1], self.window.client.platform_logout)
            run.call_args.args[2]({"result": {"operator": ""}})
        self.assertEqual(self.window._platform_operator, "")
        self.assertFalse(self.window.platform_logout_button.isEnabled())

    def test_primary_fault_and_critical_bus_diagnosis_are_visible(self) -> None:
        self.window._render_fault({"fault": {
            "message": "feedback stale 2.0s",
            "diagnoses": [
                {"severity": "warning", "suspected_component": "motor:2", "evidence": ["tracking error"]},
                {"severity": "critical", "suspected_component": "communication_bus:can0", "evidence": ["7 motors stale"]},
            ],
        }})
        text = self.window.global_fault.text()
        self.assertIn("feedback stale 2.0s", text)
        self.assertIn("communication_bus:can0", text)
        self.assertIn("7 motors stale", text)
        self.assertNotIn("疑似部件：motor:2", text)

    def test_fault_message_survives_missing_or_invalid_diagnoses(self) -> None:
        for diagnoses in (None, [], [None, "invalid"]):
            with self.subTest(diagnoses=diagnoses):
                self.window._render_fault({"fault": {"message": "driver failed", "diagnoses": diagnoses}})
                self.assertIn("driver failed", self.window.global_fault.text())
                self.assertEqual(self.window.global_fault.objectName(), "faultBarError")

    def test_fault_banner_clears_after_fault_is_removed(self) -> None:
        self.window._render_fault({"fault": "fault"})
        self.window._render_fault({"fault": None})
        self.assertIn("全局故障：无", self.window.global_fault.text())
        self.assertEqual(self.window.global_fault.objectName(), "faultBarOk")

    def test_supported_resolutions_exceed_required_shell_minimums(self) -> None:
        minimum = self.window.minimumSize()
        for width, height in ((1600, 900), (1600, 1000), (1920, 1080)):
            with self.subTest(size=(width, height)):
                self.assertLessEqual(minimum.width(), width)
                self.assertLessEqual(minimum.height(), height)
        self.assertEqual(self.window._nav_buttons[0].parent().width(), 216)
        self.assertEqual(self.window.centralWidget().layout().contentsMargins().top(), 0)
        self.assertEqual(self.window.tabs.minimumWidth(), 0)
        self.assertEqual(self.window.tabs.minimumHeight(), 0)

    def test_all_critical_controls_remain_in_their_page_hierarchy(self) -> None:
        controls = (
            (self.window.disconnect_button, 0),
            (self.window.disable_button, 1),
            (self.window.save_csv_button, 1),
            (self.window.next_run_button, 1),
            (self.window.manual_stop_button, 2),
            (self.window.manual_disable_button, 2),
        )
        for control, page_index in controls:
            with self.subTest(control=control.text()):
                ancestor = control.parentWidget()
                stack_page = self.window.tabs.widget(page_index)
                while ancestor is not None and ancestor is not stack_page:
                    ancestor = ancestor.parentWidget()
                self.assertIs(ancestor, stack_page)


if __name__ == "__main__":
    unittest.main()
