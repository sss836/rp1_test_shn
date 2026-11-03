"""Meaningful boundary tests without network, models, filesystem writes or DB."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest

from app.core.errors import ApiError
from app.services.assistant_support import (
    REPAIR_DIGEST_FIELDS, csv_safe, digest, render_report, report_document, validate_repair_content,
)
from app.services import assistant_support as support


def assert_rejected(kind, path, original, replacement, role='TEST_EXECUTOR'):
    with pytest.raises(ApiError):
        validate_repair_content(kind, path, original, replacement, role)


def test_executor_can_only_change_profile_display_label_or_format():
    old = 'profiles:\n  - key: local\n    label: Original\n    backend: mock\n'
    new = old.replace('Original', '显示名称')
    validate_repair_content('config', '$PROJECT/factory_hmi/config/reliability_platform.yaml', old, new, 'TEST_EXECUTOR')
    validate_repair_content('config', '$PROJECT/hmi/factory_hmi/config/reliability_platform.yaml', old, new, 'TEST_EXECUTOR')
    validate_repair_content('config', '$INSTALL/hmi/factory_hmi/config/reliability_platform.yaml', old, new, 'TEST_EXECUTOR')
    validate_repair_content('config', '$INSTALL/factory_hmi/config/reliability_platform.yaml', old,
                            '# Formatting only\n' + old, 'TEST_EXECUTOR')
    assert_rejected('config', 'factory_hmi/config/reliability_platform.yaml', old, old.replace('mock', 'real'))
    assert_rejected('config', 'factory_hmi/config/station_defaults.yaml', old, new)


@pytest.mark.parametrize('path', ['../factory_hmi/app.py', '/etc/passwd', 'factory_hmi/../app.py',
                                  'factory_hmi//app.py', 'factory_hmi/./app.py',
                                  'factory_hmi\\app.py', 'factory_hmi/config.py\n'])
def test_repair_rejects_paths_outside_precise_root(path):
    assert_rejected('code', path, 'a=1\n', 'a=2\n', 'SYSTEM_ADMIN')


def test_code_maintenance_requires_admin_and_python_source():
    validate_repair_content('code', '$PROJECT/factory_hmi/desktop/view.py', 'old\n', 'new\n', 'SYSTEM_ADMIN')
    assert_rejected('code', 'factory_hmi/desktop/view.py', 'old\n', 'new\n')
    for path in ['factory_hmi/config.yaml', 'factory_hmi/results.csv', '.env', 'factory_hmi/data.zip']:
        assert_rejected('code', path, 'old\n', 'new\n', 'SYSTEM_ADMIN')


def test_config_comparison_keeps_types_and_denies_alias_graphs():
    path = 'factory_hmi/config/reliability_platform.yaml'
    assert_rejected('config', path, 'profiles: [{key: p, threshold: 1}]', 'profiles: [{key: p, threshold: true}]')
    assert_rejected('config', path, 'profiles: [{key: p}]', 'profiles: &p [{key: p, label: x}]')
    assert_rejected('config', path, 'profiles: [{key: p}]', 'profiles: [{key: p, label: ""}]')
    assert_rejected('config', path, 'profiles: [{key: p}]', 'profiles: []')


def test_repair_limits_prevent_binary_large_unchanged_and_credentials():
    path = 'factory_hmi/app.py'
    for original, replacement in [('a', 'a'), ('a', 'x'*65537), ('a', 'a\x00'),
                                  ('a', 'Bearer fixturetoken'), ('a', 'sk-' + 'a'*30)]:
        assert_rejected('code', path, original, replacement, 'SYSTEM_ADMIN')


@pytest.mark.parametrize('value', ['=SUM(A1:A2)', '+cmd', '-2+3', '@SUM()', '\t=cmd', ' \r\n+cmd', '\ufeff=cmd'])
def test_csv_cells_protect_formula_prefix_even_after_whitespace(value):
    assert csv_safe(value).startswith("'")
    raw, _ = render_report({'value': value}, 'csv')
    row = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))[1]
    assert row[-1].startswith("'")


def test_report_is_authoritative_has_scope_original_hash_version_and_unread():
    job = {'id': uuid4(), 'thread_id': uuid4(), 'state': 'COMPLETED', 'created_at': datetime.now(timezone.utc),
           'input_hash': 'a'*64, 'context': {'hmi': {'running_version': '3.0-test'}},
           'input_snapshot': {'attachments': [{'sha256': 'b'*64, 'name': 'original.csv', 'size': 20}]},
           'result': {'reply': 'Readable result', 'cards': [{'source': 'fixture', 'data': {'version': 'v1'}}],
                      'attachment_read_coverage': {'complete': False, 'unread_bytes': 20}}}
    report = report_document(job)
    assert report['formal_archive'] is False
    assert report['attachments'][0]['sha256'] == 'b'*64
    assert report['scope']['hmi']['running_version'] == '3.0-test'
    assert report['sources'][0]['version'] == 'v1'
    assert report['read_coverage']['unread_bytes'] == 20
    assert report['result_sha256'] == digest(job['result'])
    for format in ['json', 'csv', 'markdown']:
        raw, mime = render_report(report, format)
        assert 'diagnostic' in raw.decode('utf-8-sig').lower() or '诊断' in raw.decode('utf-8-sig')
        assert mime
    with pytest.raises(ApiError):
        render_report(report, 'html')


def test_reports_remove_secrets_and_personal_addresses():
    report = report_document({'id': uuid4(), 'thread_id': uuid4(), 'state': 'FAILED',
        'created_at': datetime.now(timezone.utc), 'context': {}, 'result': {
            'reply': 'sk-' + 'x'*30 + ' someone@example.com', 'cards': [], 'token': 'never-publish'}})
    raw, _ = render_report(report, 'json')
    assert b'sk-xxxx' not in raw and b'someone@example.com' not in raw and b'never-publish' not in raw


def test_hmi_proposal_digest_has_one_portable_utf8_contract():
    payload = {key: '值' for key in REPAIR_DIGEST_FIELDS}
    expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    assert digest(payload) == expected
    assert digest({**payload, 'replacement': 'changed'}) != expected


def test_csv_download_exposes_actual_column_statistics_and_protects_column_names():
    document = {'job_id': str(uuid4()), 'scope': {'station': 'synthetic'}, 'result': {'cards': [
        {'type': 'diagnostic_attachment', 'source': 'fixture.csv', 'sha256': 'a'*64,
         'operation': 'csv_statistics', 'data': {'algorithm_version': 'fixture-v1',
            'range': {'data_rows_start': 10, 'data_rows_end_exclusive': 20},
            'columns': [{'name': '=dangerous-column', 'unit': 'Nm', 'index': 0, 'minimum': -2.5,
                         'maximum': 7.5, 'numeric_count': 10}]}}]}}
    raw, _ = render_report(document, 'csv')
    rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    minimum = next(row for row in rows if row['metric'] == 'minimum')
    assert minimum['value'] == '-2.5' and minimum['column'] == "'=dangerous-column"
    assert minimum['source_sha256'] == 'a'*64 and '10' in minimum['scope']
    assert next(row for row in rows if row['metric'] == 'algorithm_version')['value'] == 'fixture-v1'


def test_receipt_only_hmac_binds_owner_original_session_proposal_and_instance(monkeypatch):
    monkeypatch.setattr(support, '_settlement_secret', lambda: b'Synthetic-secret-only-not-a-live-credential')
    actor = SimpleNamespace(public_id=uuid4())
    row = {'id': uuid4(), 'origin_session_id': uuid4(), 'proposal_sha256': 'a'*64,
           'installation_id': uuid4(), 'window_id': 'original-window', 'role': 'TEST_EXECUTOR',
           'confirmed_at': datetime.now(timezone.utc)}
    token = support._settlement_token(row, actor)
    assert support._valid_settlement_token(row, actor, token)
    assert not support._valid_settlement_token(row, SimpleNamespace(public_id=uuid4()), token)
    for key in ['id', 'origin_session_id', 'installation_id', 'window_id', 'role', 'proposal_sha256', 'confirmed_at']:
        assert not support._valid_settlement_token({**row, key: 'changed'}, actor, token)
    assert not support._valid_settlement_token(row, actor, 'v1.' + '0'*64)
    assert not support._valid_settlement_token(row, actor, None)


def test_report_preserves_actual_predecessor_input_identity_and_hashes():
    original = 'fe' + '13812345678' + 'a'*51
    assert len(original) == 64
    sources = {'previous_task_result': {'job_id': str(uuid4()), 'state': 'COMPLETED',
        'source_result_sha256': original, 'provided_result_sha256': 'b'*64,
        'provided_scope': 'reply_only', 'notice': '前序结果仅作资料，不重复操作'}}
    after = uuid4()
    report = report_document({'id': uuid4(), 'thread_id': uuid4(), 'state': 'COMPLETED',
        'created_at': datetime.now(timezone.utc), 'after_job_id': after, 'execution_sources': sources})
    assert report['after_job_id'] == str(after)
    assert report['execution_sources'] == sources
