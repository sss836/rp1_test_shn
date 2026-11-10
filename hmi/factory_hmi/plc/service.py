"""Threaded PLC cabinet runtime used by the local gateway."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .async_log import AsyncPlcLog
from .client import DeferredPlcClient, PlcCommunicationError, ReadOnlyPlcClient, build_plc_client
from .config import PlcCabinetConfig, load_plc_config
from .models import CabinetPhase
from .coordination import PlcCommandCoordinator, PlcRequestConflict
from .state_machine import PlcCabinetController


class PlcControlDisabledError(RuntimeError):
    pass


class PlcCabinetService:
    def __init__(
        self,
        config: PlcCabinetConfig | None = None,
        *,
        config_path: Path | str | None = None,
        data_root: Path | str = "/var/lib/rp1-factory-hmi",
    ) -> None:
        self.config = config or load_plc_config(config_path)
        self.client = (build_plc_client(self.config.driver, self.config.protocol)
                       if self.config.driver == "mock" else DeferredPlcClient(
                           lambda: build_plc_client(self.config.driver, self.config.protocol)))
        # Deliberately session-local: restart never resumes PLC communication.
        self.communication_enabled = False
        self._communication_gate = threading.Event()
        self.read_only = self.config.access_mode == "monitor"
        if self.read_only:
            self.client = ReadOnlyPlcClient(self.client)
        self.controller = PlcCabinetController(
            self.client,
            self.config.timings,
            self.config.setpoints,
        )
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._shutdown_guard = threading.Lock()
        self._shutdown_safety_pending = 0
        self._shutdown_interrupt = threading.Event()
        self._shutdown_closing = threading.Event()
        self._shutdown_error_kind = None
        self._thread: threading.Thread | None = None
        self._last_heartbeat = 0.0
        self._last_reconnect = 0.0
        self._last_status_counter: int | None = None
        max_samples = max(
            10,
            int(
                self.config.timings.trend_retention_seconds
                * 1000
                / self.config.timings.poll_ms
            ),
        )
        self._trend: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._power_off_unverified = False
        self._persisted_logs = 0
        self._log_writer = AsyncPlcLog()
        self.shutdown_status = {"state": "not_requested", "detail": "PLC shutdown not requested"}
        self._log_path = (
            Path(data_root).expanduser().resolve() / "plc" / "operations.jsonl"
        )

        self.coordination = PlcCommandCoordinator(self)

    def _checked(self, control: str, operation: Any) -> None:
        previous = dict(self.controller.feedback)
        if not operation():
            detail = self.controller.feedback[control].detail
            # A competing rejection belongs to its request, never to the
            # currently executing shared command. Keep its audit log entry.
            self.controller.feedback = previous
            raise PlcRequestConflict("rejected", detail)

    def _tick(self, now: float | None = None) -> None:
        self.controller.tick(now)
        self.coordination.advance()

    def start(self) -> None:
        # Gateway startup is passive. Only explicit connect() opens a client
        # or creates the polling worker; configuration never auto-enables it.
        return

    def _start_enabled_worker(self) -> None:
        self._stop.clear()
        self._communication_gate.set()
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="rp1-plc-cabinet", daemon=True)
            self._thread.start()

    def _shutdown_feedback_confirmed(self) -> bool:
        status = self.controller.status
        command = self.controller.command
        return bool(self.controller.phase == CabinetPhase.OFF
                    and self.controller.communication_state == "LIVE"
                    and not self.controller.stale and status is not None
                    and not any(status.channel_permit) and not status.main_ready
                    and status.main_contactor_fb is False
                    and not command.main_enable and not command.ps1_output_enable
                    and not any(command.channel_enable) and self.controller._pending is None)

    @contextmanager
    def shutdown_safety_request(self):
        """Register an authenticated safety request before acquiring control locks."""
        with self._shutdown_guard:
            self._shutdown_safety_pending += 1
            if self._shutdown_closing.is_set():
                self._shutdown_interrupt.set()
        try:
            yield
        finally:
            with self._shutdown_guard:
                self._shutdown_safety_pending -= 1

    def request_shutdown_interrupt(self) -> None:
        """Authenticated safety routes may yield a pending exit without the PLC lock."""
        with self._shutdown_guard:
            if self._shutdown_closing.is_set():
                self._shutdown_interrupt.set()

    def _check_shutdown_interrupt(self) -> None:
        if self._shutdown_interrupt.is_set():
            raise RuntimeError("PLC shutdown interrupted by safety command; exit remains pending")

    def close(self) -> None:
        with self._shutdown_guard:
            if self._shutdown_safety_pending:
                raise RuntimeError("PLC safety request pending; exit remains pending")
            self._shutdown_interrupt.clear()
            self._shutdown_closing.set()
        try:
            self._close_impl()
        finally:
            with self._shutdown_guard:
                self._shutdown_closing.clear()

    def _close_impl(self) -> None:
        self._stop.set()
        self._communication_gate.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
            if thread.is_alive():
                self._stop.clear()  # Exit was rejected; retain the existing heartbeat worker.
                if self._shutdown_error_kind != "electrical":
                    self._shutdown_error_kind = "worker"
                self.shutdown_status = {"state": "unconfirmed", "detail": "PLC polling worker did not stop"}
                raise RuntimeError(self.shutdown_status["detail"])
        with self._lock:
            try:
                self._check_shutdown_interrupt()
                if not self.client.connected and (self._power_off_unverified or self.shutdown_status["state"] == "unconfirmed" and self._shutdown_error_kind == "electrical"):
                    raise RuntimeError("PLC shutdown remains unconfirmed; reconnect and verify actual OFF")
                if not self.client.connected or self.read_only:
                    # An unused/offline PLC must not block external-power tests.
                    # Neither this nor monitor disconnect proves electrical OFF.
                    if self.shutdown_status["state"] != "confirmed":
                        self.shutdown_status = {"state": "unverified", "detail": "PLC disconnected/read-only; external power OFF cannot be verified by software"}
                else:
                    self.shutdown_status = {"state": "stopping", "detail": "Waiting for fresh PLC shutdown feedback"}
                    self._shutdown_error_kind = "electrical"
                    self._tick()
                    self._check_shutdown_interrupt()
                    if not self._shutdown_feedback_confirmed() and not self.controller.request_stop(user="system-shutdown"):
                        raise RuntimeError(self.controller.feedback["Stop"].detail)
                    t = self.config.timings
                    deadline = time.monotonic() + (6*t.command_timeout_ms + t.km0_timeout_ms + 4*t.ps1_timeout_ms)/1000
                    while True:
                        self._check_shutdown_interrupt()
                        self._tick()
                        self._check_shutdown_interrupt()
                        if self._shutdown_feedback_confirmed():
                            self.controller.connection_power_unverified = False
                            self._power_off_unverified = False
                            self.shutdown_status = {"state": "confirmed", "detail": "Fresh PLC shutdown feedback confirmed; mechanical safety requires on-site verification"}
                            break
                        if (self.controller.phase in {CabinetPhase.FAULT, CabinetPhase.COMM_LOST, CabinetPhase.STALE}
                                or time.monotonic() >= deadline):
                            raise RuntimeError(self.controller.last_error or "PLC shutdown feedback timeout: " + self.controller.phase.value)
                        self.controller.heartbeat()
                        self._shutdown_interrupt.wait(t.poll_ms / 1000)
                self.controller.disconnect()
                self.coordination.advance()
                self._shutdown_error_kind = None
            except Exception as exc:
                self.shutdown_status = {"state": "unconfirmed", "detail": str(exc)}
                self.controller.last_error = "PLC shutdown unconfirmed: " + str(exc)
                # Preserve existing polling/heartbeat after a rejected exit. This
                # does not authorize any new start or different loss-of-link policy.
                if thread is not None:
                    self._stop.clear()
                    self._thread = threading.Thread(target=self._run, name="rp1-plc-cabinet", daemon=True)
                    self._thread.start()
                # Do not silently disconnect a live cabinet after failed shutdown.
                raise RuntimeError(self.controller.last_error) from exc
            finally:
                self._persist_logs()
        self._log_writer.close()

    def _try_connect(self, user: str) -> None:
        self.coordination.revision += 1
        try:
            if self.read_only:
                self.client.connect()
                self.controller.status = self.client.read_status()
                self.controller._ps1_feedback.reset()
                self.controller._ps1_feedback.observe(self.controller.status, time.monotonic())
                self.controller.communication_state = "LIVE"
                self.controller.phase = CabinetPhase.MONITORING
                self.controller.last_error = ""
            else:
                self.controller.connect(user=user)
        except Exception as exc:
            self.client.disconnect()
            self.controller.last_error = str(exc)
            self.controller.communication_state = "COMM_LOST"
            self.controller.phase = CabinetPhase.COMM_LOST
            self._last_reconnect = time.monotonic()
        finally:
            if not self.read_only and self.controller.connection_power_unverified:
                if self._shutdown_feedback_confirmed():
                    self.controller.connection_power_unverified = False
                    self._power_off_unverified = False
                else:
                    self._power_off_unverified = True
            self.coordination.advance()

    def connect(self, *, user: str) -> dict[str, Any]:
        with self._lock:
            self.communication_enabled = True
            if not self.client.connected:
                self._try_connect(user)
            self._start_enabled_worker()
            return self.snapshot()

    def _communication_disable_reason(self) -> str:
        command = self.controller.command
        if self.controller._pending is not None or self.controller.phase not in {
            CabinetPhase.OFF, CabinetPhase.DISCONNECTED, CabinetPhase.COMM_LOST,
            CabinetPhase.MONITORING,
        }:
            return "PLC正在执行或状态未确认，请先正常总停止并确认实际OFF"
        if (command.main_enable or command.ps1_output_enable or any(command.channel_enable)):
            return "PLC仍有供电请求，请先正常总停止并确认实际OFF"
        if self.read_only:
            return ""
        if self.client.connected:
            if not self._shutdown_feedback_confirmed():
                return "尚无新鲜的PLC实际OFF反馈，不能停用通信"
        elif self._power_off_unverified or (self._shutdown_error_kind == "electrical"
                                           and self.shutdown_status["state"] == "unconfirmed"):
            return "PLC供电断开尚未确认，必须恢复通信并核实实际OFF"
        return ""

    def disconnect(self) -> dict[str, Any]:
        with self._lock:
            reason = self._communication_disable_reason()
            if reason:
                raise PlcRequestConflict("communication_required", reason)
            if self.client.connected:
                self.controller.disconnect()
            self.communication_enabled = False
            self._communication_gate.clear()
            self._power_off_unverified = False
            self.shutdown_status = {"state": "unverified", "detail": "PLC通信已停用；不代表外部供电或机械安全已确认"}
            self.coordination.advance()
            return self.snapshot()

    def _require_control(self) -> None:
        if self.read_only:
            raise PlcControlDisabledError("PLC 处于实机只读监控模式，未启用控制操作")

    def start_sequence(
        self,
        channels: list[int],
        *,
        user: str,
        voltage: float | None = None,
        current: float | None = None,
    ) -> dict[str, Any]:
        self._require_control()
        selected = tuple(index in set(channels) for index in range(1, 5))
        with self._lock:
            self._checked("Start", lambda: self.controller.request_start(
                selected, user=user, voltage=voltage, current=current,
            ))
            self._power_off_unverified = True
            return self.snapshot()

    def stop_sequence(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('Stop', lambda: self.controller.request_stop(user=user))
            return self.snapshot()

    def set_channel(self, channel: int, enabled: bool, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked(f"ChannelEnable{channel}", lambda: self.controller.request_channel(channel, enabled, user=user))
            return self.snapshot()

    def set_ps1_setpoints(
        self,
        voltage: float,
        current: float,
        *,
        user: str,
    ) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('Setpoints', lambda: self.controller.request_setpoints(voltage, current, user=user))
            return self.snapshot()

    def reset_fault(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('ResetFaultPulse', lambda: self.controller.request_reset_fault(user=user))
            return self.snapshot()

    def all_stop(self, *, user: str) -> dict[str, Any]:
        self._require_control()
        with self._lock:
            self._checked('AllStop', lambda: self.controller.request_all_stop(user=user))
            return self.snapshot()

    def snapshot(self, *, include_history: bool = True) -> dict[str, Any]:
        with self._lock:
            coordination = self.coordination.snapshot()
            value = self.controller.snapshot(include_history=include_history)
            value["driver"] = self.config.driver
            value["communication_enabled"] = self.communication_enabled
            reason = self._communication_disable_reason()
            value["communication_disable_allowed"] = not bool(reason)
            value["communication_disable_reason"] = reason
            if (not self.communication_enabled and not self.client.connected
                    and self.controller.communication_state == "DISCONNECTED"):
                value["communication_state"] = "DISABLED"
            value["access_mode"] = self.config.access_mode
            value["read_only"] = self.read_only
            value["endpoint"] = (
                f"{self.config.protocol.get('host')}:{self.config.protocol.get('port')} / Unit {self.config.protocol.get('unit_id')}"
                if self.config.driver == "modbus_tcp" else self.config.driver
            )
            value["timings"] = {
                "heartbeat_ms": self.config.timings.heartbeat_ms,
                "poll_ms": self.config.timings.poll_ms,
                "stale_timeout_ms": self.config.timings.stale_timeout_ms,
                "command_timeout_ms": self.config.timings.command_timeout_ms,
                "km0_timeout_ms": self.config.timings.km0_timeout_ms,
                "ps1_timeout_ms": self.config.timings.ps1_timeout_ms,
                "startup_budget_ms": self.controller.startup_budget_ms,
            }
            value["dut_can_map"] = dict(self.config.dut_can_map)
            value["setpoint_limits"] = {
                "enabled": self.config.setpoints.enabled,
                "voltage_min_v": self.config.setpoints.voltage_min_v,
                "voltage_max_v": self.config.setpoints.voltage_max_v,
                "current_min_a": self.config.setpoints.current_min_a,
                "current_max_a": self.config.setpoints.current_max_a,
                "step": self.config.setpoints.step,
            }
            value["shutdown"] = dict(self.shutdown_status)
            value["logging"] = self._log_writer.snapshot()
            from .connection_logging import CONNECTION_LOG_FILTER
            value["connection_logging"] = CONNECTION_LOG_FILTER.snapshot()
            value["coordination"] = coordination
            if include_history:
                value["trend"] = list(self._trend)
            return value

    def _run(self) -> None:
        poll_seconds = self.config.timings.poll_ms / 1000.0
        while not self._stop.is_set():
            self._communication_gate.wait()
            if self._stop.wait(poll_seconds):
                break
            with self._lock:
                if not self.communication_enabled:
                    continue
                now = time.monotonic()
                if not self.client.connected:
                    if (
                        now - self._last_reconnect
                        >= self.config.timings.reconnect_ms / 1000.0
                    ):
                        self._last_reconnect = now
                        self._try_connect("system-reconnect")
                    continue
                try:
                    if self.read_only:
                        self.controller.status = self.client.read_status()
                        self.controller._ps1_feedback.observe(self.controller.status, now)
                        self.controller.communication_state = "LIVE"
                        self.controller.phase = CabinetPhase.MONITORING
                        self.controller.last_error = ""
                    else:
                        command = self.controller.command
                        if command.main_enable or command.ps1_output_enable or any(command.channel_enable):
                            self._power_off_unverified = True
                        self._tick(now)
                        if self._shutdown_feedback_confirmed():
                            self.controller.connection_power_unverified = False
                            self._power_off_unverified = False
                    if not self.read_only and (
                        now - self._last_heartbeat
                        >= self.config.timings.heartbeat_ms / 1000.0
                    ):
                        self.controller.heartbeat()
                        self._last_heartbeat = now
                    self._collect_trend()
                    self._persist_logs()
                except PlcCommunicationError as exc:
                    self.controller._communication_lost(str(exc))
                    self.client.disconnect()
                    self.coordination.advance()
                except Exception as exc:
                    self.controller.last_error = str(exc)

    def _collect_trend(self) -> None:
        status = self.controller.status
        if status is None or self.controller.stale:
            return
        if status.status_counter == self._last_status_counter:
            return
        self._last_status_counter = status.status_counter
        values = self.controller.snapshot(include_history=False)["status"]
        self._trend.append(
            {
                "timestamp": time.time(),
                "Voltage": values["Voltage"],
                "Current": values["Current"],
                "Power": values["Power"],
                "AnalogValidMask": values["AnalogValidMask"],
            }
        )

    def _persist_logs(self) -> None:
        entries = self.controller.operation_log
        if self._persisted_logs >= len(entries):
            return
        confirmed_end = self._persisted_logs
        while (
            confirmed_end < len(entries)
            and entries[confirmed_end].confirmation.value != "pending"
        ):
            confirmed_end += 1
        if confirmed_end == self._persisted_logs:
            return
        for entry in entries[self._persisted_logs : confirmed_end]:
            self._log_writer.submit(self._log_path, entry.to_dict())
        # Cursor means enqueued/attempted, not durable. Durability is exposed in logging.
        self._persisted_logs = confirmed_end


__all__ = ["PlcCabinetService"]
