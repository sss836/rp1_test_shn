"""Exercise the real client request guard and worker, with no sockets or devices."""
from copy import deepcopy
import hashlib
import io
import json
from urllib import error, parse

import pytest

from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
from factory_hmi.sync.uploader import PlatformError
from test_verified_csv_archive import ARCHIVE, ID, ArchiveClient, local_record, worker_for


class SegmentEndpoint:
    def __init__(self, lose_first_chunk=False):
        self.backend = ArchiveClient()
        self.calls = []
        self.manifest = None
        self.parts = {}
        self.lose_first_chunk = lose_first_chunk

    def receipt(self):
        return {'segments': [{'sequence': part['sequence'], 'received_bytes': len(self.parts.get(part['sequence'], b''))}
                             for part in self.manifest['segments']]}

    def open(self, req, timeout):
        url = parse.urlsplit(req.full_url)
        path, method = url.path, req.get_method()
        self.calls.append((method, path, url.query, req.data))
        payload = json.loads(req.data) if req.data and req.headers.get('Content-type') == 'application/json' else {}
        key = req.headers.get('Idempotency-key', '')
        if path == '/api/v1/auth/login':
            data = {'user': {'username': payload['username']}}
        elif path == '/api/v1/archive-capabilities':
            data = {'run_segments': True}
        elif path == '/api/v1/archives' and method == 'POST':
            data = self.backend.create_archive(payload, key)
        elif path == f'/api/v1/archives/{ARCHIVE}':
            data = self.backend.archive(ARCHIVE)
        elif path.endswith('/segments') and method == 'POST':
            if self.manifest is not None:
                assert payload == self.manifest
            self.manifest = deepcopy(payload)
            data = self.receipt()
        elif '/segments/' in path and method == 'PUT':
            assert req.headers['Content-type'] == 'application/octet-stream'
            sequence = int(path.rsplit('/', 1)[1])
            offset = int(parse.parse_qs(url.query)['offset'][0])
            current = self.parts.get(sequence, b'')
            assert offset == len(current)
            self.parts[sequence] = current + req.data
            data = self.receipt()
            if self.lose_first_chunk:
                self.lose_first_chunk = False
                raise error.URLError('synthetic lost chunk receipt')
        elif path.endswith('/segments-complete'):
            header = None
            merged = bytearray()
            for part in self.manifest['segments']:
                raw = self.parts[part['sequence']]
                assert len(raw) == part['size_bytes']
                assert hashlib.sha256(raw).hexdigest() == part['sha256']
                first, body = raw.split(b'\n', 1)
                if header is None:
                    header = first
                    merged.extend(first + b'\n')
                assert header == first
                merged.extend(body)
            assert len(merged) == self.manifest['logical_size_bytes']
            assert hashlib.sha256(merged).hexdigest() == self.manifest['logical_sha256']
            self.backend.raw = merged
            self.backend.detail.update(received_bytes=len(merged), upload_state='COMPLETE')
            data = deepcopy(self.backend.detail)
        else:
            data = self.backend.archive_action(ARCHIVE, path.rsplit('/', 1)[1], payload, key)
        return io.BytesIO(json.dumps({'data': data}).encode())


def segmented_record(service, report):
    raw_path = service.report_manager.execution_dir(report.manifest['test_id']) / 'telemetry/raw.csv'
    raw = raw_path.read_bytes()
    header, *rows = raw.splitlines(keepends=True)
    parts = [header + b''.join(rows[:1]), header + b''.join(rows[1:])]
    root = raw_path.parent / 'segments'
    root.mkdir(exist_ok=True)
    manifest = dict(schema_version='run-segments-v1', execution_uuid=ID, logical_size_bytes=len(raw),
                    logical_sha256=hashlib.sha256(raw).hexdigest(), segments=[])
    previous = ''
    for n, part in enumerate(parts):
        (root / f'{n:06d}.csv').write_bytes(part)
        digest = hashlib.sha256(part).hexdigest()
        manifest['segments'].append(dict(sequence=n, size_bytes=len(part), sha256=digest,
                                        previous_sha256=previous, sample_count=len(part.splitlines()) - 1))
        previous = digest
    (root / 'manifest.json').write_text(json.dumps(manifest))
    return raw


@pytest.mark.parametrize('lost_receipt', [False, True])
def test_real_client_segment_upload_and_retry_reach_ready(local_record, lost_receipt):
    service, report, review = local_record
    raw = segmented_record(service, report)
    endpoint = SegmentEndpoint(lose_first_chunk=lost_receipt)
    client = ReliabilityPlatformClient('https://platform.invalid')
    client._operator_opener = endpoint
    worker = worker_for(service, review, client)
    assert worker.process_once() == 1
    if lost_receipt:
        queued = service.outbox.get(ID)
        assert queued.state == 'sync_failed'
        frozen = queued.submission_payload
        service.outbox.retry(ID)
        assert worker.process_once() == 1
        assert service.outbox.get(ID).submission_payload.startswith('{')
        assert json.loads(service.outbox.get(ID).submission_payload)['create'] == json.loads(frozen)['create']
    assert bytes(endpoint.backend.raw) == raw
    assert endpoint.backend.detail['processing_state'] == 'READY'
    assert service.outbox.get(ID).state == 'awaiting_classification'
    uploads = [call for call in endpoint.calls if call[0] == 'PUT']
    assert [call[1].rsplit('/', 1)[1] for call in uploads] == ['0', '1']


@pytest.mark.parametrize('method,path', [
    ('POST', f'/api/v1/archives/{ARCHIVE}/segments/0?offset=0'),
    ('PUT', '/api/v1/archives/not-a-uuid/segments/0?offset=0'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/segments/-1?offset=0'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/segments/4096?offset=0'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/segments/0?offset=-1'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/segments/0?offset=0&redirect=elsewhere'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/segments/0?offset=0#fragment'),
    ('PUT', '/api/v1/ingestion/content?offset=0'),
    ('PUT', f'/api/v1/archives/{ARCHIVE}/other/content?offset=0'),
])
def test_binary_guard_rejects_non_archive_or_malformed_routes(method, path):
    client = ReliabilityPlatformClient('https://platform.invalid')
    endpoint = SegmentEndpoint()
    client._operator_opener = endpoint
    with pytest.raises(PlatformError):
        client._call(method, path, body=b'original csv bytes', service_auth=False)
    assert endpoint.calls == []


def test_segment_chunk_keeps_existing_four_mib_limit():
    client = ReliabilityPlatformClient('https://platform.invalid')
    endpoint = SegmentEndpoint()
    client._operator_opener = endpoint
    with pytest.raises(ValueError, match='4 MiB'):
        client.archive_segment_chunk(ARCHIVE, 0, 0, b'x' * (4 * 1024 * 1024 + 1), 'synthetic-key')
    assert endpoint.calls == []


def test_legacy_content_route_still_sends_the_original_binary_bytes():
    from types import SimpleNamespace
    calls = []
    def receive(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps({'data': {'received_bytes': len(req.data)}}).encode())
    client = ReliabilityPlatformClient('https://platform.invalid')
    client._operator_opener = SimpleNamespace(open=receive)
    original = b'csv,header\n1,2\n'
    assert client.archive_chunk(ARCHIVE, 0, original, 'synthetic-key')['received_bytes'] == len(original)
    assert len(calls) == 1 and calls[0].data == original
    assert calls[0].headers['Content-type'] == 'application/octet-stream'
