"""Session-oriented facade used by the network control gateway."""

from __future__ import annotations

import os
import json
import math
import re
import shutil
import threading
import uuid
import time
import inspect
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from factory_hmi.models import StationConfig, StationState

from ._legacy import motor_default_positions
from .backend import FakeMotorBackend, MotorsPyBackend
from .can_manager import (
    ALLOWED_ARBITRATION_BITRATES,
    ALLOWED_DATA_BITRATES,
    ARBITRATION_BITRATE,
    DATA_BITRATE,
    CanConnectOutcomeUnknown,
    CanInterfaceStatus,
    CanManager,
    HelperExecutionError,
)
from .config import load_station_config, require_supported_limb
from .controller import FactoryController
from .command_receipts import StartReceipts
from .motion_completion import automatic_completion_parameters
from .identity import build_execution_code, build_local_execution_code
from .report import ReportManager, ReportResult
from .zeroing import MotorZeroingSession
from factory_hmi.sync.outbox import Outbox


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _open_service_operation(method):
    """Serialize resource creation with close, never heartbeat/snapshot reads."""
    @wraps(method)
    def operation(self, *args, **kwargs):
        with self._lifecycle_lock:
            self._require_open()
            if any(t.active for t in self.maintenance_tasks()):
                raise RuntimeError("模块固件或参数维护占用当前实例；先取消并等待任务退出")
            return method(self, *args, **kwargs)
    return operation


