import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from copy import deepcopy
from unittest.mock import Mock,patch
from shiboken6 import isValid
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import Qt,QCoreApplication,QEvent
import pytest
from factory_hmi.desktop.motor_maintenance_dialog import MotorMaintenanceDialog


@pytest.fixture
def dialog():
    app=QApplication.instance() or QApplication([])
    state={'identity':('executor',1)}
    run=Mock();client=Mock()
    d=MotorMaintenanceDialog(client,run,lambda:state['identity']);d.timer.stop()
    row=dict(index=0,module='left_arm',joint_name='关节',interface='can9',motor_id=77,model='LRO_8462',ota_request_id=0x1099,ota_response_id=0x109a,
             cached_releases=[dict(version='1.0.10',model='LRO_8462',sha256='c'*64,release_id='a'*32)])
    callback=run.call_args;callback.args[2]({'result':dict(targets=[row],job=dict(active=False,status='idle'))});callback.kwargs['on_finished']()
    yield d,run,client,state,app
    if isValid(d):d.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def complete(run,result):
    call=run.call_args;call.args[2]({'result':result});call.kwargs['on_finished']()


def test_preview_all_or_subset_and_edit_invalidates_confirmation(dialog):
    d,run,client,state,app=dialog
    assert d.table.item(0,2).text()=='can9' and d.table.item(0,3).text()=='77'
    assert '1.0.10' in d.table.cellWidget(0,6).currentText()
    d.preview();operation=run.call_args.args[1];operation()
    assert client.motor_maintenance_action.call_args.args==('preview',{'selections':[dict(index=0,release_id='a'*32)]})
    complete(run,dict(preview_id='fixture',targets=[]))
    assert d.start_button.isEnabled()
    d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    assert not d.start_button.isEnabled() and d.preview_value is None
    d.preview();run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args[1]['selections']==[]


def test_start_requires_confirmation_and_download_does_not_write(dialog):
    d,run,client,state,app=dialog
    d.preview_value=dict(preview_id='fixture')
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No):d.start()
    client.motor_maintenance_action.assert_not_called()
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):d.start()
    run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args==('start',dict(preview_id='fixture',confirmed=True))


def test_power_popup_human_statement_and_readback_are_independent(dialog):
    d,run,client,state,app=dialog
    job=dict(job_id='fixture',active=True,status='awaiting_power',targets=[dict(index=0,status='awaiting_power',power_cycle_required=True,human_acknowledged_at=None,actual_version_verified=False)])
    with patch.object(QMessageBox,'information') as prompt:
        d.show_job(job);d.show_job(job)
        assert prompt.call_count==1 and prompt.call_args.args[1]=='请重新上下电'
    assert d.ack_button.isEnabled() and not d.read_button.isEnabled()
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):d.acknowledge()
    run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args[0]=='acknowledge'
    job['targets'][0]['human_acknowledged_at']='synthetic human statement'
    complete(run,job)
    assert d.read_button.isEnabled() and not d.job['targets'][0]['actual_version_verified']
    d.read_button.click();run.call_args.args[1]();assert client.motor_maintenance_action.call_args.args[0]=='readback'


def test_dialog_destroy_account_change_and_poll_never_write(dialog):
    d,run,client,state,app=dialog
    d.preview();call=run.call_args
    state['identity']=('executor',2)
    call.args[2]({'result':dict(preview_id='stale')});call.kwargs['on_finished']()
    assert d.preview_value is None
    d.poll();assert not d.start_button.isEnabled()
    d.preview();call=run.call_args
    d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    call.args[2]({'result':dict(preview_id='stale')});call.kwargs['on_finished']()
    client.motor_maintenance_action.assert_not_called()


def test_operator_summary_exposes_review_evidence_without_internal_session_fields():
    text=MotorMaintenanceDialog.describe(dict(actor=dict(client_session='internal-pointer'),status='awaiting_power',targets=[dict(module='left_arm',joint_name='joint',interface='can9',motor_id=77,ota_request_id=0x1099,ota_response_id=0x109a,model='LRO_8462',before=dict(raw_firmware='1.0.8'),manifest=dict(version='1.0.10',software_version='1.0.10',sha256='c'*64,source_url='https://fixture.invalid',scope='synthetic'),human_acknowledged_at=None,actual_version_verified=False)]))
    assert '等待人工重新上下电' in text and '1.0.8' in text and '1.0.10' in text and 'c'*64 in text
    assert '尚未读取' in text and '尚未通过' in text and 'internal-pointer' not in text


def test_cleanup_failure_remains_visible_and_does_not_claim_resource_release():
    text=MotorMaintenanceDialog.describe(dict(status='interrupted',cleanup_quarantined=True,cleanup_error='synthetic journal failure',targets=[]))
    assert '收尾未完成' in text and '资源保持隔离' in text and 'synthetic journal failure' in text
