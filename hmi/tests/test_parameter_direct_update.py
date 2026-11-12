"""Qt workflow with fake replies only, no hardware."""
from copy import deepcopy
from unittest.mock import Mock,patch
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from shiboken6 import isValid
import pytest
from factory_hmi.desktop.motor_parameters_dialog import MotorParametersDialog
from factory_hmi.core.production_parameters import production_record
from test_production_parameters import obs

class Runner:
    def __init__(self):self.queue=[]
    def __call__(self,label,operation,received,**kw):self.queue.append((operation,received,kw['on_finished']))
    def drain(self):
        for _ in range(30):
            if not self.queue:return
            op,received,finished=self.queue.pop(0)
            try:received(op())
            finally:finished()
        pytest.fail('request chain did not quiesce')

@pytest.fixture
def setup_dialog():
    app=QApplication.instance() or QApplication([])
    client=Mock();run=Runner();account={'value':None}
    models=['LRO_8462','LRO_6562','LRO_6562','LRO_6562','LRO_6562','LRO_5550','LRO_10062']
    rows=[dict(index=i,module='fixture',joint_name=f'joint-{i}',interface='can-fixture',motor_id=i+1,
               model=m,firmware_generation=109,observation=obs(i,model=m)) for i,m in enumerate(models)]
    records={i:production_record(r['observation']) for i,r in enumerate(rows)}
    status={'targets':rows,'job':{'active':False,'status':'idle'},'configuration_sha256':'f'*64,'write_authorized':True}
    client.motor_parameter_status.side_effect=lambda **kw:deepcopy(status)
    client.refresh_motor_versions.return_value={'request_id':'refresh-fixture','state':'complete'}
    client.motor_parameter_templates.side_effect=lambda i,**kw:{'templates':[deepcopy(records[i])]}
    jobs=[]
    def action(name,payload,**kw):
        if name=='read':
            targets=[]
            for selected in payload['selections']:
                i=selected['index'];rec=records[i]
                items=[dict(e,status='mismatch' if e['key']=='kt' else 'passed',write_status='not_written',actual={'status':'read','raw':[100] if e['key']=='kt' else e['expected_raw']}) for e in rec['template']['entries']]
                targets.append(dict(rows[i],before=rows[i]['observation'],template_record=rec,items=items,status='checked'))
            job=dict(mode='read',job_id='fresh-read',status='checked_with_differences',active=False,configuration_sha256='f'*64,targets=targets)
            jobs.append(job);status['job']=job;return deepcopy(job)
        if name=='read-reference':
            job=dict(mode='read_reference',job_id='reference',status='reference_checked',active=False,targets=[])
            status['job']=job;return job
        if name=='preview':
            wanted={r['index'] for r in payload['selections']}
            return dict(preview_id='new-preview',targets=[r for r in deepcopy(jobs[-1]['targets']) if r['index'] in wanted])
        if name=='start':return {'job_id':'write-fixture','mode':'write','status':'writing','active':True,'targets':[]}
        raise AssertionError(name)
    client.motor_parameter_action.side_effect=action
    d=MotorParametersDialog(client,run,lambda:account['value']);d.timer.stop();run.drain();d.show()
    yield d,run,client,status,records,action,account
    if isValid(d):d.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)

def names(client):return [c.args[0] for c in client.motor_parameter_action.call_args_list]

@pytest.mark.parametrize('indices',[[2],list(range(7))])
def test_single_click_model_match_read_preview_confirm_write(setup_dialog,indices):
    d,run,client,status,records,*_=setup_dialog
    d.select_rows(False)
    for i in indices:d.table.item(i,0).setCheckState(Qt.CheckState.Checked)
    d.job={'mode':'read_reference','active':False,'status':'reference_checked_with_findings'};d.controls()
    assert d.preview_button.isEnabled()
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Yes) as confirm:
        d.update_selected();d.update_selected();run.drain()
    assert client.refresh_motor_versions.call_count==1
    assert [c.args[0] for c in client.motor_parameter_templates.call_args_list]==indices
    assert names(client)==['read','preview','start'] and confirm.call_count==1
    read=client.motor_parameter_action.call_args_list[0]
    assert read.kwargs=={'local':True}
    assert read.args[1]['selections']==[{'index':i,'template_id':records[i]['template']['template_id']} for i in indices]
    assert d._update_flow is None

@pytest.mark.parametrize('case',['cancel','matching','incomplete','missing','config','failed','selection','closed'])
def test_no_write_on_cancel_or_invalid_data(setup_dialog,case):
    d,run,client,status,records,action,_=setup_dialog
    if case in ('matching','incomplete'):
        def modified(*args,**kw):
            result=action(*args,**kw)
            if args[0]=='read':
                if case=='incomplete':result['targets'][3]['items'][0].update(status='timeout',actual={'status':'timeout'})
                else:
                    for r in result['targets']:
                        for item in r['items']:item.update(status='passed',actual={'status':'read','raw':item['expected_raw']})
            return result
        client.motor_parameter_action.side_effect=modified
    if case=='missing':client.motor_parameter_templates.side_effect=lambda i,**kw:{'templates':[]}
    if case=='config':status['configuration_sha256']='e'*64
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Cancel):
        d.update_selected()
        if case=='failed':run.queue.pop(0)[2]()
        if case=='selection':d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
        if case=='closed':d.reject()
        run.drain()
    assert 'start' not in names(client) and d._update_flow is None
    if case=='cancel':assert names(client)==['read','preview'] and '已取消' in d.step_hint.text()
    elif case=='matching':assert '无需更新' in d.step_hint.text()
    elif case=='incomplete':assert '未读全' in d.step_hint.text()
    elif case=='missing':assert 'ID 1' in d.step_hint.text()
    elif case=='config':assert '配置' in d.step_hint.text()
    if case!='closed':assert d.preview_button.isEnabled()

