from copy import deepcopy
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from shiboken6 import isValid
import pytest
from factory_hmi.client import GatewayHTTPError
from factory_hmi.desktop.motor_parameters_dialog import MotorParametersDialog
from test_module_parameters import make,preview,record

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
    assert d.preview_value is None and not d.preview_button.isEnabled()

def test_confirmation_cancel_does_not_start(dialog):
    d,run,client,*_=dialog;d.preview_value={'preview_id':'x'}
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.No):d.start()
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
    assert d.read_button.isEnabled() and '模板' in d.step_hint.text()
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


def test_comparison_is_readonly_and_checkbox_scopes_update(tmp_path,dialog):
    from PySide6.QtWidgets import QAbstractItemView
    task,selections,*_=make(tmp_path)
    p=preview(task,selections);job=task.snapshot()
    d,run,client,*_=dialog
    d.configuration_sha256=job['configuration_sha256']
    d.table.cellWidget(0,5).addItem('synthetic-template','a'*32);d.table.cellWidget(0,5).setCurrentIndex(0)
    d.read();call=run.call_args
    call.args[2]({'result':job});call.kwargs['on_finished']()
    assert d.comparison.rowCount()==2
    assert 'N·m/A' in d.comparison.item(0,2).text() and '1.74' in d.comparison.item(0,3).text()
    assert d.comparison.editTriggers()==QAbstractItemView.EditTrigger.NoEditTriggers
    assert d.preview_button.isEnabled()
    assert d.table.item(0,10).text() == '有差异 2 项'
    assert '1.74' in d.table.item(0,9).text()
    d.preview_value=p
    d.update_selected()
    run.call_args.args[1]()
    client.refresh_motor_versions.assert_called_once()
    assert d._comparison_read_job_id is None  # old comparison cannot directly authorize a write
    run.call_args.kwargs['on_finished']()
    d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    assert d.preview_value is None and not d.preview_button.isEnabled()
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
    d._comparison_read_job_id='previous-read'
    receipt=dict(d.rows[0], template_record=record(), before={}, items=[dict(key='kt', expected_raw=[174], tolerance_raw=1, status='mismatch', write_status='not_written', actual={'status':'read','raw':[100]})])
    d.show_job(dict(job_id='previous-read',mode='read',active=False,status='checked_with_differences',targets=[receipt]))
    assert d.preview_button.isEnabled()
    d.read();call=run.call_args
    call.kwargs['on_finished']()  # failed operation: no success callback
    assert d.preview_button.isEnabled() and d.preview_value is None
    assert d.update_selections()==[]  # retry is enabled, old read is not reusable


def test_selected_motor_template_query_and_comparison_stay_in_scope(dialog):
    parent, run, client, identity, app = dialog
    first = dict(parent.rows[0], observation={'raw_firmware': 'fixture-108'})
    selected = dict(first, index=1, interface='can-second', observation={'raw_firmware': 'fixture-109'})
    d = MotorParametersDialog(client, run, lambda:identity['value'], parent,
                              selected_motor=(1, 'can-second', 1, 'LRO_5550'))
    d.timer.stop()
    idle = {'active': False, 'status': 'idle'}
    call = run.call_args
    call.args[2]({'result': {'targets': [first, selected], 'job': idle}})
    call.kwargs['on_finished']()
    assert d._selected() == [1]
    assert d.table.item(1, 7).text() == 'fixture-109'

    def catalog(index):
        if index == 0:
            raise GatewayHTTPError('模板查询失败', status_code=409, detail='版本证据缺失')
        return {'result': {'templates': [record()]}}

    client.motor_parameter_templates.side_effect = catalog
    d.templates()
    call = run.call_args
    call.args[2](call.args[1]())
    call.kwargs['on_finished']()
    client.motor_parameter_templates.assert_called_once_with(1)
    assert d.read_button.isEnabled()
    assert not d.table.cellWidget(0, 5).currentData()
    d.read()
    call = run.call_args
    call.args[1]()
    assert client.motor_parameter_action.call_args.args == ('read', {'selections': [{'index': 1, 'template_id': 'a'*32}]})
    call.kwargs['on_finished']()

    d.select_all_button.click()
    d.templates()
    call = run.call_args
    call.args[2](call.args[1]())
    call.kwargs['on_finished']()
    assert 'can-fixture ID 1：版本证据缺失' in d.details.toPlainText()
    assert d.read_button.isEnabled()
    d.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert d.read_button.isEnabled()
    d.clear_selection_button.click()
    assert not d.templates_button.isEnabled() and not d.read_button.isEnabled()


