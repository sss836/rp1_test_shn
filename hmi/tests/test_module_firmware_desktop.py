import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
from unittest.mock import Mock,patch
from shiboken6 import isValid
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import Qt,QCoreApplication,QEvent
import pytest
from factory_hmi.desktop.motor_maintenance_dialog import MODULE_UPGRADE, SELECTED_UPGRADE, MotorMaintenanceDialog


@pytest.fixture
def dialog():
    app=QApplication.instance() or QApplication([])
    state={'identity':('executor',1)}
    run=Mock();client=Mock()
    d=MotorMaintenanceDialog(client,run,lambda:state['identity']);d.timer.stop()
    row=dict(index=0,module='left_arm',joint_name='关节',interface='can9',motor_id=77,model='LRO_8462',ota_request_id=0x1099,ota_response_id=0x109a,
             observation={'raw_firmware': '1.0.8'}, cached_releases=[dict(version='1.0.10',model='LRO_8462',sha256='c'*64,release_id='a'*32)])
    callback=run.call_args;callback.args[2]({'result':dict(targets=[row],job=dict(active=False,status='idle'))});callback.kwargs['on_finished']()
    yield d,run,client,state,app
    if isValid(d):d.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def complete(run,result):
    call=run.call_args;call.args[2]({'result':result});call.kwargs['on_finished']()


def test_offline_maintenance_reads_before_authorizing_explicit_update(dialog):
    parent, run, client, state, app = dialog
    state['identity'] = None
    d = MotorMaintenanceDialog(client, run, lambda: state['identity'], parent)
    d.timer.stop()
    assert d.source.currentText() == '本机'
    run.call_args.args[1]()
    client.motor_maintenance_status.assert_called_once_with(local=True)
    result = {'targets': parent.rows, 'job': {'active': False, 'status': 'idle'}}
    complete(run, result)
    assert d.table.rowCount() == 1 and d.reload_button.isEnabled()
    assert '当前没有写入权限' in d.scope_hint.text() and not d.preview_button.isEnabled()
    d.preview()
    client.motor_maintenance_action.assert_not_called()

    d.reload()
    complete(run, dict(result, write_authorized=True))
    d.select_all_button.click()
    assert d.preview_button.isEnabled()
    d.preview()
    run.call_args.args[1]()
    client.motor_maintenance_action.assert_called_once_with(
        'preview', {'selections': [{'index': 0, 'release_id': 'a' * 32}]}, local=True)
    complete(run, {'preview_id': 'local-preview', 'targets': []})
    with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Cancel):
        d.start()
    assert client.motor_maintenance_action.call_count == 1
    with patch.object(QMessageBox, 'exec', return_value=QMessageBox.StandardButton.Yes):
        d.start()
    run.call_args.args[1]()
    client.motor_maintenance_action.assert_called_with(
        'start', {'preview_id': 'local-preview', 'confirmed': True}, local=True)


def test_refresh_versions_reads_locally_then_reloads_fresh_results(dialog):
    parent, run, client, state, app = dialog
    state['identity'] = None
    d = MotorMaintenanceDialog(client, run, lambda: state['identity'], parent, scope=SELECTED_UPGRADE)
    d.timer.stop()
    timed_out = dict(parent.rows[0], firmware_generation=None,
                     observation={'software': '0.1.8', 'errors': {'0x31': '版本查询超时'}})
    result = {'targets': [timed_out], 'write_authorized': True,
              'job': {'active': False, 'status': 'idle'}}
    run.call_args.args[1]()
    client.motor_maintenance_status.assert_called_once_with(local=True)
    complete(run, result)
    assert d.table.item(0, 9).text() == '完整版本读取超时'
    assert '1 台完整固件版本读取超时' in d.scope_hint.text()
    client.refresh_motor_versions.assert_not_called()

    d.preview_value = {'preview_id': 'outdated'}
    d.reload_button.click()
    assert d.preview_value is None and not d.reload_button.isEnabled()
    run.call_args.args[1]()
    client.refresh_motor_versions.assert_called_once_with()
    receipt = {'request_id': 'query-1', 'state': 'queued', 'completed_motors': 0, 'total_motors': 1}
    complete(run, receipt)
    assert '0/1 台' in d.scope_hint.text()
    assert not d.table.isEnabled() and not d.preview_button.isEnabled()

    d.poll()
    run.call_args.args[1]()
    client.motor_version_refresh_status.assert_called_once_with()
    complete(run, dict(receipt, state='complete', completed_motors=1))
    assert '正在载入本机版本结果' in d.scope_hint.text()
    d.poll()
    run.call_args.args[1]()
    assert client.motor_maintenance_status.call_count == 2
    release = dict(version='5.0.0.109', model='LRO_8462', sha256='c'*64, release_id='b'*32)
    refreshed = dict(timed_out, firmware_generation=108, observation={'raw_firmware': '5.0.0.108'},
                     upgrade_109_releases=[release])
    complete(run, dict(result, targets=[refreshed]))
    assert d.reload_button.isEnabled() and d.table.isEnabled()
    assert d.table.item(0, 8).text() == '5.0.0.108 · 已核实 108'
    assert d.checkable_rows() == [0] and d.select_all_button.isEnabled()
    client.motor_maintenance_action.assert_not_called()


