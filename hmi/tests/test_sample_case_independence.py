"""Independent sample/case selection; every request and device is mocked."""
import json
from unittest.mock import Mock, patch

from test_sample_selector import application, window
from test_wp1_command_identity import armed, payload
from factory_hmi.gateway.app import _PlatformCoordinator
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
from factory_hmi.sync.uploader import PlatformError


def select_arm003(window, profiles=None):
    window._platform_operator = 'qa'
    window.limb.setCurrentText('left_arm')
    window._platform_samples_received({'online': True, 'samples': [
        {'code': 'RP1.3-ARM-003', 'target_part_code': 'ARM', 'running': False},
        {'code': 'RP1.3-ARM-001', 'target_part_code': 'ARM', 'running': False}]})
    window.robot_id.setText('RP1.3-ARM-003')
    window.platform_station_id.setText('RD-01')
    window._platform_options_received({'profiles': profiles or [], 'cases': [
        {'code': 'REL-ARM-002', 'enabled': True, 'version_id': 'arm-version', 'version': 'CSV-20260918'},
        {'code': 'REL-ARM-003', 'enabled': True, 'version_id': 'unplanned-version', 'version': 'v2'},
        {'code': 'REL-LEG-001', 'enabled': True, 'version_id': 'leg-version'},
        {'code': 'REL-ARM-004', 'enabled': False, 'version_id': 'disabled'},
        {'code': 'REL-ARM-005', 'enabled': True, 'version_id': None}]})
    window.platform_case_code.setCurrentIndex(window.platform_case_code.findData('REL-ARM-002'))


def start_payload(window):
    with patch.object(window, '_save_local_bench'), patch.object(window, '_run_rest') as run, \
         patch.object(window.client, 'playback_start') as start, \
         patch('factory_hmi.desktop.app.QMessageBox.warning') as warning:
        window._start_playback()
        warning.assert_not_called()
        run.assert_called_once()
        run.call_args.args[1]()
        return start.call_args.kwargs


def test_published_cases_are_visible_by_part_without_any_sample_plan(window):
    select_arm003(window)
    codes = [window.platform_case_code.itemData(i) for i in range(window.platform_case_code.count())]
    assert codes == [None, 'REL-ARM-002', 'REL-ARM-003', 'OTHER']
    assert window.platform_test_profile.count() == 1
    assert window.platform_test_profile.currentData() is None
    assert '计划可选' in window.platform_context_status.text()
    # A stale plan must not reintroduce a case disabled in the live catalog.
    window._platform_profiles.append({'case_code': 'REL-ARM-004', 'sample_id': 'RP1.3-ARM-003'})
    window._refresh_case_choices()
    assert window.platform_case_code.findData('REL-ARM-004') == -1


def test_arm003_runs_arm002_locally_without_borrowing_arm001_context(window):
    select_arm003(window, [{'case_code': 'REL-ARM-002', 'sample_id': 'RP1.3-ARM-001',
                            'campaign_id': 'arm001-plan', 'asset_id': 'arm001-asset', 'ready': True}])
    actual = start_payload(window)
    assert actual['robot_id'] == 'RP1.3-ARM-003'
    assert actual['test_case_id'] == 'REL-ARM-002'
    assert actual['test_case_version'] == 'CSV-20260918'
    assert actual['recording_scope'] == 'local' and actual['record'] is True
    assert actual['bench_id'] == 'RD-01'
    for field in ('campaign_id', 'cycle_id', 'segment_id', 'asset_id', 'configuration_id', 'test_case_version_id'):
        assert actual[field] is None


def test_selecting_a_stale_foreign_plan_never_rewrites_actual_sample(window):
    select_arm003(window)
    window.platform_test_profile.addItem('stale', {'sample_id': 'RP1.3-ARM-001',
        'test_case_id': 'REL-ARM-002', 'campaign_id': 'wrong', 'asset_id': 'wrong', 'ready': True})
    window.platform_test_profile.setCurrentIndex(1)
    assert window.robot_id.text() == 'RP1.3-ARM-003'
    actual = start_payload(window)
    assert actual['robot_id'] == 'RP1.3-ARM-003' and actual['recording_scope'] == 'local'
    assert actual['asset_id'] is None and actual['campaign_id'] is None


