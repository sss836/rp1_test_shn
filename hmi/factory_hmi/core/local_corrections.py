"""Append-only local display metadata, deliberately outside sealed executions.

The caller serializes writes with ReportManager.evidence_lock(). No upload,
approval, acquisition or automatic verdict is changed by this module.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


FIELDS = {'sample_id': 200, 'operator_id': 80, 'bench_id': 64,
          'test_name': 200, 'actual_object': 200, 'notes': 300}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validated_changes(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS.keys():
        raise ValueError('本地更正只允许SN、测试人、工位、名称、实际对象和备注')
    result = {}
    for key, value in changes.items():
        if not isinstance(value, str) or len(value) > FIELDS[key] or any(ord(c) < 32 and (key != 'notes' or c not in '\n\t') for c in value):
            raise ValueError(f'{key} 必须为有界文本，不能包含控制字符')
        result[key] = value.strip()
    return result


class LocalCorrections:
    def __init__(self, data_root, test_id, manifest_bytes):
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', test_id) or test_id in {'.', '..'}:
            raise ValueError('本地记录编号无效')
        self.root = Path(data_root) / 'local-corrections' / test_id
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError('本地更正目录不允许符号链接')
        self.test_id = test_id
        manifest = json.loads(manifest_bytes)
        self.original = {key: str(manifest.get(key) or '') for key in FIELDS}
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    def state(self):
        records = []
        values = deepcopy(self.original)
        previous = ''
        for path in sorted(self.root.glob('v*.json')):
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError('本地更正记录无效')
            record = json.loads(path.read_bytes())
            payload = {key: value for key, value in record.items() if key != 'sha256'}
            if (record.get('schema') != 'rp1-local-correction-v1'
                    or record['revision'] != len(records) + 1 or path.name != f"v{record['revision']:06d}.json"
                    or record['test_id'] != self.test_id or record['manifest_sha256'] != self.manifest_sha256
                    or record['previous_sha256'] != previous or record['sha256'] != sha(payload)
                    or record['original'] != self.original or record['before'] != values):
                raise ValueError('本地更正版本或原始证据关联不符；请核对本地审计记录')
            changed = validated_changes(record['changes'])
            values = {**values, **changed}
            if record['values'] != values:
                raise ValueError('本地更正值与审计记录不一致')
            records.append(record)
            previous = record['sha256']
        return dict(test_id=self.test_id, revision=len(records), original=deepcopy(self.original),
                    values=values, records=records, local_only=True)

    def save(self, changes, *, reason, actor, expected_revision, request_id):
        changes = validated_changes(changes)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError('必须填写本地更正原因，最多500字')
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError('缺少本地更正操作人')
        if type(expected_revision) is not int or expected_revision < 0 or not re.fullmatch('[0-9a-f]{32}', request_id):
            raise ValueError('本地更正版本或请求编号无效')
        state = self.state()
        for record in state['records']:
            if record['request_id'] == request_id:
                if (record['changes'] != changes or record['reason'] != reason.strip() or record['actor'] != actor
                        or expected_revision != record['revision'] - 1):
                    raise ValueError('相同请求编号不能用于不同的本地更正')
                return dict(state, created=False)
        if expected_revision != state['revision']:
            raise ValueError('本地记录已被其他窗口更正，请刷新后重新核对')
        if not any(state['values'][key] != value for key, value in changes.items()):
            raise ValueError('更正值没有变化')
        if any(not value and state['values'][key] and key != 'notes' for key, value in changes.items()):
            raise ValueError('不能清空已有的SN、人员、工位、名称或实际对象')
        record = dict(schema='rp1-local-correction-v1', test_id=self.test_id,
                      revision=state['revision'] + 1, request_id=request_id,
                      manifest_sha256=self.manifest_sha256, original=self.original,
                      before=state['values'], changes=changes, values={**state['values'], **changes},
                      reason=reason.strip(), actor=actor,
                      recorded_at=datetime.now(timezone.utc).isoformat(),
                      previous_sha256=state['records'][-1]['sha256'] if state['records'] else '')
        record['sha256'] = sha(record)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"v{record['revision']:06d}.json"
        if path.exists():
            raise ValueError('本地更正版本已存在，请刷新')
        fd, temporary = tempfile.mkstemp(prefix='.incoming-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(canonical(record)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return dict(self.state(), created=True)


def apply_to_history(item, state):
    if not state['revision']:
        return item
    latest = state['records'][-1]
    item.update(state['values'])
    # Keep partition and platform identity from the original evidence. An SN
    # correction is a local label, not a reclassification or new platform link.
    if item.get('data_domain') == 'FACTORY':
        item['serial_number'] = item['sample_id']
    item['local_correction'] = dict(revision=state['revision'], status='本地已更正',
                                    original=state['original'], reason=latest['reason'],
                                    actor=latest['actor'], recorded_at=latest['recorded_at'])
    return item
