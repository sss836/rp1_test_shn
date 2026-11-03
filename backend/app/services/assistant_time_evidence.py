"""Streaming time evidence for frozen CSV; observations are not causal proof."""
from datetime import datetime, timezone
import math

NAMES = {'time_s','elapsed_s','time_ms','timestamp_ms','timestamp_ns','epoch_s',
         'unix_timestamp','timestamp','time','t','时间','时间戳'}


def parse_time(value, column):
    try:
        number = float(value)
    except ValueError:
        try: parsed = datetime.fromisoformat(value.strip().replace('Z','+00:00'))
        except ValueError: return None, 'unparsed', None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc).timestamp(), 'wall_clock_without_timezone', 's'
        return parsed.timestamp(), 'absolute_with_timezone', 's'
    if not math.isfinite(number): return None, 'nonfinite', None
    unit = 'ms' if column.endswith('_ms') else 'ns' if column.endswith('_ns') else 's' if column.endswith('_s') or column=='unix_timestamp' else None
    if unit is None: return number, 'numeric_unit_unknown', None
    return number*{'s':1,'ms':.001,'ns':1e-9}[unit], 'absolute_epoch_seconds' if column in {'epoch_s','unix_timestamp'} else 'relative_origin_unknown', 's'


class TimeEvidence:
    def __init__(self, header):
        names = [x.strip().lower().lstrip('\ufeff') for x in header]
        self.index = next((i for i,x in enumerate(names) if x in NAMES), None)
        self.column = names[self.index] if self.index is not None else None
        self.previous = self.start = self.end = self.period = None
        self.basis = self.unit = None
        self.pending = []; self.gaps = []; self.gap_count = self.duplicates = self.backwards = self.invalid = 0
        self.ready = False; self.invalid_intervals=0

    def _interval(self, row, a, b):
        delta = b-a
        if not math.isfinite(delta):self.invalid_intervals+=1;return
        self.duplicates += delta==0; self.backwards += delta<0
        if self.period and delta>self.period*1.5:
            self.gap_count += 1
            ratio=delta/self.period
            self.gaps.append({'data_row':row,'row_index_base':0,'previous_time':a,'current_time':b,
                'interval':delta,'reference_interval':self.period,'possible_missing_samples':max(0,round(ratio)-1) if math.isfinite(ratio) else None})
            if len(self.gaps)>24:del self.gaps[12]

    def baseline(self):
        positive = [b-a for _,a,b in self.pending if b>a and math.isfinite(b-a)]
        if positive:
            ordered=sorted(positive);middle=len(ordered)//2
            self.period = ordered[middle] if len(ordered)%2 else ordered[middle-1]/2+ordered[middle]/2
        for row,a,b in self.pending: self._interval(row,a,b)
        self.pending = []; self.ready = True

    def add(self, row_number, row):
        if self.index is None: return
        value, basis, unit = parse_time(row[self.index] if self.index<len(row) else '', self.column)
        if value is None: self.invalid += 1; return
        if self.basis is not None and self.basis!=basis: self.basis='mixed_time_bases'
        else: self.basis=basis
        self.unit=unit
        if self.start is None: self.start=value
        self.end=value
        if self.previous is not None:
            if self.ready: self._interval(row_number,self.previous,value)
            else:
                self.pending.append([row_number,self.previous,value])
                if len(self.pending)>=1024: self.baseline()
        self.previous=value

    def result(self, complete):
        if complete and not self.ready: self.baseline()
        return {'algorithm_version':'csv-time-interval-v1','time_column':self.column,
            'time_basis':self.basis,'output_unit':self.unit,'start':self.start,'end':self.end,
            'reference_interval':self.period,'reference_basis':'median_of_first_1024_positive_intervals' if self.ready else 'not_enough_evidence_yet',
            'gap_count':self.gap_count,'gap_examples':self.gaps,'duplicate_times':self.duplicates,
            'backward_times':self.backwards,'invalid_time_rows':self.invalid,
            'invalid_intervals':self.invalid_intervals,'example_selection':'first_12_and_latest_12_when_more_than_24',
            'complete':complete,'causality':'间隔异常是观察，不能单凭间隔断言采样丢失。参考间隔由数据推算，须核对采集配置；未知单位、时间原点或时区不能直接与日志对齐。'}
