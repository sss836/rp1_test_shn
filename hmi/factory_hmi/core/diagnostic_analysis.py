"""Bounded streaming evidence analysis. No execution, repair or device access.

The byte limit of a normal preview is deliberately not an analysis limit.
Scans retain a cursor and aggregate state when their CPU slice ends. Unknown
sampling rates and clocks are reported rather than invented from file names.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import stat
import time

VERSION = 'diagnostic-stream-v1'
SLICE_SECONDS = 8.0
TIME_NAMES = {'time_s', 'elapsed_s', 'time_ms', 'timestamp_ms', 'timestamp_ns',
              'epoch_s', 'unix_timestamp', 'timestamp', 'time', 't', '时间', '时间戳'}
ERROR = re.compile(r'timeout|timed\s*out|超时|connection\s*refused|连接.*拒绝|'
                   r'traceback|exception|error|errno|watchdog|heartbeat.*(?:lost|fail)|断开', re.I)
STAMP = re.compile(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:Z|[+-]\d\d:\d\d)?')


def options(query):
    if not query:
        return {}
    try:
        data = json.loads(query)
    except (ValueError, TypeError):
        return {'question': str(query)[:8000]}
    if not isinstance(data, dict) or set(data)-{'question', 'time_column', 'sample_period', 'time_unit', 'analysis_attempt'}:
        raise ValueError('诊断分析参数无效')
    if 'analysis_attempt' in data and (type(data['analysis_attempt']) is not int or data['analysis_attempt'] not in {0,1}):
        raise ValueError('仅允许一次有记录的重新检查')
    if data.get('sample_period') is not None:
        period = data['sample_period']
        if isinstance(period, bool) or not isinstance(period, (int, float)) or not math.isfinite(period) or period <= 0:
            raise ValueError('采样间隔必须是正数')
    if data.get('time_unit') not in {None, 's', 'ms', 'ns'}:
        raise ValueError('时间单位须为s、ms或ns')
    return data


def timestamp(value, column='', unit=None):
    value = value.strip()
    try:
        number = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace(',', '.').replace('Z', '+00:00'))
        except ValueError:
            return None, 'unparsed', None
        if parsed.tzinfo is None:
            # A wall clock without timezone cannot be aligned to another source.
            return parsed.replace(tzinfo=timezone.utc).timestamp(), 'wall_clock_without_timezone', 's'
        return parsed.timestamp(), 'absolute_with_timezone', 's'
    if not math.isfinite(number):
        return None, 'nonfinite', None
    inferred = unit or ('ms' if column.endswith('_ms') else 'ns' if column.endswith('_ns') else
                        's' if column.endswith('_s') or column in {'unix_timestamp'} else None)
    if inferred is None:
        return number, 'numeric_unit_unknown', None
    number *= {'s': 1, 'ms': .001, 'ns': 1e-9}[inferred]
    basis = 'absolute_epoch_seconds' if column in {'epoch_s', 'unix_timestamp'} else 'relative_origin_unknown'
    return number, basis, 's'


class Lines:
    def __init__(self, stream, state):
        self.stream, self.state = stream, state
    def __iter__(self): return self
    def __next__(self):
        raw = self.stream.readline(1024 * 1024 + 1)
        if not raw: raise StopIteration
        if len(raw) > 1024 * 1024:
            raise ValueError('单行超过1MiB；未将剩余内容计为已检查')
        self.state['offset'] = self.stream.tell()
        if self.state.get('_hash') is not None: self.state['_hash'].update(raw)
        self.state['physical_lines'] += 1
        return raw.decode('utf-8-sig' if self.state['physical_lines'] == 1 else 'utf-8', errors='strict')


def _delta(state, row, before, current):
    delta = current - before
    if not math.isfinite(delta):
        state['invalid_interval_count']=state.get('invalid_interval_count',0)+1
        return
    if delta == 0: state['duplicate_times'] += 1
    if delta < 0: state['backward_times'] += 1
    period = state.get('period')
    if period and delta > period * 1.5:
        state['gap_count'] += 1
        ratio=delta/period
        gap={'row': row, 'previous': before, 'current': current,
                                  'interval': delta, 'baseline_interval': period,
                                  'possible_missing_samples': max(0, round(ratio)-1) if math.isfinite(ratio) else None}
        state['gaps'].append(gap)
        if len(state['gaps'])>24:del state['gaps'][12]


def _baseline(state):
    pending = state['pending_intervals']
    if not state.get('period'):
        positive = [b-a for _, a, b in pending if b>a and math.isfinite(b-a)]
        if positive:
            ordered=sorted(positive);middle=len(ordered)//2
            state['period'] = ordered[middle] if len(ordered)%2 else ordered[middle-1]/2+ordered[middle]/2
            state['period_basis'] = 'inferred_from_first_1024_intervals'
    for row, before, current in pending:
        _delta(state, row, before, current)
    state['pending_intervals'] = []
    state['baseline_ready'] = True


def scan_csv(stream, state, deadline, redact):
    """State is JSON except optional private incremental _hash for local files."""
    if 'columns' not in state:
        state.update(offset=0, physical_lines=0, rows=0, columns=None, numeric={},
                     invalid_rows=[], invalid_time_rows=[], gaps=[], gap_count=0,
                     duplicate_times=0, backward_times=0, pending_intervals=[], baseline_ready=False)
        sample=stream.read(16384).decode('utf-8-sig',errors='replace')
        try:state['delimiter']=csv.Sniffer().sniff(sample,delimiters=',;\t|').delimiter
        except csv.Error:state['delimiter']=','
    stream.seek(state['offset'])
    parser = csv.reader(Lines(stream, state),delimiter=state['delimiter'],strict=True)
    while time.monotonic() < deadline:
        try: row = next(parser)
        except StopIteration:
            if not state['baseline_ready']: _baseline(state)
            return True
        if state['columns'] is None:
            state['columns'] = [redact(x) for x in row]
            names = [x.strip().lower().lstrip('\ufeff') for x in row]
            selected = state.get('requested_time_column')
            if selected and selected.lower() not in names:
                raise ValueError('指定时间列不存在，未擅自改用其他列')
            index = names.index(selected.lower()) if selected else next((i for i,x in enumerate(names) if x in TIME_NAMES), None)
            state['time_index'] = index
            state['time_column'] = names[index] if index is not None else None
            continue
        if not row: continue
        state['rows'] += 1
        if len(row) != len(state['columns']):
            state['invalid_row_count']=state.get('invalid_row_count',0)+1
            if len(state['invalid_rows'])<24:state['invalid_rows'].append({'row': state['rows'], 'actual_columns': len(row), 'expected_columns': len(state['columns'])})
        for index, value in enumerate(row[:64]):
            try: number = float(value)
            except ValueError: continue
            if not math.isfinite(number): continue
            metric = state['numeric'].setdefault(str(index), {'count': 0, 'min': number, 'max': number, 'mean': 0.0, 'm2': 0.0,'numeric_overflow':False})
            metric['count'] += 1
            previous=metric['mean'];n=metric['count']
            mean=previous*((n-1)/n)+number/n
            m2=metric['m2']+(number-previous)*(number-mean)
            metric['numeric_overflow'] |= not math.isfinite(m2) or not math.isfinite(mean)
            metric['mean']=mean if math.isfinite(mean) else previous
            metric['m2']=m2 if math.isfinite(m2) else metric['m2']
            metric['min'] = min(metric['min'], number); metric['max'] = max(metric['max'], number)
        index = state.get('time_index')
        if index is None: continue
        current, basis, unit = timestamp(row[index] if index<len(row) else '', state['time_column'], state.get('time_unit'))
        if current is None:
            if len(state['invalid_time_rows']) < 24: state['invalid_time_rows'].append(state['rows'])
            continue
        state['time_basis']=basis if state.get('time_basis') in {None,basis} else 'mixed_time_bases'
        state['time_output_unit'] = unit
        state.setdefault('time_start', current); state['time_end'] = current
        previous = state.get('previous_time')
        if previous is not None:
            if state['baseline_ready']: _delta(state, state['rows'], previous, current)
            else:
                state['pending_intervals'].append([state['rows'], previous, current])
                if len(state['pending_intervals']) >= 1024: _baseline(state)
        state['previous_time'] = current
    return False


def csv_result(state):
    metrics = []
    for index, value in state['numeric'].items():
        metrics.append({'column': state['columns'][int(index)] if int(index)<len(state['columns']) else index,
                        **{k:v for k,v in value.items() if k!='m2'},
                        'population_standard_deviation': None if value['numeric_overflow'] else math.sqrt(max(0,value['m2']/value['count'])), 'ddof': 0})
    return {'rows_analyzed': state['rows'], 'has_measurements':state['rows']>0,'columns': state['columns'], 'delimiter':state['delimiter'],'invalid_row_count':state.get('invalid_row_count',0),'numeric_statistics': metrics,
            **{key:state.get(key) for key in ('time_column','time_basis','time_output_unit','time_start','time_end',
                'period','period_basis','gap_count','gaps','duplicate_times','backward_times','invalid_interval_count','invalid_rows','invalid_time_rows')},
            'unprocessed_numeric_column_indices':list(range(64,len(state['columns'] or []))),
            'gap_example_selection':'first_12_and_latest_12_when_more_than_24',
            'time_warning':None if state.get('time_column') is not None else '没有识别到时间列，未执行采样间隔或跨日志时序判断。',
            'interpretation': '间隔异常不是已确定的采样丢失；推算采样间隔需结合真实采集配置。不同时间原点或缺时区的来源不能直接对齐。'}


def scan_log(stream, state, deadline, redact):
    if 'physical_lines' not in state:
        state.update(offset=0, physical_lines=0, matches=[], match_count=0, context=[])
    stream.seek(state['offset'])
    lines = Lines(stream, state)
    while time.monotonic() < deadline:
        try: line = next(lines)
        except StopIteration: return True
        redacted=redact(line.rstrip())
        safe = redacted[:400]
        if len(redacted)>400:state['excerpt_truncated_lines']=state.get('excerpt_truncated_lines',0)+1
        if ERROR.search(line):
            state['match_count'] += 1
            stamp = STAMP.search(line)
            parsed, basis, _ = timestamp(stamp.group()) if stamp else (None, 'not_recorded', None)
            state['matches'].append({'line': state['physical_lines'], 'text': safe,
                                         'previous_lines': list(state['context']),
                                         'timestamp': stamp.group() if stamp else None,
                                         'time_value': parsed, 'time_basis': basis})
            if len(state['matches'])>32:del state['matches'][16]
        state['context'] = (state['context'] + [safe[:200]])[-2:]
    return False


class DiagnosticAnalysis:
    def __init__(self, reader):
        self.reader, self.cache = reader, {}

    def revoke(self, scope=None):
        if scope is None: self.cache.clear()
        else:
            for key in list(self.cache):
                if key[0] == scope: self.cache.pop(key, None)

    def run(self, scope, request, path=None, info=None):
        from factory_hmi.core.diagnostic_files import redact_text
        operation = request['operation']
        budget=request.get('_active_budget_ms',int(SLICE_SECONDS*1000))
        if type(budget) is not int or budget<0:raise ValueError('有效执行预算无效')
        if budget==0:return dict(status='budget_exhausted',complete=False,eof=False,unread=True,operation=operation)
        if operation == 'discover': return self.discover(scope, request)
        if path is None or info is None: raise ValueError('分析需要已经授权并核对的普通文件')
        key = (scope, str(path), operation, str(request.get('query') or ''))
        cursor = request.get('offset', 0)
        state = self.cache.get(key)
        identity = self.reader._identity(info)
        if cursor and (state is None or state.get('offset') != cursor or state.get('_identity') != identity):
            return dict(status='analysis_restart_required', source='paired_hmi_live_read', path=self.reader.reference(path),
                        complete=False, eof=False, next_offset=0, unread='分析游标或文件版本已改变，需要从头重新核对')
        if not cursor or state is None:
            state = {'_hash': hashlib.sha256()}; self.cache[key] = state
        configuration = options(request.get('query'))
        if not cursor:
            state.update(requested_time_column=configuration.get('time_column'), time_unit=configuration.get('time_unit'))
            if configuration.get('sample_period') is not None:
                state.update(period=configuration['sample_period'], period_basis='explicit_request_in_output_time_unit')
        if not stat.S_ISREG(info.st_mode): raise ValueError('诊断分析仅允许普通文件')
        fd = self.reader._open_fd(scope, path)
        try:
            with os.fdopen(fd, 'rb') as stream:
                if self.reader._identity(os.fstat(stream.fileno())) != identity: raise ValueError('文件在打开前发生变化')
                deadline=time.monotonic()+min(SLICE_SECONDS,budget/1000)
                if operation == 'csv_diagnosis': complete = scan_csv(stream,state,deadline,redact_text)
                elif operation == 'log_diagnosis': complete = scan_log(stream,state,deadline,redact_text)
                else: raise ValueError('未知分析操作')
                self.reader._check_fd(scope, stream.fileno(), path)
                if self.reader._identity(os.fstat(stream.fileno())) != identity: raise ValueError('文件分析期间发生变化')
        except csv.Error as exc:
            self.cache.pop(key,None)
            raise ValueError('CSV格式或逻辑字段超过解析限制，本次未完整检查；请提供可解析的分段副本，原文件未改写') from exc
        except Exception:
            self.cache.pop(key,None); raise
        state['_identity'] = identity
        data = csv_result(state) if operation == 'csv_diagnosis' else {'lines_analyzed': state['physical_lines'], 'match_count': state['match_count'], 'matches':state['matches'],'match_example_selection':'first_16_and_latest_16_when_more_than_32',
            'excerpt_limit_chars':{'matched_line':400,'context_line':200},'excerpt_truncated_lines':state.get('excerpt_truncated_lines',0),
            'full_text_provided':False,'content_scope':'全文件扫描错误模式及时间，但仅提供有界的首尾匹配与前文；完整日志正文未全部提供给模型。需要时可用read继续核对原字节范围。',
            'interpretation':'日志匹配是原始观察；未自动把同时发生的现象认定为根因。无时区或无时间戳时不能直接与其他来源对齐。'}
        return dict(status='read' if complete else 'partial',source='paired_hmi_live_read',path=self.reader.reference(path),
                    operation=operation,algorithm_version=VERSION,updated_at=datetime.now(timezone.utc).isoformat(),
                    source_sha256=state['_hash'].hexdigest(),sha256_scope='file' if complete else 'analyzed_prefix',file_mtime_ns=info.st_mtime_ns,
                    data=data,scope=dict(unit='original_bytes',start=0,end_exclusive=state['offset'],total=info.st_size),
                    complete=complete,eof=complete,next_offset=None if complete else state['offset'],
                    unread_bytes=max(0,info.st_size-state['offset']),stop_reason=None if complete else 'analysis_cpu_slice',
                    root_cause_status='not_confirmed_by_scan')

    def discover(self, scope, request):
        """Find related files in already-authorized roots, never scan devices."""
        from factory_hmi.core.diagnostic_files import redact_text
        deadline = time.monotonic()+min(SLICE_SECONDS,request.get('_active_budget_ms',int(SLICE_SECONDS*1000))/1000); candidates=[]; errors=[]; visited=set(); scanned=0; dropped=0
        skip={'.git','node_modules','.venv','venv','__pycache__','dist','build'}
        roots = sorted(self.reader.roots,key=lambda p:p!=self.reader.data_root)
        stack = list(reversed([(p,0) for p in roots]))
        while stack and time.monotonic()<deadline:
            directory,depth=stack.pop()
            if directory == self.reader.data_root/'diagnostics':continue
            if depth>12:
                errors.append({'path':self.reader.reference(directory),'reason':'directory_depth_limit'});continue
            try:
                resolved=directory.resolve()
                if resolved in visited:continue
                visited.add(resolved)
                if not self.reader._allowed(scope,resolved):
                    errors.append({'path':self.reader.reference(resolved),'reason':'permission_required'});continue
                fd=self.reader._open_fd(scope,resolved,directory=True)
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if time.monotonic()>=deadline:
                                errors.append({'path':self.reader.reference(directory),'reason':'discovery_cpu_slice'});break
                            child=resolved/entry.name;scanned+=1
                            if entry.is_symlink():continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name not in skip and not str(child).endswith('database/imports'):stack.append((child,depth+1))
                                continue
                            if not entry.is_file(follow_symlinks=False):continue
                            name=entry.name.lower();kind='csv' if name.endswith('.csv') else 'log' if '.log' in name or name in {'stdout','stderr'} else None
                            if not kind:continue
                            entry_stat=entry.stat(follow_symlinks=False)
                            score=(200 if child.is_relative_to(self.reader.data_root) else 0)+(100 if 'log' in str(child).lower() else 0)
                            candidates.append({'path':self.reader.reference(child),'kind':kind,'size':entry_stat.st_size,'modified_at_ns':entry_stat.st_mtime_ns,'score':score})
                            if len(candidates)>512:
                                candidates.sort(key=lambda x:(-x['score'],-x['modified_at_ns'],x['path']))
                                del candidates[512:];dropped+=1
                finally:os.close(fd)
            except (OSError,ValueError) as exc:
                errors.append({'path':self.reader.reference(directory),'reason':redact_text(str(exc))})
        candidates.sort(key=lambda x:(-x['score'],-x['modified_at_ns'],x['path']))
        offset=request.get('offset',0);limit=min(64,request.get('limit',16));page=candidates[offset:offset+limit]
        complete=not stack and not dropped and not errors and offset+len(page)>=len(candidates)
        return dict(status='read' if complete else 'partial',source='paired_hmi_live_read',operation='discover',
                    algorithm_version=VERSION,updated_at=datetime.now(timezone.utc).isoformat(),candidates=page,
                    candidate_count=len(candidates),unlisted_candidates=dropped,entries_scanned=scanned,roots=[self.reader.reference(p) for p in roots],
                    excluded_directory_names=sorted(skip|{'database/imports (static migration catalog)','$STATE/diagnostics (private session state)'}),
                    errors=errors[:32],complete=complete,eof=complete,next_offset=None if complete else offset+len(page),
                    unread='仍有目录或候选文件未检查' if not complete else 0,
                    scope='当前实例已登记且已授权的诊断根；未把整台电脑当作已检索')
