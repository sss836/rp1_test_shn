"""CSV contract, evidence preservation and bounded pagination; no device I/O."""
import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from factory_hmi.test_record_csv import FIELDS, catalog, chunks, criteria, row
from factory_hmi.standard_record_export import standard_history_csv


def decode(content):
    return list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))


def test_contract_matches_both_apps_and_supplied_template():
    root = Path(__file__).resolve().parents[2]
    assert (root/'hmi/factory_hmi/test_record_csv.py').read_bytes() == (root/'backend/app/test_record_csv.py').read_bytes()
    assert (root/'hmi/factory_hmi/test_record_catalog.json').read_bytes() == (root/'backend/app/test_record_catalog.json').read_bytes()
    with (root/'docs/templates/RP1_测试记录标准模板_v3.csv').open(encoding='utf-8-sig',newline='') as handle:
        reader=csv.DictReader(handle)
        assert reader.fieldnames == list(FIELDS)
        rows=list(reader)
    assert len(rows)==94
    assert sum(item['测试体系']=='MOD' for item in rows)==53
    assert all(item['是否通过']=='未测试' and not item['实际结果'] and not item['记录编号'] for item in rows)


def test_unicode_quotes_newlines_leading_zero_sn_and_formula_protection():
    result=decode(b''.join(chunks([row(dict(sample_id='000012345678901234567890-aBc',
        tester='=HYPERLINK("unsafe")', actual_result='第一行,"值"\n第二行', verdict='blocked'))])))[0]
    assert list(result)==list(FIELDS)
    assert result['样品编号']=='000012345678901234567890-aBc'
    assert result['测试人员'].startswith("'=")
    assert result['实际结果']=='第一行,"值"\n第二行'
    assert result['是否通过']=='中止/阻断'


def test_stream_is_lazy_and_one_pass_for_more_than_one_page():
    visited=[]
    def values():
        for index in range(1007):
            visited.append(index)
            yield row(dict(record_id=f'run-{index}',sample_id='000'+str(index)))
    stream=chunks(values())
    assert next(stream)==b'\xef\xbb\xbf' and not visited
    assert next(stream).decode().startswith('记录编号,') and not visited
    assert 'run-0' in next(stream).decode() and visited==[0]
    remainder=list(stream)
    assert len(remainder)==1006 and visited[-1]==1006
    assert max(map(len,remainder))<2048


def test_rules_come_from_snapshot_and_runtime_v2_ignored_thresholds_are_not_reintroduced():
    assert '标准待补充' in criteria({})
    value=criteria(dict(criteria_version='hmi-runtime-v2', required_cycles=0,
        min_sample_completeness=.95,max_gap_seconds=.2,max_tracking_rms_rad=.01))
    assert '完成循环数≥0' in value and '样本完整度≥0.95' in value
    assert '跟踪RMS≤0.01' not in value and '仅记录' in value
    assert '跟踪RMS≤0.01rad' in criteria(dict(criteria_version='old-v1',max_tracking_rms_rad=.01))


@pytest.fixture
def sealed(tmp_path):
    for folder in ('analysis','config','telemetry'):(tmp_path/folder).mkdir()
    data={'manifest.json':dict(test_case_id='REL-ARM-002',bench_id='A8'),
          'config/execution.json':dict(acceptance=dict(criteria_version='hmi-runtime-v2',required_cycles=2,
             min_sample_completeness=.95,max_gap_seconds=.2)),
          'analysis/summary.json':dict(timing=dict(active_seconds=60,completed_cycles=2),
             quality=dict(sample_count=1200,sample_completeness=1,max_gap_seconds=.05),
             verdict=dict(status='passed',criteria_version='hmi-runtime-v2'),
             joint_metrics=[dict(joint_name='left_elbow_joint',tracking_rms_rad=.1,temperature_peak_c=33,torque_peak_abs_nm=4,fault_count=0)]),
          'analysis/operator_review.json':dict(verified_status='failed',tester='人工核实员')}
    for name,value in data.items():(tmp_path/name).write_text(json.dumps(value),encoding='utf-8')
    (tmp_path/'telemetry/raw.csv').write_text('immutable acquisition\n')
    return tmp_path


def test_local_export_preserves_evidence_and_separates_auto_review_and_platform_state(sealed):
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sealed.rglob('*') if p.is_file()}
    item=dict(execution_uuid='original-uuid',test_id='LOCAL-1',record_directory=str(sealed),
        sample_id='000A',operator_id='操作员',origin='HMI',verdict='passed',sync_state='pending_review',
        started_at='2026-11-04T10:00:00+08:00',ended_at='2026-11-04T10:01:00+08:00')
    result=decode(b''.join(standard_history_csv([item])))[0]
    assert result['测试项目编号']=='REL-ARM-002' and result['测试体系']=='MOD'
    assert result['记录编号']=='original-uuid' and result['样品编号']=='000A'
    assert result['是否通过']=='不通过'
    assert '封存自动判定=passed' in result['实际结果'] and '人员核实=failed' in result['实际结果']
    assert '平台同步状态=pending_review' in result['备注'] and '峰值力矩=4N·m' in result['实际结果']
    assert result['开始时间'].endswith('+08:00')
    assert before=={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sealed.rglob('*') if p.is_file()}


def test_local_correction_is_exported_with_original_and_no_platform_approval_rewrite(sealed):
    item=dict(test_id='LOCAL-1',record_directory=str(sealed),sample_id='new-000A',verdict='blocked',sync_state='approved',
        local_correction=dict(revision=1,original={'sample_id':'old-000A','verdict':'passed'},reason='人工纠正',actor='test'),
        stage_code='changed-stage')
    result=decode(b''.join(standard_history_csv([item])))[0]
    assert result['样品编号']=='new-000A' and result['是否通过']=='中止/阻断'
    assert 'old-000A' in result['备注'] and 'changed-stage' in result['备注']
    assert '平台同步状态=approved' in result['备注'] and '封存自动判定=passed' in result['实际结果']


def test_missing_unknown_and_corrupt_record_remain_unknown_without_inventing_model_or_results(tmp_path):
    result=decode(b''.join(standard_history_csv([dict(test_id='unknown',record_directory=str(tmp_path))])))[0]
    assert result['样品编号']==result['机型']==result['测试项目编号']==result['开始时间']==''
    assert result['是否通过']=='未测试' and '标准待补充' in result['通过标准']
    (tmp_path/'manifest.json').write_text('invalid JSON')
    result=decode(b''.join(standard_history_csv([dict(test_id='unknown',record_directory=str(tmp_path))])))[0]
    assert '读取失败' in result['备注'] and result['实际结果']==''
