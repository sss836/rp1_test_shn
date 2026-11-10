import os
from unittest import mock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox
from factory_hmi.desktop.app import FactoryMainWindow, launcher_window_fd
from factory_hmi.quick_profiles import catalog

@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def no_network(window):
    window.reconnect_timer=QTimer(window)
    window.snapshot_timer=QTimer(window)
    window.lease_timer=QTimer(window)
    window.websocket=QWebSocket()


@pytest.fixture
def window(app):
    with mock.patch.object(FactoryMainWindow,'_setup_network',no_network), mock.patch.object(FactoryMainWindow,'_run_rest'):
        window=FactoryMainWindow('http://127.0.0.1:65534')
    window.quick_page.timer.stop();window.sample_timer.stop()
    yield window
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def test_new_window_button_requests_independent_launcher_child(window, monkeypatch):
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        monkeypatch.setenv("RP1_HMI_NEW_WINDOW_FD", str(write_fd))
        window._new_window_fd = launcher_window_fd()
        assert window._new_window_fd == write_fd
        client = window.client
        window.new_window_button.click()
        assert os.read(read_fd, 1) == b"+"
        assert window.client is client
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_quick_page_selection_and_explicit_changed_mapping_prepare(window):
    page=window.quick_page
    page._catalog({'items':catalog()})
    with mock.patch.object(page.client,'quick_prepare',return_value={}) as prepare:
        page.select('waist_hip')
        assert page.test_case.text() == 'UNIT-AGING-003 · 腰髋老化'
        operation=page.runner.call_args.args[1]
        operation()
        assert prepare.call_args.args==('waist_hip',['can1','can0','can1','can0'])
        page._received({'phase':'ready','detail':'ready'});page._finished()
        assert page.run_button.isEnabled()
        page.primary_can.setCurrentText('can2')
        assert not page.run_button.isEnabled()
        page._prepare();page.runner.call_args.args[1]()
        assert prepare.call_args.args==('waist_hip',['can1','can2','can1','can2'])
    window.quick_nav_button.click()
    assert window.tabs.currentIndex()==4
    assert window.quick_nav_button.isChecked()


def test_quick_run_duration_local_sample_and_uncertain_response_cancellation(window):
    page=window.quick_page
    page.selected='left_arm';page._phase='ready';page.sample.setText('任意离线标识')
    with mock.patch.object(page.client,'quick_start',return_value={}) as start:
        page.start(2.5,True)
        page.runner.call_args.args[1]()
        assert start.call_args.args==(2.5,'任意离线标识')
        assert start.call_args.kwargs=={'physical_estop_confirmed':True,'power_source':'external',
                                      'data_domain':'RESEARCH','bench_id':'','operator_id':'',
                                      'completion_mode':'configured'}
    page._start_failed(TimeoutError('simulated response loss'))
    assert page.runner.call_args.args[1]==page.client.quick_abort


def test_switching_part_updates_suggested_sample_but_preserves_operator_input(window):
    page=window.quick_page;page._catalog({'items':catalog()})
    page.select('left_arm');page._finished();page._phase='ready'
    assert page.sample.text()=='本地验证-left_arm'
    page.select('right_arm');page._finished();page._phase='ready'
    assert page.sample.text()=='本地验证-right_arm'
    page.sample.setText('现场真实序号-123')
    page.select('waist_hip')
    assert page.sample.text()=='现场真实序号-123'


def test_confirmed_start_rejection_is_not_replaced_by_end_and_reason_survives_poll(window):
    from factory_hmi.client import GatewayHTTPError
    page=window.quick_page
    error=GatewayHTTPError('rejected',status_code=409,detail='PLC 通信未就绪')
    with mock.patch.object(page,'runner') as run:
        page._start_failed(error)
        run.assert_not_called()
    page._status_received({'phase':'fault','detail':'PLC 通信未就绪','failure':'PLC 通信未就绪'})
    assert page.detail.text()=='PLC 通信未就绪'
    page._status_received({'phase':'ended','detail':'已结束，电机已失能'})
    assert 'PLC 通信未就绪' in page.detail.text()


def test_external_default_and_explicit_plc_mode_are_sent_by_the_ui(window):
    page=window.quick_page
    assert page.power_source.currentData()=='external'
    assert 'PLC 在线状态不影响' in page.power_note.text()
    page.selected='left_arm';page._phase='ready';page.sample.setText('样机')
    page.power_source.setCurrentIndex(1)
    assert page.power_source.currentData()=='plc'
    with mock.patch.object(page.client,'quick_start') as start:
        page.start(1,True)
        page.runner.call_args.args[1]()
        assert start.call_args.kwargs['power_source']=='plc'
    assert not page.power_source.isEnabled()


def test_arm_dialog_defaults_to_downward_finish_and_requires_pose_confirmation(window):
    page = window.quick_page
    page.selected = 'left_arm'

    def confirm_dialog(dialog):
        checkboxes = dialog.findChildren(QCheckBox)
        safety = next(box for box in checkboxes if '物理急停安全' in box.text())
        use_model = next(box for box in checkboxes if 'MuJoCo' in box.text())
        pose = dialog.findChild(QCheckBox, 'quickMujocoPoseConfirmed')
        confirm = dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
        assert use_model.isChecked()
        safety.setChecked(True)
        assert not confirm.isEnabled()
        pose.setChecked(True)
        assert confirm.isEnabled()
        return QDialog.DialogCode.Accepted

    with mock.patch.object(QDialog, 'exec', confirm_dialog), mock.patch.object(page, 'start') as start:
        page._ask_run()
    start.assert_called_once_with(1.0, True, True, True)
