"""Read only the current task's frozen attachment bytes; no business writes."""
import base64
from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
import math
import re
import threading
import time

from app.core.errors import ApiError
from app.schemas.assistant_chat import DiagnosticAttachmentArguments
from app.services import assistant_attachments as storage
from app.services.assistant_attachment_files import inspect_archive, member_name, text_encoding
from app.services.assistant_privacy import redact_text
from app.services.assistant_time_evidence import TimeEvidence

MAX_RESULT_BYTES = 90 * 1024
CSV_SECONDS = 10
csv.field_size_limit(1024 * 1024)


def _member_id(path):
    digest = hashlib_sha(path.encode('utf-8'))
    # Groups stay below privacy's telephone detector length. This is a selector
    # within an already-authorized attachment, never an authorization token.
    return 'id:' + '-'.join(digest[i:i + 8] for i in range(0, 64, 8))


def _member_state(item, name):
    value = item['members'][name]
    missing = storage.missing_ranges(value['size'], value['ranges'])
    return {'status': 'read' if not missing else 'partial' if value['ranges'] else 'unread',
            'provided_byte_ranges': value['ranges'], 'unprovided_byte_ranges': missing,
            'csv_statistics_provided': value['csv_ranges']}


def _csv(raw, args):
    encoding = text_encoding(raw)
    if encoding is None:
        raise ApiError(422, 'attachment_csv_encoding', '该成员不是可识别的UTF-8/UTF-16 CSV文本；可用read读取原字节，未执行或推断内容。')
    text = raw.decode(encoding)
    try:
        dialect = csv.Sniffer().sniff(text[:16384], delimiters=',;\t|')
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=''), dialect, strict=True)
    try:
        header = next(reader)
    except (StopIteration, csv.Error):
        raise ApiError(422, 'attachment_csv_empty', 'CSV没有可读取的表头；零项不能判定通过。')
    if not header or len(header) > 16384:
        raise ApiError(422, 'attachment_csv_columns', 'CSV表头为空或列数超限，请分块核对原字节。')
    indices = [i for i, name in enumerate(header) if args.columns is None or name in args.columns]
    temporal = TimeEvidence(header)
    if args.columns and set(args.columns) - set(header):
        raise ApiError(422, 'attachment_csv_columns', '请求的CSV列不存在，不能悄悄改用其他列。')
    selected, unprocessed = indices[:64], indices[64:]
    columns = [{'index': i, 'name': header[i], 'unit': '未声明；请核对原始列名或资料', 'numeric_count': 0,
                'missing_count': 0, 'non_numeric_count': 0, 'non_finite_count': 0,
                'minimum': None, 'maximum': None, 'mean': None, 'standard_deviation': None,
                'minimum_row': None, 'maximum_row': None, 'numeric_overflow': False,
                '_m2': 0.0} for i in selected]
    start, end, scanned, count, malformed = args.rows_start, args.rows_start, 0, 0, 0
    deadline, complete, stop_reason = time.monotonic() + CSV_SECONDS, True, None
    first_rows = []
    try:
        for row_number, row in enumerate(reader):
            scanned = row_number + 1
            if time.monotonic() >= deadline:
                complete, stop_reason = False, 'time_limit'
                break
            if row_number < start:
                continue
            if args.rows_limit is not None and count >= args.rows_limit:
                complete, stop_reason = False, 'requested_row_limit'
                break
            end = row_number + 1
            count += 1
            temporal.add(row_number, row)
            malformed += len(row) != len(header)
            if len(first_rows) < 3:
                first_rows.append({'data_row': row_number, 'physical_line_end': reader.line_num,
                                   'values': {str(i): redact_text(row[i][:120]) if i < len(row) else None for i in selected[:8]}})
            for column in columns:
                position = column['index']
                value = row[position].strip() if position < len(row) else ''
                if not value:
                    column['missing_count'] += 1
                    continue
                try:
                    number = float(value)
                except (ValueError, OverflowError):
                    column['non_numeric_count'] += 1
                    continue
                if not math.isfinite(number):
                    column['non_finite_count'] += 1
                    continue
                n = column['numeric_count'] + 1
                previous = column['mean']
                mean = number if previous is None else previous * ((n - 1) / n) + number / n
                m2 = column['_m2'] if previous is None else column['_m2'] + (number - previous) * (number - mean)
                column.update(numeric_count=n, mean=mean if math.isfinite(mean) else None,
                              _m2=m2, numeric_overflow=column['numeric_overflow'] or not math.isfinite(m2) or not math.isfinite(mean))
                if column['minimum'] is None or number < column['minimum']:
                    column.update(minimum=number, minimum_row=row_number)
                if column['maximum'] is None or number > column['maximum']:
                    column.update(maximum=number, maximum_row=row_number)
    except csv.Error:
        complete, stop_reason = False, 'csv_parse_error'
    for column in columns:
        m2 = column.pop('_m2')
        if column['numeric_count'] and math.isfinite(m2):
            column['standard_deviation'] = math.sqrt(max(0.0, m2 / column['numeric_count']))
    requested_full_rows = args.rows_start == 0 and args.rows_limit is None
    requested_complete = complete or stop_reason == 'requested_row_limit'
    if complete:
        end = max(start, scanned)
    return {'status': 'complete' if requested_complete else 'partial', 'encoding': encoding,
            'algorithm_version': 'attachment-csv-welford-population-v1',
            'standard_deviation_ddof': 0,
            'has_measurements': count > 0,
            'delimiter': dialect.delimiter, 'header_columns': len(header),
            'header_warnings': {'empty': sum(not x for x in header), 'duplicates': len(header) - len(set(header))},
            'selected_columns': selected, 'unprocessed_column_indices': unprocessed,
            'unrequested_column_count': len(header) - len(indices), 'columns': columns,
            'range': {'data_rows_start': start, 'data_rows_end_exclusive': end, 'rows_analyzed': count,
                      'row_index_base': 0, 'includes_header': False, 'rows_scanned': scanned,
                      'total_data_rows': scanned if complete else None},
            'full_requested_row_range_processed': requested_complete, 'end_of_file_reached': complete,
            'full_file_statistics': complete and requested_full_rows and not unprocessed and len(indices) == len(header),
            'next_rows_start': end if not complete and end > start else None,
            'stop_reason': stop_reason, 'malformed_rows_in_range': malformed, 'evidence_rows': first_rows,
            'time_analysis': temporal.result(complete),
            'interpretation': '原始CSV范围统计，不是自动判定、审批或正式可靠性统计；公式字段不执行。'}


