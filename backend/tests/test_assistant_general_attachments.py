"""Original diagnostic bytes, task scope, bounded archives and full CSV evidence."""
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import tarfile
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4
import zipfile

import pytest
from PIL import Image
from pydantic import ValidationError
from app.core.errors import ApiError
from app.services import assistant_attachments as storage
from app.services import assistant_attachment_files as files
from app.services.assistant_attachment_queries import read_diagnostic_attachment
from app.schemas.assistant_chat import ChatSubmit, DiagnosticAttachmentArguments


@pytest.fixture
def area(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'get_settings', lambda: SimpleNamespace(assistant_attachment_root=str(tmp_path)))
    return SimpleNamespace(public_id=uuid4()), SimpleNamespace(public_id=uuid4()), tmp_path


def query(actor, frozen, state, **kwargs):
    return read_diagnostic_attachment(actor, frozen, {'attachment_id': frozen[0]['id'], **kwargs}, read_state=state)


def archive(kind='zip', entries=None):
    entries = entries or [('data.csv', b'time_s,value\n0,1\n1,9\n'), ('src/run.py', b'raise RuntimeError("NEVER EXECUTE")'), ('payload.bin', b'\x00\xff\x81')]
    stream = io.BytesIO()
    if kind == 'zip':
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as target:
            for name, content in entries:
                target.writestr(name, content)
    else:
        with tarfile.open(fileobj=stream, mode='w:gz' if kind == 'tar.gz' else 'w') as target:
            for name, content in entries:
                item = tarfile.TarInfo(name)
                item.size = len(content)
                target.addfile(item, io.BytesIO(content))
    return stream.getvalue()


@pytest.mark.parametrize('name,mime,raw,kind', [
    ('trace.csv', 'text/csv', b'a,b\n1,2\n', 'text'),
    ('script.py', 'application/octet-stream', b'print("do not execute")\n', 'text'),
    ('driver.unknown', 'application/octet-stream', b'\x00\x81\xff', 'binary'),
    ('release.deb', 'application/vnd.debian.binary-package', b'!<arch>\n\x00', 'binary'),
    ('manual.pdf', 'application/pdf', b'%PDF\x00\xff', 'binary'),
    ('diagram.svg', 'image/svg+xml', b'<svg><script>NEVER EXECUTE</script></svg>', 'text'),
    ('no-extension', 'application/octet-stream', b'\x00\xff\x80', 'binary'),
])
def test_any_extension_preserves_original_bytes_and_source_hash(area, name, mime, raw, kind):
    actor, _, _ = area
    meta = storage.upload(actor, raw, name, mime)
    assert meta['kind'] == kind and meta['diagnostic_only']
    assert meta['sha256'] == meta['source_sha256'] == hashlib.sha256(raw).hexdigest()
    assert storage.read(actor, meta['id'])[1] == raw
    assert not any(word in str(meta) for word in ('submission_id', 'archive_id', 'APPROVED'))


def test_original_image_is_preserved_with_compatible_png_model_preview(area):
    actor, _, _ = area
    stream = io.BytesIO()
    Image.new('RGB', (50, 30), 'red').save(stream, format='JPEG')
    raw = stream.getvalue()
    meta = storage.upload(actor, raw, 'shot.jpg', 'image/jpeg')
    state = {}
    content = storage.model_content(actor, [meta], state)
    assert storage.read(actor, meta['id'])[1] == raw
    assert content[0]['type'] == 'image' and content[0]['mimeType'] == 'image/png'
    assert base64.b64decode(content[0]['data']).startswith(b'\x89PNG')
    assert storage.attachment_read_summary([meta], state)['attachments'][0]['image_preview_provided']


@pytest.mark.parametrize('format,mime', [('TIFF', 'image/tiff'), ('BMP', 'image/bmp')])
def test_other_image_formats_preserved_as_general_files(area, format, mime):
    actor, _, _ = area
    stream = io.BytesIO();Image.new('RGB', (8, 8), 'red').save(stream, format=format)
    raw = stream.getvalue()
    meta = storage.upload(actor, raw, 'picture.' + format.lower(), mime)
    assert meta['kind'] == 'binary' and 'not_visually_decoded' in meta['visual_status']
    assert storage.read(actor, meta['id'])[1] == raw


def test_octet_stream_real_png_is_visual_and_svg_is_never_rendered(area):
    actor, _, _ = area
    stream = io.BytesIO();Image.new('RGB', (8, 8), 'red').save(stream, format='PNG')
    meta = storage.upload(actor, stream.getvalue(), 'unknown.data', 'application/octet-stream')
    assert meta['kind'] == 'image' and meta['mime'] == 'image/png'
    assert storage.model_content(actor, [meta])[0]['type'] == 'image'


