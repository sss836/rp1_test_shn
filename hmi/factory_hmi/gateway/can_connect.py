"""A station-scoped, durable receipt for a single CAN connection action."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import logging
from pathlib import Path
import threading
import time
from typing import Any

from fastapi import HTTPException

from factory_hmi.core.command_receipts import StartReceipts


logger = logging.getLogger(__name__)


class CanConnectCommands:
    def __init__(self, directory: Path) -> None:
        self._path = directory / "can-connect-receipts.sqlite3"
        self._receipts: StartReceipts | None = None
        self._lock = threading.Lock()
        self._active: str | None = None

    def _store(self) -> StartReceipts:
        if self._receipts is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._receipts = StartReceipts(self._path)
        return self._receipts

    @staticmethod
    def _key(owner: str, command_id: str) -> str:
        return StartReceipts.digest([owner, command_id])

    def _public_receipt(self, key: str, command_id: str, stored: Mapping[str, Any]) -> dict[str, Any]:
        status = stored["status"]
        error = stored.get("error")
        if status == "pending" and self._active != key:
            status = "unknown"
            error = "连接回执未完成；请核对工位并安全恢复，未重发连接"
        return {"command_id": command_id, "status": status, "result": stored.get("result"), "error": error}

    def get(self, owner: str, command_id: str) -> dict[str, Any]:
        key = self._key(owner, command_id)
        with self._lock:
            return self._public_receipt(key, command_id, self._store().get(key))

    def submit(self, owner: str, command_id: str, payload: Mapping[str, Any],
               execute: Callable[[], Any]) -> dict[str, Any]:
        key, digest = self._key(owner, command_id), StartReceipts.digest(dict(payload))
        with self._lock:
            store = self._store()
            previous = store.get(key)
            if previous["status"] != "not_found":
                if previous["payload_hash"] != digest:
                    raise HTTPException(409, "连接请求编号已用于不同配置")
                return self._public_receipt(key, command_id, previous)
            if self._active is not None:
                raise HTTPException(409, "工位已有 CAN 连接请求在处理，请核对原回执")
            store.begin(key, digest)
            self._active = key
            try:
                threading.Thread(target=self._run, args=(key, command_id, store, execute),
                                 daemon=True, name="factory-can-connect").start()
            except RuntimeError:
                self._active = None
                store.finish(key, "failed", error="无法启动 CAN 连接任务")
                raise HTTPException(503, "无法启动 CAN 连接任务")
            return {"command_id": command_id, "status": "pending", "result": None, "error": None}

    def _run(self, key: str, command_id: str, store: StartReceipts, execute: Callable[[], Any]) -> None:
        started = time.monotonic()
        result, error, status = None, None, "unknown"
        logger.info("CAN connect started: command_id=%s", command_id)
        try:
            result = execute()
            can = result.get("can") if isinstance(result, Mapping) else None
            if not isinstance(can, Mapping) or can.get("all_up") is not True:
                result = None
                error = "工位未返回已核验的 CAN 连接状态"
            else:
                status = "connected"
        except HTTPException as exc:
            status = "unknown" if exc.status_code >= 500 else "failed"
            error = str(exc.detail)
        except Exception as exc:
            logger.exception("CAN connect reply unavailable: command_id=%s", command_id)
            error = str(exc)
        finally:
            with self._lock:
                try:
                    store.finish(key, status, result=result, error=error)
                except Exception:
                    logger.exception("CAN connect receipt not saved: command_id=%s", command_id)
                finally:
                    self._active = None
            logger.log(logging.INFO if status == "connected" else logging.WARNING,
                       "CAN connect completed: command_id=%s status=%s elapsed_s=%.3f error=%s",
                       command_id, status, time.monotonic() - started, error or "")