def test_refresh_deadline_releases_ui_for_an_explicit_retry(dialog):
    d, run, client, state, app = dialog
    d.reload_button.click()
    complete(run, {'request_id': 'query-1', 'state': 'queued', 'completed_motors': 0, 'total_motors': 1})
    d._version_refresh_deadline = 0
    d.poll()
    assert '等待本机版本结果超时' in d.scope_hint.text()
    assert d.reload_button.isEnabled()
    client.motor_version_refresh_status.assert_not_called()


def test_terminal_poll_clears_update_choice_when_scan_connection_closed(dialog):
    d, run, client, state, app = dialog
    d.select_all_button.click()
    assert d.preview_button.isEnabled()
    stale = dict(d.rows[0], observation={"raw_firmware": "1.0.8", "status": "stale"},
                 cached_releases=[])
    d.show_job({"job_id": "fixture", "active": True, "status": "queued"})
    d.poll()
    complete(run, {"targets": [stale], "scan_required": True,
                   "job": {"job_id": "fixture", "active": False, "status": "version_unknown"}})
    assert not d.preview_button.isEnabled()
    assert d.reload_button.isEnabled()
    assert "已过期" in d.table.item(0, 8).text()
    assert "重新扫描" in d.scope_hint.text()


def test_local_numeric_108_selection_and_signed_ota_choice(dialog):
    parent, run, client, state, _app = dialog
    state['identity'] = None
    d = MotorMaintenanceDialog(client, run, lambda: state['identity'], parent, scope=SELECTED_UPGRADE)
    d.timer.stop()
    release = dict(version='5.0.0.109', model='LRO_8462', sha256='c'*64, release_id='b'*32)
    row = parent.rows[0]
    local = dict(row, firmware_generation=None, confirmed_firmware_generation=108,
                 firmware_confirmation_source='local_numeric_evidence',
                 eligibility_error='缺少适用的签名 OTA 兼容映射',
                 observation={'software': '0.1.8', 'errors': {'0x31': '版本查询超时'}},
                 cached_releases=[release], upgrade_109_releases=[])
    strict = dict(row, index=1, motor_id=78, firmware_generation=108,
                  observation={'raw_firmware': '5.0.0.108'}, upgrade_109_releases=[release])
    newer = dict(row, index=2, motor_id=79, firmware_generation=109,
                 observation={'raw_firmware': '5.0.0.109'})
    unknown = dict(row, index=3, motor_id=80, observation=local['observation'])
    result = {'targets': [local, strict, newer, unknown], 'write_authorized': True,
              'job': {'active': False, 'status': 'idle'}}
    complete(run, result)

    assert d.table.item(0, 8).text() == '数值 0.1.8 · 108（本地核实）'
    assert d.table.item(0, 9).text() == '缺少签名固件或 OTA 映射'
    assert '0X31' in d.table.item(0, 9).toolTip() and '签名 OTA' in d.table.item(0, 9).toolTip()
    assert d.table.cellWidget(0, 6).count() == 0
    assert d.table.item(3, 9).text() == '完整版本读取超时'
    assert d.select_all_button.text() == '全选 108' and d.checkable_rows() == [0, 1]
    d.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert d.selected_rows() == [0] and not d.preview_button.isEnabled()
    d.select_all_button.click()
    assert d.selected_rows() == [0, 1] and not d.preview_button.isEnabled()
    count = run.call_count
    d.preview()
    assert run.call_count == count
    d.clear_selection_button.click()
    d.table.item(1, 0).setCheckState(Qt.CheckState.Checked)
    assert d.preview_button.isEnabled()
    d.preview()
    run.call_args.args[1]()
    client.motor_maintenance_action.assert_called_once_with(
        'preview', {'mode': SELECTED_UPGRADE, 'selections': [{'index': 1, 'release_id': 'b'*32}]}, local=True)
    complete(run, {'preview_id': 'strict-only', 'targets': []})

    d.scope.setCurrentIndex(d.scope.findData(None))
    assert not d.table.cellWidget(0, 6).isEnabled() and d.table.cellWidget(0, 6).count() == 0

    d.scope.setCurrentIndex(d.scope.findData(SELECTED_UPGRADE))
    d.reload()
    signed_local = dict(local, eligibility_error=None, upgrade_109_releases=[release])
    result = dict(result, targets=[signed_local, strict, newer])
    complete(run, result)
    d.clear_selection_button.click()
    d.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert d.preview_button.isEnabled() and d.table.item(0, 9).text() == '有差异'
    d.preview()
    run.call_args.args[1]()
    client.motor_maintenance_action.assert_called_with(
        'preview', {'mode': SELECTED_UPGRADE, 'selections': [{'index': 0, 'release_id': 'b'*32}]}, local=True)
    preview_target = dict(signed_local, before=local['observation'], manifest=release)
    complete(run, {'preview_id': 'numeric-approved', 'targets': [preview_target]})

    def inspect_confirmation(box):
        assert '数值 0.1.8 → 5.0.0.109' in box.informativeText()
        return QMessageBox.StandardButton.Cancel

    with patch.object(QMessageBox, 'exec', inspect_confirmation):
        d.start()
    d.scope.setCurrentIndex(d.scope.findData(MODULE_UPGRADE))
    assert d.selected_rows() == [0, 1] and d.preview_button.isEnabled()
    assert '109 的 1 台将跳过' in d.scope_hint.text()


