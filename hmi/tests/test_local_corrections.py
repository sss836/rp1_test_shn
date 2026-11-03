"""Local metadata corrections never modify sealed facts or queue an upload."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from factory_hmi.core.service import FactoryService
from factory_hmi.execution_history import history_csv
from test_execution_history import service, seed, decode_csv
from test_verified_csv_archive import local_record


def save(service, test_id='test', changes=None, revision=0, request='a' * 32):
    return service.correct_execution_locally(test_id, changes or {'sample_id': '0000000000123'},
        reason='合成更正原因', actor='本机窗口:synthetic', expected_revision=revision, request_id=request)


def test_local_only_edit_updates_list_filter_ledger_and_keeps_approved_state(service):
    root = seed(service, 'test', '2026-10-28T00:00:00Z', 'old-SN')
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest.update(bench_id='A01', test_name='原名称', actual_object='原对象')
    (root / 'manifest.json').write_text(json.dumps(manifest))
    with service.outbox._connect() as db:
        db.execute("INSERT INTO execution_outbox (execution_uuid,test_id,bundle_path,manifest_json,state,created_at,updated_at) VALUES ('synthetic','test','/not-uploaded','{}','approved',0,0)")
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    queue_before = [i.to_dict() for i in service.outbox.list()]
    service.submit_execution = Mock(side_effect=AssertionError('must not submit'))
    changes = dict(sample_id='0000000000123', operator_id='更正测试人', bench_id='B02',
                   test_name='更正名称', actual_object='更正对象', notes='本地备注')
    result = save(service, changes=changes)
    assert result['revision'] == 1 and result['created'] and result['local_only']
    page = service.execution_history_page(sn='0000000000123', bench='B02')
    assert page['total'] == 1
    row = page['items'][0]
    assert all(row[k] == v for k, v in changes.items())
    assert row['sync_state'] == 'approved' and row['verdict'] == 'passed'
    assert row['data_domain'] == 'FACTORY' and row['data_domain_source'] == 'HISTORICAL_BENCH'
    assert service.execution_history_page(sn='old-SN')['total'] == 0
    assert row['local_correction']['original']['sample_id'] == 'old-SN'
    csv = decode_csv(b''.join(history_csv([row])))[0]
    assert csv['模块编号/SN'] == '0000000000123' and csv['工厂SN'] == '0000000000123'
    assert csv['原始SN'] == 'old-SN' and csv['本地更正'] == '本地已更正'
    assert csv['更正操作人'] == '本机窗口:synthetic' and csv['本地备注'] == '本地备注'
    ledger = decode_csv(Path(page['ledger_path']).read_bytes())[0]
    assert ledger['模块编号/SN'] == '0000000000123' and ledger['同步状态'] == '已批准'
    assert all(p.read_bytes() == value for p, value in before.items())
    assert [i.to_dict() for i in service.outbox.list()] == queue_before
    service.submit_execution.assert_not_called()


def test_revisions_retry_restart_conflict_and_request_binding(service):
    seed(service, 'test', '2026-10-28T00:00:00Z')
    one = save(service)
    retry = save(service)
    assert retry['revision'] == 1 and retry['created'] is False
    with pytest.raises(ValueError, match='不同'):
        save(service, changes={'sample_id': 'another'})
    with pytest.raises(ValueError, match='其他窗口'):
        save(service, changes={'notes': 'new'}, request='b' * 32)
    two = save(service, changes={'notes': 'new'}, revision=1, request='b' * 32)
    assert two['revision'] == 2 and two['records'][1]['before']['sample_id'] == one['values']['sample_id']
    assert two['records'][1]['previous_sha256'] == one['records'][0]['sha256']
    other = FactoryService(data_root=service.data_root / 'other', report_root=service.report_manager.data_root)
    try:
        assert other.execution_local_corrections('test') == {k: v for k, v in two.items() if k not in {'created','ledger_error','ledger_path'}}
    finally:
        other.close()


def test_two_service_instances_cannot_overwrite_the_same_revision(service):
    seed(service, 'test', '2026-10-28T00:00:00Z')
    other = FactoryService(data_root=service.data_root / 'other', report_root=service.report_manager.data_root)
    def run(pair):
        svc, letter = pair
        try: return save(svc, changes={'notes': letter}, request=letter * 32)
        except ValueError as exc: return str(exc)
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, [(service, 'a'), (other, 'b')]))
        assert sum(isinstance(r, dict) for r in results) == 1
        assert any('其他窗口' in r for r in results if isinstance(r, str))
        assert service.execution_local_corrections('test')['revision'] == 1
    finally:
        other.close()


@pytest.mark.parametrize('key', ['verdict', 'active_seconds', 'raw_csv_path', 'data_domain', 'station_id', 'sync_state', 'approved'])
def test_measurements_verdict_partition_and_approval_cannot_be_corrected(service, key):
    seed(service, 'test', '2026-10-28T00:00:00Z')
    with pytest.raises(ValueError, match='只允许'):
        save(service, changes={key: 'changed'})
    assert service.execution_local_corrections('test')['revision'] == 0
    assert service.outbox.list() == []


def test_atomic_save_failure_and_ledger_failure_are_reported_truthfully(service, monkeypatch):
    import factory_hmi.core.local_corrections as corrections
    seed(service, 'test', '2026-10-28T00:00:00Z')
    with monkeypatch.context() as m:
        m.setattr(corrections.os, 'replace', Mock(side_effect=OSError('synthetic disk-full')))
        with pytest.raises(OSError, match='disk-full'): save(service)
    assert service.execution_local_corrections('test')['revision'] == 0
    with monkeypatch.context() as m:
        m.setattr(service.report_manager, 'write_ledger', Mock(side_effect=OSError('synthetic ledger failure')))
        result = save(service)
        assert result['revision'] == 1 and 'ledger failure' in result['ledger_error']
        assert save(service)['created'] is False
    page = service.execution_history_page()
    assert not page['ledger_error'] and decode_csv(Path(page['ledger_path']).read_bytes())[0]['更正版本'] == '1'


def test_tampered_audit_is_visible_but_not_applied_or_extended(service):
    seed(service, 'test', '2026-10-28T00:00:00Z', 'original')
    save(service)
    path = service.report_manager.data_root / 'local-corrections/test/v000001.json'
    value = json.loads(path.read_text()); value['values']['sample_id'] = 'tampered'; path.write_text(json.dumps(value))
    row = service.execution_history_page()['items'][0]
    assert row['sample_id'] == 'original' and row['local_correction_error']
    with pytest.raises(ValueError): save(service, revision=1, request='b' * 32)


@pytest.mark.parametrize('changes', [{'sample_id':''}, {'notes':'x'*301}, {'operator_id':'bad\x00name'}, {'notes':True}])
def test_invalid_local_values_create_no_revision(service, changes):
    seed(service, 'test', '2026-10-28T00:00:00Z')
    with pytest.raises(ValueError): save(service, changes=changes)
    assert service.execution_local_corrections('test')['revision'] == 0


def test_later_submission_requires_review_and_keeps_original_identity_and_seal(local_record):
    service, report, review = local_record
    before = service.report_manager.validate('synthetic-test')
    result = save(service, 'synthetic-test')
    assert result['revision'] == 1 and not service.outbox.list()
    with pytest.raises(ValueError, match='必须核实'):
        service.submit_execution('synthetic-test')
    queued = service.submit_execution('synthetic-test', review=review)
    assert queued['manifest']['sample_id'] == report.manifest['sample_id']
    assert service.report_manager.validate('synthetic-test') == before
    assert queued['state'] not in {'approved', 'uploaded'}
