"""Rate-limit only repeated pymodbus connection diagnostics, not safety faults."""
from __future__ import annotations
from collections import OrderedDict
import logging
import threading
import time


class ConnectionLogFilter(logging.Filter):
    def __init__(self, interval=10.0, capacity=64, clock=time.monotonic):
        super().__init__()
        self.interval, self.capacity, self.clock = interval, capacity, clock
        self._seen = OrderedDict()
        self._lock = threading.Lock()
        self.suppressed = 0

    def filter(self, record):
        message = record.getMessage()
        # Exact known transport messages only. New errors and critical records
        # remain visible. Do not change the network retry or safety policy.
        if record.levelno >= logging.CRITICAL or not (
            message.startswith("Connection to (") or message.startswith("Failed to connect")
        ):
            return True
        key = (record.levelno, message)
        now = self.clock()
        with self._lock:
            previous = self._seen.get(key)
            if previous is not None and now - previous[0] < self.interval:
                self._seen[key] = (previous[0], previous[1] + 1)
                self.suppressed += 1
                return False
            skipped = previous[1] if previous else 0
            self._seen[key] = (now, 0)
            self._seen.move_to_end(key)
            while len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
        if skipped:
            record.msg = message + f" [suppressed {skipped} identical connection messages in {self.interval:g}s]"
            record.args = ()
        return True

    def snapshot(self):
        with self._lock:
            return {"suppressed_repeats": self.suppressed, "interval_seconds": self.interval}


CONNECTION_LOG_FILTER = ConnectionLogFilter()


def install_connection_log_filter():
    # pymodbus.logging.Log owns this logger; a filter on its parent would not
    # run for propagated child records.
    logger = logging.getLogger("pymodbus.logging")
    if CONNECTION_LOG_FILTER not in logger.filters:
        logger.addFilter(CONNECTION_LOG_FILTER)
