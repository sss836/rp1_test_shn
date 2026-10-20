import os
import time
from unittest import mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from PySide6.QtWebSockets import QWebSocket
from factory_hmi.desktop.sample_selector import SampleSelector, sample_choices
from factory_hmi.desktop.app import FactoryMainWindow

SAMPLES = [
    {'code': 'RP1.3-ARM-001', 'target_part_code': 'ARM', 'running': False},
    {'code': 'RP1.3-ARM-002', 'target_part_code': 'ARM', 'running': True},
    {'code': 'RP1.3-SARM-001', 'target_part_code': 'SARM', 'running': False},
    {'code': 'RP1.3-LEG-001', 'target_part_code': 'LEG', 'running': False},
    {'code': 'RP1.3-WAISTHIP-001', 'target_part_code': 'WAISTHIP', 'running': False},
    {'code': 'RP1.3-LOWER-001', 'target_part_code': 'LOWER', 'running': False},
]

@pytest.fixture(scope='module')
def application():
    return QApplication.instance() or QApplication([])


def test_online_part_choices_busy_disabled_and_free_text_rejected(application):
    widget = SampleSelector()
    widget.update_choices(SAMPLES, 'left_arm', True)
    assert not widget.isEditable()
    assert [widget.itemText(i) for i in range(1, widget.count())] == ['RP1.3-ARM-001', 'RP1.3-ARM-002 在运行']
    assert not widget.model().item(2).isEnabled()
    widget.setText('RP1.3-ARM-001')
    widget.setText('RP1.3-ARM-002')
    assert widget.text() == 'RP1.3-ARM-001'
    widget.setText('UNKNOWN')
    assert widget.text() == 'RP1.3-ARM-001'
    widget.update_choices(SAMPLES, 'left_leg', True)
    assert widget.text() == ''
    assert widget.count() == 2
    widget.deleteLater()


def test_short_degree_and_waist_are_separate_from_full_limb_and_lower():
    assert [v['code'] for v in sample_choices(SAMPLES, 'right_short_arm')] == ['RP1.3-SARM-001']
    assert [v['code'] for v in sample_choices(SAMPLES, 'waist_hip')] == ['RP1.3-WAISTHIP-001']
    assert [v['code'] for v in sample_choices(SAMPLES, 'biped_waist')] == ['RP1.3-LOWER-001']


def test_offline_accepts_arbitrary_identifier_even_if_previously_busy(application):
    widget = SampleSelector()
    widget.update_choices(SAMPLES, 'left_arm', True)
    widget.update_choices(SAMPLES, 'left_arm', False)
    widget.setText('离线临时样机 a8-测试')
    assert widget.isEditable() and widget.selection_allowed()
    assert widget.text() == '离线临时样机 a8-测试'
    widget.deleteLater()


@pytest.fixture
def window(application):
    def network(target):
        target.reconnect_timer = QTimer(target)
        target.snapshot_timer = QTimer(target)
        target.lease_timer = QTimer(target)
        target.websocket = QWebSocket()
    with mock.patch.object(FactoryMainWindow, '_setup_network', network), mock.patch.object(FactoryMainWindow, '_run_rest'):
        value = FactoryMainWindow('http://127.0.0.1:65534')
    value.sample_timer.stop()
    yield value
    value.deleteLater()


def test_offline_start_detaches_cached_context_without_case_or_bench_gate(window):
    window.robot_id.setText('任意离线编号')
    window.platform_test_profile.addItem('Cached', {'sample_id': 'OLD', 'ready': False, 'campaign_id': 'stale'})
    window.platform_test_profile.setCurrentIndex(1)
    with mock.patch.object(window, '_save_local_bench'), mock.patch.object(window, '_run_rest') as run, mock.patch.object(window.client, 'playback_start') as start:
        window._start_playback()
        run.assert_called_once()
        run.call_args.args[1]()
        assert start.call_args.kwargs['robot_id'] == '任意离线编号'
        assert start.call_args.kwargs['recording_scope'] == 'local'
        assert start.call_args.kwargs['campaign_id'] is None


def test_catalog_expiry_logout_and_failed_refresh_restore_free_entry(window):
    window._platform_operator = 'qa'
    window._platform_samples_received({'online': True, 'samples': SAMPLES})
    assert not window.robot_id.isEditable()
    window._samples_checked_at = time.monotonic()-16
    window._refresh_sample_choices()
    assert window.robot_id.isEditable()
    window._platform_samples_received({'online': True, 'samples': SAMPLES})
    window._platform_samples_received({'online': False, 'error': 'simulated network loss'})
    assert window.robot_id.isEditable()
    window._platform_samples_received({'online': True, 'samples': SAMPLES})
    with mock.patch.object(window, '_run_rest'):
        window._render_platform_presence({'operator': ''})
    assert window.robot_id.isEditable()


def test_refresh_is_single_flight_and_busy_update_prevents_a_second_start(window):
    window._platform_operator = 'qa'
    window._platform_samples_received({'online': True, 'samples': SAMPLES})
    window.robot_id.setText('RP1.3-ARM-001')
    with mock.patch.object(window, '_run_rest') as run:
        window._refresh_platform_samples()
        window._refresh_platform_samples()
        assert run.call_count == 1
        run.call_args.kwargs['on_finished']()
        assert not window._samples_pending
    changed = [dict(item, running=True) if item['code']=='RP1.3-ARM-001' else item for item in SAMPLES]
    window._platform_samples_received({'online': True, 'samples': changed})
    with mock.patch.object(window, '_run_rest') as run, mock.patch('factory_hmi.desktop.app.QMessageBox.warning') as warning:
        window._start_playback()
        assert warning.called
        run.assert_not_called()


def test_gateway_samples_use_current_identity_and_release_online_mode_on_transport_loss():
    from factory_hmi.gateway.app import _PlatformCoordinator
    with mock.patch.dict(os.environ, {'RP1_FACTORY_PLATFORM_MODE':'reliability_v1'}):
        coordinator = _PlatformCoordinator(mock.Mock())
    uploader = mock.Mock()
    coordinator._uploader = uploader
    assert coordinator.samples()['online'] is False
    uploader.sample_options.assert_not_called()
    coordinator._identity = 'qa'
    uploader.sample_options.return_value = SAMPLES
    assert coordinator.samples() == {'online':True, 'samples':SAMPLES}
    uploader.sample_options.side_effect = TimeoutError('isolated timeout')
    assert coordinator.samples()['online'] is False


def test_unchanged_refresh_preserves_offline_edit_cursor(application):
    widget = SampleSelector()
    widget.update_choices(SAMPLES, 'left_arm', False)
    widget.setText('离线样机-编辑中')
    widget.lineEdit().setCursorPosition(2)
    widget.update_choices(SAMPLES, 'right_leg', False)
    assert widget.lineEdit().cursorPosition() == 2
    assert widget.text() == '离线样机-编辑中'
    widget.deleteLater()
