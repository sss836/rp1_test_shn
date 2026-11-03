"""WP1 Qt text/draft checks. All timers and network entry points are intercepted."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import unittest
from unittest import mock
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication
from factory_hmi.desktop.app import FactoryMainWindow


def no_network(window):
    window.reconnect_timer=QTimer(window)
    window.snapshot_timer=QTimer(window)
    window.lease_timer=QTimer(window)
    window.websocket=QWebSocket()


class WP1DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=QApplication.instance() or QApplication([])

    def setUp(self):
        with mock.patch.object(FactoryMainWindow,'_setup_network',no_network), mock.patch.object(FactoryMainWindow,'_run_rest'):
            self.window=FactoryMainWindow('http://127.0.0.1:65534')
        self.rest_patcher=mock.patch.object(self.window,'_run_rest')
        self.rest_patcher.start()
        self.window.quick_page.timer.stop()
        self.window.sample_timer.stop()
        self.window.plc_page.stop()
        self.window.mujoco_completion.setChecked(False)

    def tearDown(self):
        self.window.pool.waitForDone(2000)
        self.app.processEvents()
        self.rest_patcher.stop()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

    def test_fault_text_distinguishes_request_feedback_and_no_safety_claim(self):
        for request,feedback,want in [('sent','unknown','实际失能反馈未知'),('failed','unknown','失能请求失败'),('sent','confirmed_mock','mock反馈已失能')]:
            with self.subTest(request=request,feedback=feedback):
                response=dict(playback_stop='stopped',disable_request=request,disable_feedback=feedback,safe_confirmed=False)
                self.window._render_aging_status({'playback':None,'fault_response':response})
                text=self.window.completion_label.text()
                self.assertIn('播放已停止',text)
                self.assertIn(want,text)
                self.assertIn('机械安全未确认',text)
                self.assertNotIn('已安全',text)

    def test_end_text_does_not_promise_automatic_parking(self):
        self.assertEqual(self.window.stop_button.text(),'结束当前测试')
        self.window._render_aging_status({'playback':{'completion':{'phase':'holding'}}})
        self.assertIn('保持位置，未自动失能',self.window.completion_label.text())
        self.assertIn('按配置',self.window.stop_button.toolTip())
        with mock.patch.object(self.window.quick_page,'runner'):
            self.window.quick_page._end()
        self.assertIn('取决于本次配置',self.window.quick_page.detail.text())

    def test_next_draft_cannot_change_queued_start_or_current_record_id(self):
        self.window._last_snapshot={'config_revision':'reviewed-version'}
        self.window.robot_id.setText('RP1.3-UPPER-001')
        self.window.platform_station_id.setText('B-WP1')
        self.window.platform_case_code.setCurrentIndex(self.window.platform_case_code.findData('OTHER'))
        self.window.test_id.setProperty('record_id','previous-accepted-record')
        with mock.patch.object(self.window,'_sample_selection_allowed',return_value=True), \
             mock.patch.object(self.window,'_sample_catalog_online',return_value=True), \
             mock.patch('factory_hmi.desktop.app.QInputDialog.getText',return_value=('synthetic draft',True)), \
             mock.patch.object(self.window,'_save_local_bench'), \
             mock.patch.object(self.window,'_run_rest') as run, \
             mock.patch.object(self.window.client,'playback_start') as start:
            self.window._start_playback()
            operation=run.call_args.args[1]
            original_speed=self.window.speed.value()
            self.window.speed.setValue(.5)
            self.window.robot_id.setText('next-object')
            self.window._last_snapshot={'config_revision':'changed-later'}
            operation()
            self.assertEqual(start.call_args.kwargs['robot_id'],'RP1.3-UPPER-001')
            self.assertEqual(start.call_args.kwargs['speed'],original_speed)
            self.assertEqual(start.call_args.kwargs['expected_config_revision'],'reviewed-version')
            self.assertEqual(self.window.test_id.property('record_id'),'previous-accepted-record')

    def test_receipt_identity_cannot_overwrite_later_fault_state(self):
        payload={'start_receipt_recovered':True,'result':{'playback':{'test_id':'accepted-run'},'state':'running'},'snapshot':{'state':'fault'}}
        with mock.patch.object(self.window,'_render_snapshot') as render:
            self.window._playback_started(payload)
            render.assert_called_once_with({'state':'fault'})
        self.assertEqual(self.window.test_id.property('record_id'),'accepted-run')
