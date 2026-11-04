"""Read-only standard export, keeping sealed facts and local corrections distinct."""
import json
from pathlib import Path
from .test_record_csv import catalog, chunks, criteria, row, text


def _read(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('记录元数据路径无效')
    if not path.exists():
        return {}
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError('记录元数据超过读取上限')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('记录元数据必须为对象')
    return data


def standard_rows(items):
    definitions = catalog()
    for item in items:
        manifest, context, summary, review = {}, {}, {}, {}
        errors = []
        if item.get('record_directory'):
            root = Path(item['record_directory'])
            for relative, target in [('manifest.json', manifest), ('config/execution.json', context),
                                     ('analysis/summary.json', summary), ('analysis/operator_review.json', review)]:
                try:
                    target.update(_read(root, relative))
                except (OSError, ValueError, TypeError) as exc:
                    errors.append(relative + '读取失败：' + str(exc))
        timing, quality, verdict = (summary.get(key) if isinstance(summary.get(key), dict) else {} for key in ('timing', 'quality', 'verdict'))
        acceptance = context.get('acceptance') or {}
        if not acceptance and item.get('record_directory'):
            try:
                import yaml
                path = Path(item['record_directory']) / 'config/station.yaml'
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1024 * 1024:
                    acceptance = (yaml.safe_load(path.read_text()) or {}).get('factory_hmi_acceptance') or {}
            except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError) as exc:
                errors.append('判据快照读取失败：' + str(exc))
        actual = []
        for key, label, unit in [('active_seconds', '有效运行', '秒'), ('completed_cycles', '完成循环', '次'),
                                 ('sample_count', '有效样本', ''), ('sample_completeness', '样本完整度', ''),
                                 ('max_gap_seconds', '最大采样间隔', '秒')]:
            value = item.get(key, timing.get(key, quality.get(key)))
            if value is not None and value != '':
                actual.append(label + '=' + text(value) + unit)
        if verdict:
            actual.append('封存自动判定=' + text(verdict.get('status')))
        if review:
            actual.append('人员核实=' + text(review.get('verified_status')))
        for joint in summary.get('joint_metrics') or []:
            if not isinstance(joint, dict):
                continue
            metrics = [label + '=' + text(joint[key]) + unit for key, label, unit in (
                ('tracking_rms_rad', '跟踪RMS', 'rad'), ('torque_peak_abs_nm', '峰值力矩', 'N·m'),
                ('temperature_peak_c', '最高温度', '℃'), ('fault_count', '故障区间', '')) if joint.get(key) is not None]
            if metrics:
                actual.append(text(joint.get('joint_name')) + '：' + '，'.join(metrics))
        correction = item.get('local_correction') or {}
        notes = ['本地记录ID=' + text(item.get('test_id')), '显示测试单号=' + text(item.get('display_test_code', item.get('execution_code'))),
                 '数据分区=' + text(item.get('data_domain')), '平台同步状态=' + text(item.get('sync_state', 'local_completed'))]
        if item.get('review_reason'):
            notes.append('平台审批意见=' + text(item['review_reason']))
        if item.get('notes'):
            notes.append('本地备注=' + text(item['notes']))
        if correction:
            notes.append('本地更正（不修改封存证据及平台审批）=' + json.dumps(correction, ensure_ascii=False, separators=(',', ':')))
            from .core.local_corrections import FIELDS
            notes.append('本地更正后业务值=' + json.dumps({key:item.get(key) for key in FIELDS if key in item}, ensure_ascii=False, separators=(',', ':')))
        notes += errors
        code = item.get('test_case_id') or manifest.get('test_case_id') or context.get('test_case_id') or ''
        definition = definitions.get(code, {})
        yield row(dict(record_id=item.get('execution_uuid') or item.get('test_id'),
            hmi_id=item.get('bench_id') or manifest.get('bench_id'), sample_id=item.get('sample_id'),
            model=context.get('model') or manifest.get('model'),
            data_source={'HMI': '上位机', 'HISTORY': '历史导入', 'OTHER': '其他'}.get(item.get('origin', 'HMI'), item.get('origin')),
            case_code=code, original_check_id=context.get('original_check_id', ''),
            test_content=item.get('test_name') or definition.get('name') or manifest.get('test_name') or item.get('actual_object'),
            started_at=item.get('started_at'), ended_at=item.get('ended_at'), tester=item.get('operator_id'),
            actual_result='；'.join(actual), criteria_text=criteria(acceptance),
            criteria_version=verdict.get('criteria_version') or acceptance.get('criteria_version') or 'UNCONFIGURED',
            verdict=(item.get('verdict') if correction and text(item.get('verdict')) != text((correction.get('original') or {}).get('verdict')) else review.get('verified_status')) or item.get('verdict') or verdict.get('status'), evidence=item.get('record_directory') or item.get('raw_csv_path'),
            notes='；'.join(notes)), definitions)


def standard_history_csv(items):
    return chunks(standard_rows(items))