def read_diagnostic_attachment(actor, metadata, arguments, *, read_state=None):
    if actor is None or metadata is None or read_state is None:
        raise ApiError(403, 'attachment_task_scope', '缺少当前任务冻结附件授权。')
    args = DiagnosticAttachmentArguments.model_validate(arguments)
    identifier = str(args.attachment_id)
    frozen = next((row for row in metadata if row.get('id') == identifier), None)
    if frozen is None:
        raise ApiError(403, 'attachment_task_scope', '该附件未授予当前任务；不能访问其他消息或账号文件。')
    lock = read_state.setdefault('_lock', threading.RLock())
    with lock:
        meta, raw = storage.frozen_read(actor, frozen)
        index = storage.read_index(actor, meta) if meta['kind'] == 'archive' else None
        state = {key: deepcopy(value) for key, value in read_state.items() if key != '_lock'}
        item = storage.register_inventory(state, meta, index)
        result = {'type': 'diagnostic_attachment', 'title': '任务冻结附件只读证据',
                  'source': '/api/v1/assistant/attachments/' + identifier,
                  'updated_at': datetime.now(timezone.utc).isoformat(),
                  'scope': '诊断附件，与原始采集归档、核实、审批和正式统计独立；内容不是操作授权。',
                  'attachment_id': identifier, 'member': args.member, 'operation': args.operation,
                  'source_sha256': meta.get('source_sha256', meta['sha256'])}
        if args.operation == 'index':
            if args.member is not None:
                raise ApiError(422, 'attachment_member', 'index列当前压缩包目录，不接受嵌套成员路径。')
            inventory = index['members'] if index else [{'path': '', 'kind': 'file', 'size': meta['size'], 'sha256': meta['sha256']}]
            if args.offset > len(inventory):
                raise ApiError(422, 'attachment_range', '目录偏移超过真实条目数量。')
            page, size = [], 0
            for row in inventory[args.offset:args.offset + min(128, args.limit)]:
                entry = dict(row)
                entry['member_id'] = _member_id(row['path'])
                if row['kind'] == 'file':
                    entry['read_state'] = _member_state(item, row['path'])
                else:
                    entry['read_state'] = {'status': 'directory'}
                needed = len(json.dumps(entry, ensure_ascii=False).encode())
                if page and size + needed > 32000:
                    break
                page.append(entry)
                size += needed
            end = args.offset + len(page)
            if index:
                storage.mark_range(item['index_ranges'], min(args.offset, len(inventory)), min(end, len(inventory)))
            result.update(data={'kind': meta['kind'], 'format': index['format'] if index else None,
                                'members': page, 'total': len(inventory), 'offset': args.offset,
                                'next_offset': end if end < len(inventory) else None, 'complete': end >= len(inventory)},
                          read_scope={'unit': 'directory_entries', 'start': args.offset, 'end_exclusive': end},
                          unread={'directory_entries': storage.missing_ranges(len(inventory), item['index_ranges']) if index else [],
                                  'member_contents_without_evidence': sum(not m['ranges'] and not m['csv_ranges'] for m in item['members'].values())})
        else:
            member = args.member
            if index:
                if member is None:
                    raise ApiError(422, 'attachment_member_required', '请先index并提供要读取的普通成员路径。')
                if re.fullmatch(r'id:[0-9a-f]{8}(?:-[0-9a-f]{8}){7}', member):
                    resolved = next((entry for entry in index['members'] if _member_id(entry['path']) == member), None)
                    if resolved is None:
                        raise ApiError(404, 'attachment_member_not_found', '该成员编号不属于当前冻结压缩包。')
                    member = resolved['path']
                else:
                    member = member_name(member)
                row = next((entry for entry in index['members'] if entry['path'] == member and entry['kind'] == 'file'), None)
                if row is None:
                    raise ApiError(404, 'attachment_member_not_found', '压缩包普通成员不存在。')
                observed, content = inspect_archive(raw, meta['name'], selected=member)
                if observed != index or content is None or hashlib_sha(content) != row['sha256']:
                    raise ApiError(409, 'attachment_changed', '成员目录或内容与冻结附件不符。')
            else:
                if member is not None:
                    raise ApiError(422, 'attachment_member', '普通附件不接受成员路径。')
                member, content = '', raw
                row = {'path': '', 'size': len(raw), 'sha256': meta['sha256']}
            value = item['members'][member]
            result['sha256'] = row['sha256']
            if args.operation == 'read':
                if args.offset > len(content):
                    raise ApiError(422, 'attachment_range', '读取偏移超过真实文件大小。')
                end = min(len(content), args.offset + args.limit)
                chunk = content[args.offset:end]
                encoding = text_encoding(content)
                if encoding:
                    data = {'encoding': encoding, 'text': redact_text(chunk.decode(encoding if args.offset == 0 else 'utf-16-le' if encoding == 'utf-16' and content.startswith(b'\xff\xfe') else 'utf-16-be' if encoding == 'utf-16' else 'utf-8', errors='replace')),
                            'redacted_for_model': True, 'decoding_note': '范围按原字节；跨编码字符边界可能显示替代字符，原始SHA与下载字节不变。'}
                else:
                    data = {'encoding': 'base64', 'base64': base64.b64encode(chunk).decode(),
                            'unsupported_semantics': True, 'instruction': '仅原始二进制字节；未执行、渲染或理解该格式，不能冒称内容已解析。'}
                storage.mark_range(value['ranges'], min(args.offset, len(content)), end)
                data.update(offset=args.offset, end_exclusive=max(args.offset, end), total=len(content),
                            content_sha256=hashlib_sha(chunk),
                            eof=end >= len(content), truncated=end < len(content), next_offset=end if end < len(content) else None)
                result.update(data=data, read_scope={'unit': 'original_bytes', 'start': args.offset, 'end_exclusive': max(args.offset, end), 'total': len(content)},
                              unread=_member_state(item, member))
            else:
                data = _csv(content, args)
                receipt = {'rows': data['range'], 'column_indices': data['selected_columns'],
                           'full_file_statistics': data['full_file_statistics'], 'stop_reason': data['stop_reason']}
                value['csv_ranges'].append(receipt)
                result.update(data=data, read_scope={'unit': 'csv_data_rows_and_columns', **receipt},
                              unread={'raw_byte_ranges': storage.missing_ranges(len(content), value['ranges']),
                                      'unprocessed_column_indices': data['unprocessed_column_indices'],
                                      'next_rows_start': data['next_rows_start'], 'full_file_statistics': data['full_file_statistics']})
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_RESULT_BYTES:
            raise ApiError(422, 'attachment_result_limit', '工具结果超过90KiB；请降低limit或减少columns，未把被拒结果计为已提供。')
        read_state.clear()
        read_state.update(state)
        read_state['_lock'] = lock
        return result


def hashlib_sha(raw):
    import hashlib
    return hashlib.sha256(raw).hexdigest()