def test_changing_sample_clears_optional_context_instead_of_blocking_case(window):
    profile = {'case_code': 'REL-ARM-002', 'test_case_id': 'REL-ARM-002',
               'sample_id': 'RP1.3-ARM-001', 'ready': True}
    select_arm003(window, [profile])
    window.robot_id.setText('RP1.3-ARM-001')
    assert window.platform_test_profile.count() == 2
    assert window.platform_test_profile.currentIndex() == 0
    window.platform_test_profile.setCurrentIndex(1)
    window.robot_id.setText('RP1.3-ARM-003')
    assert window.platform_test_profile.currentData() is None
    assert start_payload(window)['test_case_id'] == 'REL-ARM-002'


def test_an_incompatible_part_is_rejected_even_if_inserted_into_the_combo(window):
    select_arm003(window)
    window.platform_case_code.addItem('wrong leg case', 'REL-LEG-001')
    window.platform_case_code.setCurrentIndex(window.platform_case_code.count()-1)
    with patch.object(window, '_run_rest') as run, patch('factory_hmi.desktop.app.QMessageBox.warning') as warning:
        window._start_playback()
        run.assert_not_called()
        assert '部位' in warning.call_args.args[1]


def test_case_catalog_request_is_authenticated_read_only_and_filters_unpublished(tmp_path):
    client = Mock()
    worker = ReliabilityUploaderWorker(Outbox(tmp_path/'outbox.sqlite3'), client)
    assert worker.case_options() == []
    client._call.assert_not_called()
    worker._identity = 'qa'
    ready = {'code': 'REL-ARM-002', 'enabled': True, 'version_id': 'published'}
    client._call.return_value = [ready, {'code': 'draft', 'enabled': True},
                                 {'code': 'disabled', 'enabled': False, 'version_id': 'v'}]
    assert worker.case_options() == [ready]
    client._call.assert_called_once_with('GET', '/api/v1/setup/catalog', service_auth=False, expect='list')


def test_optional_plan_failure_does_not_hide_independent_case_catalog():
    with patch.dict('os.environ', {'RP1_FACTORY_PLATFORM_MODE': 'reliability_v1'}), \
         patch('factory_hmi.sync.reliability_platform.load_test_profiles', return_value=[]):
        coordinator = _PlatformCoordinator(Mock())
        coordinator._identity = 'qa'
        coordinator._uploader = Mock()
        coordinator._uploader.case_options.return_value = [{'code': 'REL-ARM-002'}]
        coordinator._uploader.context_profiles.side_effect = PlatformError('plan unavailable')
        result = coordinator.options()
    assert result['cases'] == [{'code': 'REL-ARM-002'}] and result['profiles'] == []
    assert result['context_error'] == 'plan unavailable'


def test_actual_service_records_real_sample_and_selected_case_without_formal_context(armed):
    p = payload(armed, 'independent-arm003')
    p.update(test_case_id='REL-ARM-002', robot_id='RP1.3-ARM-003', bench_id='RD-01',
             test_case_version='CSV-20260918', recording_scope='local', data_domain='RESEARCH')
    armed.start(**p)
    context = json.loads(next(armed.record_root.glob('*.context.json')).read_text())
    armed.stop(reason='synthetic test only')
    assert armed.controller.wait(3)
    armed.wait_for_reports(5)
    assert context['sample_id'] == 'RP1.3-ARM-003' and context['actual_object'] == 'RP1.3-ARM-003'
    assert context['test_case_id'] == 'REL-ARM-002' and context['test_case_version'] == 'CSV-20260918'
    assert context['recording_scope'] == 'local' and context['execution_code'] == ''
    for field in ('asset_id', 'campaign_id', 'configuration_id', 'test_case_version_id'):
        assert context[field] is None
    manifest = json.loads(next(armed.report_manager.executions_root.glob('*/manifest.json')).read_text())
    assert manifest['sample_id'] == 'RP1.3-ARM-003'
    assert manifest['test_case_id'] == 'REL-ARM-002'
    assert manifest['recording_scope'] == 'local'
    assert manifest['execution_code'] == ''
