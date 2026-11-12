"""Prepare resource shutdown while HTTP and process supervision remain alive."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

LIFECYCLE_PATH = "/api/v1/lifecycle/shutdown"
SITE_ISOLATED_HEADER = "X-RP1-Site-Isolated"
# Shutdown must not remove the existing operator safety controls. These
# exact routes cannot start/reconfigure a test; endpoint auth, lease, PLC
# revision and mechanical-support checks still apply after admission.
SAFETY_PATHS = frozenset({
    "/api/v1/plc/stop", "/api/v1/plc/all-stop",
    "/api/v1/motors/disable", "/api/v1/playback/disable",
    "/api/v1/manual/stop", "/api/v1/disconnect", "/api/v1/quick/abort",
    "/api/v1/lease/protective-stop/disable",
})
# Fault reset is allowed only after close has failed, with the existing PLC
# LIVE/all-output-requests-OFF/revision checks. It does not enable any output.
RECOVERY_PATHS = SAFETY_PATHS | frozenset({
    "/api/v1/lease/recover", "/api/v1/plc/reset-fault",
})
HEARTBEAT_PATH = "/api/v1/lease/heartbeat"


class ShutdownCoordinator:
    def __init__(self, close: Callable[[], None], *,
                 close_isolated: Callable[[], None] | None = None, drain_timeout_s: float = 10.0):
        self._close = close
        self._close_isolated = close_isolated
        self._condition = threading.Condition(threading.RLock())
        self._state = "running"
        self._detail = "Shutdown not requested"
        self._active = 0
        self._attempt = 0
        self._site_isolated = False
        self._drain_timeout_s = drain_timeout_s

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {"state": self._state, "ready": self._state == "ready",
                    "detail": self._detail, "attempt": self._attempt,
                    "active_requests": self._active, "site_isolated": self._site_isolated}

    def enter(self, path: str) -> bool:
        with self._condition:
            if self._state != "running" and not (
                (self._state == "preparing" and path in SAFETY_PATHS)
                or (self._state == "blocked" and path in RECOVERY_PATHS)
            ):
                return False
            self._active += 1
            return True

    def leave(self) -> None:
        with self._condition:
            self._active -= 1
            self._condition.notify_all()

    def request(self, *, site_isolated: bool = False) -> dict[str, Any]:
        with self._condition:
            if self._state == "ready":
                return self.snapshot()
            if self._state == "preparing":
                if site_isolated and not self._site_isolated:
                    raise ValueError("已有另一种退出方式正在处理，请等待该次结果")
                return self.snapshot()
            close = self._close_isolated if site_isolated else self._close
            if close is None:
                raise ValueError("当前网关不支持隔离后退出")
            self._state = "preparing"
            self._site_isolated = site_isolated
            self._detail = "Draining accepted requests before closing resources"
            self._attempt += 1
            threading.Thread(target=self._prepare, args=(close,), name="gateway-prepare-shutdown", daemon=True).start()
            return self.snapshot()

    def _prepare(self, close: Callable[[], None]) -> None:
        try:
            deadline = time.monotonic() + self._drain_timeout_s
            with self._condition:
                while self._active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Accepted control request is still pending; no resources were closed")
                    self._condition.wait(remaining)
            close()
            # Safety requests can arrive during close. Keep their existing
            # service/lease serialization, then drain before acknowledging
            # readiness. The final zero check and ready transition share the
            # admission lock, so no accepted safety request can escape it.
            with self._condition:
                deadline = time.monotonic() + self._drain_timeout_s
                while self._active:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("Operator safety request is still pending; exit remains blocked")
                    self._condition.wait(remaining)
                self._state = "ready"
                self._detail = "Software resources closed; this is not proof of external power OFF or mechanical safety"
        except Exception as exc:
            logger.error("Shutdown blocked; gateway remains alive: %s", exc)
            with self._condition:
                self._state, self._detail = "blocked", str(exc)


class ShutdownGate:
    """Atomically reject new writes, and drain already admitted ASGI requests."""
    def __init__(self, app, coordinator: ShutdownCoordinator):
        self.app = app
        self.coordinator = coordinator

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}
                or path in {LIFECYCLE_PATH, HEARTBEAT_PATH}):
            await self.app(scope, receive, send)
            return
        if not self.coordinator.enter(path):
            response = JSONResponse(status_code=409, content={
                "detail": "Gateway shutdown requested; new control is disabled",
                "shutdown": self.coordinator.snapshot(),
            })
            await response(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            self.coordinator.leave()