def test_busy_local_view_cannot_cancel_another_modes_job(dialog):
    d, run, client, state, app = dialog
    d.source.setCurrentIndex(0)
    complete(run, {'targets': [], 'write_authorized': True,
                   'job': {'active': True, 'status': 'other_account', 'targets': []}})
    assert not d.cancel_button.isEnabled() and d.source.isEnabled()
    d.source.setCurrentIndex(1)
    run.call_args.args[1]()
    client.motor_maintenance_status.assert_called_once_with()


def test_preview_all_or_subset_and_edit_invalidates_confirmation(dialog):
    d,run,client,state,app=dialog
    assert d.table.item(0,2).text()=='can9' and d.table.item(0,3).text()=='77'
    assert '1.0.10' in d.table.cellWidget(0,6).currentText()
    d.preview();operation=run.call_args.args[1];operation()
    assert client.motor_maintenance_action.call_args.args==('preview',{'selections':[dict(index=0,release_id='a'*32)]})
    complete(run,dict(preview_id='fixture',targets=[]))
    assert d.preview_value is not None
    d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    assert not d.preview_button.isEnabled() and d.preview_value is None
    count = run.call_count
    d.preview()
    assert run.call_count == count
    d.select_all_button.click()
    assert d.selected_rows() == [0]
    assert d.table.item(0, 9).text() == '有差异'


def test_start_requires_confirmation_and_download_does_not_write(dialog):
    d,run,client,state,app=dialog
    d.preview_value=dict(preview_id='fixture')
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.No):d.start()
    client.motor_maintenance_action.assert_not_called()
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Yes):d.start()
    run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args==('start',dict(preview_id='fixture',confirmed=True))


@pytest.mark.parametrize('response', ['ff0201', 'ff020200bcf53ec2'])
def test_power_popup_human_statement_and_readback_are_independent(dialog,response):
    d,run,client,state,app=dialog
    job=dict(job_id='fixture',active=True,status='awaiting_power',targets=[dict(index=0,status='awaiting_power',power_cycle_required=True,human_acknowledged_at=None,actual_version_verified=False,write_receipt={'selfcheck_response':response,'reset_sent':True})])
    with patch.object(QMessageBox,'information') as prompt:
        d.show_job(job);d.show_job(job)
        assert prompt.call_count==1 and prompt.call_args.args[1]=='请统一重新上下电'
    assert d.ack_button.isEnabled() and not d.read_button.isEnabled()
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):d.acknowledge()
    run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args==('confirm-power-and-readback',{'job_id':'fixture','confirmed':True})
    job['targets'][0]['human_acknowledged_at']='synthetic human statement'
    complete(run,job)
    assert d.read_button.isHidden() and not d.job['targets'][0]['actual_version_verified']
    assert client.motor_maintenance_action.call_count==1


