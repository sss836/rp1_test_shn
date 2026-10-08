"""Authenticated, read-only workstation telemetry independent of report uploads."""

from __future__ import annotations

import math
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


def installation_id(data_root: Path) -> str:
    path = data_root / "sync" / "installation-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            value = str(uuid4())
            handle.write(value + "\n")
    except FileExistsError:
        value = path.read_text(encoding="utf-8").strip()
    return str(UUID(value))


def _text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]


def _number(value: Any) -> float:
    try:
        result = float(value or 0)
    except (ValueError, TypeError):
        return 0.0
    return max(0.0, min(result, 315360000)) if math.isfinite(result) else 0.0


def build_presence_snapshot(runtimes: Mapping[str, Mapping[str, Any]], plc: Mapping[str, Any]) -> dict[str, Any]:
    runs = []
    for name, snapshot in list(runtimes.items())[:32]:
        station = snapshot.get("station") or {}
        playback = snapshot.get("playback") or {}
        progress = playback.get("progress") or snapshot.get("progress") or {}
        state = str(snapshot.get("state") or station.get("state") or "idle").lower()
        if state in {"disconnected", "ready", "connected", "configured"}:
            state = "idle"
        if state not in {"idle", "armed", "running", "paused", "stopping", "fault", "completed"}:
            state = "unknown"
        runs.append({
            "session_key": _text(name, 128), "state": state,
            "test_id": _text(playback.get("test_id"), 255),
            "sample_id": _text(playback.get("robot_id"), 128),
            "test_case": _text(station.get("limb"), 128),
            "active_seconds": _number(progress.get("active_seconds")),
            "completed_cycles": min(int(_number(progress.get("completed_cycles"))), 2147483647),
        })
    status = plc.get("status") or {}
    channels = status.get("ChannelPermit") or []
    return {
        "runs": runs,
        "plc": {
            "driver": _text(plc.get("driver") or "unknown", 32),
            "phase": _text(plc.get("phase") or "DISCONNECTED", 64),
            "communication_state": _text(plc.get("communication_state") or "DISCONNECTED", 32),
            "stale": bool(plc.get("stale", True)),
            "powered_channels": [index + 1 for index, enabled in enumerate(channels[:4]) if enabled],
        },
    }


class HmiPresencePublisher:
    def __init__(
        self, client: Any, *, data_root: Path,
        snapshot_provider: Callable[[], dict[str, Any]], interval: float = 10.0,
    ) -> None:
        self.client = client
        self.installation_id = installation_id(data_root)
        self.station_name = (os.environ.get("RP1_FACTORY_STATION_NAME") or socket.gethostname())[:128]
        self.bench_id = os.environ.get("RP1_FACTORY_BENCH_ID", "")[:64]
        self.snapshot_provider = snapshot_provider
        self.interval = interval
        self.connection_id = ""
        self.sequence = 0
        self.last_error = ""
        self.last_success_at = 0.0
        self._sampled_at = 0.0
        self._snapshot: dict[str, Any] = {"runs": [], "plc": {}}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self) -> None:
        self.connection_id = str(uuid4())
        self.sequence = 0
        self._stop.clear()
        self.send_once()
        self._thread = threading.Thread(target=self._run, name="rp1-platform-presence", daemon=True)
        self._thread.start()

    def send_once(self) -> None:
        try:
            snapshot = self.snapshot_provider()
        except Exception:
            snapshot = None
        if snapshot is not None:
            self._snapshot = snapshot
            self._sampled_at = time.monotonic()
        age = min(86400.0, time.monotonic() - self._sampled_at) if self._sampled_at else 86400.0
        self.sequence += 1
        self.client.heartbeat_presence(self.connection_id, {
            "installation_id": self.installation_id, "station_name": self.station_name,
            "bench_id": self.bench_id, "sequence": self.sequence,
            "snapshot_age_seconds": age, **self._snapshot,
        })
        self.last_success_at = time.monotonic()
        self.last_error = ""

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.send_once()
            except Exception as exc:
                self.last_error = str(exc)[:300]
                if getattr(exc, "status", None) in {401, 403, 409}:
                    self._stop.set()

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=7)
        if self.connection_id:
            try:
                self.client.disconnect_presence(self.connection_id)
            except Exception:
                pass
        self.connection_id = ""

    def status(self) -> dict[str, Any]:
        return {
            "online": bool(self.connection_id and not self._stop.is_set() and self.last_success_at and time.monotonic() - self.last_success_at < 45),
            "last_error": self.last_error,
        }
