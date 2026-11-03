"""Local v2 display corrections retain v1 journals, sealed facts and real receipts."""
import hashlib
import json
from pathlib import Path

import pytest

from factory_hmi.core.local_corrections import FIELDS, LABELS, LocalCorrections, canonical, sha, validated_changes
from factory_hmi.execution_history import history_csv
from test_execution_history import service, seed, decode_csv
from test_local_corrections import save


def original_record(service):
    root = seed(service, 'test', '2026-11-03T01:00:00Z', 'original-SN')
    manifest = json.loads((root/'manifest.json').read_text())
    manifest.update(test_case_id='OTHER', bench_id='A8', test_name='原始测试', actual_object='原始部位')
    (root/'manifest.json').write_text(json.dumps(manifest))
    summary = dict(verdict={'status': 'failed', 'criteria_version': 'original-rule'},
                   timing=dict(started_at=manifest['started_at'], ended_at='2026-11-03T01:02:00Z',
                               active_seconds=100, wall_seconds=120, paused_seconds=20, completed_cycles=5),
                   quality=dict(sample_count=100, expected_sample_count=120, sample_completeness=0.8, max_gap_seconds=0.5),
                   joint_metrics=[{'fault_count': 1}])
    (root/'analysis/summary.json').write_text(json.dumps(summary))
    (root/'config').mkdir()
    (root/'config/execution.json').write_text(json.dumps(dict(limb='left_arm', module_name='左臂', record_rate_hz=20)))
    service.report_manager.refresh_history_index('test')
    return root


def test_full_business_edit_filters_and_export_keep_originals_and_zero(service):
    root = original_record(service)
    before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with service.outbox._connect() as db:
        db.execute("INSERT INTO execution_outbox (execution_uuid,test_id,bundle_path,manifest_json,state,created_at,updated_at,execution_code) VALUES ('synthetic','test','/never-uploaded','{}','approved',0,0,'OFFICIAL-123')")
    queue = [row.to_dict() for row in service.outbox.list()]
    changes = dict(display_test_code='LOCAL-DISPLAY-001', sample_id='0000123', test_case_id='REL-ARM-002',
                   test_case_version='v2', robot_id='样机2', module_name='右臂', limb='right_arm',
                   operator_id='更正人员', bench_id='B02', stage_code='STAGE2', stage_name='复核',
                   test_name='更正测试', actual_object='更正部位', verdict='passed', verdict_note='手工复核',
                   criteria_version='local-rule', machine_status='completed', outcome='completed', stop_reason='本地说明',
                   started_at='2026-11-04T09:00:00+08:00', ended_at='2026-11-04T09:01:00+08:00',
                   active_seconds='0', wall_seconds='0', paused_seconds='0', completed_cycles='0',
                   sample_count='0', expected_sample_count='0', sample_completeness='0', max_gap_seconds='0',
                   record_rate_hz='0', fault_count='0', data_domain='RESEARCH', recording_scope='local', origin='OTHER',
                   software_version='本地版本标记', calibration_version='本地标定标记', notes='本地备注',
                   analysis_note='本地解释', local_status_note='本地同步说明', local_review_note='本地审批说明')
    assert set(changes) == set(FIELDS)
    result = save(service, changes=changes)
    assert result['records'][0]['schema'] == 'rp1-local-correction-v2'
    page = service.execution_history_page(start_at='2026-11-04T00:00:00+08:00', end_at='2026-11-05T00:00:00+08:00', data_domain='RESEARCH', sn='0000123', bench='B02')
    assert page['total'] == 1
    row = page['items'][0]
    assert row['test_id'] == 'test' and row['execution_uuid'] == 'test'
    assert row['execution_code'] == 'OFFICIAL-123' and row['display_test_code'] == 'LOCAL-DISPLAY-001'
    assert row['verdict'] == 'passed' and row['local_correction']['original']['verdict'] == 'failed'
    assert row['sync_state'] == 'approved' and row['review_reason'] == ''
    assert row['data_domain_source'] == 'LOCAL_CORRECTION'
    assert row['active_seconds'] == 0 and row['sample_count'] == 0 and row['completed_cycles'] == 0
    assert service.execution_history_page(data_domain='FACTORY')['total'] == 0
    assert service.execution_history_page(start_at='2026-11-03T00:00:00Z', end_at='2026-11-04T00:00:00Z')['total'] == 0
    exported = decode_csv(b''.join(history_csv([row])))[0]
    assert exported['测试单号'] == 'LOCAL-DISPLAY-001' and exported['本地记录ID'] == 'test'
    assert exported['本机值·完成循环数'] == '0' and exported['本机值·有效样本数'] == '0'
    assert exported['本机值·有效运行时长（秒）'] != '' and float(exported['本机值·有效运行时长（秒）']) == 0
    assert exported['原始值·有效运行时长（秒）'] == '100' and exported['原始值·本地判定'] == 'failed'
    assert all(p.read_bytes() == raw for p,raw in before.items())
    assert [row.to_dict() for row in service.outbox.list()] == queue
    ledger = decode_csv(Path(page['ledger_path']).read_bytes())[0]
    assert ledger['测试单号'] == 'LOCAL-DISPLAY-001' and ledger['同步状态'] == '已批准'


