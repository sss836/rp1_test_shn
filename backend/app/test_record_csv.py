"""RP1 test-record CSV v1. Mirrored in platform app; parity is tested.

This is a presentation contract, never a telemetry or approval interchange.
"""
import csv
import io
import json
from pathlib import Path

SCHEMA = 'rp1-test-record-v2'
FIELDS = ('记录编号', '上位机编号', '样品编号', '机型', '测试体系', '测试分类', '记录层级', '测试项目编号', '原始检查ID', '测试内容', '开始时间', '结束时间', '测试人员', '实际结果', '通过标准', '是否通过', '备注')
KEYS = ('record_id', 'hmi_id', 'sample_id', 'model', 'test_system', 'test_category', 'record_level', 'case_code', 'original_check_id', 'test_content', 'started_at', 'ended_at', 'tester', 'actual_result', 'criteria_text', 'verdict', 'notes')
VERDICTS = {'passed': '通过', 'failed': '不通过', 'blocked': '中止/阻断', 'unconfigured': '标准待补充',
            '未配置判据': '标准待补充', 'unknown': '待复核', '': '未测试'}
LIMITS = {'required_active_seconds': ('有效运行时长≥', '秒'), 'required_cycles': ('完成循环数≥', '次'),
          'min_sample_completeness': ('样本完整度≥', ''), 'max_gap_seconds': ('最大采样间隔≤', '秒'),
          'max_temperature_rise_c': ('最大温升≤', '℃'), 'max_tracking_rms_rad': ('跟踪RMS≤', 'rad'),
          'max_torque_abs_nm': ('最大绝对力矩≤', 'N·m'),
          'max_torque_p999_utilization': ('P99.9力矩利用率≤', '')}


def text(value):
    if value is None:
        return ''
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def catalog():
    return {item['case_code']: item for item in json.loads(Path(__file__).with_name('test_record_catalog.json').read_text(encoding='utf-8'))}


def criteria(acceptance):
    """Only the recorded snapshot supplies quantitative thresholds."""
    if not isinstance(acceptance, dict) or not acceptance.get('criteria_version') or acceptance['criteria_version'] == 'UNCONFIGURED':
        return '标准待补充；未配置数值判据'
    ignored = {'max_temperature_rise_c', 'max_tracking_rms_rad', 'max_torque_p999_utilization'} if acceptance['criteria_version'] == 'hmi-runtime-v2' else set()
    result = [prefix + text(acceptance[key]) + unit for key, (prefix, unit) in LIMITS.items()
              if acceptance.get(key) is not None and key not in ignored]
    if acceptance['criteria_version'] == 'hmi-runtime-v2':
        result += ['时间单调', '无非零驱动故障', '无安全或产品失败', '正常完成且未人工中断',
                   '温升、力矩利用率和跟踪RMS仅记录，不参与此版本判定']
    return '；'.join(result) or '标准待补充；该版本未记录可执行阈值'


def row(data, definitions=None):
    value = dict(data)
    definition = (definitions or {}).get(text(value.get('case_code')), {})
    value.setdefault('test_content', definition.get('name', ''))
    value.setdefault('test_system', definition.get('test_system', ''))
    value.setdefault('test_category', definition.get('test_category', ''))
    value.setdefault('record_level', '项目')
    value['verdict'] = VERDICTS.get(text(value.get('verdict')), text(value.get('verdict')))
    # A catalog text is not substituted for the sealed execution's actual rules.
    return dict(zip(FIELDS, (text(value.get(key)) for key in KEYS)))


def chunks(rows):
    """Bounded, one-pass UTF-8 BOM CSV with spreadsheet formula protection."""
    yield b'\xef\xbb\xbf'
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer, lineterminator='\r\n')
    writer.writerow(FIELDS)
    yield buffer.getvalue().encode('utf-8')
    buffer.seek(0); buffer.truncate(0)
    for item in rows:
        writer.writerow("'" + text(item.get(key)) if text(item.get(key)).lstrip().startswith(('=', '+', '-', '@'))
                        else text(item.get(key)) for key in FIELDS)
        yield buffer.getvalue().encode('utf-8')
        buffer.seek(0); buffer.truncate(0)
