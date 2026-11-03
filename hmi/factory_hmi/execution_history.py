"""Read-only local history queries and CSV views; acquisition files stay intact."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterable, Mapping, Any
from factory_hmi.data_domain import domain_metadata, DOMAIN_LABELS


SYNC_LABELS = {
    "local_completed": "本地完成", "uploading": "上传中", "validating": "平台处理中",
    "pending_review": "待审批", "approved": "已批准", "uploaded": "平台已接收",
    "rejected": "已拒绝", "sync_failed": "同步失败", "awaiting_classification": "已归档／待归类",
    "archived": "已归档", "needs_verification": "需要重新核实",
    "processing_failed": "解析失败（可显式重试）",
}


def display_code(item):
    if 'display_test_code' in item:
        return item['display_test_code']
    return item.get('execution_code') or item.get('test_id', '')


def _history_domain(item):
    if item.get('local_correction'):
        # The local partition is a view. The sealed platform binding remains
        # unchanged; changing bench alone still does not reclassify a view.
        return {'data_domain': item['data_domain'], 'data_domain_source': item['data_domain_source'],
                'serial_number': item.get('sample_id', '') if item['data_domain'] == 'FACTORY' else ''}
    return domain_metadata(item)


def instant(value: Any, *, query: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None:
            if query:
                raise ValueError("日期筛选必须包含时区。")
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        if query:
            raise ValueError("日期筛选必须是带时区的 ISO 时间。") from exc
        return None


def history_bounds(start_at: str = "", end_at: str = "") -> tuple[datetime | None, datetime | None]:
    start, end = instant(start_at, query=True), instant(end_at, query=True)
    if start is not None and end is not None and start >= end:
        raise ValueError("开始日期不能晚于结束日期。")
    return start, end


def select_history(items: Iterable[dict[str, Any]], *, start_at: str = "", end_at: str = "", sn: str = "", data_domain: str = "", bench: str = "") -> list[dict[str, Any]]:
    start, end = history_bounds(start_at, end_at)
    if data_domain and data_domain not in DOMAIN_LABELS:
        raise ValueError("未知数据分区")
    selected = []
    for item in items:
        metadata = _history_domain(item)
        if data_domain and metadata["data_domain"] != data_domain:
            continue
        if bench.strip().casefold() not in str(item.get("bench_id") or "").casefold():
            continue
        if sn.strip().casefold() not in str(metadata["serial_number"] or item.get("sample_id", "")).casefold():
            continue
        when = instant(item.get("started_at"))
        if (start or end) and (when is None or (start and when < start) or (end and when >= end)):
            continue
        selected.append(item)
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    selected.sort(key=lambda item: (instant(item.get("started_at")) or minimum, str(item.get("test_id", ""))), reverse=True)
    return selected


def history_csv(items: Iterable[Mapping[str, Any]]) -> Iterable[bytes]:
    """UTF-8 BOM for desktop spreadsheets; SN is never converted to a number."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    from factory_hmi.core.local_corrections import FIELDS, LABELS, V1_FIELDS
    extra_keys = [key for key in FIELDS if key not in V1_FIELDS]
    yield b"\xef\xbb\xbf"
    writer.writerow(("测试单号", "本地记录ID", "模块编号/SN", "判定", "同步状态", "审批意见",
                     "开始时间(ISO/含时区)", "结束时间(ISO/含时区)", "工位", "测试人", "测试名称", "实际对象", "记录目录", "原始采集CSV", "数据分区", "分区依据", "工厂SN",
                     "本地更正", "更正版本", "更正原因", "更正操作人", "更正时间", "本地备注", "原始SN", "原始测试人", "原始工位", "原始测试名称", "原始实际对象", "原始备注",
                     *['本机值·' + LABELS[key] for key in extra_keys],
                     *['原始值·' + LABELS[key] for key in extra_keys]))
    for number, item in enumerate(items, 1):
        sync_state = str(item.get("sync_state") or "local_completed")
        correction = item.get('local_correction') or {}
        original = correction.get('original') or {}
        metadata = _history_domain(item)
        values = (display_code(item), item.get("test_id", ""),
                  item.get("sample_id", ""), item.get("verdict", ""), SYNC_LABELS.get(sync_state, sync_state),
                  item.get("review_reason", ""), item.get("started_at", ""), item.get("ended_at", ""),
                  item.get("bench_id") or item.get("station_id", ""), item.get("operator_id", ""),
                  item.get("test_name", ""), item.get("actual_object", ""),
                  item.get("record_directory", ""), item.get("raw_csv_path", ""),
                  DOMAIN_LABELS[metadata["data_domain"]], metadata["data_domain_source"], metadata["serial_number"],
                  correction.get('status') or ('更正记录异常' if item.get('local_correction_error') else ''), correction.get('revision'),
                  correction.get('reason'), correction.get('actor'), correction.get('recorded_at'), item.get('notes'),
                  *[original.get(key, item.get(key, '')) for key in V1_FIELDS],
                  *[item.get(key, '') for key in extra_keys],
                  *[original.get(key, item.get(key, '')) for key in extra_keys])
        # User-entered cells must not become spreadsheet formulae. Long numeric
        # identifiers remain byte-for-byte strings in CSV; import them as text.
        writer.writerow("'" + str(value) if str(value).lstrip().startswith(("=", "+", "-", "@")) else ("" if value is None else str(value))
                        for value in values)
        if number % 128 == 0:
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)
    if buffer.tell():
        yield buffer.getvalue().encode("utf-8")
