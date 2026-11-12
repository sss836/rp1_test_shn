"""Offline UI contract: script Pn evidence is explicit, raw and never number-echo proof."""
from copy import deepcopy
from unittest.mock import Mock,patch

import pytest
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from PySide6.QtWidgets import QApplication,QMessageBox
from shiboken6 import isValid

from factory_hmi.desktop.motor_parameters_dialog import MotorParametersDialog
from factory_hmi.core.parameter_reference import reference_for_parameter


def script_receipt():
    template=dict(schema='rp1-motor-parameters-v2',template_id='a'*32,version='script-v1',
                  model='LRO_5550',protocol_profile='lro-pn-script-v1',source='confirmed-script.py',
                  source_sha256='b'*64,applicability=[dict(raw_firmware='1.0.9-full',
                  software='1.0.9',hardware='H2',protocol='P1')])
    result=dict(status='read',raw=[200],observed_at='2026-10-30T12:00:00Z',
                protocol_profile='lro-pn-script-v1',association_method='single_flight_time_window',
                parameter_number_echoed=False,request_id='read-pn0401',response_hex='FF FE 11 00 00 00 C8')
    item=dict(key='pn_0401',expected_raw=[200],tolerance_raw=0,status='passed',write_status='not_run',actual=result)
    row=dict(index=0,module='left',joint_name='joint',interface='can-fixture',motor_id=1,
             model='LRO_5550',before=dict(raw_firmware='1.0.9-full',software='1.0.9',hardware='H2',protocol='P1'),
             template_record=dict(template=template,sha256='c'*64,published_by='admin-fixture'),
             items=[item],status='checked')
    return dict(job_id='fixture-only',active=False,mode='read',status='checked',targets=[row])


@pytest.fixture
def dialog():
    app=QApplication.instance() or QApplication([])
    run=Mock();client=Mock();identity={'value':'executor-fixture'}
    window=MotorParametersDialog(client,run,lambda:identity['value']);window.timer.stop()
    response={'result':{'targets':[dict(index=0,module='left',joint_name='joint',
              interface='can-fixture',motor_id=1,model='LRO_5550')],
              'job':dict(active=False,status='idle')}}
    run.call_args.args[2](response);run.call_args.kwargs['on_finished']()
    yield window,run,client,identity,app
    if isValid(window):window.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def test_pn_preview_keeps_32_bit_integer_and_template_provenance():
    job=script_receipt();text=MotorParametersDialog.describe(job)
    assert '脚本兼容：单请求时序关联，回包不含Pn编号' in text
    assert '200 / 0x000000C8 / 00 00 00 C8' in text
    assert '不套用串口倍率' in text
    assert '模板新值 [200.0]' in text
    assert '模板型号 LRO_5550' in text and 'raw_firmware=1.0.9-full' in text
    assert 'software=1.0.9' in text and 'hardware=H2' in text and 'protocol=P1' in text
    assert '实际版本 1.0.9-full；软件 1.0.9；硬件 H2；协议 P1' in text
    assert 'confirmed-script.py' in text and 'b'*64 in text and '发布者 admin-fixture' in text
    assert job['targets'][0]['items'][0]['actual']['raw']==[200]


def test_usb_reference_is_explicitly_separate_from_confirmed_can_profile():
    job=script_receipt();item=job['targets'][0]['items'][0]
    item['reference']=reference_for_parameter('pn_0401')
    text=MotorParametersDialog.describe(job)
    assert 'USB/串口独立参考，不替换已发布CAN脚本模板' in text
    assert '×0.01' in text and '模板新值 [200.0]' in text
    assert item['expected_raw']==[200] and item['actual']['raw']==[200]


def test_immediate_and_post_power_evidence_keep_status_error_and_frame():
    job=script_receipt();row=job['targets'][0];item=row['items'][0]
    item['immediate']=deepcopy(item['actual']);item['immediate']['request_id']='immediate-pn0401'
    item['post_power']=deepcopy(item['actual']);item['post_power'].update(
        status='association_unknown',request_id='post-power-pn0401',error='late response boundary')
    row.update(human_acknowledged_at='manual-only',post_power_verified=False)
    text=MotorParametersDialog.describe(job)
    assert '即时回读证据：读取返回（read）' in text
    assert '再次回读证据：未核验，不能作为成功证据（association_unknown）' in text
    for token in ('read-pn0401','immediate-pn0401','post-power-pn0401','late response boundary',
                  'FF FE 11 00 00 00 C8','人工重新上下电声明：manual-only；再次回读核验：未通过'):
        assert token in text
    assert '再次回读 Pn原始整数：200 / 0x000000C8' in text


def test_unassociated_reply_is_not_presented_as_actual_old_value():
    job=script_receipt();item=job['targets'][0]['items'][0]
    item['status']='association_unknown';item['actual'].update(status='association_unknown',
          association_method=None,protocol_profile=None,parameter_number_echoed=None)
    text=MotorParametersDialog.describe(job)
    assert 'pn_0401：未核验响应原始值' in text
    assert '响应关联：未核验' in text
    assert '独立读取证据：未核验，不能作为成功证据' in text
    assert '回包含Pn编号' not in text


def test_script_label_requires_explicit_profile_and_non_echoed_marker():
    result=script_receipt()['targets'][0]['items'][0]['actual']
    result.pop('parameter_number_echoed')
    text=MotorParametersDialog.response_details('读取',result)
    assert '脚本兼容：' not in text
    assert '响应关联：single_flight_time_window' in text


def test_template_selector_discloses_profile_and_complete_applicability(dialog):
    window,run,client,*_=dialog;record=script_receipt()['targets'][0]['template_record']
    client.motor_parameter_templates.return_value={'templates':[record]}
    window.templates();call=run.call_args
    call.args[2](call.args[1]());call.kwargs['on_finished']()
    combo=window.table.cellWidget(0,5)
    assert combo.currentData()=='a'*32 and '脚本兼容Pn' in combo.currentText()
    tooltip=combo.itemData(0,Qt.ItemDataRole.ToolTipRole)
    assert 'raw_firmware=1.0.9-full' in tooltip and 'b'*64 in tooltip
    assert 'Pn参考资料的显示倍率不用于换算发送值' in window.details.toPlainText()
    assert not window.start_button.isEnabled()


def test_write_confirmation_preserves_existing_explicit_action(dialog):
    window,run,client,*_=dialog
    window.preview_value={'preview_id':'reviewed-preview','targets':script_receipt()['targets']}
    with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):window.start()
    run.call_args.args[1]()
    assert client.motor_parameter_action.call_args.args==('start',{'preview_id':'reviewed-preview','confirmed':True})
    assert window.preview_value is None


def test_account_change_discards_pn_evidence_and_preview(dialog):
    window,run,client,identity,*_=dialog
    window.show_job(script_receipt());window.preview_value={'preview_id':'old-account'}
    identity['value']='other-fixture';window.poll()
    assert window.preview_value is None and window.details.toPlainText()==''
    assert window.table.rowCount()==0 and not window.start_button.isEnabled()
