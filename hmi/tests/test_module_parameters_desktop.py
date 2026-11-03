from copy import deepcopy
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from shiboken6 import isValid
import pytest
from factory_hmi.desktop.motor_parameters_dialog import MotorParametersDialog
from test_module_parameters import make,preview

@pytest.fixture
def dialog():
    app=QApplication.instance() or QApplication([]);identity={'value':'executor'};run=Mock();client=Mock()
    d=MotorParametersDialog(client,run,lambda:identity['value']);d.timer.stop()
    call=run.call_args;call.args[2]({'result':{'targets':[dict(index=0,module='left',joint_name='joint',interface='can-fixture',motor_id=1,model='LRO_5550')],'job':{'active':False,'status':'idle'}}});call.kwargs['on_finished']()
    yield d,run,client,identity,app
    if isValid(d):d.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

def test_distinct_read_and_write_no_value_editor(dialog):
    d,run,client,*_=dialog
    assert not d.table.item(0,3).flags()&Qt.ItemFlag.ItemIsEditable
    d.table.cellWidget(0,5).addItem('synthetic-template','a'*32)
    d.table.cellWidget(0,5).setCurrentIndex(0)
    d.read();run.call_args.args[1]();assert client.motor_parameter_action.call_args.args==('read',{'selections':[{'index':0,'template_id':'a'*32}]})
    assert d.preview_value is None and not d.start_button.isEnabled()

def test_confirmation_cancel_does_not_start(dialog):
    d,run,client,*_=dialog;d.preview_value={'preview_id':'x'}
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.No):d.start()
    client.motor_parameter_action.assert_not_called()

def test_preview_human_units_and_power_statement_distinct(tmp_path,dialog):
    task,selections,*_=make(tmp_path);p=preview(task,selections)
    d,run,client,*_=dialog;text=d.describe(p)
    assert 'N·m/A' in text and '非运行增益' in text and '100' not in text.split('kt：')[1].split('→')[0]
    row=deepcopy(p['targets'][0]);row.update(power_cycle_required=True,status='awaiting_power',human_acknowledged_at=None)
    j=dict(job_id='test',active=True,status='awaiting_power',targets=[row])
    with patch.object(QMessageBox,'information') as message:
        d.show_job(j);d.show_job(j);assert message.call_count==1
    assert d.ack_button.isEnabled() and not d.verify_button.isEnabled()
    row['human_acknowledged_at']='synthetic';d.show_job(j)
    assert d.verify_button.isEnabled() and '再次回读核验：未通过' in d.details.toPlainText()

def test_account_change_drops_preview_and_receipt(dialog):
    d,run,client,identity,*_=dialog;d.preview_value={'preview_id':'x'};d.details.setPlainText('old account evidence')
    identity['value']='other';d.poll()
    assert d.preview_value is None and d.table.rowCount()==0 and d.details.toPlainText()==''

def test_deleted_dialog_late_callback_safe(dialog):
    d,run,client,*_=dialog
    d.table.cellWidget(0,5).addItem('synthetic-template','a'*32);d.table.cellWidget(0,5).setCurrentIndex(0)
    d.read();call=run.call_args
    d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    call.args[2]({'result':{}});call.kwargs['on_finished']()


def test_missing_template_and_selection_have_clear_reasons(dialog):
    from PySide6.QtWidgets import QHeaderView
    d,run,client,*_=dialog
    assert not d.read_button.isEnabled() and '模板' in d.step_hint.text()
    assert d.table.cellWidget(0,5).placeholderText()=='先获取适用模板'
    count=run.call_count;d.read();assert run.call_count==count
    assert d.write_group.isHidden()
    assert d.table.horizontalHeader().sectionResizeMode(1)==QHeaderView.ResizeMode.Stretch
    assert d.table.horizontalHeader().sectionResizeMode(5)==QHeaderView.ResizeMode.Stretch
    d.table.cellWidget(0,5).addItem('synthetic-template','a'*32);d.table.cellWidget(0,5).setCurrentIndex(0)
    assert d.read_button.isEnabled()
    d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    assert not d.read_button.isEnabled() and '勾选' in d.step_hint.text()
    client.motor_parameter_action.assert_not_called()


def test_comparison_is_readonly_and_changed_selection_requires_fresh_read(tmp_path,dialog):
    from PySide6.QtWidgets import QAbstractItemView
    task,selections,*_=make(tmp_path)
    p=preview(task,selections);job=task.snapshot()
    d,run,client,*_=dialog
    d.table.cellWidget(0,5).addItem('synthetic-template','a'*32);d.table.cellWidget(0,5).setCurrentIndex(0)
    d.read();call=run.call_args
    call.args[2]({'result':job});call.kwargs['on_finished']()
    assert d.comparison.rowCount()==2
    assert 'N·m/A' in d.comparison.item(0,2).text() and '1.74' in d.comparison.item(0,3).text()
    assert d.comparison.editTriggers()==QAbstractItemView.EditTrigger.NoEditTriggers
    assert d.preview_button.isEnabled()
    d.preview_value=p;d.controls();assert d.start_button.isEnabled()
    d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    assert d.preview_value is None and not d.start_button.isEnabled() and not d.preview_button.isEnabled()
    task.cancel()


def test_closed_parameter_dialog_drops_pending_reply(dialog):
    d,run,client,*_=dialog
    d.reload();call=run.call_args
    d.show();d.timer.start();d.reject()
    assert d._closed and not d.timer.isActive()
    call.args[2]({'result':{'targets':[],'job':{}}});call.kwargs['on_finished']()
    assert d.table.rowCount()==1


def test_failed_new_read_cannot_reuse_old_job_for_write_preview(dialog):
    d,run,client,*_=dialog
    combo=d.table.cellWidget(0,5);combo.addItem('synthetic-template','a'*32);combo.setCurrentIndex(0)
    d.show_job(dict(job_id='previous-read',mode='read',active=False,status='checked',targets=[]))
    d._read_revision=d._selection_revision;d.controls();assert d.preview_button.isEnabled()
    d.read();call=run.call_args
    call.kwargs['on_finished']()  # failed operation: no success callback
    assert not d.preview_button.isEnabled() and d.preview_value is None