def test_preview_budget_and_secret_redaction_do_not_rewrite_private_original(area):
    actor, _, _ = area
    raw = b'password=synthetic-secret\n' + b'long source line\n' * 10000
    metadata = [storage.upload(actor, raw, f'log-{i}.txt', 'text/plain') for i in range(3)]
    state = {}
    content = storage.model_content(actor, metadata, state)
    text = ''.join(x['text'] for x in content)
    assert len(text) <= 24000 and 'synthetic-secret' not in text
    assert all(meta['id'] in text for meta in metadata)
    assert all(storage.read(actor, meta['id'])[1] == raw for meta in metadata)
    summary = storage.attachment_read_summary(metadata, state)
    assert all(r['provided'][0]['unprovided_byte_ranges'] for r in summary['attachments'])


def test_dense_redaction_preview_keeps_every_mixed_attachment_discoverable(area):
    actor, _, _ = area
    dense = b'password=x\n' * 12000
    raws = [dense, b'\x00\xff\x81', b'time_s,value\n0,1\n1,9\n']
    metadata = [storage.upload(actor, raw, name, 'application/octet-stream')
                for raw, name in zip(raws, ['dense.log', 'unknown.bin', 'facts.csv'])]
    state = {}
    content = storage.model_content(actor, metadata, state)
    text = ''.join(row['text'] for row in content if row['type'] == 'text')
    assert len(text) <= storage.MODEL_TEXT_LIMIT
    assert all(meta['id'] in text for meta in metadata)
    assert 'password=x' not in text
    assert [storage.read(actor, meta['id'])[1] for meta in metadata] == raws
    rows = storage.attachment_read_summary(metadata, state)['attachments']
    assert rows[0]['provided'][0]['provided_byte_ranges'][0][0] == 0
    assert 0 < rows[0]['provided'][0]['provided_byte_ranges'][0][1] < len(dense)
    assert rows[0]['provided'][0]['unprovided_byte_ranges']
    assert rows[1]['members_without_content_evidence'] == 1


def test_csv_requested_range_complete_is_distinct_from_full_file(area):
    actor, _, _ = area
    meta = storage.upload(actor, b'value\n1\n2\n3\n', 'range.csv', 'text/csv')
    result = query(actor, [meta], {}, operation='csv_statistics', rows_limit=2)
    data = result['data']
    assert data['full_requested_row_range_processed']
    assert not data['full_file_statistics']
    assert data['next_rows_start'] == 2
    assert data['range']['rows_analyzed'] == 2
    assert data['range']['total_data_rows'] is None


def test_invalid_utf16_is_retained_as_binary_not_claimed_decoded_csv(area):
    actor, _, _ = area
    raw = b'\xff\xfeA'
    meta = storage.upload(actor, raw, 'broken.csv', 'text/csv')
    assert meta['kind'] == 'binary' and storage.read(actor, meta['id'])[1] == raw
    with pytest.raises(ApiError) as rejected:
        query(actor, [meta], {}, operation='csv_statistics')
    assert rejected.value.code == 'attachment_csv_encoding'


@pytest.mark.parametrize('kind', ['zip', 'tar', 'tar.gz'])
def test_all_regular_formats_indexed_and_read_without_extracting_or_executing(area, kind):
    actor, _, root = area
    raw = archive(kind)
    meta = storage.upload(actor, raw, '材料.' + kind, 'application/octet-stream')
    assert meta['kind'] == 'archive' and storage.read(actor, meta['id'])[1] == raw
    state = {}
    page = query(actor, [meta], state, operation='index', limit=1)
    assert page['data']['total'] == 3 and page['data']['next_offset'] == 1
    rest = query(actor, [meta], state, operation='index', offset=1, limit=128)
    assert len(rest['data']['members']) == 2 and rest['data']['complete']
    script = query(actor, [meta], state, member='src/run.py')
    assert 'NEVER EXECUTE' in script['data']['text']
    binary = query(actor, [meta], state, member='payload.bin')
    assert base64.b64decode(binary['data']['base64']) == b'\x00\xff\x81'
    assert binary['data']['unsupported_semantics']
    statistics = query(actor, [meta], state, member='data.csv', operation='csv_statistics')
    assert statistics['data']['full_file_statistics'] and statistics['data']['columns'][1]['mean'] == 5
    assert not (root / 'src').exists()
    assert not list(root.rglob('run.py'))


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/escape', 'folder/../../escape', 'a\\b', 'a//b', 'a/./b'])
@pytest.mark.parametrize('kind', ['zip', 'tar.gz'])
def test_archive_paths_cannot_escape_or_be_ambiguous(area, name, kind):
    actor, _, root = area
    with pytest.raises(ApiError) as rejected:
        storage.upload(actor, archive(kind, [(name, b'payload')]), 'attack.' + kind, 'application/octet-stream')
    assert rejected.value.code == 'attachment_archive_unsafe'
    assert not list(root.rglob('metadata.json'))


