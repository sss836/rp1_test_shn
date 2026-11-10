"""One latest-only display sampler per station runtime."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)
DEFAULT_FRAME_BYTES = 1024**2


@dataclass(frozen=True, slots=True)
class DisplayFrame:
    sequence: int
    sampled_monotonic: float
    sampled_at: str
    text: str

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.sampled_monotonic


class DisplayPublisher:
    """Collect and encode independently of the number or speed of readers.

    The collector may take device locks. Readers only take the short lock
    protecting the frame reference. Neither a read nor a blocked consumer can
    trigger or postpone sampling.
    """

    def __init__(
        self,
        collect: Callable[[], Mapping[str, Any]],
        *,
        rate_hz: float = 10.0,
        max_bytes: int = DEFAULT_FRAME_BYTES,
    ) -> None:
        if not math.isfinite(rate_hz) or rate_hz <= 0:
            raise ValueError("display rate must be finite and positive")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("display byte limit must be a positive integer")
        self._collect = collect
        self._interval = 1 / max(1.0, min(rate_hz, 30.0))
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frame: DisplayFrame | None = None
        self._sequence = 0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                if self._stop.is_set():
                    raise RuntimeError("previous display sampler is still stopping")
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="factory-display", daemon=True)
            self._thread.start()

    def request_stop(self) -> None:
        with self._lock:
            self._stop.set()
            self._frame = None

    def stop(self, timeout: float = 1.0) -> bool:
        """Invalidate the display and wait briefly for an in-flight collection."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("display stop timeout must be finite and non-negative")
        with self._lock:
            self._stop.set()
            self._frame = None
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def latest(self, max_age_s: float | None = None) -> DisplayFrame | None:
        with self._lock:
            frame = self._frame
        if frame is not None and (max_age_s is None or frame.age_s <= max_age_s):
            return frame
        return None

    def _run(self) -> None:
        last_warning = -math.inf
        while not self._stop.is_set():
            started = time.monotonic()
            sampled_at = datetime.now(timezone.utc).isoformat()
            try:
                payload = self._collect()
                if not isinstance(payload, Mapping):
                    raise TypeError("display snapshot must be a mapping")
                sequence = self._sequence + 1
                frame_data = {**payload, "_display": {"sequence": sequence, "sampled_at": sampled_at}}
                text = json.dumps(frame_data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                if len(text.encode("utf-8")) > self._max_bytes:
                    raise ValueError("display frame exceeds byte limit")
                with self._lock:
                    if not self._stop.is_set():
                        self._frame = DisplayFrame(sequence, started, sampled_at, text)
                        self._sequence = sequence
            except Exception as exc:
                if started - last_warning >= 30.0:
                    logger.warning("Display snapshot unavailable (%s)", type(exc).__name__)
                    last_warning = started
            # Let an overrun skip slots instead of producing a catch-up burst.
            elapsed = time.monotonic() - started
            self._stop.wait(self._interval - elapsed if elapsed < self._interval else self._interval)
