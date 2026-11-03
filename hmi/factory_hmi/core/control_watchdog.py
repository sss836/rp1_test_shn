"""Monitor successful dispatches without acquiring motor, UI or recorder locks."""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class CommandWatchdog:
    def __init__(self, timeout_s: float, on_timeout: Callable[[str, int], None]) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("control watchdog timeout must be positive and finite")
        self.timeout_s = float(timeout_s)
        self._on_timeout = on_timeout
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._last_dispatch = 0.0
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self._generation = 0

    def arm(self) -> None:
        self.disarm()
        with self._lock:
            self._last_dispatch = time.monotonic()
            self.error = None
            stop = self._stop = threading.Event()
            self._generation += 1
            generation = self._generation
            self._thread = threading.Thread(target=self._run, args=(stop, generation),
                                            name="factory-control-watchdog", daemon=True)
            self._thread.start()

    def disarm(self) -> None:
        with self._lock:
            self._stop.set()
            self._generation += 1

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation

    def touch(self) -> None:
        with self._lock:
            self._last_dispatch = time.monotonic()

    def _run(self, stop: threading.Event, generation: int) -> None:
        while not stop.wait(min(.05, self.timeout_s / 4)):
            with self._lock:
                age = time.monotonic() - self._last_dispatch
                if stop.is_set() or self._stop is not stop or age <= self.timeout_s:
                    continue
                stop.set()
                self.error = f"controller_timeout: no successful dispatch for {age:.3f}s (limit {self.timeout_s:.3f}s)"
            self._on_timeout(self.error, generation)
            return
