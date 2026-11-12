"""Private, bounded IPC to the process that owns one PLC cabinet."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
from socketserver import UnixStreamServer
import stat
import subprocess
import sys
import tempfile
import threading
from typing import Any

from .config import PlcCabinetConfig, PlcSetpointConfig, PlcTimingConfig, load_plc_config
from .coordination import PlcRequestConflict
from .service import PlcCabinetService, PlcControlDisabledError

_LOG = logging.getLogger(__name__)
_REQUEST_BYTES = 64 * 1024
_REPLY_BYTES = 8 * 1024 * 1024
_SAFETY_COMMANDS = {"stop", "all-stop"}
_COMMANDS = _SAFETY_COMMANDS | {"connect", "disconnect", "start", "channel", "setpoints", "reset-fault"}
_METHODS = {"snapshot", "shutdown-status", "execute", "close"}


def _receive(connection: socket.socket, maximum: int) -> dict[str, Any]:
    with connection.makefile("rb") as stream:
        line = stream.readline(maximum + 1)
    if not line.endswith(b"\n") or len(line) > maximum:
        raise ValueError("Missing or oversized cabinet IPC frame")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError("Cabinet IPC frame must be an object")
    return value


def _send(connection: socket.socket, value: dict[str, Any], maximum: int) -> None:
    message = json.dumps(value, ensure_ascii=False, allow_nan=False).encode() + b"\n"
    if len(message) > maximum:
        raise ValueError("Cabinet IPC frame exceeds its size limit")
    connection.sendall(message)


@dataclass(frozen=True)
class _ClientConfig:
    driver: str
    access_mode: str
    dut_can_map: dict[str, str]


class PlcCabinetClient:
    """Attached caller. Lifecycle authorization is held only by the process owner."""

    def __init__(self, config: _ClientConfig, path: str, token: str, command_timeout: float,
                 *, lifecycle_token: str | None = None):
        self.config = config
        self.read_only = config.access_mode == "monitor"
        self.coordination = self
        self._path = path
        self._token = token
        self._lifecycle_token = lifecycle_token
        self._command_timeout = command_timeout
        self._last_snapshot: dict[str, Any] = {}
        self._snapshot_guard = threading.Lock()

    @classmethod
    def attach(cls, info: dict[str, Any]) -> PlcCabinetClient:
        return cls(_ClientConfig(**info["config"]), info["path"], info["token"], info["command_timeout"])

    def _request(self, method: str, params: dict[str, Any], *, timeout: float,
                 owner: bool = False) -> dict[str, Any]:
        if not self._path:
            raise ConnectionError("PLC cabinet process is not started")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(self._path)
            token = self._lifecycle_token if owner else self._token
            _send(connection, {"method": method, "token": token, "params": params}, _REQUEST_BYTES)
            response = _receive(connection, _REPLY_BYTES)
        if response.get("ok") is True:
            return response["result"]
        detail = str(response.get("detail", "PLC 柜进程响应无效"))
        code = str(response.get("code", "cabinet_unconfirmed"))
        if code == "control_disabled":
            raise PlcControlDisabledError(detail)
        raise PlcRequestConflict(code, detail)

    def snapshot(self, *, include_history: bool = True) -> dict[str, Any]:
        try:
            value = self._request("snapshot", {"include_history": include_history}, timeout=1.0)
        except (OSError, ValueError, PlcRequestConflict):
            with self._snapshot_guard:
                value = self._last_snapshot
                coordination = dict(value.get("coordination", {"epoch": "", "revision": 0, "snapshot_sequence": 0}))
            detail = "PLC 柜进程状态未确认，请检查本机连接和实际供电反馈"
            return {
                "phase": "COMM_LOST", "communication_state": "COMM_LOST", "stale": True,
                "status": None, "command": {}, "feedback": {}, "last_error": detail,
                "communication_enabled": bool(value.get("communication_enabled")),
                "communication_disable_allowed": False, "communication_disable_reason": detail,
                "output_admission": {"allowed": False, "reason": detail},
                "driver": self.config.driver, "access_mode": self.config.access_mode,
                "read_only": self.read_only, "dut_can_map": dict(self.config.dut_can_map),
                "coordination": {**coordination, "busy": True},
                "timings": dict(value.get("timings", {})),
                "shutdown": {"state": "unconfirmed", "detail": detail},
            }
        self._remember(value)
        return value

    def _remember(self, value: dict[str, Any]) -> None:
        with self._snapshot_guard:
            current = self._last_snapshot.get("coordination", {}).get("snapshot_sequence", 0)
            if value["coordination"]["snapshot_sequence"] >= current:
                # Keep only what the fail-closed fallback needs. Do not cache
                # histories or caller-owned mutable feedback.
                coordination = value["coordination"]
                self._last_snapshot = {
                    "communication_enabled": value["communication_enabled"],
                    "coordination": {key: coordination[key] for key in ("epoch", "revision", "snapshot_sequence")},
                    "timings": dict(value["timings"]),
                }

    def execute(self, operation: str, payload: dict[str, Any], *, user: str,
                client_id: str | None, request_id: str | None,
                expected_epoch: str | None, expected_revision: int | None) -> dict[str, Any]:
        try:
            result = self._request("execute", {
                "operation": operation, "payload": payload, "user": user, "client_id": client_id,
                "request_id": request_id, "expected_epoch": expected_epoch,
                "expected_revision": expected_revision,
            }, timeout=self._command_timeout)
        except (OSError, ValueError) as exc:
            raise PlcRequestConflict("cabinet_unconfirmed", "PLC 柜指令结果未确认，请检查实际反馈") from exc
        self._remember(result["result"])
        return result

    def shutdown_safety_request(self):
        # The authenticated command is registered inside the cabinet process
        # before acquiring its control lock. This compatibility context adds
        # no gateway-side lock or round trip.
        return nullcontext()


class PlcCabinetProcess(PlcCabinetClient):
    """Starts and supervises the cabinet child without giving callers its stop key."""

    def __init__(self, config: PlcCabinetConfig | None = None, *, config_path: str | Path | None = None,
                 data_root: str | Path = "/var/lib/rp1-factory-hmi"):
        self._cabinet_config = config or load_plc_config(config_path)
        self._data_root = Path(data_root).expanduser().resolve()
        self._process: subprocess.Popen | None = None
        self._owner: socket.socket | None = None
        self._directory: Path | None = None
        self._shutdown_result = {"state": "not_requested", "detail": "PLC shutdown not requested"}
        self._confirmed_close = False
        self._closed = False
        timing = self._cabinet_config.timings
        # Budget the RPC without constructing or validating the real driver
        # before the operator opts in to PLC communication.
        protocol = self._cabinet_config.protocol
        try:
            timeout_ms = int(protocol.get("timeout_ms", 2000))
            retries = int(protocol.get("retries", 0))
        except (TypeError, ValueError, OverflowError):
            timeout_ms, retries = 2000, 0
        io_seconds = min(max(timeout_ms, 1), 300_000) * (min(max(retries, 0), 10) + 1) / 1000
        command_timeout = 3 * timing.command_timeout_ms / 1000 + 6 * io_seconds + 5
        self._close_timeout = ((6 * timing.command_timeout_ms + timing.km0_timeout_ms
                                + 4 * timing.ps1_timeout_ms) / 1000 + 4 * io_seconds + 15)
        super().__init__(_ClientConfig(self._cabinet_config.driver, self._cabinet_config.access_mode,
                                      dict(self._cabinet_config.dut_can_map)), "", secrets.token_urlsafe(32), command_timeout,
                         lifecycle_token=secrets.token_urlsafe(32))

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def connection_info(self) -> dict[str, Any]:
        if self._process is None or self._process.poll() is not None or self._confirmed_close:
            raise RuntimeError("PLC cabinet process is not available for attachment")
        return {"path": self._path, "token": self._token, "config": asdict(self.config),
                "command_timeout": self._command_timeout}

    @property
    def shutdown_status(self) -> dict[str, Any]:
        if self._process is None or self._confirmed_close:
            return dict(self._shutdown_result)
        try:
            return self._request("shutdown-status", {}, timeout=.5)
        except (OSError, ValueError, PlcRequestConflict):
            return {"state": "unconfirmed", "detail": "PLC 柜进程未报告停机状态"}

    def start(self) -> None:
        if self._closed or self._process is not None:
            raise RuntimeError("PLC cabinet process cannot be started twice")
        self._directory = Path(tempfile.mkdtemp(prefix="rp1-plc-"))
        self._path = str(self._directory / "cabinet.sock")
        owner, child = socket.socketpair()
        try:
            with child:
                self._process = subprocess.Popen(
                    [sys.executable, "-m", "factory_hmi.plc.process", str(child.fileno())],
                    pass_fds=(child.fileno(),), start_new_session=True,
                )
            owner.settimeout(5.0)
            _send(owner, {
                "path": self._path, "token": self._token, "lifecycle_token": self._lifecycle_token,
                "data_root": str(self._data_root), "config": asdict(self._cabinet_config),
            }, _REQUEST_BYTES)
            ready = _receive(owner, _REPLY_BYTES)
            if ready.get("ok") is not True:
                raise RuntimeError(str(ready.get("detail", "PLC cabinet worker did not start")))
            owner.settimeout(None)
            self._owner = owner
        except (OSError, ValueError, RuntimeError):
            owner.close()
            # Communication starts disabled. The worker exits on EOF even
            # when the readiness response could not be delivered.
            if self._process is not None:
                with suppress(subprocess.TimeoutExpired):
                    self._process.wait(timeout=2)
            if self._process is None or self._process.poll() is not None:
                self._remove_endpoint()
                self._process = None
            raise

    def close(self, *, site_isolated: bool = False) -> None:
        if type(site_isolated) is not bool:
            raise ValueError("site_isolated must be a boolean")
        if self._process is None or self._closed:
            return
        if not self._confirmed_close:
            try:
                params = {"site_isolated": True} if site_isolated else {}
                self._shutdown_result = self._request("close", params, timeout=self._close_timeout, owner=True)
            except (OSError, ValueError, PlcRequestConflict) as exc:
                self._shutdown_result = {"state": "unconfirmed", "detail": str(exc)}
                raise RuntimeError(f"PLC 柜停机未确认：{exc}") from exc
            self._confirmed_close = True
        # The child keeps its endpoint alive until the owner receives the
        # safe shutdown reply, so a lost reply can be checked again.
        if self._owner is not None:
            self._owner.close()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PLC 柜已报告停机，进程尚未退出") from exc
        self._closed = True
        self._remove_endpoint()

    def _remove_endpoint(self) -> None:
        if self._directory is not None:
            with suppress(FileNotFoundError):
                Path(self._path).unlink()
            with suppress(FileNotFoundError):
                self._directory.rmdir()
            self._directory = None
            self._path = ""


@contextmanager
def _cabinet_lock(config: PlcCabinetConfig, data_root: Path):
    if config.driver == "mock":
        lock_path = data_root / "plc" / "mock-cabinet.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        # Same configured physical endpoint stays exclusive across state dirs.
        # This host lock does not arbitrate other computers on the control VLAN.
        identity = [config.driver, str(config.protocol.get("host", "")).strip().lower(),
                    config.protocol.get("port"), config.protocol.get("unit_id")]
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        lock_path = Path("/tmp") / f"rp1-plc-cabinet-{digest}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("PLC cabinet lock is not owned by this service user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("PLC cabinet already has an active owner") from exc
        yield
    finally:
        os.close(fd)


class _CabinetServer(UnixStreamServer):
    request_queue_size = 16
    timeout = .1

    def __init__(self, path: str, service: PlcCabinetService, token: str, lifecycle_token: str):
        self.service = service
        self._token = token
        self._lifecycle_token = lifecycle_token
        self.exiting = threading.Event()
        self.orphaned = threading.Event()
        self.closed = False
        self.closing = False
        self._preparing = False
        self._site_isolating = False
        self._active_normal = 0
        self._active_safety = 0
        self._gate = threading.Condition()
        self._close_lock = threading.Lock()
        self._slots = {name: threading.BoundedSemaphore(size) for name, size in (
            ("snapshot", 2), ("shutdown-status", 2), ("execute", 1),
            ("stop", 1), ("all-stop", 1), ("close", 1),
        )}
        super().__init__(path, None)

    def process_request(self, connection: socket.socket, _address) -> None:
        connection.settimeout(.25)
        try:
            request = _receive(connection, _REQUEST_BYTES)
            method, token, params = request["method"], request["token"], request["params"]
            if not isinstance(method, str) or method not in _METHODS or not isinstance(params, dict):
                raise ValueError("Invalid cabinet request")
            expected = self._lifecycle_token if method == "close" else self._token
            if not isinstance(token, str) or not token.isascii() or not hmac.compare_digest(token, expected):
                raise PlcRequestConflict("cabinet_forbidden", "PLC 柜进程权限校验失败")
            if method == "close":
                if params.keys() - {"site_isolated"} or type(params.get("site_isolated", False)) is not bool:
                    raise ValueError("Invalid cabinet close request")
                if self.closed:
                    with connection:
                        _send(connection, {"ok": True, "result": self._shutdown_status()}, _REPLY_BYTES)
                    return
            slot_name = params.get("operation") if method == "execute" else method
            if method == "execute" and slot_name not in _SAFETY_COMMANDS:
                slot_name = "execute"
            if slot_name not in self._slots:
                raise ValueError("Invalid cabinet method")
            slot = self._slots[slot_name]
            if not slot.acquire(blocking=False):
                raise PlcRequestConflict("busy", "PLC 柜正在处理请求，请刷新实际状态")
            try:
                threading.Thread(target=self._answer, args=(connection, method, params, slot), daemon=True).start()
            except RuntimeError:
                slot.release()
                raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            with connection, suppress(OSError, ValueError):
                _send(connection, self._error(exc), _REPLY_BYTES)

    @staticmethod
    def _error(error: Exception) -> dict[str, Any]:
        code = error.code if isinstance(error, PlcRequestConflict) else (
            "control_disabled" if isinstance(error, PlcControlDisabledError) else "cabinet_unconfirmed")
        return {"ok": False, "code": code, "detail": str(error)}

    def _answer(self, connection, method, params, slot) -> None:
        try:
            with connection:
                try:
                    response = {"ok": True, "result": self._dispatch(method, params)}
                except Exception as exc:
                    response = self._error(exc)
                with suppress(OSError, ValueError):
                    _send(connection, response, _REPLY_BYTES)
        finally:
            slot.release()

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "snapshot":
            return self.service.snapshot(**params)
        if method == "shutdown-status":
            return self._shutdown_status()
        if method == "close":
            return self.close_cabinet(site_isolated=params.get("site_isolated", False))
        operation = params.get("operation")
        if operation not in _COMMANDS:
            raise ValueError("Invalid PLC operation")
        safety = operation in _SAFETY_COMMANDS and params.get("client_id") and params.get("request_id")
        with ExitStack() as safety_context:
            with self._gate:
                recover = (operation == "reset-fault" and not self.orphaned.is_set()
                           and not self._preparing
                           and self.service.shutdown_status["state"] == "unconfirmed")
                if (self.closed or self._site_isolating
                        or ((self.closing or self.orphaned.is_set()) and not (safety or recover))):
                    raise PlcRequestConflict("cabinet_closing", "PLC 柜正在停机，已禁止新的控制操作")
                if safety:
                    safety_context.enter_context(self.service.shutdown_safety_request())
                    self._active_safety += 1
                else:
                    self._active_normal += 1
            try:
                return self.service.coordination.execute(**params)
            finally:
                safety_context.close()
                with self._gate:
                    if safety:
                        self._active_safety -= 1
                    else:
                        self._active_normal -= 1
                    self._gate.notify_all()

    def _shutdown_status(self) -> dict[str, Any]:
        return {**self.service.shutdown_status, "ready": self.closed}

    def close_cabinet(self, *, site_isolated: bool = False) -> dict[str, Any]:
        with self._close_lock:
            if self.closed:
                return self._shutdown_status()
            with self._gate:
                self.closing = True
                self._preparing = True
                self._site_isolating = site_isolated
            try:
                with self._gate:
                    if not self._gate.wait_for(
                        lambda: not self._active_normal and (not site_isolated or not self._active_safety),
                        timeout=10.0,
                    ):
                        self.service.shutdown_status = {"state": "unconfirmed", "detail": "PLC command still active"}
                        raise RuntimeError("PLC command still active")
                if site_isolated:
                    self.service.close(site_isolated=True)
                else:
                    self.service.close()
                with self._gate:
                    self.closed = True
                return self._shutdown_status()
            finally:
                with self._gate:
                    self._site_isolating = False
                    self._preparing = False

    def owner_lost(self) -> None:
        with self._gate:
            if self.orphaned.is_set():
                return
            self.orphaned.set()
        try:
            self.close_cabinet()
        except Exception:
            # Keep the process, readbacks, and device lock available while
            # power is uncertain. Do not pretend that process exit is safe.
            _LOG.exception("Cabinet owner disappeared and PLC shutdown is unconfirmed")
        else:
            self.exiting.set()


def _worker(owner_fd: int) -> None:
    with socket.socket(fileno=owner_fd) as owner:
        try:
            initial = _receive(owner, _REQUEST_BYTES)
            config_data = initial["config"]
            config = PlcCabinetConfig(**{
                **config_data, "timings": PlcTimingConfig(**config_data["timings"]),
                "setpoints": PlcSetpointConfig(**config_data["setpoints"]),
            })
            root = Path(initial["data_root"])
            with _cabinet_lock(config, root):
                service = PlcCabinetService(config, data_root=root)
                service.start()
                with _CabinetServer(initial["path"], service, initial["token"], initial["lifecycle_token"]) as server:
                    os.chmod(initial["path"], 0o600)
                    _send(owner, {"ok": True}, _REPLY_BYTES)

                    def watch_owner() -> None:
                        with suppress(OSError):
                            owner.recv(1)
                        server.owner_lost()

                    def stop(_signum, _frame) -> None:
                        threading.Thread(target=server.owner_lost, daemon=True).start()

                    signal.signal(signal.SIGTERM, stop)
                    signal.signal(signal.SIGINT, stop)
                    threading.Thread(target=watch_owner, daemon=True).start()
                    while not server.exiting.is_set():
                        server.handle_request()
                socket_path = Path(initial["path"])
                with suppress(FileNotFoundError):
                    socket_path.unlink()
                with suppress(FileNotFoundError):
                    socket_path.parent.rmdir()
        except (OSError, RuntimeError, ValueError) as exc:
            with suppress(OSError, ValueError):
                _send(owner, {"ok": False, "detail": str(exc)}, _REPLY_BYTES)
            raise


if __name__ == "__main__":
    _worker(int(sys.argv[1]))
