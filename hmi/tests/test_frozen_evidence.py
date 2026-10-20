"""Synthetic evidence/transport contracts. No hardware or central network calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tarfile

import pytest
from test_verified_csv_archive import local_record, ArchiveClient, worker_for, ID, ARCHIVE
from factory_hmi.core.report import ReportManager
from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
from factory_hmi.sync.uploader import PlatformError


def sealed_bytes(report):
    return {str(path): path.read_bytes() for path in report.execution_dir.rglob('*') if path.is_file()} | {str(report.bundle_path): report.bundle_path.read_bytes()}


@pytest.mark.parametrize('relative', ['telemetry/raw.csv', 'analysis/summary.json', 'config/execution.json', 'config/station.yaml'])
def test_tampered_evidence_cannot_be_resealed_or_reviewed(local_record, relative):
    service, report, review = local_record
    path = report.execution_dir / relative
    path.write_bytes(path.read_bytes() + b' ')
    before = sealed_bytes(report)
    with pytest.raises(ValueError, match='冻结证据不一致'):
        service.submit_execution(report.manifest['test_id'], review=review)
    with pytest.raises(ValueError, match='冻结证据不一致'):
        service.rebuild_report(report.manifest['test_id'], actor='synthetic-account', reason='检验证据')
    assert sealed_bytes(report) == before
    assert not list((report.execution_dir / 'analysis').glob('operator_review*.json'))
    assert service.outbox.list() == []


def test_finalize_same_id_never_replaces_or_removes_existing_execution(local_record):
    service, report, _ = local_record
    context = json.loads((report.execution_dir / 'config/execution.json').read_text())
    context['active_seconds'] = 999
    before = sealed_bytes(report)
    with pytest.raises(ValueError, match='禁止覆盖'):
        service.report_manager.finalize(report.execution_dir / 'telemetry/raw.csv', context)
    assert sealed_bytes(report) == before


@pytest.mark.parametrize('state', ['approved', 'pending_review'])
def test_append_analysis_preserves_original_and_approved_request_bytes(local_record, monkeypatch, state):
    service, report, review = local_record
    service.submit_execution(report.manifest['test_id'], review=review)
    payload = '{ "frozen" : "exact original request" }'
    service.outbox.update(ID, state=state, submission_payload=payload)
    before = sealed_bytes(report)
    import factory_hmi.core.report as module
    original = module._build_summary
    def new_analysis(*args, **kwargs):
        result = original(*args, **kwargs)
        result['verdict']['reasons'].append('synthetic new algorithm')
        return result
    monkeypatch.setattr(module, '_build_summary', new_analysis)
    result = service.rebuild_report(report.manifest['test_id'], actor='logged-in-account', reason='合成版本复验')
    revision = result['analysis_revision']
    assert revision['actor'] == 'logged-in-account' and revision['reason'] == '合成版本复验'
    assert revision['usage'] == 'local_view_only' and revision['revision'] == 1
    assert revision['baseline']['files']['analysis/summary.json']['sha256'] == hashlib.sha256(report.summary_path.read_bytes()).hexdigest()
    assert all(Path(name).read_bytes() == data for name, data in before.items())
    assert service.outbox.get(ID).submission_payload == payload
    assert service.outbox.get(ID).state == state
    assert service.execution_summary(report.manifest['test_id'])['verdict'] == report.summary['verdict']
    assert 'synthetic new algorithm' in result['report_text']


def test_append_analysis_failure_publishes_no_version(local_record, monkeypatch):
    service, report, _ = local_record
    before = sealed_bytes(report)
    import factory_hmi.core.report as module
    def fail(*_args, **_kwargs):
        raise RuntimeError('synthetic analyzer failure')
    monkeypatch.setattr(module, '_build_summary', fail)
    with pytest.raises(RuntimeError, match='synthetic analyzer'):
        service.rebuild_report(report.manifest['test_id'], actor='account', reason='合成失败')
    assert sealed_bytes(report) == before
    assert not (report.execution_dir / 'analysis/revisions').exists()


def test_parallel_analysis_managers_append_distinct_versions(local_record):
    service, report, _ = local_record
    def analyze(number):
        manager = ReportManager(service.report_manager.data_root)
        return manager.rebuild(report.manifest['test_id'], actor='account', reason=f'合成并发 {number}')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(analyze, [1, 2]))
    assert sorted(result.analysis_revision['revision'] for result in results) == [1, 2]
    revisions = service.report_manager.analysis_revisions(report.manifest['test_id'])
    assert revisions[1]['previous_version_sha256'] == revisions[0]['version_sha256']


def replace_tar_summary(report, target):
    with tarfile.open(report.bundle_path, 'r:gz') as old, tarfile.open(target, 'w:gz') as new:
        for member in old.getmembers():
            content = old.extractfile(member).read()
            if member.name == 'analysis/summary.json':
                value = json.loads(content); value['timing']['active_seconds'] = 0.125
                content = json.dumps(value).encode(); member.size = len(content)
            new.addfile(member, io.BytesIO(content))
    report.bundle_path.write_bytes(target.read_bytes())


@pytest.mark.parametrize('target', ['tar', 'analysis/summary.json', 'config/execution.json', 'config/station.yaml'])
def test_post_review_tamper_never_reaches_network(local_record, tmp_path, target):
    service, report, review = local_record
    client = ArchiveClient(); worker = worker_for(service, review, client)
    if target == 'tar':
        replace_tar_summary(report, tmp_path / 'bad.tar.gz')
    else:
        path = report.execution_dir / target; path.write_bytes(path.read_bytes() + b' ')
    worker.process_once()
    assert client.calls == []
    assert not service.outbox.get(ID).submission_payload
    assert service.outbox.get(ID).last_error


@pytest.mark.parametrize('terminal, expected', [('duration_complete', 'completed'), ('cycles_complete', 'completed'), ('fault', 'failed')])
def test_terminal_outcome_maps_from_frozen_evidence(local_record, terminal, expected):
    service, report, review = local_record
    context = json.loads((report.execution_dir / 'config/execution.json').read_text())
    context.update(test_id='terminal-' + terminal, outcome=terminal)
    fresh = service.report_manager.finalize(report.execution_dir / 'telemetry/raw.csv', context)
    row = service.submit_execution(fresh.manifest['test_id'], review=review)
    service.outbox.update(row['execution_uuid'], submission_owner='submit-account')
    client = ArchiveClient(); worker = ReliabilityUploaderWorker(service.outbox, client); worker.authenticate('submit-account', 'fake')
    worker.process_once()
    create = next(call[1] for call in client.calls if call[0] == 'create')
    assert create['facts']['machine_status'] == expected
    assert json.loads((fresh.execution_dir / 'config/execution.json').read_text())['outcome'] == terminal


CANONICAL = '44444444-4444-4444-8444-444444444444'
class ImportClient(ArchiveClient):
    def __init__(self):
        super().__init__(); self.lose_import = False; self.import_payload = None
    def create_archive(self, payload, key):
        raise AssertionError('incomplete history must not use strict /archives')
    def import_archive(self, payload, key):
        self.calls.append(('import', deepcopy(payload), key))
        assert set(payload) == {'filename', 'size_bytes', 'sha256', 'test_name', 'actual_object', 'station_id', 'classification'}
        reused = self.detail is not None
        if self.import_payload is not None:
            assert self.import_payload == (payload, key)
        self.import_payload = (deepcopy(payload), key)
        if self.detail is None:
            self.detail = {'id': ARCHIVE, 'execution_uuid': CANONICAL, 'revision': 0, 'received_bytes': 0,
                'processing_state': 'WAITING', 'classification_state': 'UNCLASSIFIED',
                'sha256': payload['sha256'], 'size_bytes': payload['size_bytes'], 'is_owner': True, 'can_edit': True}
        if self.lose_import:
            self.lose_import = False
            raise PlatformError('lost import response')
        return {'archive': deepcopy(self.detail), 'reused': reused}


def history_queue(local_record, tmp_path, client):
    service, _, review = local_record
    path = tmp_path / 'history.csv'; path.write_text('time_s,hip_pos_rad\n100,0\n180,1\n')
    item = service.import_history([str(path)], {'test_name': '合成历史', 'actual_object': '对象', 'bench_id': 'A10', 'operator_id': '测试人员'})[0]
    row = service.submit_execution(item['test_id'], review=review)
    service.outbox.update(row['execution_uuid'], submission_owner='submit-account')
    worker = ReliabilityUploaderWorker(service.outbox, client); worker.authenticate('submit-account', 'fake')
    return service, item, row['execution_uuid'], worker


def test_history_unknown_facts_uses_import_and_persists_canonical_binding(local_record, tmp_path):
    client = ImportClient(); service, item, local_uuid, worker = history_queue(local_record, tmp_path, client)
    summary = service.execution_summary(item['test_id'])
    assert summary['timing']['active_seconds'] is None and summary['timing']['completed_cycles'] is None
    assert summary['quality']['sample_completeness'] is None
    worker.process_once()
    row = service.outbox.get(local_uuid)
    binding = json.loads(row.submission_payload)['archive_binding']
    assert binding['local_execution_uuid'] == local_uuid and binding['server_execution_uuid'] == CANONICAL
    assert row.upload_id == ARCHIVE
    assert row.state == 'awaiting_classification'
    assert [call[0] for call in client.calls] == ['import', 'chunk', 'complete', 'verification']
    client.calls.clear(); service.outbox.retry(local_uuid)
    reloaded = ReliabilityUploaderWorker(service.outbox, client); reloaded.authenticate('submit-account', 'fake')
    reloaded.process_once()
    assert client.calls == []


def test_lost_import_receipt_replays_frozen_request_and_never_guesses_uuid(local_record, tmp_path):
    client = ImportClient(); client.lose_import = True
    service, _, local_uuid, worker = history_queue(local_record, tmp_path, client)
    worker.process_once(); assert not service.outbox.get(local_uuid).upload_id
    frozen = service.outbox.get(local_uuid).submission_payload
    service.outbox.retry(local_uuid); worker.process_once()
    calls = [call for call in client.calls if call[0] == 'import']
    assert calls[0] == calls[1]
    assert json.loads(frozen)['create'] == json.loads(service.outbox.get(local_uuid).submission_payload)['create']
    assert service.outbox.get(local_uuid).upload_id == ARCHIVE


def test_import_reused_approved_evidence_is_read_only(local_record, tmp_path):
    client = ImportClient(); service, _, local_uuid, worker = history_queue(local_record, tmp_path, client)
    row = service.outbox.get(local_uuid); raw = row.manifest['baseline_evidence']['files']['telemetry/raw.csv']
    client.detail = {'id': ARCHIVE, 'execution_uuid': CANONICAL, 'revision': 9, 'received_bytes': raw['size_bytes'],
        'processing_state': 'READY', 'classification_state': 'CLASSIFIED', 'sha256': raw['sha256'], 'size_bytes': raw['size_bytes'],
        'is_owner': False, 'can_edit': False, 'current_verification': {'revision': 2}, 'submission_status': 'APPROVED', 'submission_revision': 4}
    worker.process_once()
    assert [call[0] for call in client.calls] == ['import']
    assert service.outbox.get(local_uuid).state == 'approved'


def test_classification_poll_never_reverifies_or_submits(local_record):
    service, _, review = local_record
    client = ArchiveClient(); worker = worker_for(service, review, client); worker.process_once()
    assert service.outbox.get(ID).state == 'awaiting_classification'
    frozen = service.outbox.get(ID).submission_payload
    client.calls.clear(); client.detail['classification_state'] = 'CLASSIFIED'
    service.outbox.retry(ID); worker.process_once()
    assert client.calls == [] and service.outbox.get(ID).submission_payload == frozen
    assert service.outbox.get(ID).state == 'awaiting_classification'
    assert service.outbox.get(ID).remote_state == 'CLASSIFIED'


def test_old_unbound_verification_is_not_silently_signed(local_record):
    service, report, review = local_record
    legacy_manifest = {**report.manifest, 'archive_protocol': 'reliability_v1_csv',
        'csv_path': str(report.execution_dir / 'telemetry/raw.csv'), 'operator_review': review}
    service.outbox.enqueue(legacy_manifest, report.bundle_path)
    service.outbox.update(ID, submission_owner='submit-account')
    before = sealed_bytes(report)
    client = ArchiveClient(); worker = ReliabilityUploaderWorker(service.outbox, client); worker.authenticate('submit-account', 'fake')
    worker.process_once()
    assert client.calls == []
    assert '重新核实' in service.outbox.get(ID).last_error
    assert not service.outbox.get(ID).submission_payload
    assert sealed_bytes(report) == before


def test_old_pending_request_is_only_reconciled_even_without_new_binding(local_record):
    service, report, review = local_record
    legacy_manifest = {**report.manifest, 'archive_protocol': 'reliability_v1_csv',
        'csv_path': str(report.execution_dir / 'telemetry/raw.csv'), 'operator_review': review}
    service.outbox.enqueue(legacy_manifest, report.bundle_path)
    payload = '{ "create" : {"old":true} }'
    service.outbox.update(ID, submission_owner='submit-account', state='pending_review', remote_state='PENDING',
        upload_id=ARCHIVE, submission_id='33333333-3333-4333-8333-333333333333', submission_payload=payload)
    client = ArchiveClient(); client.detail = {'id': ARCHIVE, 'execution_uuid': ID, 'submission_status': 'PENDING'}
    client.receipt['status'] = 'APPROVED'
    worker = ReliabilityUploaderWorker(service.outbox, client); worker.authenticate('submit-account', 'fake')
    report.bundle_path.write_bytes(b'synthetic unavailable original file')
    worker.process_once()
    assert client.calls == []
    assert service.outbox.get(ID).state == 'approved'
    assert service.outbox.get(ID).submission_payload == payload


def test_poll_network_failure_cannot_turn_into_mutating_retry(local_record):
    service, _, review = local_record
    client = ArchiveClient(); worker = worker_for(service, review, client); worker.process_once()
    original = client.archive
    def fail(_id):
        raise PlatformError('synthetic offline')
    client.archive = fail; client.calls.clear()
    service.outbox.retry(ID); worker.process_once()
    assert service.outbox.get(ID).state == 'awaiting_classification'
    client.archive = original
    service.outbox.retry(ID); worker.process_once()
    assert client.calls == []


def test_history_sidecar_claims_are_not_trusted_acquisition_facts(local_record, tmp_path):
    service, _, _ = local_record
    path = tmp_path / 'claimed.csv'; path.write_text('time_s,hip_pos_rad\n100,0\n180,1\n')
    path.with_suffix('.context.json').write_text(json.dumps({'active_seconds': 80, 'completed_cycles': 500,
        'machine_status': 'completed', 'outcome': 'completed', 'started_at': '2020-01-01T00:00:00Z', 'ended_at': '2020-01-01T01:00:00Z'}))
    item, = service.import_history([str(path)], {'test_name': '声明历史', 'actual_object': '对象', 'bench_id': 'A10', 'operator_id': '人员'})
    summary = service.execution_summary(item['test_id'])
    assert summary['timing']['active_seconds'] is None
    assert summary['timing']['completed_cycles'] is None
    manifest = service.report_manager.manifest(item['test_id'])
    assert manifest['machine_status'] == 'unknown'
    context = json.loads((service.report_manager.execution_dir(item['test_id']) / 'config/execution.json').read_text())
    assert context['history_declared_context']['active_seconds'] == 80


def test_reanalysis_actor_comes_from_authenticated_gateway_identity(local_record):
    from factory_hmi.gateway.app import _PlatformCoordinator
    from factory_hmi.gateway.schemas import ExecutionActionRequest
    service, _, _ = local_record
    coordinator = _PlatformCoordinator(service.outbox)
    called = []
    with pytest.raises(ValueError, match='登录'):
        coordinator.analyze(lambda actor: called.append(actor))
    assert called == []
    coordinator._identity = 'actual-signed-in-account'
    coordinator.analyze(lambda actor: called.append(actor))
    assert called == ['actual-signed-in-account']
    with pytest.raises(ValueError):
        ExecutionActionRequest(test_id='synthetic-test', reason='测试', actor='forged')


def test_gateway_runtime_injects_actor_and_previews_only_appended_version(local_record, tmp_path):
    from factory_hmi.gateway.app import GatewayRuntime, _PlatformCoordinator
    service, report, _ = local_record
    coordinator = _PlatformCoordinator(service.outbox)
    coordinator._identity = 'signed-in-owner'
    runtime = GatewayRuntime(service, data_root=tmp_path / 'gateway', outbox=service.outbox, platform=coordinator)
    before = sealed_bytes(report)
    result = runtime.rebuild_execution(report.manifest['test_id'], '明确追加分析原因')
    assert result['analysis_revision']['actor'] == 'signed-in-owner'
    versions = service.execution_analysis_versions(report.manifest['test_id'])
    assert versions['submission_baseline'] == 'baseline-v1'
    assert versions['items'][0]['report_text'] == result['report_text']
    assert all(Path(name).read_bytes() == data for name, data in before.items())
    assert service.outbox.list() == []


def test_gateway_client_sends_reason_but_never_actor():
    from factory_hmi.client import GatewayClient
    from unittest.mock import Mock
    client = GatewayClient('https://synthetic.invalid')
    client.post = Mock(return_value={}); client.get = Mock(return_value={})
    client.rebuild_execution('test', '新算法复验')
    client.post.assert_called_once_with('/api/v1/executions/rebuild', {'test_id': 'test', 'reason': '新算法复验'})
    client.execution_analysis_versions('test')
    client.get.assert_called_once_with('/api/v1/executions/test/analysis-revisions')


def test_recovered_old_sidecar_is_retained_as_claims_not_runtime(tmp_path):
    records = tmp_path / 'records'; records.mkdir()
    raw = records / 'orphan.csv'; raw.write_text('time_s,hip_pos_rad\n10,0\n90,1\n')
    context = {'test_id': 'orphan', 'execution_uuid': ID, 'active_seconds': 999,
        'completed_cycles': 123, 'outcome': 'completed', 'machine_status': 'completed'}
    sidecar = raw.with_suffix('.context.json'); sidecar.write_text(json.dumps(context))
    original = sidecar.read_bytes()
    report, = ReportManager(tmp_path / 'data').recover_orphans(records)
    assert report.manifest['origin'] == 'HISTORY' and report.manifest['machine_status'] == 'unknown'
    assert report.summary['timing']['active_seconds'] is None
    frozen = json.loads((report.execution_dir / 'config/execution.json').read_text())
    assert frozen['history_declared_context'] == context
    assert frozen['history_context_sha256'] == hashlib.sha256(original).hexdigest()
    assert sidecar.read_bytes() == original


def test_old_history_strict_frozen_request_is_not_rebound_or_resubmitted(local_record, tmp_path):
    client = ImportClient(); service, item, local_uuid, worker = history_queue(local_record, tmp_path, client)
    payload = '{ "create" : { "facts" : { "active_seconds" : 80 } } }'
    service.outbox.update(local_uuid, submission_payload=payload, state='needs_verification')
    before = sealed_bytes(type('Report', (), {'execution_dir': service.report_manager.execution_dir(item['test_id']), 'bundle_path': service.report_manager.bundle_path(item['test_id'])}))
    with pytest.raises(ValueError, match='核对回执'):
        service.submit_execution(item['test_id'], review=local_record[2])
    assert all(Path(name).read_bytes() == data for name, data in before.items())
    assert service.outbox.get(local_uuid).submission_payload == payload
    service.outbox.update(local_uuid, state='local_completed')
    worker.process_once()
    assert client.calls == [] and service.outbox.get(local_uuid).submission_payload == payload


def test_desktop_reanalysis_requires_reason_and_reports_local_version(monkeypatch):
    pytest.importorskip('PySide6')
    from types import SimpleNamespace
    from unittest.mock import Mock
    from factory_hmi.desktop.app import FactoryMainWindow, QInputDialog
    captured = []
    owner = SimpleNamespace(_selected_test_id=lambda: 'synthetic-test', client=SimpleNamespace(rebuild_execution=Mock(return_value={})),
        _run_rest=lambda *args: captured.append(args), _submission_action_ok=Mock(), _show_local_analysis=Mock())
    monkeypatch.setattr(QInputDialog, 'getText', lambda *_args: ('核对新分析算法', True))
    FactoryMainWindow._rebuild_execution(owner)
    assert captured[0][0] == '追加本地分析'
    captured[0][1]()
    owner.client.rebuild_execution.assert_called_once_with('synthetic-test', '核对新分析算法')
    result = {'analysis_revision': {'revision': 3, 'actor': 'signed-in', 'reason': '核对新分析算法'}, 'report_text': 'version-three'}
    captured[0][2]({'result': result})
    assert '仍绑定原始基线' in owner._submission_action_ok.call_args.args[0]
    assert owner._show_local_analysis.call_args.args[0]['items'][0]['report_text'] == 'version-three'
    captured.clear(); monkeypatch.setattr(QInputDialog, 'getText', lambda *_args: ('', False))
    FactoryMainWindow._rebuild_execution(owner)
    assert captured == []


def test_desktop_version_view_is_readonly_and_selects_exact_version(monkeypatch):
    pytest.importorskip('PySide6')
    from factory_hmi.desktop.app import FactoryMainWindow, QApplication, QWidget, QDialog, QTextEdit, QComboBox
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    seen = []
    def inspect(dialog):
        text, = dialog.findChildren(QTextEdit)
        versions, = dialog.findChildren(QComboBox)
        assert text.isReadOnly() and versions.count() == 2
        assert '第二份结果 <b>literal</b>' in text.toPlainText()
        versions.setCurrentIndex(0)
        assert '第一份结果' in text.toPlainText() and '第二份结果' not in text.toPlainText()
        seen.append(True)
        return 0
    monkeypatch.setattr(QDialog, 'exec', inspect)
    FactoryMainWindow._show_local_analysis(parent, {'items': [
        {'revision': 1, 'created_at': 'time1', 'actor': 'account1', 'reason': '原因1', 'version_sha256': 'sha1', 'report_text': '第一份结果'},
        {'revision': 2, 'created_at': 'time2', 'actor': 'account2', 'reason': '原因2', 'version_sha256': 'sha2', 'report_text': '第二份结果 <b>literal</b>'}]})
    assert seen == [True]
    parent.close(); parent.deleteLater(); app.processEvents()