def test_v1_journal_remains_identical_and_accepts_a_v2_append(service):
    root = original_record(service)
    manifest = (root/'manifest.json').read_bytes()
    decoded = json.loads(manifest)
    fields = ('sample_id', 'operator_id', 'bench_id', 'test_name', 'actual_object', 'notes')
    original = {key:str(decoded.get(key) or '') for key in fields}
    after = dict(original, sample_id='legacy-local-SN')
    record = dict(schema='rp1-local-correction-v1', test_id='test', revision=1, request_id='f'*32,
                  manifest_sha256=hashlib.sha256(manifest).hexdigest(), original=original, before=original,
                  changes={'sample_id':'legacy-local-SN'}, values=after, reason='历史更正', actor='旧窗口',
                  recorded_at='2026-11-03T01:00:00+00:00', previous_sha256='')
    record['sha256'] = sha(record)
    path = service.report_manager.data_root/'local-corrections/test/v000001.json'
    path.parent.mkdir(parents=True); path.write_bytes(canonical(record)); legacy_bytes=path.read_bytes()
    state = service.execution_local_corrections('test')
    assert state['revision'] == 1 and state['values']['sample_id'] == 'legacy-local-SN'
    assert state['values']['active_seconds'] == '100'
    updated = save(service, changes={'completed_cycles':'0', 'verdict':'passed'}, revision=1, request='b'*32)
    assert updated['revision'] == 2 and updated['records'][1]['schema'] == 'rp1-local-correction-v2'
    assert updated['records'][1]['previous_sha256'] == record['sha256']
    assert path.read_bytes() == legacy_bytes
    assert service.execution_history_page()['items'][0]['completed_cycles'] == 0
    assert service.execution_local_corrections('test')['revision'] == 2


@pytest.mark.parametrize('changes', [
    {'active_seconds':'NaN'}, {'active_seconds':'Infinity'}, {'active_seconds':'-1'},
    {'completed_cycles':'1.5'}, {'sample_count':'1000000000001'}, {'sample_count':True},
    {'sample_completeness':'1.01'}, {'record_rate_hz':'100001'}, {'data_domain':'UNKNOWN'},
    {'started_at':'2026-11-03 10:00:00'}, {'ended_at':'broken'}, {'verdict':'auto-approved'},
    {'display_test_code':'../bad\x00path'}, {'submission_id':'forged'}, {'sync_state':'approved'},
])
def test_v2_invalid_types_and_protected_fields_are_rejected(service, changes):
    original_record(service)
    with pytest.raises(ValueError): save(service, changes=changes)
    assert service.execution_local_corrections('test')['revision'] == 0


def test_time_order_and_unknown_numeric_are_explicit(service):
    original_record(service)
    with pytest.raises(ValueError, match='结束时间'):
        save(service, changes={'ended_at':'2026-11-02T00:00:00Z'})
    result = save(service, changes={'sample_count':'', 'active_seconds':''})
    assert result['values']['sample_count'] == ''
    row = service.execution_history_page()['items'][0]
    assert row['sample_count'] is None and row['active_seconds'] is None
    assert row['completed_cycles'] == 5  # An unrelated field is not blanked.


def test_unchanged_display_alias_does_not_mask_official_code(service):
    original_record(service)
    with service.outbox._connect() as db:
        db.execute("INSERT INTO execution_outbox (execution_uuid,test_id,bundle_path,manifest_json,state,created_at,updated_at,execution_code) VALUES ('synthetic','test','/never-uploaded','{}','approved',0,0,'OFFICIAL-123')")
    save(service, changes={'notes':'local-only'})
    row = service.execution_history_page()['items'][0]
    assert not row.get('display_test_code') and row['execution_code'] == 'OFFICIAL-123'
    assert decode_csv(b''.join(history_csv([row])))[0]['测试单号'] == 'OFFICIAL-123'


