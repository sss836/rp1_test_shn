"""GUI-driven lease renewal with its own cancellable, bounded HTTP request.

No REST pool workers and no background renewal of an unresponsive GUI.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from factory_hmi.client import GatewayClient, GatewayError, GatewayHTTPError

logger = logging.getLogger(__name__)


class LeaseHeartbeat(QObject):
    failed = Signal(object)
    renewed = Signal(object)

    def __init__(self, client: GatewayClient, enabled: Callable[[], bool], parent: QObject,
                 *, manager: Any = None) -> None:
        super().__init__(parent)
        self.client = client
        self.enabled = enabled
        self.manager = manager or QNetworkAccessManager(self)
        self.reply: Any = None
        self.closed = False
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._timeout)
        self.retry = QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self.pulse)

    def pulse(self) -> None:
        if self.closed or not self.enabled() or self.reply is not None:
            return
        generation = self.client.lease_generation
        if generation is None:
            return
        req = QNetworkRequest(QUrl(self.client.base_url + "/api/v1/lease/heartbeat"))
        req.setRawHeader(b"Content-Type", b"application/json")
        req.setRawHeader(b"X-RP1-Client-ID", self.client.client_id.encode())
        req.setRawHeader(b"X-RP1-Lease-Generation", str(generation).encode())
        if self.client.operator_token:
            req.setRawHeader(b"X-RP1-Operator-Token", self.client.operator_token.encode())
        reply = None
        try:
            reply = self.reply = self.manager.post(req, b"{}")
            reply.finished.connect(lambda: self._finished(reply, generation))
            self.deadline.start(max(1, round(self.client.lease_heartbeat_timeout * 1000)))
        except Exception as exc:
            # A dispatch/binding error must not latch `reply` forever. The
            # next GUI pulse may retry; no independent liveness is invented.
            self.reply = None
            self.deadline.stop()
            logger.exception("lease heartbeat setup failed: generation=%s reply_type=%s",
                             generation, type(reply).__name__)
            self._discard_reply(reply, abort=True)
            self._failed(GatewayError("lease heartbeat request setup failed",
                                      detail=f"{type(exc).__name__}: {exc}"))

    def _timeout(self) -> None:
        reply = self.reply
        if reply is None:
            return
        try:
            reply.abort()
        except Exception:
            logger.exception("lease heartbeat abort failed")
        # Some transport failures do not deliver finished after cancellation.
        # Retire the request anyway, and ignore any later acknowledgement.
        if self.reply is reply:
            self.reply = None
            self.deadline.stop()
            self._discard_reply(reply)
            self._failed(GatewayError("lease heartbeat deadline exceeded"))

    @staticmethod
    def _discard_reply(reply: Any, *, abort: bool = False) -> None:
        # A foreign Qt model object is not owned by this sender. Never delete
        # it while recovering from an unexpected post() return value.
        if (not isinstance(reply, QObject)
                or not callable(getattr(reply, "abort", None))
                or not callable(getattr(reply, "readAll", None))):
            return
        if abort:
            try:
                reply.abort()
            except Exception:
                logger.exception("lease heartbeat reply cancellation failed")
        try:
            reply.deleteLater()
        except RuntimeError:
            logger.exception("lease heartbeat reply already destroyed")

    def _failed(self, exc: Exception) -> None:
        self.failed.emit(exc)
        if (not self.closed and self.enabled()
                and not (isinstance(exc, GatewayHTTPError)
                         and exc.status_code in {400, 401, 403, 404, 409, 422})):
            self.retry.start(150)

    def _finished(self, reply: Any, generation: int) -> None:
        if self.reply is not reply:
            self._discard_reply(reply)
            return
        self.reply = None
        self.deadline.stop()
        try:
            if self.closed or generation != self.client.lease_generation:
                return
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if status and int(status) >= 400:
                raise GatewayHTTPError("lease heartbeat rejected", status_code=int(status),
                                       detail=bytes(reply.readAll()).decode(errors="replace"))
            if reply.error() != QNetworkReply.NoError:
                raise GatewayError("lease heartbeat failed or deadline exceeded", detail=reply.errorString())
            payload = json.loads(bytes(reply.readAll()).decode())
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise GatewayError("invalid lease heartbeat acknowledgement")
            if self.client.observe_lease(payload):
                self.renewed.emit(payload)
        except Exception as exc:
            self._failed(exc)
        finally:
            self._discard_reply(reply)

    def stop(self) -> None:
        self.closed = True
        self.retry.stop()
        self.deadline.stop()
        if self.reply is not None:
            reply, self.reply = self.reply, None
            self._discard_reply(reply, abort=True)