@pytest.mark.parametrize('kind', ['zip', 'tar', 'tar.gz'])
def test_archive_links_and_special_devices_rejected(area, kind):
    actor, _, _ = area
    stream = io.BytesIO()
    if kind == 'zip':
        with zipfile.ZipFile(stream, 'w') as target:
            info = zipfile.ZipInfo('link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            target.writestr(info, '/etc/passwd')
    else:
        with tarfile.open(fileobj=stream, mode='w:gz' if kind == 'tar.gz' else 'w') as target:
            info = tarfile.TarInfo('device')
            info.type = tarfile.CHRTYPE
            target.addfile(info)
    with pytest.raises(ApiError):
        storage.upload(actor, stream.getvalue(), 'attack.' + kind, 'application/octet-stream')


def test_zip_nul_duplicate_bomb_and_declared_limits_are_rejected(area, monkeypatch):
    actor, _, _ = area
    duplicate = archive('zip', [('same', b'1'), ('same', b'2')])
    nul = archive('zip', [('name', b'1')]).replace(b'name', b'n\x00me')
    bomb = archive('zip', [('zeros', b'\x00' * (2 * 1024**2))])
    for raw in (duplicate, nul, bomb):
        with pytest.raises(ApiError):
            storage.upload(actor, raw, 'bad.zip', 'application/octet-stream')
    monkeypatch.setattr(files, 'MAX_MEMBER_BYTES', 4)
    with pytest.raises(ApiError):
        storage.upload(actor, archive('zip', [('large', b'12345')]), 'large.zip', 'application/octet-stream')


def test_member_count_and_total_expansion_limits(area, monkeypatch):
    actor, _, _ = area
    monkeypatch.setattr(files, 'MAX_MEMBERS', 2)
    with pytest.raises(ApiError):
        storage.upload(actor, archive('zip'), 'many.zip', 'application/octet-stream')
    monkeypatch.setattr(files, 'MAX_MEMBERS', 10)
    monkeypatch.setattr(files, 'MAX_EXPANDED_BYTES', 10)
    with pytest.raises(ApiError):
        storage.upload(actor, archive('tar', [('a', b'123456'), ('b', b'123456')]), 'large.tar', 'application/octet-stream')


def test_forged_zip_count_is_checked_before_zipfile_allocates(area, monkeypatch):
    actor, _, _ = area
    raw = bytearray(archive('zip'))
    end = raw.rfind(b'PK\x05\x06')
    raw[end + 8:end + 12] = struct.pack('<HH', 1, 1)
    monkeypatch.setattr(files, 'MAX_MEMBERS', 2)
    monkeypatch.setattr(files.zipfile, 'ZipFile', lambda *args, **kwargs: pytest.fail('must reject directory before allocating entry objects'))
    with pytest.raises(ApiError):
        storage.upload(actor, bytes(raw), 'forged.zip', 'application/octet-stream')


def test_tar_gzip_trailer_and_hidden_concatenated_archive_rejected(area):
    actor, _, _ = area
    compressed = bytearray(archive('tar.gz'))
    compressed[-1] ^= 1
    for name, raw in [('bad.tar.gz', bytes(compressed)), ('hidden.tar', archive('tar') + archive('tar', [('hidden', b'not indexed')]))]:
        with pytest.raises(ApiError):
            storage.upload(actor, raw, name, 'application/octet-stream')


def test_frozen_task_scope_owner_expiration_and_tamper_remain_enforced(area):
    actor, other, root = area
    meta = storage.upload(actor, b'trace', 'trace.txt', 'text/plain')
    unknown = storage.upload(actor, b'other', 'other.txt', 'text/plain')
    with pytest.raises(ApiError):
        read_diagnostic_attachment(actor, [meta], {'attachment_id': unknown['id']}, read_state={})
    with pytest.raises(ApiError):
        query(other, [meta], {})
    with pytest.raises(ApiError):
        read_diagnostic_attachment(actor, [meta], {'attachment_id': meta['id']})
    path = root / str(actor.public_id) / meta['id']
    original = (path / 'metadata.json').read_bytes()
    changed = json.loads(original);changed['name'] = 'different.txt'
    (path / 'metadata.json').write_text(json.dumps(changed))
    with pytest.raises(ApiError):
        query(actor, [meta], {})
    (path / 'metadata.json').write_bytes(original)
    (path / 'content').write_bytes(b'other')
    with pytest.raises(ApiError):
        query(actor, [meta], {})
    (path / 'content').write_bytes(b'trace')
    changed = json.loads(original);changed['expires_at'] = '2000-01-01T00:00:00+00:00'
    (path / 'metadata.json').write_text(json.dumps(changed))
    with pytest.raises(ApiError):
        query(actor, [meta], {})


def test_symlink_and_index_tamper_rejected(area):
    actor, _, root = area
    meta = storage.upload(actor, archive('zip'), 'safe.zip', 'application/octet-stream')
    folder = root / str(actor.public_id) / meta['id']
    (folder / 'index.json').write_text('{}')
    with pytest.raises(ApiError):
        query(actor, [meta], {}, operation='index')
    raw = (folder / 'content').read_bytes()
    (folder / 'content').unlink()
    target = root / 'original';target.write_bytes(raw)
    (folder / 'content').symlink_to(target)
    with pytest.raises(ApiError):
        storage.read(actor, meta['id'])


def test_32mib_nonempty_512mib_quota_and_three_attachment_contract(area, monkeypatch):
    actor, _, root = area
    assert storage.MAX_BYTES == 32 * 1024**2 and storage.ACCOUNT_QUOTA == 512 * 1024**2
    monkeypatch.setattr(storage, 'MAX_BYTES', 4)
    for raw in (b'', b'12345'):
        with pytest.raises(ApiError):
            storage.upload(actor, raw, 'file', 'application/octet-stream')
    storage.upload(actor, b'1234', 'one', 'text/plain')
    stored = sum(p.stat().st_size for p in (root / str(actor.public_id)).glob('*/*') if p.is_file())
    monkeypatch.setattr(storage, 'ACCOUNT_QUOTA', stored + 1)
    with pytest.raises(ApiError):
        storage.upload(actor, b'12', 'two', 'text/plain')
    with pytest.raises(ValidationError):
        ChatSubmit(request_key=uuid4(), message='diagnose', attachments=[uuid4() for _ in range(4)])


def test_csv_full_tail_statistics_range_and_read_receipt_are_separate(area):
    actor, _, _ = area
    raw = ('time_s,value\n' + ''.join(f'{i},{i}\n' for i in range(10000)) + '10000,1000000\n').encode()
    meta = storage.upload(actor, raw, 'large.csv', 'text/csv')
    state = {}
    storage.model_content(actor, [meta], state)
    result = query(actor, [meta], state, operation='csv_statistics')
    assert result['data']['full_file_statistics']
    column = result['data']['columns'][1]
    assert column['maximum'] == 1000000 and column['maximum_row'] == 10000
    assert column['numeric_count'] == 10001 and result['sha256'] == hashlib.sha256(raw).hexdigest()
    assert result['unread']['raw_byte_ranges']  # statistics are not full raw text delivered to model
    restricted = query(actor, [meta], state, operation='csv_statistics', rows_start=3, rows_limit=2, columns=['value'])
    assert restricted['data']['columns'][0]['mean'] == 3.5
    assert not restricted['data']['full_file_statistics']
    assert restricted['data']['range']['rows_analyzed'] == 2
    summary = storage.attachment_read_summary([meta], state)
    storage.delete(actor, meta['id'])
    assert storage.attachment_read_summary([meta], state) == summary  # pure, no file reads
    summary['attachments'].clear()
    assert storage.attachment_read_summary([meta], state)['attachments']


def test_csv_missing_nonfinite_formula_and_malformed_rows_are_facts(area):
    actor, _, _ = area
    raw = b'a,b\n1,NaN\n,2\n=EXECUTE(),3\n4\n'
    meta = storage.upload(actor, raw, 'measurements.csv', 'text/csv')
    data = query(actor, [meta], {}, operation='csv_statistics')['data']
    assert data['columns'][0]['missing_count'] == 1 and data['columns'][0]['non_numeric_count'] == 1
    assert data['columns'][1]['non_finite_count'] == 1 and data['malformed_rows_in_range'] == 1
    assert data['columns'][1]['missing_count'] == 1


def test_chunk_cursor_binary_roundtrip_and_arguments_cannot_escape(area):
    actor, _, _ = area
    raw = bytes(range(256)) * 100
    meta = storage.upload(actor, raw, 'unknown.bin', 'application/octet-stream')
    state = {};chunks=[];offset=0
    while True:
        result = query(actor, [meta], state, offset=offset, limit=4096)
        assert len(json.dumps(result).encode()) <= 90 * 1024
        chunks.append(base64.b64decode(result['data']['base64']))
        offset = result['data']['next_offset']
        if offset is None:
            break
    assert b''.join(chunks) == raw and not result['unread']['unprovided_byte_ranges']
    for change in ({'offset': -1}, {'offset': True}, {'limit': 65537}, {'path': '/etc/passwd'}):
        with pytest.raises(ValidationError):
            query(actor, [meta], {}, **change)
    with pytest.raises(ApiError):
        query(actor, [meta], {}, offset=len(raw) + 1)


def test_upload_http_filename_headers_and_diagnostic_only(area, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routes import assistant_chat as routes
    from app.core.auth import get_request_actor
    actor, _, _ = area
    monkeypatch.setattr(routes, 'require_runtime', lambda current: None)
    app = FastAPI();app.include_router(routes.router)
    app.dependency_overrides[get_request_actor] = lambda: actor
    with TestClient(app) as client:
        for header in ('X-Filename-UTF8', 'X-Filename'):
            response = client.post('/api/v1/assistant/attachments', content=b'a,b\n1,2\n',
                                   headers={header: quote('中文 测量.csv'), 'Content-Type': 'text/csv'})
            assert response.status_code == 201, response.text
            meta = response.json()['data']
            assert meta['name'] == '中文 测量.csv' and meta['diagnostic_only']
            downloaded = client.get('/api/v1/assistant/attachments/' + meta['id'])
            assert downloaded.content == b'a,b\n1,2\n' and downloaded.headers['x-content-type-options'] == 'nosniff'


def test_checksums_survive_privacy_while_phone_and_invalid_checksums_are_redacted():
    from app.services.assistant_privacy import public_evidence
    digest = 'a' + '13800138000' + 'b' * 52
    for field in ('sha256', 'source_sha256', 'index_sha256', 'content_sha256', 'file_sha256'):
        assert public_evidence({field: digest})[field] == digest
        assert '13800138000' not in public_evidence({field: 'phone 13800138000'})[field]
    assert '13800138000' not in public_evidence({'text': 'phone 13800138000'})['text']
    assert 'password' not in public_evidence({'password': digest})


def test_csv_timeout_or_empty_measurements_never_claim_complete_numeric_evidence(area, monkeypatch):
    from app.services import assistant_attachment_queries as queries
    actor, _, _ = area
    meta = storage.upload(actor, b'a,b\n1,2\n3,4\n', 'data.csv', 'text/csv')
    monkeypatch.setattr(queries, 'CSV_SECONDS', 0)
    data = query(actor, [meta], {}, operation='csv_statistics')['data']
    assert data['status'] == 'partial' and not data['full_file_statistics']
    assert data['stop_reason'] == 'time_limit' and not data['has_measurements']


def test_result_size_rejection_does_not_mark_silently_dropped_evidence(area):
    actor, _, _ = area
    meta = storage.upload(actor, b'password=x\n' * 10000, 'private.log', 'text/plain')
    state = {}
    with pytest.raises(ApiError) as rejected:
        query(actor, [meta], state, limit=65536)
    assert rejected.value.code == 'attachment_result_limit'
    assert meta['id'] not in state


def test_orphan_upload_cleanup_keeps_completed_original(area):
    actor, _, root = area
    completed = storage.upload(actor, b'old completed', 'old.txt', 'text/plain')
    orphan = root / str(actor.public_id) / '.incoming-interrupted'
    orphan.mkdir();(orphan / 'content').write_bytes(b'not published')
    storage.upload(actor, b'new complete', 'new.txt', 'text/plain')
    assert not orphan.exists()
    assert storage.read(actor, completed['id'])[1] == b'old completed'


def test_member_selector_survives_redacted_display_path_without_bypassing_scope(area):
    from app.services.assistant_privacy import public_evidence
    actor, _, _ = area
    name = 'diagnostic-13800138000.csv'
    meta = storage.upload(actor, archive('zip', [(name, b'a,b\n1,2\n')]), 'private.zip', 'application/octet-stream')
    state = {}
    page = public_evidence(query(actor, [meta], state, operation='index'))
    member = page['data']['members'][0]
    assert '13800138000' not in member['path']
    result = query(actor, [meta], state, member=member['member_id'])
    assert result['data']['text'] == 'a,b\n1,2\n'
    with pytest.raises(ApiError):
        query(actor, [meta], state, member='id:' + '-'.join(['0' * 8] * 8))
