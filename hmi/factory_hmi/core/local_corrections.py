"""Append-only local business views; sealed evidence and platform receipts stay intact."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from factory_hmi.data_domain import domain_metadata

V1_FIELDS = {'sample_id': 200, 'operator_id': 80, 'bench_id': 64,
             'test_name': 200, 'actual_object': 200, 'notes': 300}
# key, Chinese label, maximum text length. These are business display fields,
# never filesystem paths, execution UUIDs, or authoritative platform states.
GROUPS = (
    ('基本信息', (
        ('display_test_code', '显示测试单号', 255), ('sample_id', '模块编号 / SN', 200),
        ('test_case_id', '测试用例编号', 100), ('test_case_version', '用例版本', 100),
        ('robot_id', '样机编号', 200), ('module_name', '部位名称', 100), ('limb', '部位标识', 64),
        ('operator_id', '实际测试人', 80), ('bench_id', '工位', 64),
        ('stage_code', '阶段编号', 64), ('stage_name', '阶段名称', 100),
        ('test_name', '测试名称', 200), ('actual_object', '实际测试对象', 200),
    )),
    ('时间与结果', (
        ('verdict', '本地判定', 32), ('verdict_note', '本地判定说明', 2000),
        ('criteria_version', '判据版本标记', 100), ('machine_status', '运行结束状态', 64),
        ('outcome', '结束原因标识', 64), ('stop_reason', '结束原因说明', 500),
        ('started_at', '开始时间（ISO / 含时区）', 64), ('ended_at', '结束时间（ISO / 含时区）', 64),
        ('active_seconds', '有效运行时长（秒）', 64), ('wall_seconds', '总历时（秒）', 64),
        ('paused_seconds', '暂停时长（秒）', 64), ('completed_cycles', '完成循环数', 32),
    )),
    ('记录质量与分区', (
        ('sample_count', '有效样本数', 32), ('expected_sample_count', '预期样本数', 32),
        ('sample_completeness', '样本完整度（0～1）', 64), ('max_gap_seconds', '最大采样间隔（秒）', 64),
        ('record_rate_hz', '记录频率（Hz）', 64), ('fault_count', '故障区间计数', 32),
        ('data_domain', '本机数据分区', 32), ('recording_scope', '本机记录范围标签', 32),
        ('origin', '记录来源标签', 32), ('software_version', '软件版本标记', 100),
        ('calibration_version', '标定版本标记', 100), ('notes', '本地备注', 2000),
        ('analysis_note', '本地分析说明', 2000), ('local_status_note', '本地同步说明', 1000),
        ('local_review_note', '本地审批说明', 1000),
    )),
)
FIELDS = {key: limit for _, rows in GROUPS for key, _, limit in rows}
LABELS = {key: label for _, rows in GROUPS for key, label, _ in rows}
ENUMS = {
    'verdict': {'passed': '通过', 'failed': '失败', 'blocked': '中止 / 阻断', 'unconfigured': '未配置判据'},
    'data_domain': {'RESEARCH': '研发', 'FACTORY': '工厂', 'UNCLASSIFIED': '分区待归类'},
    'recording_scope': {'local': '本地', 'platform': '平台'},
    'origin': {'HMI': '上位机', 'HISTORY': '历史补录', 'OTHER': '其他'},
}
INTEGER_FIELDS = {'completed_cycles', 'sample_count', 'expected_sample_count', 'fault_count'}
NUMBER_LIMITS = {**{key: Decimal('315360000') for key in ('active_seconds', 'wall_seconds', 'paused_seconds', 'max_gap_seconds')},
                 'sample_completeness': Decimal('1'), 'record_rate_hz': Decimal('100000')}
TIME_FIELDS = {'started_at', 'ended_at'}
MULTILINE_FIELDS = {'notes', 'verdict_note', 'stop_reason', 'analysis_note', 'local_status_note', 'local_review_note'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text_value(value):
    return '' if value is None else str(value)


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        raise ValueError('本地更正时间必须是带时区的ISO时间，例如 2026-11-03T17:30:00+08:00') from None


def validated_changes(changes, *, legacy=False):
    allowed = V1_FIELDS if legacy else FIELDS
    if not isinstance(changes, dict) or not changes or set(changes) - allowed.keys():
        raise ValueError('本地更正只允许已列出的业务字段；内部身份、原始文件及平台真实状态不可更正')
    result = {}
    for key, value in changes.items():
        if not isinstance(value, str) or len(value) > allowed[key] or any(ord(c) < 32 and (key not in MULTILINE_FIELDS or c not in '\n\t') for c in value):
            raise ValueError(f'{LABELS.get(key, key)}必须为有界文本，不能包含控制字符')
        value = value.strip()
        if not legacy and value:
            if key in ENUMS and value not in ENUMS[key]:
                raise ValueError(f'{LABELS[key]}的选项无效')
            if key in TIME_FIELDS:
                value = _time(value).isoformat()
            if key in INTEGER_FIELDS:
                if not re.fullmatch(r'[0-9]{1,13}', value) or int(value) > 10**12:
                    raise ValueError(f'{LABELS[key]}必须是0～1000000000000的整数')
                value = str(int(value))
            if key in NUMBER_LIMITS:
                try: number = Decimal(value)
                except InvalidOperation: raise ValueError(f'{LABELS[key]}必须是有限非负数') from None
                if not number.is_finite() or not 0 <= number <= NUMBER_LIMITS[key]:
                    raise ValueError(f'{LABELS[key]}必须是0～{NUMBER_LIMITS[key]}的有限数')
                value = format(number.normalize(), 'f') if number else '0'
        result[key] = value
    return result


def _business_original(manifest, summary, context):
    timing, quality, verdict = (summary.get(key) or {} for key in ('timing', 'quality', 'verdict'))
    values = {key: text_value(manifest.get(key, context.get(key))) for key in FIELDS}
    for key in ('active_seconds', 'wall_seconds', 'paused_seconds', 'completed_cycles'):
        values[key] = text_value(timing.get(key, context.get(key)))
    for key in ('sample_count', 'expected_sample_count', 'sample_completeness', 'max_gap_seconds'):
        values[key] = text_value(quality.get(key))
    for key in TIME_FIELDS:
        values[key] = '' if manifest.get('timestamp_provenance') == 'missing' else text_value(manifest.get(key, timing.get(key)))
    values['display_test_code'] = text_value(manifest.get('execution_code') or manifest.get('test_id'))
    values['verdict'] = ('未配置判据' if verdict.get('criteria_version') == 'UNCONFIGURED' or verdict.get('status') == 'unconfigured'
                         else text_value(verdict.get('status', manifest.get('status'))))
    values['criteria_version'] = text_value(verdict.get('criteria_version'))
    values['fault_count'] = text_value(sum(int(row.get('fault_count') or 0) for row in summary.get('joint_metrics', []))) if summary.get('joint_metrics') is not None else ''
    values['data_domain'] = domain_metadata(manifest)['data_domain']
    values['recording_scope'] = text_value(manifest.get('recording_scope', 'platform'))
    values['origin'] = text_value(manifest.get('origin', 'HMI'))
    software = manifest.get('software') or context.get('software') or {}
    values['software_version'] = text_value(software.get('hmi_version') or software.get('version') or software.get('application_version')) if isinstance(software, dict) else ''
    values['calibration_version'] = text_value((manifest.get('provenance') or {}).get('calibration_version') or context.get('calibration_version'))
    for key in V1_FIELDS:
        values[key] = str(manifest.get(key) or '')
    return values


def view_values(values):
    result = dict(values)
    result.pop('display_test_code', None)
    for key in INTEGER_FIELDS | NUMBER_LIMITS.keys():
        if key not in values:
            continue
        value = values.get(key, '')
        try: result[key] = (int(value) if key in INTEGER_FIELDS else float(value)) if value != '' else None
        except (ValueError, TypeError): result[key] = None
    return result


class LocalCorrections:
    def __init__(self, data_root, test_id, manifest_bytes, *, summary=None, context=None):
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,255}', test_id) or test_id in {'.', '..'}:
            raise ValueError('本地记录编号无效')
        self.root = Path(data_root) / 'local-corrections' / test_id
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError('本地更正目录不允许符号链接')
        self.test_id = test_id
        manifest = json.loads(manifest_bytes)
        self.legacy_original = {key: str(manifest.get(key) or '') for key in V1_FIELDS}
        self.original = _business_original(manifest, summary or {}, context or {})
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()

    def state(self):
        records = []; values = deepcopy(self.original); previous = ''
        for path in sorted(self.root.glob('v*.json')):
            if path.is_symlink() or path.stat().st_size > 65536:
                raise ValueError('本地更正记录无效')
            record = json.loads(path.read_bytes())
            payload = {key: value for key, value in record.items() if key != 'sha256'}
            legacy = record.get('schema') == 'rp1-local-correction-v1'
            original = self.legacy_original if legacy else self.original
            before = {key: values[key] for key in V1_FIELDS} if legacy else values
            if (record.get('schema') not in {'rp1-local-correction-v1', 'rp1-local-correction-v2'}
                    or record['revision'] != len(records) + 1 or path.name != f"v{record['revision']:06d}.json"
                    or record['test_id'] != self.test_id or record['manifest_sha256'] != self.manifest_sha256
                    or record['previous_sha256'] != previous or record['sha256'] != sha(payload)
                    or record['original'] != original or record['before'] != before):
                raise ValueError('本地更正版本或原始证据关联不符；请核对本地审计记录')
            changed = validated_changes(record['changes'], legacy=legacy)
            values = {**values, **changed}
            after = {key: values[key] for key in V1_FIELDS} if legacy else values
            if record['values'] != after:
                raise ValueError('本地更正值与审计记录不一致')
            records.append(record); previous = record['sha256']
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
        values = {**state['values'], **changes}
        if values['started_at'] and values['ended_at']:
            if _time(values['ended_at']) < _time(values['started_at']):
                raise ValueError('结束时间不能早于开始时间')
        record = dict(schema='rp1-local-correction-v2', test_id=self.test_id,
                      revision=state['revision'] + 1, request_id=request_id,
                      manifest_sha256=self.manifest_sha256, original=self.original,
                      before=state['values'], changes=changes, values=values,
                      reason=reason.strip(), actor=actor, recorded_at=datetime.now(timezone.utc).isoformat(),
                      previous_sha256=state['records'][-1]['sha256'] if state['records'] else '')
        record['sha256'] = sha(record)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"v{record['revision']:06d}.json"
        if path.exists(): raise ValueError('本地更正版本已存在，请刷新')
        data = canonical(record)
        if len(data) > 65536: raise ValueError('本地更正版本超过64KiB上限')
        fd, temporary = tempfile.mkstemp(prefix='.incoming-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(self.root, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        return dict(self.state(), created=True)


def apply_to_history(item, state):
    if not state['revision']: return item
    latest = state['records'][-1]
    changed = {key: value for key, value in state['values'].items() if value != state['original'][key]}
    item.update(view_values(changed))
    if 'display_test_code' in changed: item['display_test_code'] = changed['display_test_code']
    if 'data_domain' in changed:
        item['data_domain'] = changed['data_domain'] or 'UNCLASSIFIED'
        item['data_domain_source'] = 'LOCAL_CORRECTION'
    item['serial_number'] = item.get('sample_id', '') if item.get('data_domain') == 'FACTORY' else ''
    item['local_correction'] = dict(revision=state['revision'], status='本地已更正', original=state['original'],
                                  reason=latest['reason'], actor=latest['actor'], recorded_at=latest['recorded_at'])
    return item