def test_parameter_scope_does_not_follow_reused_index(dialog):
    parent, run, client, identity, app = dialog
    d = MotorParametersDialog(client, run, lambda:identity['value'], parent,
                              selected_motor=(0, 'can-fixture', 1, 'LRO_5550'))
    d.timer.stop()
    call = run.call_args
    call.args[2]({'result': {'targets': [dict(parent.rows[0], motor_id=2)], 'job': {'active': False, 'status': 'idle'}}})
    call.kwargs['on_finished']()
    assert not d._selected()
    assert not d.templates_button.isEnabled()



def complete_version_preparation(d, run, client):
    d.show()
    call=run.call_args
    call.args[1]()
    client.refresh_motor_versions.assert_called()
    call.args[2]({'result':{'request_id':'fresh','state':'complete'}})
    call.kwargs['on_finished']()
    call=run.call_args
    targets=[dict(row,observation={'status':'known'}) for row in d.rows]
    call.args[2]({'result':{'targets':targets,'job':{'active':False,'status':'idle'},
                           'configuration_sha256':d.configuration_sha256,'write_authorized':True}})
    call.kwargs['on_finished']()
    return run.call_args

def test_read_reference_checks_selected_motor_without_template_or_write_preview(dialog):
    d, run, client, *_ = dialog
    row = dict(d.rows[0], firmware_generation=108)
    d.reload()
    call = run.call_args
    call.args[2]({'result': {'targets': [row, dict(row, index=1, motor_id=2, firmware_generation=None)],
                             'job': {'active': False, 'status': 'idle'}, 'configuration_sha256': 'e'*64}})
    call.kwargs['on_finished']()
    assert d._selected() == [0]
    assert d.read_reference_button.isEnabled() and d.read_button.isEnabled()
    d.select_all_button.click()
    assert d.read_reference_button.isEnabled() and '版本需刷新' in d.step_hint.text()
    d.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)
    d.read_reference_button.click()
    call = complete_version_preparation(d,run,client)
    call.args[1]()
    assert client.motor_parameter_action.call_args.args == (
        'read-reference', {'indices': [0], 'configuration_sha256': 'e'*64})
    client.motor_parameter_templates.assert_not_called()

    items = [
        {'key': 'kt', 'expected_raw': [174], 'tolerance_raw': 0, 'status': 'reference_difference',
         'actual': {'status': 'read', 'raw': [100]}},
        {'key': 'kp', 'expected_raw': None, 'tolerance_raw': 0, 'status': 'read',
         'actual': {'status': 'read', 'raw': [0, 500]}},
    ]
    receipt = dict(row, before={'raw_firmware': 'fixture-109'}, items=items,
                   reference_source='synthetic', reference_sha256='b'*64)
    call.args[2]({'result': {'mode': 'read_reference', 'status': 'reference_checked_with_findings',
                             'active': False, 'job_id': 'reference-only', 'targets': [receipt]}})
    call.kwargs['on_finished']()
    assert d.comparison.horizontalHeaderItem(3).text() == '109 脚本参考值'
    assert d.comparison.isColumnHidden(4) and d.comparison.isColumnHidden(6)
    assert d.comparison.item(0, 3).text() == '1.74 N·m/A'
    assert d.comparison.item(0, 5).text() == '与参考有差异'
    assert d.comparison.item(1, 3).text() == '—'
    assert d.comparison.item(1, 5).text() == '已记录实测值'
    assert '差异仅供检查' in d.details.toPlainText()
    assert d.preview_button.isEnabled() and d.write_group.isHidden()
    assert not d.update_selections()  # reference findings still are not a write preview
    d.update_selected()
    run.call_args.args[1]()
    assert client.refresh_motor_versions.call_count==2