def test_dialog_destroy_account_change_and_poll_never_write(dialog):
    d,run,client,state,app=dialog
    d.preview();call=run.call_args
    state['identity']=('executor',2)
    call.args[2]({'result':dict(preview_id='stale')});call.kwargs['on_finished']()
    assert d.preview_value is None
    d.poll();assert d.preview_value is None
    d.preview();call=run.call_args
    d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    call.args[2]({'result':dict(preview_id='stale')});call.kwargs['on_finished']()
    client.motor_maintenance_action.assert_not_called()


def test_operator_summary_exposes_review_evidence_without_internal_session_fields():
    text=MotorMaintenanceDialog.describe(dict(actor=dict(client_session='internal-pointer'),status='awaiting_power',targets=[dict(module='left_arm',joint_name='joint',interface='can9',motor_id=77,ota_request_id=0x1099,ota_response_id=0x109a,model='LRO_8462',before=dict(raw_firmware='1.0.8'),manifest=dict(version='1.0.10',software_version='1.0.10',sha256='c'*64,source_url='https://fixture.invalid',scope='synthetic'),human_acknowledged_at=None,actual_version_verified=False)]))
    assert '等待统一重新上下电' in text and '1.0.8' in text and '1.0.10' in text and 'c'*64 in text
    assert '尚未读取' in text and '尚未通过' in text and 'internal-pointer' not in text


def test_cleanup_failure_remains_visible_and_does_not_claim_resource_release():
    text=MotorMaintenanceDialog.describe(dict(status='interrupted',cleanup_quarantined=True,cleanup_error='synthetic journal failure',targets=[]))
    assert '收尾未完成' in text and '资源保持隔离' in text and 'synthetic journal failure' in text