class FactoryService:
    """Own at most one configured station and expose JSON-friendly operations."""

    def __init__(
        self,
        *,
        data_root: Path | str | None = None,
        report_root: Path | str | None = None,
        can_manager: CanManager | None = None,
        outbox: Outbox | None = None,
    ) -> None:
        root = data_root or os.environ.get(
            "RP1_FACTORY_DATA_ROOT",
            "~/.local/share/rp1-factory-hmi",
        )
        self.data_root = Path(root).expanduser().resolve()
        self.record_root = self.data_root / "records"
        self.trajectory_root = self.data_root / "trajectories"
        self.record_root.mkdir(parents=True, exist_ok=True)
        self.trajectory_root.mkdir(parents=True, exist_ok=True)
        self.report_manager = ReportManager(report_root or self.data_root)
        self.outbox = outbox or Outbox(self.data_root / "sync" / "outbox.sqlite3")
        from .recording.policy import CapacityReservations
        self.capacity_reservations = CapacityReservations(self.report_manager.data_root / "record-capacity.sqlite3")
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._closing = threading.Event()
        self._closed = False
        self._maintenance = None
        self._parameters = None
        self._controller: FactoryController | None = None
        self._zeroing: MotorZeroingSession | None = None
        self._backend_name: str | None = None
        self._config_path: Path | None = None
        self._last_stop_reason: str | None = None
        self._can_manager = can_manager or CanManager()
        self._can_interfaces: tuple[str, ...] = ()
        self._display_can: tuple[str, float, dict[str, Any]] | None = None
        self._display_can_probe: tuple[str, float] | None = None
        self._can_connecting = False
        self._can_bindings: tuple[dict[str, Any], ...] = ()
        self._fake_can_connected = False
        self._report_threads: set[threading.Thread] = set()
        self._queued_report_ids: set[str] = set()
        self._report_errors: dict[str, str] = {}
        self._recording_session: Any = None
        self._record_identity: dict[str, str] = {}
        self._config_revision = uuid.uuid4().hex
        self._start_receipts = StartReceipts(self.data_root / "start-receipts.sqlite3")
        self._record_sidecar: Path | None = None
        self._live_report_context: dict[str, Any] | None = None
        from .report_jobs import resume_report_jobs
        resume_report_jobs(self.report_manager.data_root)
        # Empty stations have nothing to recover. In particular, do not create
        # an evidence lock/SQLite connection/empty ledger after __init__ returns.
        # The first explicit history query still creates the empty ledger.
        if (any(self.record_root.glob("*.checkpoint.json")) or any(self.record_root.glob("*.csv"))
                or any(self.report_manager.executions_root.glob("*/manifest.json"))):
            self._start_report_worker(self._recover_existing_records, "factory-report-recovery")

    def _require_open(self) -> None:
        if self._closing.is_set():
            raise RuntimeError("station service is closing or closed")

    def _require_recording_closed(self) -> None:
        session = self._recording_session
        if session is not None and session.recording_close_pending():
            raise RuntimeError("记录文件仍在收尾或写线程尚未退出；保留当前记录与占用，请等待或检查记录错误")

    def _start_report_worker(self, target, name: str, args=()) -> None:
        with self._lock:
            self._require_open()
            self._report_threads = {t for t in self._report_threads if t.is_alive()}
            worker = threading.Thread(target=target, name=name, args=args, daemon=True)
            self._report_threads.add(worker)
            # Start under the registry lock: close must never join an unstarted
            # thread. Keep finished handles until a join/reap confirms exit.
            worker.start()

    def _wait_for_report_staging(self, timeout: float) -> None:
        """Wait for CSV sealing and handoff, including startup recovery."""
        if not np.isfinite(timeout) or timeout < 0:
            raise ValueError("report timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                workers = list(self._report_threads)
            if not workers:
                return
            for worker in workers:
                if worker is threading.current_thread():
                    raise RuntimeError("report worker cannot wait for itself")
                worker.join(max(0.0, deadline - time.monotonic()))
            with self._lock:
                self._report_threads.difference_update(t for t in workers if not t.is_alive())
                pending = [t.name for t in self._report_threads if t.is_alive()]
            if not pending:
                return
            if time.monotonic() >= deadline:
                message = "report shutdown timed out; still running: " + ", ".join(pending)
                with self._lock:
                    self._report_errors["shutdown"] = message
                raise TimeoutError(message)

    def _reap_report_receipts(self) -> None:
        from .report_jobs import report_job_status

        with self._lock:
            pending = tuple(self._queued_report_ids)
        for key in pending:
            receipt = report_job_status(self.report_manager.data_root, key)
            if receipt["state"] not in {"completed", "failed", "cancelled"}:
                continue
            with self._lock:
                self._queued_report_ids.discard(key)
                if receipt["state"] == "completed":
                    self._report_errors.pop(key, None)
                else:
                    self._report_errors[key] = receipt["error"] or f"report job {receipt['state']}"

    def wait_for_reports(self, timeout: float = 5.0) -> None:
        """Explicitly wait for detached reports as well as local staging."""
        deadline = time.monotonic() + timeout
        self._wait_for_report_staging(timeout)
        while True:
            self._reap_report_receipts()
            with self._lock:
                if not self._queued_report_ids:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("detached reports still running; source evidence retained")
            time.sleep(min(.05, remaining))

    def close(self, timeout: float = 5.0, *, disconnect_can: bool = False) -> dict[str, Any]:
        """Normally disconnect, then finish local CSV handoff before return.

        Timeout bounds the report drain, not an unresponsive native disconnect.
        A failed close remains closing and can be retried; it never authorizes
        deleting state or forcibly terminating a device/worker.
        """
        with self._lifecycle_lock:
            if self._closed:
                return {"closed": True, "report_errors": dict(self._report_errors)}
            status = self.protective_damping_snapshot()
            if status and status.get("active") is True:
                raise RuntimeError("保护阻尼仍在运行；先可靠支撑机构并显式失能，再关闭服务")
            with self._lock:
                self._closing.set()
                controller = self._controller
            for task in self.maintenance_tasks():
                task.cancel(timeout)
            disconnect_error = None
            if controller is not None:
                try:
                    shutdown_disconnect = getattr(controller, "disconnect_for_shutdown", controller.disconnect)
                    shutdown_disconnect()
                except Exception as exc:
                    disconnect_error = exc
                    with self._lock:
                        self._report_errors["shutdown-disconnect"] = str(exc)
            self._wait_for_report_staging(timeout)
            self._require_recording_closed()
            if disconnect_error is not None:
                raise RuntimeError("station disconnect failed during close") from disconnect_error
            if disconnect_can:
                with self._lock:
                    if self._can_interfaces:
                        self._disconnect_can_locked("工位安全退出")
            self._zeroing = None
            self._closed = True
            return {"closed": True, "report_errors": dict(self._report_errors)}

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    @property
    def controller(self) -> FactoryController:
        with self._lock:
            if self._controller is None:
                raise RuntimeError("station is not configured")
            return self._controller

    @_open_service_operation
    def configure(
        self,
        *,
        config_path: str,
        limb: str,
        backend: str,
        bus_bindings: Sequence[Mapping[str, Any]] | None = None,
        motor_overrides: Sequence[Mapping[str, Any]] | None = None,
        station_config: StationConfig | None = None,
    ) -> dict[str, Any]:
        self._require_recording_closed()
        self._require_protective_damping_released()
        backend = backend.strip().lower()
        if backend not in {"motors_py", "fake"}:
            raise ValueError("backend must be motors_py or fake")
        path = Path(config_path).expanduser().resolve(strict=True)
        with self._lock:
            if self._controller is not None:
                state = self._controller.state
                if state is not StationState.DISCONNECTED:
                    raise RuntimeError(
                        f"disconnect the current station before configuring; state={state.value}"
                    )
                if self.can_status()["all_up"]:
                    raise RuntimeError(
                        "disconnect CAN interfaces before configuring another station"
                    )
            if station_config is None:
                station_config = load_station_config(
                    limb,
                    path,
                    bus_bindings=bus_bindings,
                    motor_overrides=motor_overrides,
                )
            elif not isinstance(station_config, StationConfig) or station_config.limb != require_supported_limb(limb):
                raise ValueError("station configuration does not match the requested module")
            can_interfaces = tuple(
                sorted(
                    {
                        entry.interface
                        for entry in station_config.entries
                        if "can" in entry.interface_type.lower()
                    }
                )
            )
            if not can_interfaces:
                raise ValueError("station configuration has no SocketCAN interfaces")
            if bus_bindings is None:
                normalized_bindings = tuple(
                    {
                        "index": index,
                        "interface": interface,
                        "mode": "canfd",
                        "bitrate": ARBITRATION_BITRATE,
                        "dbitrate": DATA_BITRATE,
                    }
                    for index, interface in enumerate(can_interfaces)
                )
            else:
                normalized_bindings = tuple(dict(item) for item in bus_bindings)
                timing_by_interface: dict[str, tuple[object, object, object]] = {}
                for item in normalized_bindings:
                    interface = str(item.get("interface", ""))
                    timing = (
                        item.get("mode", "canfd"),
                        item.get("bitrate", ARBITRATION_BITRATE),
                        item.get("dbitrate", DATA_BITRATE),
                    )
                    previous = timing_by_interface.setdefault(interface, timing)
                    if previous != timing:
                        raise ValueError(
                            f"reused CAN interface {interface} must use identical timing"
                        )
            if backend == "fake":
                initial = motor_default_positions(
                    station_config.raw,
                    station_config.limb,
                )
                motor_backend = FakeMotorBackend(
                    station_config.entries,
                    initial_positions=initial,
                )
            else:
                motor_backend = MotorsPyBackend(station_config.entries)
            self._controller = FactoryController(
                station_config,
                motor_backend,
                enable_recording=False,
            )
            self._zeroing = None
            self._backend_name = backend
            self._config_path = path
            self._config_revision = uuid.uuid4().hex
            self._last_stop_reason = None
            self._can_interfaces = can_interfaces
            self._can_bindings = normalized_bindings
            self._fake_can_connected = False
            return self.snapshot()

    @_open_service_operation
    def clear_configuration(self) -> dict[str, Any]:
        """Safely stop hardware and remove the active station configuration."""

        self._require_protective_damping_released()
        with self._lock:
            if self._controller is not None:
                self._controller.disconnect()
            if self._can_interfaces:
                self._disconnect_can_locked("清除工位配置")
            self._controller = None
            self._zeroing = None
            self._backend_name = None
            self._config_path = None
            self._last_stop_reason = None
            self._can_interfaces = ()
            self._can_bindings = ()
            self._fake_can_connected = False
            return self.snapshot()

    def _can_payload(
        self,
        statuses: list[CanInterfaceStatus],
    ) -> dict[str, Any]:
        interfaces = [status.to_dict() for status in statuses]
        expected = {
            str(item["interface"]): item
            for item in self._can_bindings
            if item.get("interface")
        }

        all_up = bool(expected) and set(expected) == {status.interface for status in statuses} and all(
            self._can_link_matches(status, expected[status.interface]) for status in statuses
        )
        return {
            "connected": all_up,
            "all_up": all_up,
            "bitrate": (
                self._can_bindings[0].get("bitrate")
                if len(self._can_bindings) == 1
                else None
            ),
            "dbitrate": (
                self._can_bindings[0].get("dbitrate")
                if len(self._can_bindings) == 1
                else None
            ),
            "fd": (
                self._can_bindings[0].get("mode", "canfd") == "canfd"
                if len(self._can_bindings) == 1
                else None
            ),
            "bindings": [dict(item) for item in self._can_bindings],
            "interfaces": interfaces,
        }

    @staticmethod
    def _can_link_matches(status: CanInterfaceStatus, binding: Mapping[str, Any]) -> bool:
        fd = str(binding.get("mode", "canfd")).lower() == "canfd"
        return (
            status.exists and status.up and status.fd is fd
            and status.bitrate == int(binding.get("bitrate", ARBITRATION_BITRATE))
            and (not fd or status.dbitrate == int(binding.get("dbitrate", DATA_BITRATE)))
            and status.bus_state not in {"bus-off", "stopped"}
        )

    def _fake_can_statuses(self) -> list[CanInterfaceStatus]:
        connected = self._fake_can_connected
        return [
            CanInterfaceStatus(
                interface=str(binding["interface"]),
                exists=True,
                up=connected,
                operstate="up" if connected else "down",
                bitrate=int(binding["bitrate"]) if connected else None,
                dbitrate=(
                    int(binding["dbitrate"])
                    if connected
                    and binding.get("mode", "canfd") == "canfd"
                    and binding.get("dbitrate") is not None
                    else None
                ),
                fd=(
                    binding.get("mode", "canfd") == "canfd"
                    if connected
                    else None
                ),
                bus_state="error-active" if connected else "stopped",
                tx_errors=0,
                rx_errors=0,
                evidence=("fake: virtual SocketCAN state",),
            )
            for binding in self._can_bindings
        ]

    def can_status(self) -> dict[str, Any]:
        with self._lock:
            if not self._can_interfaces:
                return self._can_payload([])
            if self._backend_name == "fake":
                return self._can_payload(self._fake_can_statuses())
            if self._can_connecting:
                return {**self._can_payload([]), "stale": True, "connecting": True}
            revision, interfaces = self._config_revision, self._can_interfaces
            started = time.monotonic()
        statuses = self._can_manager.status(interfaces)
        with self._lock:
            if revision != self._config_revision or self._can_connecting:
                return {**self._can_payload([]), "stale": True, "connecting": self._can_connecting}
            payload = self._can_payload(statuses)
            if self._display_can is None or self._display_can[1] <= started:
                self._display_can = revision, started, payload
            return payload

    def can_interfaces(self) -> dict[str, Any]:
        """Discover policy-approved CAN links independently of station YAML."""

        allowed_interfaces = tuple(f"can{index}" for index in range(5))
        policy_payload: dict[str, Any] = {
            "allowed_interfaces": list(allowed_interfaces),
            "allowed_bitrates": sorted(ALLOWED_ARBITRATION_BITRATES),
            "allowed_dbitrates": sorted(ALLOWED_DATA_BITRATES),
            "allow_classic_can": True,
            "allow_can_fd": True,
        }
        try:
            from factory_hmi.can_helper import load_policy

            policy = load_policy()
            allowed_interfaces = tuple(sorted(policy.allowed_interfaces))
            policy_payload = {
                "allowed_interfaces": list(allowed_interfaces),
                "allowed_bitrates": sorted(policy.allowed_bitrates),
                "allowed_dbitrates": sorted(policy.allowed_dbitrates),
                "allow_classic_can": policy.allow_classic_can,
                "allow_can_fd": policy.allow_can_fd,
            }
        except Exception:
            # Source-tree and fake-backend development may not have an
            # installed root-owned policy; the helper still enforces it in
            # production before performing any privileged operation.
            pass

        try:
            existing = set(self._can_manager.list_interfaces())
            names = tuple(sorted(existing | set(allowed_interfaces)))
            statuses = self._can_manager.status(names)
        except Exception:
            names = allowed_interfaces
            statuses = [
                CanInterfaceStatus(
                    interface=name,
                    exists=False,
                    up=False,
                    operstate=None,
                    bitrate=None,
                    dbitrate=None,
                    fd=None,
                    bus_state=None,
                    tx_errors=None,
                    rx_errors=None,
                    evidence=("netlink enumeration unavailable",),
                )
                for name in names
            ]
        return {
            "items": [status.to_dict() for status in statuses],
            "policy": policy_payload,
        }

    @_open_service_operation
    def can_connect(
        self,
        *,
        bus_bindings: Sequence[Mapping[str, Any]] | None = None,
        expected_config_revision: str | None = None,
        command_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._can_interfaces:
                raise RuntimeError("configure a station before connecting CAN")
            revision = self._config_revision
            if expected_config_revision is not None and revision != expected_config_revision:
                raise ValueError("配置已变化，请刷新后重新连接 CAN")
            if bus_bindings is not None:
                requested = tuple(dict(item) for item in bus_bindings)
                if requested != self._can_bindings:
                    raise ValueError(
                        "CAN bindings changed after configuration; apply configuration again"
                    )
            interfaces = self._can_interfaces
            bindings = {str(item["interface"]): dict(item) for item in self._can_bindings}
            controller = self._controller
            fake = self._backend_name == "fake"
            self._can_connecting = True
            self._display_can = None
        changed: list[str] = []
        reused: list[str] = []
        try:
            # The lifecycle lock still excludes configuration, other device
            # commands and close. No display lock is held through device I/O.
            statuses = []
            if not fake:
                if os.environ.get("RP1_FACTORY_CAN_DISABLED") == "1":
                    raise HelperExecutionError("CAN 配置未启用：当前包不配置物理 CAN")
                before = {item.interface: item for item in self._can_manager.status(interfaces)}
                for interface in interfaces:
                    binding = bindings[interface]
                    timing = {"bitrate": int(binding["bitrate"]), "dbitrate": binding.get("dbitrate"),
                              "fd": binding.get("mode", "canfd") == "canfd"}
                    previous = before.get(interface)
                    if (previous is not None and previous.bus_state == "error-active"
                            and self._can_link_matches(previous, binding)):
                        try:
                            can_reuse = self._can_manager.authorize_reuse(interface, **timing)
                        except PermissionError as exc:
                            raise ValueError(str(exc)) from exc
                        if can_reuse:
                            reused.append(interface)
                            statuses.append(previous)
                            continue
                    if controller is not None and controller.backend.connected:
                        raise RuntimeError("请先断开电机，再重新配置 CAN 接口")
                    verified = self._can_manager.connect([interface], **timing)
                    changed.append(interface)
                    if (len(verified) != 1 or verified[0].interface != interface
                            or not self._can_link_matches(verified[0], binding)):
                        raise RuntimeError(f"{interface} 启用后未确认接口健康或配置时序不匹配")
                    statuses.extend(verified)
            with self._lock:
                if self._config_revision != revision:
                    raise RuntimeError("连接期间配置已变化，不能确认旧配置的连接")
                if fake:
                    self._fake_can_connected = True
                    statuses = self._fake_can_statuses()
                can = self._can_payload(statuses)
                if not can["all_up"]:
                    raise RuntimeError("未确认所有配置的 CAN 接口均已就绪")
                sampled_at = time.monotonic()
                self._display_can = revision, sampled_at, can
                self._display_can_probe = revision, sampled_at
                discovered = bool(controller and controller.backend.connected)
                enabled = bool(controller and controller.backend.motors_enabled)
                state = controller.state.value if controller is not None else "disconnected"
                return {"can": {**can, "sampled_monotonic_s": sampled_at, "stale": False},
                        "configured": True, "connected": discovered, "state": state,
                        "config_revision": revision, "reused_interfaces": reused,
                        "station": {"limb": controller.config.limb if controller is not None else "",
                                    "state": state, "can_connected": True, "motors_discovered": discovered,
                                    "motors_enabled": enabled, "connection_phase": (
                            "motors_enabled" if enabled else "motors_discovered" if discovered else "can_connected")}}
        except Exception as exc:
            if changed:
                try:
                    self._can_manager.disconnect(changed)
                except Exception as rollback:
                    raise CanConnectOutcomeUnknown(f"{exc}；CAN 回退未确认：{rollback}") from exc
            raise
        finally:
            with self._lock:
                self._can_connecting = False

    def _disconnect_can_locked(self, reason: str) -> None:
        if self._controller is not None and self._controller.backend.connected:
            raise RuntimeError("disconnect motors before disconnecting CAN")
        if self._backend_name == "fake":
            self._fake_can_connected = False
        elif self._can_interfaces:
            self._can_manager.disconnect(self._can_interfaces)
            statuses = self._can_manager.status(self._can_interfaces)
            if ({row.interface for row in statuses} != set(self._can_interfaces)
                    or any(row.up for row in statuses)):
                raise RuntimeError("CAN关闭状态未确认；保留Pn通道隔离")
        from .motor_parameter_transport import archive_pn_quarantine
        archive_pn_quarantine(
            self.report_manager.data_root / "pn-quarantine", self._can_interfaces,
            reason=reason + "，接口关闭已确认；重新接入仍需Pn静默窗口。"
                            "仅重新建立主机时序边界，不代表检测到电机重新上电。",
        )

    def can_disconnect(self, *, wait_for_recording: bool = False) -> dict[str, Any]:
        # Use the same boundary as maintenance start/close, so a fresh task
        # cannot start between cancellation and the explicit interface down.
        with self._lifecycle_lock:
            self._require_open()
            session = self._recording_session
            if wait_for_recording and session is not None and session.recording_close_pending():
                session.wait_recording(timeout=2.0)
            self._require_recording_closed()
            for task in self.maintenance_tasks():
                task.cancel()
            with self._lock:
                self._disconnect_can_locked("用户显式断开CAN")
                return self.snapshot()

    @_open_service_operation
    def can_recover(self) -> dict[str, Any]:
        with self._lock:
            if self._controller is not None and self._controller.backend.connected:
                raise RuntimeError("disconnect motors before recovering CAN")
            if self._backend_name == "fake":
                self._fake_can_connected = True
            else:
                for binding in self._can_bindings:
                    self._can_manager.recover(
                        [str(binding["interface"])],
                        bitrate=int(binding["bitrate"]),
                        dbitrate=(
                            int(binding["dbitrate"])
                            if binding.get("dbitrate") is not None
                            else None
                        ),
                        fd=binding.get("mode", "canfd") == "canfd",
                    )
                statuses = self._can_manager.status(self._can_interfaces)
                if not self._can_payload(statuses)["all_up"]:
                    raise RuntimeError("CAN recovery did not restore healthy interfaces")
            return self.snapshot()

    @_open_service_operation
    def connect(self) -> dict[str, Any]:
        """Backward-compatible discovery plus enable operation."""

        if not self.can_status()["all_up"]:
            self.can_connect()
        self.discover_motors()
        return self.enable_motors(physical_estop_confirmed=True)

    @_open_service_operation
    def discover_motors(self) -> dict[str, Any]:
        if not self.can_status()["all_up"]:
            raise RuntimeError("connect and validate all CAN interfaces first")
        self.controller.discover_motors()
        return self.snapshot()

    @_open_service_operation
    def enable_motors(
        self,
        *,
        physical_estop_confirmed: bool,
    ) -> dict[str, Any]:
        if not physical_estop_confirmed:
            raise RuntimeError(
                "physical emergency-stop and safety circuit confirmation is required"
            )
        controller = self.controller
        was_enabled = controller.backend.motors_enabled
        try:
            controller.enable_motors()
        except Exception:
            # A partially failed all-motor enable leaves the transport open and
            # makes subsequent discover/configure actions report confusing
            # state conflicts. Close the motor transport deterministically;
            # SocketCAN itself stays up so the operator can correct the hardware
            # fault and scan again. A duplicate enable request must not tear
            # down an already healthy, position-holding station.
            if not was_enabled:
                try:
                    controller.disconnect()
                except Exception:
                    pass
            raise
        return self.snapshot()

    def protective_damping_snapshot(self):
        controller = self._controller
        session = getattr(controller, "_protective_damping", None) if controller is not None else None
        return session.snapshot() if session is not None else None

    def _require_protective_damping_released(self):
        status = self.protective_damping_snapshot()
        if status and status.get('active') is True:
            raise RuntimeError('保护阻尼中；先可靠支撑机构并显式失能，再修改配置')

    def enter_protective_damping(self, reason: str):
        controller = self._controller
        if controller is None:return None
        return controller.enter_protective_damping(reason, kd=1.0)

    def disable_motors(self) -> dict[str, Any]:
        if any(t.active for t in self.maintenance_tasks()):
            for task in self.maintenance_tasks():
                task.cancel()
            return self.snapshot()
        controller = self.controller
        controller.disable_motors()
        return self.snapshot()

    def disconnect(self) -> dict[str, Any]:
        for task in self.maintenance_tasks():
            task.cancel()
        controller = self.controller
        controller.disconnect()
        with self._lock:
            self._zeroing = None
        return self.snapshot()

    @_open_service_operation
    def import_trajectory(self, path: str) -> dict[str, Any]:
        self.controller.state_machine.require({StationState.DISCONNECTED, StationState.CONNECTED, StationState.COMPLETED})
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("trajectory path must be a file")
        safe_name = _SAFE_NAME.sub("_", source.name).strip("._")
        if not safe_name:
            raise ValueError("trajectory filename is invalid")
        destination = (self.trajectory_root / safe_name).resolve()
        if destination.parent != self.trajectory_root.resolve():
            raise ValueError("trajectory destination escapes the data directory")
        if source != destination:
            shutil.copy2(source, destination)
        trajectory = self.controller.load_trajectory(destination)
        self._config_revision = uuid.uuid4().hex
        return trajectory.to_dict()

    load_trajectory = import_trajectory

    @_open_service_operation
    def arm(self) -> dict[str, Any]:
        self.controller.arm()
        return self.snapshot()

    def start_receipt(self, command_id: str) -> dict[str, Any]:
        return self._start_receipts.get(command_id)

    @_open_service_operation
    def start(self, *, command_id: str | None = None, expected_state: str | None = None,
              expected_config_revision: str | None = None, **kwargs) -> dict[str, Any]:
        # Normalize defaults so omitted/default-valued fields describe the same command.
        bound = inspect.signature(self._start_once).bind(**kwargs)
        bound.apply_defaults()
        payload = {**bound.arguments, "expected_state": expected_state,
                   "expected_config_revision": expected_config_revision}
        key = command_id or uuid.uuid4().hex  # legacy caller: one distinct command
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", key):
            raise ValueError("command_id must contain 8-128 safe characters")
        digest = self._start_receipts.digest(payload)
        receipt = self._start_receipts.get(key)
        if receipt["status"] != "not_found":
            if receipt["payload_hash"] != digest:
                raise ValueError("启动命令编号已用于不同载荷")
            if receipt["status"] == "accepted":
                return receipt["result"]
            raise RuntimeError(receipt.get("error") or "启动结果待确认；先查询回执，不可重发设备动作")
        self._start_receipts.begin(key, digest)
        previous_playback = self._controller.playback if self._controller else None
        try:
            if expected_config_revision is not None and expected_config_revision != self._config_revision:
                raise ValueError("配置或轨迹版本已变化，请刷新后重新预检")
            if expected_state is not None and self.controller.state.value != expected_state:
                raise ValueError("启动状态已变化，请刷新当前执行")
            self.controller.state_machine.require(StationState.ARMED)
            result = self._start_once(**bound.arguments)
        except Exception as exc:
            # A live action is never labelled rejected simply because response assembly failed.
            active = (self._controller and self._controller.playback is not previous_playback
                      and self._controller.playback is not None)
            self._start_receipts.finish(key, "unknown" if active else "rejected", error=str(exc))
            raise
        result["start_command"] = {"command_id": key, "status": "accepted"}
        self._start_receipts.finish(key, "accepted", result=result)
        return result

    def _start_once(
        self,
        *,
        speed: float = 1.0,
        loop: bool = True,
        cycles: int = 0,
        duration_hours: float | None = None,
        record: bool = True,
        recording_scope: str | None = None,
        data_domain: str | None = None,
        record_rate_hz: float = 20.0,
        test_id: str | None = None,
        robot_id: str | None = None,
        execution_uuid: str | None = None,
        campaign_id: str | None = None,
        cycle_id: str | None = None,
        segment_id: str | None = None,
        asset_id: str | None = None,
        configuration_id: str | None = None,
        test_case_version_id: str | None = None,
        test_case_id: str | None = None,
        test_case_version: str | None = None,
        test_name: str | None = None,
        bench_id: str | None = None,
        station_id: str | None = None,
        operator_id: str | None = None,
        stage_code: str | None = None,
        stage_name: str | None = None,
        subject_map: Mapping[str, str] | None = None,
        completion_mode: str = "configured",
        parking_pose_confirmed: bool = False,
    ) -> dict[str, Any]:
        self._require_recording_closed()
        controller = self.controller
        automatic_completion_parameters(controller.config)
        report_context: dict[str, Any] | None = None
        record_path: Path | None = None
        sidecar: Path | None = None
        effective_test_id = test_id
        execution_code = ""
        selected_case = str(test_case_id or "").strip().upper()
        from factory_hmi.data_domain import domain_metadata, bench_evidence
        if data_domain is not None and data_domain not in {"RESEARCH", "FACTORY"}:
            raise ValueError("data_domain must be RESEARCH or FACTORY")
        resolved_sample = str(robot_id or "").strip()
        resolved_bench = str(bench_id or os.environ.get("RP1_FACTORY_BENCH_ID", "")).strip()
        domain = domain_metadata({"data_domain": data_domain, "bench_id": resolved_bench, "robot_id": resolved_sample})
        if data_domain == "FACTORY" and (not resolved_sample or not resolved_bench):
            raise ValueError("工厂测试必须填写 SN 和实际台架")
        if data_domain == "FACTORY" and (len(resolved_sample) > 200 or not bench_evidence(resolved_bench)):
            raise ValueError("工厂 SN 最多200字，实际台架须为有效编号")
        if data_domain == "FACTORY" and not record:
            raise ValueError("工厂模式须启用记录，才能保存原始采样与质量摘要")
        if not selected_case:
            raise ValueError("测试用例编号必填，没有对应用例请选择 OTHER 并填写测试名称")
        if selected_case == "OTHER":
            if not str(test_name or "").strip():
                raise ValueError("其他用例必须填写临时测试名称")
            if not str(robot_id or "").strip() or not str(bench_id or "").strip():
                raise ValueError("其他用例必须填写实际测试对象和工位")
        if not record and (recording_scope == "platform" or any((campaign_id, cycle_id, segment_id, asset_id, configuration_id, test_case_version_id)) or selected_case != "OTHER"):
            raise ValueError("正式任务必须启用测量记录；无记录仅允许 OTHER 调试，不取得正式资格")
        requirements=(controller.config.raw.get("factory_hmi_acceptance") or {}).get("observation_requirements")
        if requirements:
            from .recording.policy import check_observation_requirements
            observation=check_observation_requirements(requirements,controller.telemetry_hub.capabilities())
            if not observation["admitted"]:raise ValueError("配方观测能力不足："+"；".join(observation["reasons"]))
        if record:
            reserve = int(os.environ.get("RP1_FACTORY_RECORD_RESERVE_BYTES", str(512 * 1024**2)))
            if reserve < 0:
                raise ValueError("record reserve bytes cannot be negative")
            if shutil.disk_usage(self.record_root).free < reserve:
                raise RuntimeError("recording disk free space below configured reserve")
            from .recording.policy import capacity_budget, freeze_policy
            from scripts.log_motor_telemetry import csv_fieldnames
            duration_s = (float(duration_hours)*3600 if duration_hours else
                controller.trajectory.duration_s / speed * (cycles or 1) if not loop or cycles else 7*24*3600)
            row_bound = max(128, len(csv_fieldnames(controller.config.entries))*26)
            capacity = capacity_budget(row_bytes=row_bound, rate_hz=record_rate_hz,
                duration_s=max(.05,duration_s), free_bytes=shutil.disk_usage(self.record_root).free,
                reserve_bytes=reserve, log_bytes=math.ceil(duration_s)*128)
            if not capacity["admitted"]:
                raise RuntimeError(f"记录容量不足：需要 {capacity['required_bytes']} 字节，可用 {capacity['free_bytes']} 字节；未删除已有记录")
            recording_policy = freeze_policy(purpose="production" if domain["data_domain"] == "FACTORY" else "development", recipe=selected_case)
            started_at = datetime.now(timezone.utc)
            resolved_test_case = test_case_id or controller.config.limb
            if domain["data_domain"] == "FACTORY" and not resolved_sample.strip():
                raise ValueError("工厂测试必须填写 SN")
            platform_context = {
                "campaign_id": campaign_id, "cycle_id": cycle_id,
                "segment_id": segment_id, "asset_id": asset_id,
                "configuration_id": configuration_id,
                "test_case_version_id": test_case_version_id,
            }
            has_platform_context = any(platform_context.values())
            scope = recording_scope or ("platform" if has_platform_context else "local")
            if scope not in {"local", "platform"}:
                raise ValueError("recording_scope must be local or platform")
            if data_domain == "FACTORY" and scope != "local":
                raise ValueError("工厂记录使用独立 SN 身份，不能绑定研发执行上下文")
            if scope == "local" and has_platform_context:
                raise ValueError("本地记录不能同时携带平台测试上下文；请明确选择记录用途。")
            execution_id = str(execution_uuid or uuid.uuid4())
            uuid.UUID(execution_id)
            if scope == "local":
                canonical_test_id = build_local_execution_code(
                    resolved_test_case, started_at, execution_id
                )
            else:
                resolved_test_case = str(test_case_id or "").strip().upper()
                resolved_sample = resolved_sample.strip().upper()
                resolved_bench = resolved_bench.strip().upper()
                platform_context["station_id"] = station_id or os.environ.get("RP1_FACTORY_STATION_ID", "")
                missing = [key for key, value in platform_context.items() if not value]
                if missing:
                    raise ValueError("平台记录上下文不完整，缺少：" + "、".join(missing))
                for key, value in platform_context.items():
                    try:
                        uuid.UUID(str(value))
                    except ValueError as exc:
                        raise ValueError(f"平台记录 {key} 必须是真实上下文的 UUID，不能使用显示编号。") from exc
                try:
                    build_execution_code(
                        resolved_test_case, started_at, resolved_sample, resolved_bench
                    )
                except ValueError as exc:
                    raise ValueError(f"平台记录编号不符合要求：{exc}。仅本地保存请取消选择平台档案。") from exc
                canonical_test_id = build_local_execution_code(
                    resolved_test_case, started_at, execution_id
                )
                execution_code = ""
            if test_id and test_id != canonical_test_id:
                raise ValueError(
                    "test_id does not match the canonical execution identity"
                )
            safe_test_id = _SAFE_NAME.sub("_", canonical_test_id).strip("._")
            effective_test_id = safe_test_id
            if not safe_test_id:
                raise ValueError("test_id does not contain a usable filename")
            record_path = self.record_root / f"{safe_test_id}.csv"
            if record_path.exists() or record_path.with_suffix(".context.json").exists():
                raise ValueError("记录编号已存在，不能覆盖之前的记录。")
            trajectory = controller.trajectory
            now = started_at.isoformat().replace("+00:00", "Z")
            run_request = {
                "duration_s": float(duration_hours) * 3600.0 if duration_hours is not None else None,
                "loop": loop,
                "cycles": cycles,
                "speed": speed,
                "completion_mode": completion_mode,
                "parking_pose_confirmed": parking_pose_confirmed,
            }
            if stage_code in {"QUICK_EXTERNAL", "QUICK_PLC"}:
                run_request["power_source"] = "external" if stage_code == "QUICK_EXTERNAL" else "plc"
            report_context = {
                "capacity_admission": capacity, "recording_policy": recording_policy,
                "execution_uuid": execution_id,
                "test_id": safe_test_id,
                "execution_code": execution_code,
                "recording_scope": scope,
                "origin": "OTHER" if selected_case == "OTHER" else "HMI",
                **domain,
                "campaign_id": campaign_id,
                "cycle_id": cycle_id,
                "segment_id": segment_id,
                "asset_id": asset_id,
                "configuration_id": configuration_id,
                "test_case_version_id": test_case_version_id,
                "test_case_id": resolved_test_case,
                "test_case_version": test_case_version or "",
                "test_name": str(test_name or resolved_test_case).strip(),
                "actual_object": resolved_sample,
                "sample_id": resolved_sample,
                "robot_id": resolved_sample,
                "bench_id": resolved_bench,
                "station_id": station_id
                or os.environ.get("RP1_FACTORY_STATION_ID", ""),
                "operator_id": operator_id or "",
                "stage_code": stage_code or "AGING",
                "stage_name": stage_name or "老化运行",
                "run_request": run_request,
                "subject_map": dict(subject_map or {}),
                "started_at": now,
                "record_rate_hz": float(record_rate_hz),
                "module_name": controller.config.limb,
                "limb": controller.config.limb,
                "entries": [
                    {
                        "motor_id": int(getattr(entry, "motor_id", index)),
                        "joint_name": str(
                            getattr(entry, "joint_name", f"joint_{index}")
                        ),
                        "module_name": controller.config.limb,
                        "motor_type": str(entry.motor_type),
                        "motor_model": int(entry.motor_model),
                    }
                    for index, entry in enumerate(controller.config.entries)
                ],
                "software": {
                    "hmi_version": os.environ.get(
                        "RP1_FACTORY_HMI_VERSION", "0.1.0"
                    )
                },
                "config": controller.config.raw,
                "acceptance": controller.config.raw.get(
                    "factory_hmi_acceptance", {}
                ),
                "trajectory": trajectory.to_dict() if trajectory else {},
            }
            if data_domain == "FACTORY":
                from .factory_quality import freeze_factory_quality
                report_context["factory_quality_policy"] = freeze_factory_quality(report_context)
            sidecar = record_path.with_suffix(".context.json")
            sidecar.write_text(
                json.dumps(
                    report_context,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        candidate_identity = {
            "test_id": effective_test_id or "",
            "execution_uuid": str((report_context or {}).get("execution_uuid") or ""),
            "execution_code": execution_code,
            "recording_scope": report_context.get("recording_scope", "") if report_context else "",
            **domain, "bench_id": resolved_bench, "operator_id": str(operator_id or ""),
            "campaign_id": str((report_context or {}).get("campaign_id") or ""),
            "cycle_id": str((report_context or {}).get("cycle_id") or ""),
            "asset_id": str((report_context or {}).get("asset_id") or ""),
            "configuration_id": str((report_context or {}).get("configuration_id") or ""),
            "test_case_version_id": str((report_context or {}).get("test_case_version_id") or ""),
            "test_case_id": str((report_context or {}).get("test_case_id") or ""),
            "station_id": str((report_context or {}).get("station_id") or ""),
        }
        if report_context is not None:
            try:
                self.capacity_reservations.reserve(report_context["execution_uuid"],capacity["required_bytes"],shutil.disk_usage(self.record_root).free)
            except Exception:
                if sidecar is not None: sidecar.unlink(missing_ok=True)
                raise
        previous_playback = controller.playback
        previous = (controller.enable_recording, controller.record_output, controller.completion_output)
        controller.enable_recording = record
        controller.record_output = record_path
        controller.completion_output = self.data_root / "motion-completion" / (str(uuid.uuid4()) + ".json")
        try:
            controller.start(
                loop=loop,
                cycles=cycles,
                duration_s=(
                    None
                    if duration_hours is None
                    else float(duration_hours) * 3600.0
                ),
                speed=speed,
                record_rate_hz=record_rate_hz,
                test_id=effective_test_id,
                robot_id=robot_id,
                completion_mode=completion_mode,
                parking_pose_confirmed=parking_pose_confirmed,
            )
        except Exception:
            controller.enable_recording, controller.record_output, controller.completion_output = previous
            if report_context is not None and controller.playback is previous_playback:
                self.capacity_reservations.complete(report_context["execution_uuid"])
            if sidecar is not None:
                sidecar.unlink(missing_ok=True)
            raise
        # Commit identity and download pointer only after playback accepted the run.
        self._record_identity = candidate_identity
        self._record_sidecar = sidecar if record else None
        controller.telemetry_hub.begin((report_context or {}).get("execution_uuid") or test_id or "local", self._config_revision)
        self._live_report_context = report_context
        self._recording_session = controller.playback if record else None
        if report_context is not None and record_path is not None:
            self._start_report_worker(self._finalize_after_run,
                f"factory-report-{report_context['test_id']}",
                (controller, record_path, report_context, controller.playback))
        return self.snapshot()

    def pause(self) -> dict[str, Any]:
        self.controller.pause()
        return self.snapshot()

    @_open_service_operation
    def resume(self) -> dict[str, Any]:
        self.controller.resume()
        return self.snapshot()

    def finish(self, reason: str | None = None, *, complete_cycle: bool = False) -> dict[str, Any]:
        self._last_stop_reason = reason
        self.controller.finish(reason=reason, complete_cycle=complete_cycle)
        return self.snapshot()

    def stop(self, *, reason: str | None = None) -> dict[str, Any]:
        self._last_stop_reason = reason
        self.controller.stop(reason=reason)
        return self.snapshot()

    def prepare_next_run(self) -> dict[str, Any]:
        self._require_recording_closed()
        self.controller.prepare_next_run()
        return self.snapshot()

    @_open_service_operation
    def manual_move(
        self,
        *,
        targets_deg: Sequence[float],
        speed_deg_s: float,
    ) -> dict[str, Any]:
        controller = self.controller
        if len(targets_deg) != len(controller.config.entries):
            raise ValueError(
                f"manual target requires {len(controller.config.entries)} joints, "
                f"got {len(targets_deg)}"
            )
        controller.manual_move(
            np.deg2rad(np.asarray(targets_deg, dtype=np.float64)),
            speed_rad_s=float(np.deg2rad(speed_deg_s)),
        )
        return self.snapshot()

    def manual_stop(self) -> dict[str, Any]:
        self.controller.manual_stop()
        return self.snapshot()

    def preview_record_identity(self, run_id: str) -> dict[str, str] | None:
        """Only the exact current execution can expose a durable-window identity."""
        with self._lock:
            identity = self._record_identity
            if identity.get("execution_uuid") != run_id or not identity.get("test_id"):
                return None
            return {"execution_uuid": run_id, "test_id": identity["test_id"]}

    def record_file(self) -> Path:
        # Download only from the committed recording session, never a start draft.
        output = self._recording_session.record_output if self._recording_session is not None else None
        if output is None:
            raise RuntimeError("no recorded CSV is available")
        resolved = Path(output).expanduser().resolve()
        root = self.record_root.resolve()
        if resolved.parent != root:
            raise RuntimeError("recorded CSV is outside the records directory")
        if not resolved.is_file():
            raise RuntimeError("recorded CSV has not been finalized")
        session = self._recording_session
        if session is not None and not session.snapshot()["recording"]["finished"]:
            raise RuntimeError("recorded CSV is not closed or recording failed")
        return resolved

    def execution_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.execution_history_page(limit=limit)["items"]

    def execution_history_page(self, limit: int | None = 100, *, offset: int = 0,
                               start_at: str = "", end_at: str = "", sn: str = "", data_domain: str = "", bench: str = "") -> dict[str, Any]:
        from factory_hmi.execution_history import history_bounds, select_history
        history_bounds(start_at, end_at)
        if not self.report_manager.history_index.initialized():
            raise RuntimeError("历史索引正在恢复，请稍后刷新；损坏时请显式重建索引")
        page = self.report_manager.history_index.page(limit=min(limit or 500, 500), offset=offset,
            start_at=start_at, end_at=end_at, sn=sn, data_domain=data_domain, bench=bench)
        self._annotate_execution_history(page["items"])
        return {**page, "ledger_path": str(self.report_manager.executions_root / "执行台账.csv"), "ledger_error": ""}

    def iter_execution_history(self, **filters):
        cursor = None
        while True:
            page = self.report_manager.history_index.page(limit=500, cursor=cursor, **filters)
            self._annotate_execution_history(page["items"])
            yield from page["items"]
            cursor = page["next_cursor"]
            if not cursor: break

    def _annotate_execution_history(self, items: list[dict[str, Any]]) -> None:
        by_test_id = self.outbox.for_test_ids([str(item["test_id"]) for item in items])
        for item in items:
            queued = by_test_id.get(str(item["test_id"]))
            item.update(
                {
                    "sync_state": queued.state if queued else "local_completed",
                    "sync_error": queued.last_error if queued else "",
                    "review_reason": queued.review_reason if queued else "",
                    "submission_id": queued.submission_id if queued else "",
                    "execution_code": (
                        queued.execution_code if queued and queued.execution_code
                        else item.get("execution_code", "")
                    ),
                    "execution_code_confirmed": bool(queued and queued.execution_code),
                }
            )

    def execution_location(self, test_id: str = "") -> dict[str, str]:
        directory = self.report_manager.execution_dir(test_id) if test_id else self.report_manager.executions_root
        return {"directory": str(directory), "executions_root": str(self.report_manager.executions_root),
                "ledger_path": str(self.report_manager.executions_root / "执行台账.csv"),
                "test_id": test_id}

    def execution_local_corrections(self, test_id: str) -> dict[str, Any]:
        return self.report_manager.local_corrections(test_id)

    def correct_execution_locally(self, test_id: str, changes: dict[str, str], *, reason: str,
                                  actor: str, expected_revision: int, request_id: str) -> dict[str, Any]:
        # One cross-window/process boundary protects the appended correction
        # and derived ledger. This path never calls submit/enqueue/wake/upload.
        with self.report_manager.evidence_lock():
            result = self.report_manager.local_correction_store(test_id).save(
                changes, reason=reason, actor=actor, expected_revision=expected_revision, request_id=request_id)
            self.report_manager.refresh_history_index(test_id)
            items = self.iter_execution_history()
            ledger_error = ''
            try:
                self.report_manager.write_ledger(items)
            except OSError as exc:
                ledger_error = str(exc)
        return {**result, 'ledger_error': ledger_error,
                'ledger_path': str(self.report_manager.executions_root / '执行台账.csv')}

    def execution_summary(self, test_id: str) -> dict[str, Any]:
        summary = self.report_manager.summary(test_id)
        queued = self.outbox.get_by_test_id(test_id)
        if queued and isinstance(queued.manifest.get("central_analysis"), Mapping):
            summary["central_analysis"] = dict(queued.manifest["central_analysis"])
        return summary

    def execution_analysis_versions(self, test_id: str) -> dict[str, Any]:
        revisions = self.report_manager.analysis_revisions(test_id)
        execution = self.report_manager.execution_dir(test_id)
        return {"test_id": test_id, "submission_baseline": "baseline-v1", "items": [
            {**revision, "report_text": (execution / "analysis/revisions" / f"v{revision['revision']:06d}" / "report.md").read_text(encoding="utf-8")}
            for revision in revisions]}

    def execution_report_file(self, test_id: str) -> Path:
        return self.report_manager.report_path(test_id)

    def execution_bundle_file(self, test_id: str) -> Path:
        return self.report_manager.bundle_path(test_id)

    def rebuild_report(self, test_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        result = self.report_manager.rebuild(test_id, actor=actor, reason=reason)
        return {**self._report_result(result), "analysis_revision": result.analysis_revision,
            "report_text": result.report_path.read_text(encoding="utf-8"), "submission_baseline": "baseline-v1"}

    def submit_execution(
        self,
        test_id: str,
        reason: str = "",
        verified_status: str = "",
        operator_summary: str = "",
        review: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        queued = self.outbox.get_by_test_id(test_id)
        if queued and queued.state in {"approved", "uploaded", "pending_review", "archived", "awaiting_classification", "processing_failed"}:
            return queued.to_dict()
        if not review:
            raise ValueError("必须核实测试人、有效工位、人工结论和测试员记录后才能提交")
        if queued and queued.submission_payload and queued.state not in {"rejected", "needs_verification"}:
            raise ValueError("已有冻结请求尚未完成回执核对。请点“重试归档”；旧摘要若已拒收，将先核对平台记录，再转为“需要重新核实”。")
        if queued and queued.submission_payload and queued.manifest.get("archive_protocol") != "reliability_v1_csv":
            if queued.state != "needs_verification" or not self.outbox.legacy_summary_recovery_verified(queued):
                raise ValueError("旧摘要必须先重试并核对平台回执，不能覆盖原请求")
        if queued and queued.submission_payload and queued.manifest.get("origin") == "HISTORY":
            previous = json.loads(queued.submission_payload)
            if previous.get("transport") != "history_import":
                raise ValueError("旧历史记录已有冻结归档请求，请先在平台核对回执；不能改写旧请求或把缺失运行事实重新送审")
        # Evidence validation precedes all review/version writes.
        self.report_manager.validate(test_id)
        central_analysis = queued.manifest.get("central_analysis") if queued else None
        review_path = self.report_manager.write_operator_review(test_id, review, central_analysis=central_analysis)
        verified = json.loads(review_path.read_text(encoding="utf-8"))
        manifest = dict(self.report_manager.manifest(test_id))
        raw_path = self.report_manager.execution_dir(test_id) / "telemetry/raw.csv"
        actual_object = verified.get("actual_object") or manifest.get("actual_object") or manifest.get("sample_id")
        test_name = verified.get("test_name") or manifest.get("test_name")
        if not actual_object or actual_object == "unknown" or not test_name:
            raise ValueError("请补齐测试名称和实际测试对象")
        manifest.update(archive_protocol="reliability_v1_csv", csv_path=str(raw_path),
            operator_review=verified, baseline_evidence=verified["baseline_evidence"], test_name=test_name, actual_object=actual_object,
            submission_reason=str(reason or verified["submission_reason"]).strip()[:300])
        if central_analysis:
            manifest["central_analysis"] = central_analysis
        item = self.outbox.enqueue(
            manifest,
            self.report_manager.bundle_path(test_id),
        )
        if queued and queued.submission_payload:
            frozen = json.loads(queued.submission_payload)
            if "create" in frozen and queued.manifest.get("archive_protocol") == "reliability_v1_csv":
                item = self.outbox.update(item.execution_uuid, submission_payload=json.dumps(
                    {**{key: frozen[key] for key in ("create", "transport", "archive_binding") if key in frozen}, "review": verified}, ensure_ascii=False, sort_keys=True))
        return item.to_dict()

    def retry_submission(self, test_id: str) -> dict[str, Any]:
        item = self.outbox.get_by_test_id(test_id)
        if item is None:
            raise ValueError("尚未核实提交，请先打开核实窗口")
        if item.state in {"rejected", "needs_verification"}:
            raise ValueError("驳回后必须重新核实并提交，不能直接重试旧结论")
        return self.outbox.retry(item.execution_uuid).to_dict()

    def import_history(self, paths: Sequence[str], metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Explicit local selection only; never uploads or starts hardware."""
        from factory_hmi.core.report import _sha256
        if not paths or len(paths) > 100:
            raise ValueError("请选择 1 至 100 个历史 CSV")
        results = []
        for value in paths:
            source = Path(value).expanduser()
            if source.is_symlink() or source.suffix.lower() != ".csv":
                raise ValueError("只能选择普通 CSV 文件")
            source = source.resolve(strict=True)
            digest = _sha256(source)
            sidecar = source.with_suffix(".context.json")
            context = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
            if not isinstance(context, dict):
                raise ValueError("历史上下文文件格式无效")
            declared_context = dict(context)
            context["history_declared_context"] = declared_context
            context["history_context_sha256"] = _sha256(sidecar) if sidecar.is_file() else None
            context.setdefault("actual_object", context.get("sample_id") or context.get("robot_id") or "")
            context.setdefault("test_name", context.get("test_case_id") or "")
            identity = str(context.get("execution_uuid") or uuid.uuid5(uuid.NAMESPACE_URL, "rp1-history-sha256:" + digest))
            uuid.UUID(identity)
            known = None
            for item in self.report_manager.history(limit=10000):
                existing_manifest = self.report_manager.manifest(item["test_id"])
                same_file = any(entry.get("path") == "telemetry/raw.csv" and entry.get("sha256") == digest for entry in existing_manifest.get("files", []))
                if item["execution_uuid"] == identity or same_file:
                    known = item
                    break
            if known:
                results.append({**known, "duplicate": True})
                continue
            for field in ("test_name", "actual_object", "bench_id", "operator_id"):
                explicit = str(metadata.get(field) or context.get(field) or "").strip()
                if not explicit:
                    raise ValueError("历史补录必须填写测试名称、实际对象、工位和测试人")
                context[field] = explicit
            # CSV coordinate span does not measure active runtime or completed cycles.
            # Missing acquisition facts stay unknown; imports use the historical intake.
            formal_keys = ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")
            formal_context = all(context.get(key) for key in formal_keys) and context.get("test_case_id") not in {None, "", "OTHER"}
            if formal_context:
                try:
                    for key in formal_keys:
                        uuid.UUID(str(context[key]))
                except ValueError:
                    formal_context = False
            context.update(execution_uuid=identity, test_id="HISTORY_" + identity.replace("-", ""),
                origin="HISTORY", recording_scope="platform" if formal_context else "local", test_case_id=context.get("test_case_id") or "OTHER",
                sample_id=context["actual_object"], machine_status="unknown",
                timestamp_provenance="missing",
                started_at="1970-01-01T00:00:00Z", ended_at="1970-01-01T00:00:00Z",
                active_seconds=None, completed_cycles=None, record_rate_hz=None, outcome="unknown")
            result = self.report_manager.finalize(source, context)
            results.append(self._report_result(result))
        self.execution_history_page(limit=1)
        return results

    def sync_status(self, test_id: str) -> dict[str, Any]:
        item = self.outbox.get_by_test_id(test_id)
        return (
            item.to_dict()
            if item is not None
            else {"test_id": test_id, "state": "local_completed"}
        )

    def recover_reports(self) -> list[dict[str, Any]]:
        results = [
            self._report_result(result)
            for result in self.report_manager.recover_orphans(self.record_root)
        ]
        self.execution_history_page(limit=1)
        return results

    def _recover_existing_records(self) -> None:
        try:
            if not self.report_manager.history_index.initialized():
                self.report_manager.rebuild_history_index()
            from .recording.segments import recover_checkpoint
            inspected=0
            for checkpoint in self.record_root.glob("*.checkpoint.json"):
                source=checkpoint.with_name(checkpoint.name.removesuffix(".checkpoint.json")+".csv")
                if source.with_suffix(".segments.json").exists():continue
                inspected+=1
                if inspected>100:
                    self._report_errors["partial-recovery-budget"]="未完成记录超过本次恢复预算，请分批检查；原始文件均保留"
                    break
                event=source.with_suffix(".recovery.json")
                if not event.exists():
                    recovered=recover_checkpoint(source)
                    from .report import _write_fsynced,_json_bytes
                    _write_fsynced(event,_json_bytes(recovered))
                self._report_errors[source.stem]="记录存在未封段尾部；已保留原件和确认持久前缀，需核实，未自动续测"
            from .report_jobs import recover_report_jobs
            for result in recover_report_jobs(self.report_manager.data_root):
                if result["state"] == "failed":
                    self._report_errors[result["id"]] = result["error"]
            self.recover_reports()
        except Exception as exc:
            with self._lock:
                self._report_errors["startup-recovery"] = str(exc)

    def _finalize_after_run(
        self,
        controller: FactoryController,
        record_path: Path,
        context: dict[str, Any],
        playback_session: Any = None,
    ) -> None:
        test_id = str(context["test_id"])
        try:
            # Hold the execution's playback identity across quick disconnects.
            # A new configuration or disconnect may clear controller.playback
            # before this background finalizer gets CPU time.
            if playback_session is not None:
                playback_session.wait()
                # Reuse this execution's existing waiter, not a shared queue:
                # another window's slow CSV/evidence must not block its close.
                finalize_recording = getattr(playback_session, "finalize_recording", None)
                if finalize_recording is not None:
                    finalize_recording()
                if not playback_session.wait_recording(timeout=7.0):
                    raise TimeoutError("记录收尾超时；原始证据与仍活动的写线程保留，未生成完成报告")
                playback = playback_session.snapshot()
                if playback.get("record_error"):
                    raise RuntimeError(playback["record_error"])
            else:
                controller.wait()
                playback = controller.snapshot().get("playback")
            progress = (
                playback.get("progress", {})
                if isinstance(playback, Mapping)
                else {}
            )
            completion = playback.get("completion") if isinstance(playback, Mapping) else None
            fault = playback.get("fault") if isinstance(playback, Mapping) else None
            outcome = "fault" if fault else str(progress.get("outcome") or "interrupted")
            if isinstance(completion, Mapping):
                fields = ("phase", "safe_disabled", "automatic_return_enabled", "source",
                          "parameter_revision", "preset_sha256", "return_seconds",
                          "settling_seconds", "approach_completed", "finish_cycle_requested",
                          "finish_cycle_completed", "alarm")
                context["motion_completion"] = {key: completion[key] for key in fields if key in completion}
            if isinstance(fault, Mapping):
                context["terminal_fault"] = dict(fault)
            if isinstance(playback, Mapping) and playback.get("test_started_at"):
                context["preparation"] = {"requested_at": context.get("started_at"),
                    "first_frame_approach": (completion or {}).get("approach"),
                    "approach_elapsed_s": (completion or {}).get("approach_elapsed_s", 0.)}
                context["started_at"] = playback["test_started_at"]
            context.update(
                {
                    "ended_at": (playback.get("ended_at") if isinstance(playback, Mapping) else None)
                    or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "active_seconds": float(progress.get("active_seconds") or 0),
                    "completed_cycles": int(
                        progress.get("completed_cycles") or 0
                    ),
                    "outcome": outcome,
                    "manual_stop": outcome == "stopped",
                    "safety_failure": bool(fault),
                    "stop_reason": (
                        str(fault.get("message") or "")
                        if isinstance(fault, Mapping)
                        else playback.get("stop_reason") or ""
                    ),
                }
            )
            from .report_jobs import enqueue_report_job
            job = enqueue_report_job(self.report_manager.data_root, record_path, context)
            with self._lock:
                self._queued_report_ids.add(job)
                self._report_errors.pop(test_id, None)
            self._reap_report_receipts()
        except Exception as exc:
            with self._lock:
                self._report_errors[test_id] = str(exc)

    @staticmethod
    def _report_result(result: ReportResult) -> dict[str, Any]:
        return {
            "test_id": result.manifest["test_id"],
            "execution_uuid": result.manifest["execution_uuid"],
            "bundle_path": str(result.bundle_path),
            "summary_path": str(result.summary_path),
            "report_path": str(result.report_path),
            "verdict": result.summary["verdict"]["status"],
        }

    @_open_service_operation
    def reset(self) -> dict[str, Any]:
        controller = self.controller
        controller.reset(to_default=True)
        return self.snapshot()

    reset_to_default = reset

    @_open_service_operation
    def reset_motor_fault(self) -> dict[str, Any]:
        if not self.can_status()["all_up"]:
            raise RuntimeError("connect and validate CAN before resetting motor faults")
        self.controller.reset_motor_fault()
        return self.snapshot()

    def request_motion_stop(self) -> bool:
        # Do not take the controller's command lock: reset holds it while
        # returning. Signalling is cancellation-only and does not touch CAN.
        controller = self._controller
        return bool(controller and controller.request_motion_stop())

    def disable(self) -> dict[str, Any]:
        if any(t.active for t in self.maintenance_tasks()):
            for task in self.maintenance_tasks():
                task.cancel()
        controller = self.controller
        if controller.state in {StationState.RUNNING, StationState.PAUSED}:
            controller.stop()
        controller.disconnect()
        return self.snapshot()

    emergency_disable = disable

    @_open_service_operation
    def zero_motor(self, motor_index: int) -> dict[str, Any]:
        result = self.controller.zero_motors([int(motor_index)])
        return {
            **result,
            "snapshot": self.snapshot(),
        }

    @_open_service_operation
    def zero_all_motors(self) -> dict[str, Any]:
        controller = self.controller
        result = controller.zero_motors(
            list(range(len(controller.config.entries)))
        )
        return {
            **result,
            "snapshot": self.snapshot(),
        }

    @_open_service_operation
    def zero_prepare(self, motor_index: int) -> dict[str, Any]:
        controller = self.controller
        with self._lock:
            if self._zeroing is not None and self._zeroing.state.value == "reading":
                if self._zeroing.current_index == int(motor_index):
                    return self._zeroing.snapshot()
                raise RuntimeError(
                    "finish or abort the current motor zeroing before selecting another"
                )
            # Each row action is an independent single-motor operation. A
            # confirmed motor must not force the operator through the remaining
            # motor list or prevent selecting the same motor again later.
            self._zeroing = controller.create_zeroing_session()
            return self._zeroing.prepare(motor_index)

    def zero_read(self) -> dict[str, Any]:
        return self._require_zeroing().read().to_dict()

    @_open_service_operation
    def zero_confirm(self, *, allow_on_fault: bool = False) -> dict[str, Any]:
        if allow_on_fault:
            raise ValueError("factory gateway does not permit fault-state zeroing")
        result = self._require_zeroing().confirm(force_on_error=False)
        return {"zeroed": bool(result), "zeroing": self._require_zeroing().snapshot()}

    def zero_skip(self) -> dict[str, Any]:
        skipped = self._require_zeroing().skip()
        return {"skipped_index": skipped, "zeroing": self._require_zeroing().snapshot()}

    def zero_abort(self) -> dict[str, Any]:
        zeroing = self._require_zeroing()
        zeroing.abort()
        return zeroing.snapshot()

    def adopt_platform_execution_code(self, execution_code: str) -> dict[str, str]:
        code = str(execution_code or "").strip()
        if "_E" not in code:
            raise ValueError("平台执行编号必须包含数据库分配的 _E 后缀")
        with self._lock:
            if not self._record_identity.get("test_id"):
                raise RuntimeError("没有正在记录的平台测试")
            self._record_identity["execution_code"] = code
            if self._live_report_context is not None:
                self._live_report_context["execution_code"] = code
            sidecar = self._record_sidecar
        if sidecar is not None and sidecar.is_file():
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["execution_code"] = code
            sidecar.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return {"execution_code": code}

    def maintenance_tasks(self):
        return tuple(t for t in (self._maintenance, self._parameters) if t is not None)

    def maintenance_targets(self):
        from .motor_maintenance import effective_targets
        return effective_targets(self.controller.config)

    def maintenance_ready(self):
        self._require_open()
        controller = self.controller
        value = controller.snapshot()
        playback = value.get("playback") or {}
        if (controller.backend.motors_enabled or controller.position_hold_active
                or controller.state in {StationState.RUNNING, StationState.PAUSED, StationState.ARMED, StationState.STOPPING}
                or (playback.get("completion") or {}).get("active")
                or self._zeroing is not None and self._zeroing.snapshot().get("state") in {"prepared", "reading"}):
            raise RuntimeError("请先停止测试、失能并结束归零/保持动作，再维护固件")
        if not self.can_status()["all_up"]:
            raise RuntimeError("CAN接口未就绪；维护不会自动配置或启动接口")

    def collect_display_snapshot(self) -> dict[str, Any]:
        """Called only by this runtime's periodic display producer."""
        with self._lock:
            self._require_open()
            controller = self._controller
            revision = self._config_revision
            connecting = self._can_connecting
            interfaces = self._can_interfaces if self._backend_name != "fake" else ()
            started = time.monotonic()
            probe = self._display_can_probe
            query_can = bool(interfaces) and not connecting and (
                probe is None or probe[0] != revision or started - probe[1] >= 1.0
            )
            if query_can:
                self._display_can_probe = revision, started

        if controller is not None and not connecting:
            controller.sample_display_feedback()
        if query_can:
            try:
                manager = self._can_manager
                statuses = manager.try_status(interfaces) if isinstance(manager, CanManager) else manager.status(interfaces)
            except (RuntimeError, OSError):
                # Keep the last verified result only within its display TTL.
                pass
            else:
                with self._lock:
                    cached = self._display_can
                    if (statuses is not None and not self._can_connecting and revision == self._config_revision
                            and (cached is None or cached[1] <= started)):
                        self._display_can = revision, started, self._can_payload(statuses)
        return self.display_snapshot()

    def display_snapshot(self) -> dict[str, Any]:
        return self._snapshot(for_display=True)

    def snapshot(self) -> dict[str, Any]:
        return self._snapshot(for_display=False)

    def _snapshot(self, *, for_display: bool) -> dict[str, Any]:
        # Never take the task lock while holding the service lock: preview
        # reads final targets in the opposite direction.
        maintenance_snapshot = self._maintenance.snapshot() if self._maintenance else {"active": False, "status": "idle"}
        parameters_snapshot = self._parameters.snapshot() if self._parameters else {"active": False, "status": "idle"}
        with self._lock:
            if self._controller is None:
                return {
                    "state": "disconnected",
                    "connected": False,
                    "configured": False,
                    "station": {
                        "connection_phase": "can_disconnected",
                        "can_connected": False,
                        "motors_discovered": False,
                        "motors_enabled": False,
                    },
                    "motors": [],
                    "configured_motors": [],
                    "trajectory": None,
                    "progress": None,
                    "can": self._can_payload([]),
                    "fault": None,
                    "diagnostics": [],
                }
            snapshot = (self._controller.snapshot(refresh_feedback=False)
                        if for_display else self._controller.snapshot())
            snapshot["motor_maintenance"] = maintenance_snapshot
            snapshot["motor_parameters"] = parameters_snapshot
            snapshot["motor_versions"] = self._controller.backend.motor_versions()
            playback = snapshot.get("playback")
            identity = dict(self._record_identity)
            if isinstance(playback, dict) and playback.get("test_id") == identity.get("test_id"):
                playback["execution_code"] = identity.get("execution_code", "")
                playback["execution_uuid"] = identity.get("execution_uuid", "")
                playback.update({key: identity.get(key, "") for key in ("data_domain", "data_domain_source", "serial_number", "bench_id", "operator_id")})
            snapshot["config_revision"] = self._config_revision
            snapshot["platform"] = {
                key: identity.get(key, "")
                for key in (
                    "campaign_id", "cycle_id", "asset_id", "configuration_id",
                    "test_case_version_id", "test_case_id", "station_id", "execution_code",
                    "data_domain", "data_domain_source", "serial_number", "bench_id", "operator_id",
                )
            }
            snapshot["platform"]["execution_uuid"] = (
                identity.get("execution_uuid", "") if isinstance(playback, dict)
                and playback.get("test_id") and playback.get("test_id") == identity.get("test_id") else ""
            )
            configured_motors = [
                {
                    "index": int(entry.index),
                    "motor_id": int(entry.motor_id),
                    "joint_name": entry.joint_name,
                    "bus": entry.interface,
                    "motor_type": entry.motor_type,
                    "motor_model": int(entry.motor_model),
                }
                for entry in self._controller.config.entries
            ]
            station = dict(snapshot.get("station") or {})
            state = str(station.get("state", self._controller.state.value))
            fault = snapshot.get("fault")
            diagnoses = fault.get("diagnoses", []) if isinstance(fault, dict) else []
            if for_display and self._backend_name != "fake":
                cached = self._display_can
                if (cached is not None and cached[0] == self._config_revision
                        and time.monotonic() - cached[1] <= 2.5):
                    can_snapshot = {**cached[2], "sampled_monotonic_s": cached[1], "stale": False}
                else:
                    can_snapshot = {**self._can_payload([]), "stale": True}
            elif self._backend_name == "fake":
                can_snapshot = {**self.can_status(), "sampled_monotonic_s": time.monotonic(), "stale": False}
            else:
                can_snapshot = self.can_status()
            motors_discovered = bool(station.get("motors_discovered", station.get("backend_connected", False)))
            motors_enabled = bool(station.get("motors_enabled", False))
            if motors_enabled:
                connection_phase = "motors_enabled"
            elif motors_discovered:
                connection_phase = "motors_discovered"
            elif can_snapshot["all_up"]:
                connection_phase = "can_connected"
            else:
                connection_phase = "can_disconnected"
            station.update(
                {
                    "connection_phase": connection_phase,
                    "can_connected": bool(can_snapshot["all_up"]),
                    "motors_discovered": motors_discovered,
                    "motors_enabled": motors_enabled,
                }
            )
            snapshot.update(
                {
                    "state": state,
                    "connected": motors_discovered,
                    "configured": True,
                    "backend": self._backend_name,
                    "config_path": str(self._config_path) if self._config_path else None,
                    "configured_motors": configured_motors,
                    "station": station,
                    "can": can_snapshot,
                    "diagnostics": diagnoses,
                    "last_stop_reason": self._last_stop_reason,
                    "report_errors": dict(self._report_errors),
                    "zeroing": (
                        self._zeroing.snapshot()
                        if self._zeroing is not None
                        else snapshot.get("zeroing")
                    ),
                }
            )
            return snapshot

    def _require_zeroing(self) -> MotorZeroingSession:
        with self._lock:
            if self._zeroing is None:
                raise RuntimeError("zeroing session has not been prepared")
            return self._zeroing


__all__ = ["FactoryService"]
