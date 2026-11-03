"""Disposable, bounded curve summaries derived only from saved window evidence.

The recorder and device threads never call this module. Queries incrementally
index existing JSONL under a short work budget; incomplete work is explicit.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

LEVELS = (1, 10, 60, 600, 3600, 86400, 604800, 31536000, 10**12)
KEEP = 600
MAX_LINE = 256 * 1024


class CurveCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ValueError('invalid curve cache path')
        self.db = sqlite3.connect(path, timeout=.05)
        self.db.execute('PRAGMA cache_size=-2048')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA wal_autocheckpoint=128')
        self.db.execute('PRAGMA max_page_count=32768')  # 128 MiB, derived data only.
        self.db.execute('CREATE TABLE IF NOT EXISTS bins(level INTEGER,bucket INTEGER,payload TEXT,PRIMARY KEY(level,bucket))')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')

    def close(self):
        self.db.close()

    def meta(self):
        return {k: json.loads(v) for k, v in self.db.execute('SELECT key,value FROM meta')}

    def ingest(self, source: Path, run_id: str, durable_end: float, sealed: bool,
               *, byte_budget=4 * 1024**2, time_budget=.2):
        size = source.stat().st_size
        with source.open('rb') as f:
            first = f.readline(MAX_LINE + 1)
            if len(first) > MAX_LINE:
                raise ValueError('saved window exceeds curve budget')
            if not first.endswith(b'\n'):
                return dict(indexing=False, indexed_bytes=0, source_bytes=size)
            identity = [run_id, hashlib.sha256(first).hexdigest()]
            meta = self.meta()
            if meta.get('identity') != identity or meta.get('offset', 0) > size:
                self.db.execute('DELETE FROM bins')
                self.db.execute('DELETE FROM meta')
                meta = {'identity': identity, 'offset': 0, 'first_s': None, 'last_s': 0}
            offset = int(meta['offset'])
            f.seek(offset)
            deadline = time.monotonic() + time_budget
            blocked_tail = False
            while f.tell() - offset < byte_budget and time.monotonic() < deadline:
                position = f.tell()
                line = f.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    raise ValueError('saved window exceeds curve budget')
                if not line.endswith(b'\n'):
                    f.seek(position); blocked_tail = True; break
                window = json.loads(line)
                start, end = float(window['start_s']), float(window['end_s'])
                if not math.isfinite(start + end) or not 0 <= start < end <= 10**12:
                    raise ValueError('invalid saved window range')
                if not sealed and end > durable_end:
                    f.seek(position); blocked_tail = True; break
                if meta['first_s'] is None:
                    meta['first_s'] = start
                if start < meta['last_s'] - 1e-6:
                    raise ValueError('saved windows are out of order')
                gap = start > meta['last_s'] + .2
                self._add(window, gap)
                meta['last_s'] = end
            meta['offset'] = f.tell()
        for key, value in meta.items():
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
        self.db.commit()
        return dict(indexing=meta['offset'] < size and not blocked_tail,
                    indexed_bytes=meta['offset'], source_bytes=size)

    def _add(self, window, gap):
        metrics = window.get('metrics', {})
        if not isinstance(metrics, dict) or len(metrics) > 512:
            raise ValueError('invalid saved channel budget')
        incoming = {}
        for name, stats in metrics.items():
            count, invalid = int(stats.get('count', 0)), int(stats.get('invalid', 0))
            if count < 0 or invalid < 0 or len(name) > 256:
                raise ValueError('invalid saved channel statistics')
            if count:
                mean, low, high = (float(stats[k]) for k in ('mean', 'min', 'max'))
                if not math.isfinite(mean + low + high + mean * count):
                    raise ValueError('nonfinite saved channel statistics')
                incoming[name] = [mean * count, count, low, high, invalid]
            else:
                incoming[name] = [0., 0, None, None, invalid]
        start, end = float(window['start_s']), float(window['end_s'])
        for level in LEVELS:
            bucket = int(start // level)
            row = self.db.execute('SELECT payload FROM bins WHERE level=? AND bucket=?', (level, bucket)).fetchone()
            value = json.loads(row[0]) if row else {'start': start, 'end': end, 'gap': False, 'metrics': {}}
            value['end'] = max(value['end'], end)
            value['gap'] |= gap
            for name, add in incoming.items():
                old = value['metrics'].setdefault(name, [0., 0, None, None, 0])
                old[0] += add[0]; old[1] += add[1]; old[4] += add[4]
                if add[1]:
                    old[2] = add[2] if old[2] is None else min(old[2], add[2])
                    old[3] = add[3] if old[3] is None else max(old[3], add[3])
            self.db.execute('INSERT OR REPLACE INTO bins VALUES (?,?,?)',
                            (level, bucket, json.dumps(value, allow_nan=False, separators=(',', ':'))))
            self.db.execute('DELETE FROM bins WHERE level=? AND bucket<?', (level, bucket - KEEP + 1))

    def query(self, columns: dict, units: dict, start: float, end: float):
        width = (end - start) / 600
        level = LEVELS[-1]
        for candidate in LEVELS:
            row = self.db.execute('SELECT min(bucket),max(bucket) FROM bins WHERE level=?', (candidate,)).fetchone()
            if row[0] is not None and candidate >= width and row[0] * candidate <= start:
                level = candidate; break
        width = max(width, level)
        point_count = min(600, max(1, math.ceil((end - start) / width)))
        values = {key: {} for key in columns}
        rows = self.db.execute('SELECT payload FROM bins WHERE level=? AND bucket BETWEEN ? AND ? ORDER BY bucket',
                               (level, int(start // level), int(end // level)))
        edge_approximation = False
        for (payload,) in rows:
            item = json.loads(payload)
            if item['end'] <= start or item['start'] > end:
                continue
            edge_approximation |= item['start'] < start or item['end'] > end
            center = (max(start, item['start']) + min(end, item['end'])) / 2
            bucket = min(point_count - 1, int((center - start) / width))
            for key, column in columns.items():
                source = item['metrics'].get(column)
                if not source:
                    continue
                old = values[key].setdefault(bucket, [0., 0, None, None, False])
                old[0] += source[0]; old[1] += source[1]
                old[4] |= bool(source[4]) or item['gap']
                if source[1]:
                    old[2] = source[2] if old[2] is None else min(old[2], source[2])
                    old[3] = source[3] if old[3] is None else max(old[3], source[3])
        series = []
        for key, bins in values.items():
            points, extrema = [], []
            for bucket in range(point_count):
                x = (start + bucket * width + min(width, end - start - bucket * width) / 2) * 1000
                value = bins.get(bucket)
                valid = value is not None and value[1] and not value[4]
                points.append([x, value[0] / value[1] if valid else None])
                extrema.append([x, value[2] if valid else None, value[3] if valid else None])
            series.append(dict(id=key, unit=units[key], points=points, extrema=extrema))
        return dict(series=series, resolution_s=max(width, level),
                    method='saved-window-weighted-mean-v1', edge_approximation=edge_approximation)
