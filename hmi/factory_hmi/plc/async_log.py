"""Bounded operational logging. Never performs filesystem I/O on control threads."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import threading


class AsyncPlcLog:
    def __init__(self, capacity=2048):
        self.queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.failed = 0
        self.dropped = 0
        self.written = 0
        self.last_error = ''

    def submit(self, path: Path, record: dict) -> bool:
        # Serialize before enqueue: later state changes cannot rewrite evidence.
        line = json.dumps(record, ensure_ascii=False) + '\n'
        with self._lock:
            if self._stop.is_set():
                self.dropped += 1
                self.last_error = 'PLC log writer closed'
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='rp1-plc-log', daemon=True)
                self._thread.start()
            try:
                self.queue.put_nowait((path, line))
                return True
            except queue.Full:
                self.dropped += 1
                self.last_error = 'PLC operational log queue full; evidence incomplete'
                return False

    @staticmethod
    def _write(path, line):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _run(self):
        while not self._stop.is_set() or not self.queue.empty():
            try:
                path, line = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._write(path, line)
                with self._lock:
                    self.written += 1
            except Exception as exc:
                # A partial append is uncertain: no blind retry/duplicate record.
                with self._lock:
                    self.failed += 1
                    self.last_error = str(exc)
            finally:
                self.queue.task_done()

    def snapshot(self):
        with self._lock:
            return dict(pending=self.queue.unfinished_tasks, written=self.written,
                        failed=self.failed, dropped=self.dropped, last_error=self.last_error,
                        degraded=bool(self.failed or self.dropped or self.last_error))

    def close(self, timeout=.25):
        with self._lock:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                with self._lock:
                    self.last_error = 'PLC log flush pending; disk may be slow'
        return self.snapshot()
