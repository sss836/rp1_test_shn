"""Supplied Membus metadata remains separate from CAN values and permission."""
from copy import deepcopy
import json

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QPlainTextEdit
from shiboken6 import isValid

from factory_hmi.core.parameter_reference import (CATALOG_PATH, CATALOG_SHA256, DOCUMENTATION_ZIP_SHA256,
    _validated_catalog, describe_reference, reference_for_parameter, reference_text)
from factory_hmi.core.motor_parameter_transport import decode
from factory_hmi.motor_parameter_contract import PN_NUMBERS, MODELS, sha
from test_module_parameters import finish, make
from test_module_parameters_desktop import dialog


def test_nine_reference_definitions_keep_transport_scope_and_source():
    assert len(PN_NUMBERS) == 9
    for number in PN_NUMBERS:
        reference = reference_for_parameter(f'pn_{number:04x}')
        assert reference['status'] == 'reference_only' and reference['can_applicability'] == 'unverified'
        assert reference['transport'] == 'usb_serial_membus'
        assert reference['raw_width_bits'] == 16 and reference['signed'] is False
        assert reference['source_json_sha256'] == CATALOG_SHA256
        assert reference['documentation_zip_sha256'] == DOCUMENTATION_ZIP_SHA256
        assert set(reference['parameter_set_ids']) == {'0707', '0808', '0901', 'C001'}
        assert not set(reference['parameter_set_ids']) & set(MODELS)
        assert len(reference['library_sources']) == 4
        assert not {'actual', 'expected_raw', 'applicability', 'confirmation', 'published_by'} & reference.keys()
    assert reference_for_parameter('kt') is None and reference_for_parameter('pn_ffff') is None


@pytest.mark.parametrize('key,factor,unit,default,bounds,reboot', [
    ('pn_0401','0.01','0.01ms',100,[0,65535],False),
    ('pn_0f09','0.1','0.1A',0,[0,65535],True),
    ('pn_0e71','1.0','V',0,[0,1000],True),
])
def test_original_factor_unit_default_and_range_are_kept_separate(key,factor,unit,default,bounds,reboot):
    reference = reference_for_parameter(key)
    assert (reference['display_factor'],reference['unit_text'],reference['raw_default'],reference['raw_range'],reference['requires_reboot']) == (factor,unit,default,bounds,reboot)
    text = describe_reference(reference)
    assert '×'+factor in text and '单位原文 '+unit in text and '非实测/非模板' in text


def test_hex_alarm_mask_and_unknown_bit_definitions_not_invented():
    reference = reference_for_parameter('pn_0e05')
    assert reference['show_base'] == 'NBL' and reference['raw_range'] == [0,65535]
    assert '0000H～FFFFH' in describe_reference(reference)
    assert 'bit_fields' not in reference


def test_catalog_tampering_rejected_and_consumer_changes_do_not_mutate_reference():
    raw = CATALOG_PATH.read_bytes()
    with pytest.raises(ValueError, match='哈希'):
        _validated_catalog(raw+b' ')
    first = reference_for_parameter('pn_0402')
    first['raw_range'][1] = 999999
    first['library_sources'][0]['line'] = -1
    again = reference_for_parameter('pn_0402')
    assert again['raw_range'] == [0,800] and again['library_sources'][0]['line'] > 0


def test_reference_enriches_persisted_unknown_report_without_changing_template_or_authorizing_write(tmp_path,dialog):
    task,selections,writes,values,actor,record,targets = make(tmp_path)
    record['template']['entries'] = [dict(key='pn_0e71',expected_raw=[75],tolerance_raw=0)]
    record['sha256'] = sha(record['template'])
    original = deepcopy(record)
    task.reader = lambda row,item,stop: decode(item['key'],row['motor_id'],b'\xff\xfe\x11\x00\x00\x00\x4b',row)
    try:
        task.read(selections)
        job = finish(task)
        item = job['targets'][0]['items'][0]
        assert item['reference']['name'] == '过压检测电平'
        assert item['reference']['raw_default'] == 0
        assert item['expected_raw'] == [75] and item['actual']['raw'] == [75]
        assert item['status'] == 'association_unknown' and item['write_status'] == 'not_written'
        assert record == original and job['targets'][0]['template_record'] == original
        with pytest.raises(ValueError, match='禁止写入'):
            task.preview(job['job_id'])
        assert not writes
        stored = json.loads((task.root/(job['job_id']+'.json')).read_text())
        assert stored['targets'][0]['items'][0]['reference'] == item['reference']
        window = dialog[0]
        text = window.describe(job)
        assert '未核验响应原始值 [75.0]' in text and '响应归属未核验' in text
        assert 'CAN适用性待核实' in text and CATALOG_SHA256 in text
    finally:
        task.cancel()


def test_reference_window_is_read_only_reused_and_never_calls_device_or_network(dialog):
    window,run_rest,client,*_ = dialog
    calls = run_rest.call_count
    window.reference_button.click()
    child = window.reference_dialog
    details = child.findChild(QPlainTextEdit)
    assert details.isReadOnly()
    assert 'USB/串口Membus' in details.toPlainText() and DOCUMENTATION_ZIP_SHA256 in details.toPlainText()
    assert '不会替换管理员模板' in details.toPlainText()
    window.reference_button.click()
    assert window.reference_dialog is child and run_rest.call_count == calls
    client.motor_parameter_action.assert_not_called()
    window.reject()
    QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    assert not isValid(child)
