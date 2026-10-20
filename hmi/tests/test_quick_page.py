import os
from unittest import mock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import QTimer, QCoreApplication, QEvent
from PySide6.QtWebSockets import QWebSocket
from PySide6.QtWidgets import QApplication
from factory_hmi.desktop.app import FactoryMainWindow
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


def test_new_window_button_uses_same_gateway_and_independent_client_session(window,app):
    with mock.patch.object(FactoryMainWindow,'_setup_network',no_network), mock.patch.object(FactoryMainWindow,'_run_rest'):
        window.new_window_button.click()
    assert len(window._child_windows)==1
    child=window._child_windows[0]
    assert child.isVisible()
    assert child.client.base_url==window.client.base_url
    assert child.client.client_id!=window.client.client_id
    child.quick_page.timer.stop();child.sample_timer.stop()
    child.close()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    assert len(window._child_windows)==0


def test_quick_page_selection_and_explicit_changed_mapping_prepare(window):
    page=window.quick_page
    page._catalog({'items':catalog()})
    with mock.patch.object(page.client,'quick_prepare',return_value={}) as prepare:
        page.select('waist_hip')
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
        assert start.call_args.kwargs=={'physical_estop_confirmed':True,'power_source':'external'}
    page._start_failed(TimeoutError('simulated response loss'))
    assert page.runner.call_args.args[1]==page.client.quick_end


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