def test_waits_for_version_and_async_read_then_previews_once(setup_dialog):
    d,run,client,status,_,action,_=setup_dialog
    client.refresh_motor_versions.return_value={'request_id':'r','state':'reading'}
    client.motor_version_refresh_status.return_value={'request_id':'r','state':'reading'}
    def queued(*args,**kw):
        result=action(*args,**kw)
        if args[0]=='read':result.update(active=True,status='reading')
        return result
    client.motor_parameter_action.side_effect=queued
    d.update_selected();run.drain();assert not client.motor_parameter_templates.called
    client.motor_version_refresh_status.return_value={'request_id':'r','state':'complete'}
    d.poll();run.drain();d.poll();run.drain();assert names(client)==['read']
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Cancel) as confirm:
        d.poll();run.drain();d.poll();run.drain()
    assert names(client)==['read','preview'] and confirm.call_count==1

def test_version_timeout_releases_ui(setup_dialog):
    d,run,client,*_=setup_dialog
    client.refresh_motor_versions.return_value={'request_id':'r','state':'reading'}
    client.motor_version_refresh_status.return_value={'request_id':'r','state':'reading'}
    d.update_selected();run.drain();d._update_flow['deadline']=0;d.poll()
    assert not names(client) and d.preview_button.isEnabled() and '超时' in d.step_hint.text()


@pytest.mark.parametrize('intent',['reference','compare','templates','update'])
@pytest.mark.parametrize('indices',[[0],list(range(7))])
def test_every_entry_refreshes_disconnected_cache_before_queries(setup_dialog,intent,indices):
    """Model real maintenance: parameter read closes scanner, making cache stale."""
    d,run,client,status,records,action,_=setup_dialog
    d.select_rows(False)
    for i in indices:d.table.item(i,0).setCheckState(Qt.CheckState.Checked)
    connected=False
    def mark_disconnected():
        nonlocal connected
        connected=False
        for row in status['targets']:
            row['observation']['status']='stale';row['firmware_generation']=None
    mark_disconnected()
    d.rows=deepcopy(status['targets']);d.controls()
    def refresh():
        nonlocal connected
        connected=True
        for row in status['targets']:
            row['observation']['status']='known';row['firmware_generation']=109
        return {'request_id':'refreshed','state':'complete'}
    def catalog(i,**kw):
        assert connected, 'template query used disconnected version cache'
        return {'templates':[deepcopy(records[i])]}
    def perform(name,payload,**kw):
        if name in ('read','read-reference'):
            assert connected, 'read attempted before fresh version discovery'
            mark_disconnected()
        return action(name,payload,**kw)
    client.refresh_motor_versions.side_effect=refresh
    client.motor_parameter_templates.side_effect=catalog
    client.motor_parameter_action.side_effect=perform
    button={'reference':d.read_reference_button,'compare':d.read_button,
            'templates':d.templates_button,'update':d.preview_button}[intent]
    with patch.object(QMessageBox,'exec',return_value=QMessageBox.StandardButton.Cancel):
        for _ in range(2):
            assert button.isEnabled()
            button.click();run.drain()
            assert d._update_flow is None and not d.pending
            mark_disconnected()
    assert client.refresh_motor_versions.call_count==2
    expected={'reference':['read-reference'],'compare':['read'],
              'templates':[],'update':['read','preview']}[intent]
    assert names(client)==expected*2
    assert 'start' not in names(client)
    if intent!='reference':
        assert [c.args[0] for c in client.motor_parameter_templates.call_args_list]==indices*2
    else:
        assert not client.motor_parameter_templates.called
    client.enable_motors.assert_not_called()


@pytest.mark.parametrize('intent',['reference','compare','templates'])
@pytest.mark.parametrize('problem',['selection','unknown','timeout','close'])
def test_read_preparation_never_continues_after_invalidating(setup_dialog,intent,problem):
    d,run,client,status,*_=setup_dialog
    if problem=='unknown':
        status['targets'][4]['firmware_generation']=None
        status['targets'][4]['observation']['status']='timeout'
    if problem=='timeout':
        client.refresh_motor_versions.return_value={'request_id':'r','state':'reading'}
        client.motor_version_refresh_status.return_value={'request_id':'r','state':'reading'}
    {'reference':d.read_reference,'compare':d.read_comparison,'templates':d.refresh_templates}[intent]()
    if problem=='selection':d.table.item(0,0).setCheckState(Qt.CheckState.Unchecked)
    if problem=='close':d.reject()
    run.drain()
    if problem=='timeout':d._update_flow['deadline']=0;d.poll();run.drain()
    assert not names(client) and not client.motor_parameter_templates.called
    assert d._update_flow is None