def test_108_to_109_scope_sends_only_approved_108_targets(dialog):
    parent, run, client, state, app = dialog
    legacy = parent.rows[0]
    release = dict(version='fixture-109', model='LRO_8462', sha256='b'*64, release_id='b'*32)
    first = dict(legacy, firmware_generation=108, observation={'raw_firmware': 'fixture-108'},
                 upgrade_109_releases=[release])
    other_108 = dict(first, index=1, motor_id=78, joint_name='关节 2')
    already = dict(legacy, index=2, motor_id=79, joint_name='关节 3',
                   firmware_generation=109, observation={'raw_firmware': 'fixture-109'})
    unknown = dict(legacy, index=3, motor_id=80, joint_name='关节 4', observation={'software': '1.0.8'})
    d = MotorMaintenanceDialog(client, run, lambda:state['identity'], parent,
                               selected_motor=(1, 'can9', 78, 'LRO_8462'), scope=SELECTED_UPGRADE)
    d.timer.stop()
    idle = {'active': False, 'status': 'idle'}
    complete(run, {'targets': [first, other_108, already, unknown], 'job': idle})
    assert d.selected_rows() == [1]
    assert not d.table.item(2, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
    assert '108/109 未核实' in d.table.item(3, 8).text()
    d.preview()
    run.call_args.args[1]()
    assert client.motor_maintenance_action.call_args.args == (
        'preview', {'mode': SELECTED_UPGRADE, 'selections': [{'index': 1, 'release_id': 'b'*32}]})
    complete(run, {'preview_id': 'selected', 'targets': []})
    d.scope.setCurrentIndex(d.scope.findData(MODULE_UPGRADE))
    assert d.preview_value is None
    assert d.selected_rows() == [0, 1]
    assert not d.preview_button.isEnabled() and '身份未核实' in d.scope_hint.text()

    d.reload()
    complete(run, {'targets': [first, other_108, already], 'job': idle, 'configuration_sha256': 'd'*64})
    assert d.preview_button.isEnabled() and '109 的 1 台将跳过' in d.scope_hint.text()
    assert not d.table.item(0, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
    d.preview()
    run.call_args.args[1]()
    assert client.motor_maintenance_action.call_args.args == (
        'preview', {'mode': MODULE_UPGRADE, 'configuration_sha256': 'd'*64, 'selections': [
            {'index': 0, 'release_id': 'b'*32}, {'index': 1, 'release_id': 'b'*32}]})
    complete(run, {'preview_id': 'whole', 'targets': [],
                   'skipped_targets': [dict(already, before=already['observation'], status='already_109')]})
    assert '跳过已是 109 的电机：关节 3 · can9 ID 79' in d.details.toPlainText()


def test_failed_update_stays_visible_after_refresh_without_success_power_prompt(dialog):
    d, run, client, state, app = dialog
    row = d.rows[0]
    other = dict(row, index=1, motor_id=78)
    job = {'job_id': 'failed-connect', 'active': False, 'status': 'failed',
           'error': '升级握手失败，未发送擦除或固件数据',
           'targets': [dict(row, status='failed', bytes_written=0, power_cycle_required=True),
                       dict(other, status='not_run', bytes_written=0)]}
    value = {'targets': [row, other], 'job': job, 'write_authorized': True}
    with patch.object(QMessageBox, 'information') as information:
        d._apply_status(value)
        information.assert_not_called()
        assert '本次更新失败' in d.job_summary.text()
        assert '（0 字节）' in d.job_summary.text() and '未执行 1 台' in d.job_summary.text()
        assert d.table.item(1, 9).text() == '未执行'
        d._apply_status(value, after_version_refresh=True)
        d.select_all_button.click()
        assert '升级握手失败' in d.job_summary.text() and not d.job_summary.isHidden()
        information.assert_not_called()
        d.show_job({'job_id': 'new-task', 'active': True, 'status': 'writing',
                    'targets': [dict(row, status='writing', bytes_written=32)]})
        assert '本次更新失败' not in d.job_summary.text()
        assert '（32 字节）' in d.job_summary.text()


def test_power_prompt_only_follows_successful_device_selfcheck(dialog):
    d, run, client, state, app = dialog
    row = dict(d.rows[0], status='awaiting_power', bytes_written=100,
               power_cycle_required=True, write_receipt={'selfcheck_response': 'ff0201', 'reset_sent': True})
    job = {'job_id': 'write-complete', 'active': True, 'status': 'awaiting_power', 'targets': [row]}
    with patch.object(QMessageBox, 'information') as information:
        d.show_job(job)
        d.show_job(job)
        information.assert_called_once()
        assert '校验应答已按产线脚本接受' in information.call_args.args[2]
        assert '校验应答已按产线脚本接受' in d.job_summary.text()


def test_batch_ui_waits_for_every_write_then_one_click_starts_readback(dialog):
    d,run,client,state,app=dialog
    first=dict(d.rows[0],index=0,status='written_awaiting_batch_power',bytes_written=100,
               write_finished_at='fixture',write_receipt={'selfcheck_response':'ff020200bcf53ec2'})
    second=dict(first,index=1,status='pending',bytes_written=0,write_finished_at=None,write_receipt=None)
    job=dict(job_id='batch',active=True,power_cycle_scope='batch',status='writing',targets=[first,second])
    with patch.object(QMessageBox,'information') as message:
        d.show_job(job)
        assert message.call_count==0 and not d.ack_button.isEnabled()
        assert '已写完 1/2 台' in d.job_summary.text() and '待写 1 台' in d.job_summary.text()
        second.update(write_receipt={'selfcheck_response':'ff0201'},write_finished_at='fixture',bytes_written=100)
        for row in job['targets']:row['status']='awaiting_power'
        job['status']='awaiting_power'
        d.show_job(job);d.show_job(job)
        assert message.call_count==1 and d.read_button.isHidden() and d.ack_button.isEnabled()
        assert '已写完 2/2 台' in d.job_summary.text() and '待写 0 台' in d.job_summary.text()
    with patch.object(QMessageBox,'question') as question:
        d.ack_button.click();d.ack_button.click()
        question.assert_not_called()
    assert d.pending and not d.ack_button.isEnabled()
    run.call_args.args[1]()
    client.motor_maintenance_action.assert_called_once_with('confirm-power-and-readback',{'job_id':'batch','confirmed':True})
    job['status']='readback_requested'
    for row in job['targets']:row['status']='readback_requested'
    complete(run,job)
    assert not d.ack_button.isEnabled() and not d.pending