def test_logged_out_parameter_dialog_uses_local_status_import_and_read(dialog):
    d, run, client, identity, _ = dialog
    identity['value'] = None
    d.poll()
    call = run.call_args
    call.args[1]()
    client.motor_parameter_status.assert_called_once_with(local=True)
    row = dict(index=0,module='left',joint_name='joint',interface='can-fixture',motor_id=1,
               model='LRO_5550',firmware_generation=108)
    call.args[2]({'result': {'targets': [row], 'configuration_sha256': 'e'*64,
                             'job': {'active': False, 'status': 'idle'}, 'write_authorized': True}})
    call.kwargs['on_finished']()
    assert d.read_reference_button.isEnabled() and d.read_button.isEnabled()
    with patch.object(QFileDialog,'getOpenFileName',return_value=('/fixture/catalog.json','')):
        d.import_button.click()
    call = run.call_args
    call.args[1]()
    call.kwargs['on_finished']()
    client.motor_parameter_import.assert_called_once_with('/fixture/catalog.json')
    d.read_reference_button.click()
    call = complete_version_preparation(d,run,client)
    call.args[1]()
    call.kwargs['on_finished']()
    client.motor_parameter_action.assert_called_once_with(
        'read-reference', {'indices': [0], 'configuration_sha256': 'e'*64}, local=True)
    client.motor_parameter_templates.return_value = {'result': {'templates': [record()]}}
    d.templates()
    call = run.call_args
    call.args[2](call.args[1]())
    call.kwargs['on_finished']()
    client.motor_parameter_templates.assert_called_once_with(0, local=True)
    d.read_button.click()
    call = complete_version_preparation(d,run,client)
    call.args[2](call.args[1]())  # templates after fresh version, before read
    call.kwargs['on_finished']()
    call = run.call_args
    call.args[1]()
    call.kwargs['on_finished']()
    client.motor_parameter_action.assert_called_with(
        'read', {'selections': [{'index': 0, 'template_id': 'a'*32}],
                 'configuration_sha256': 'e'*64}, local=True)


def test_local_scope_available_while_logged_in_and_keeps_own_job(dialog):
    d, run, client, identity, _ = dialog
    assert d.source.currentText() == '测试平台'
    d.source.setCurrentIndex(0)
    call = run.call_args
    call.args[1]()
    client.motor_parameter_status.assert_called_once_with(local=True)
    job = {'job_id': 'local-read', 'active': True, 'mode': 'read_reference', 'status': 'reading',
           'targets': [], 'local': True}
    call.args[2]({'result': {'targets': [], 'job': job, 'write_authorized': True}})
    call.kwargs['on_finished']()
    assert not d.source.isEnabled() and d.cancel_button.isEnabled()
    identity['value'] = 'another-platform-account'
    d.poll()
    call = run.call_args
    call.args[2]({'result': {'job': job}})
    call.kwargs['on_finished']()
    assert d.job == job and d.cancel_button.isEnabled()
    d.cancel_button.click()
    call = run.call_args
    call.args[1]()
    call.kwargs['on_finished']()
    client.motor_parameter_action.assert_called_once_with('cancel', {'job_id': 'local-read'}, local=True)


def test_builtin_model_template_defaults_with_imported_alternative(dialog):
    from factory_hmi.core.production_parameters import production_record
    from test_production_parameters import obs
    d,run,client,*_=dialog
    builtin=production_record(obs(model='LRO_5550'))
    client.motor_parameter_templates.return_value={'result':{'templates':[builtin,record()]}}
    d.templates();call=run.call_args;call.args[2](call.args[1]());call.kwargs['on_finished']()
    combo=d.table.cellWidget(0,5)
    assert combo.currentData()==builtin['template']['template_id']
    assert combo.currentText()=='产线参数 · LRO_5550 · 109全量'
    d.clear_selection_button.click();assert d._selected()==[]
    d.select_all_button.click();assert d._selected()==[0]


def test_batch_parameter_completion_only_prompts_once(dialog,tmp_path):
    task,selections,*_=make(tmp_path,2);p=preview(task,selections)
    d,run,*_=dialog
    for row in p['targets']:
        row.update(power_cycle_required=True,status='written_awaiting_batch_power')
    job=dict(job_id='batch',mode='write',active=True,status='writing',targets=p['targets'])
    with patch.object(QMessageBox,'information') as message:
        d.show_job(job);assert message.call_count==0
        job['status']='awaiting_power';job['production_batch_power']=True
        for row in job['targets']:row['status']='awaiting_power'
        d.show_job(job);d.show_job(job);assert message.call_count==1
    task.cancel()