def test_all_business_fields_can_be_cleared_without_changing_the_frozen_request(service):
    root = original_record(service)
    before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with service.outbox._connect() as db:
        db.execute("INSERT INTO execution_outbox (execution_uuid,test_id,bundle_path,manifest_json,state,created_at,updated_at,submission_payload) VALUES ('synthetic','test','/never-uploaded','{}','sync_failed',0,0,'immutable pending request')")
    queue = [row.to_dict() for row in service.outbox.list()]
    changes = {key:'' for key in FIELDS}
    result = save(service, changes=changes)
    assert result['revision'] == 1
    assert all(value == '' for value in result['values'].values())
    page = service.execution_history_page(data_domain='UNCLASSIFIED')
    assert page['total'] == 1
    row = page['items'][0]
    assert row['data_domain'] == 'UNCLASSIFIED' and row['data_domain_source'] == 'LOCAL_CORRECTION'
    assert row['test_id'] == 'test' and row['display_test_code'] == ''
    assert row['started_at'] == '' and row['sample_count'] is None
    assert row['local_correction']['original']['sample_id'] == 'original-SN'
    exported = decode_csv(b''.join(history_csv([row])))[0]
    assert exported['测试单号'] == '' and exported['本地记录ID'] == 'test'
    assert exported['数据分区'] == '分区待归类'
    assert exported['原始SN'] == 'original-SN' and exported['原始值·有效样本数'] == '100'
    assert [row.to_dict() for row in service.outbox.list()] == queue
    assert all(p.read_bytes() == raw for p,raw in before.items())


def test_old_index_payload_is_upgraded_once_from_sealed_records(service):
    from factory_hmi.core.report import ReportManager
    root = original_record(service)
    before = {p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    index = service.report_manager.history_index
    with index.connect() as db:
        db.execute("DELETE FROM metadata WHERE k='view_version'")
        db.execute("UPDATE runs SET payload=? WHERE test_id='test'", (json.dumps({'test_id':'test', 'sample_id':'stale-v1-index'}),))
    assert not index.initialized()
    restarted = ReportManager(service.report_manager.data_root)
    assert restarted.history_index.initialized()
    row = restarted.history_index.page()['items'][0]
    assert row['sample_id'] == 'original-SN' and row['sample_count'] == 100
    assert row['module_name'] == '左臂' and row['record_rate_hz'] == 20
    assert all(p.read_bytes() == raw for p,raw in before.items())
    # Subsequent opens reuse the current view, rather than rescanning raw CSV.
    with restarted.history_index.connect() as db:
        assert db.execute("SELECT v FROM metadata WHERE k='view_version'").fetchone()[0] == 'local-business-v2'


def test_history_rebuild_yields_one_expanded_record_before_reading_the_next(service, monkeypatch):
    root = original_record(service)
    original_glob = Path.glob
    def bounded_glob(path, pattern):
        if path == service.report_manager.executions_root and pattern == '*/manifest.json':
            yield root/'manifest.json'
            raise RuntimeError('second record requested')
        else:
            yield from original_glob(path, pattern)
    monkeypatch.setattr(Path, 'glob', bounded_glob)
    stream = service.report_manager._scan_history()
    assert next(stream)['sample_count'] == 100
    with pytest.raises(RuntimeError, match='second record'):
        next(stream)


def test_unchanged_chinese_unconfigured_verdict_remains_a_valid_original(service):
    root = original_record(service)
    summary = json.loads((root/'analysis/summary.json').read_text())
    summary['verdict'] = {'status':'unconfigured', 'criteria_version':'UNCONFIGURED'}
    (root/'analysis/summary.json').write_text(json.dumps(summary))
    result = save(service, changes={'notes':'only local notes'})
    assert result['original']['verdict'] == '未配置判据'
    assert service.execution_history_page()['items'][0]['verdict'] == '未配置判据'


def test_local_schema_accepts_many_business_fields_and_keeps_actor_revision_protection():
    from pydantic import ValidationError
    from factory_hmi.gateway.schemas import LocalCorrectionRequest
    body = dict(changes={key:'' for key in FIELDS}, reason='本地更正', expected_revision=0, request_id='a'*32)
    assert len(LocalCorrectionRequest(**body).changes) == len(FIELDS)
    with pytest.raises(ValidationError): LocalCorrectionRequest(**dict(body, actor='forged'))
    with pytest.raises(ValidationError): LocalCorrectionRequest(**dict(body, expected_revision=True))
