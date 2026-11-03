"""No network or hardware calls: validate explicit model-pose confirmation."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from unittest.mock import patch
import pytest
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QMessageBox
from factory_hmi.desktop.app import FactoryMainWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    def no_network(value):
        value.reconnect_timer=QTimer(value); value.snapshot_timer=QTimer(value)
        value.lease_timer=QTimer(value); value.websocket=QWebSocket()
    with patch.object(FactoryMainWindow,'_setup_network',no_network), patch.object(FactoryMainWindow,'_run_rest'):
        value=FactoryMainWindow('http://invalid.test')
        for timer in value.findChildren(QTimer): timer.stop()
        value.robot_id.setText('ISOLATED-ARM')
        value._active_limb='left_arm'
        yield value
        value.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def test_selected_model_requires_confirmation_and_cancel_does_not_start(window):
    assert window.mujoco_completion.isChecked()
    window._run_rest.reset_mock()
    with patch('factory_hmi.desktop.app.QMessageBox.question',return_value=QMessageBox.StandardButton.Cancel) as prompt:
        window._start_playback()
    prompt.assert_called_once()
    assert '肘关节目标' in prompt.call_args.args[2]
    window._run_rest.assert_not_called()


def test_confirmation_reaches_local_start_without_a_parameter_file(window):
    window._run_rest.reset_mock()
    with patch('factory_hmi.desktop.app.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes), patch.object(window.client,'playback_start') as start:
        window._start_playback()
        window._run_rest.call_args.args[1]()
    assert start.call_args.kwargs['completion_mode']=='mujoco_hanging'
    assert start.call_args.kwargs['parking_pose_confirmed'] is True


def test_factory_selected_model_is_sent_independently_of_power_source(window):
    page=window.quick_page
    page.selected='left_arm'; page._phase='ready'; page.sample.setText('ISOLATED-QUICK')
    with patch.object(page.client,'quick_start') as start:
        page.start(1.,True,True,True)
        page.runner.call_args.args[1]()
    assert start.call_args.kwargs['completion_mode']=='mujoco_hanging'
    assert start.call_args.kwargs['parking_pose_confirmed'] is True
    assert start.call_args.kwargs['power_source']=='external'
