"""Exclusive motor backends for Linux hardware and deterministic tests."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable

import numpy as np

from factory_hmi.models import MotorSample, MotorZeroResult
from .control_watchdog import CommandWatchdog
from .motor_versions import MotorVersions, firmware_generation

from ._legacy import (
    MotorEntry,
    add_motors_python_paths,
    command_mit as legacy_command_mit,
    create_motor_driver,
)


class MotorBackendError(RuntimeError):
    pass


class BackendNotConnectedError(MotorBackendError):
    pass


class BackendInUseError(MotorBackendError):
    pass


class MotorSafetyError(MotorBackendError):
    pass


class _MaintenanceTransport:
    """Retryable ownership handle; close marks completion only after release."""
    def __init__(self, backend):
        self.backend = backend
        self.closed = False

    def __enter__(self):
        self.backend._begin_maintenance_transport(self)
        return self

    def close(self):
        if not self.closed:
            self.backend._finish_maintenance_transport(self)
            self.closed = True

    def __exit__(self, exc_type, exc, traceback):
        self.close()


_RESOURCE_LOCK = threading.Lock()
_RESOURCE_OWNERS: dict[str, int] = {}


class MotorBackend(ABC):
    """Thread-safe, exclusive connection to one configured motor group."""

    def __init__(self, entries: Iterable[MotorEntry]) -> None:
        self.entries = tuple(entries)
        if not self.entries:
            raise ValueError("motor backend requires at least one motor entry")
        self._lock = threading.RLock()
        self._connected = False
        self._enabled = False
        self._claimed_resources: tuple[str, ...] = ()
        self._command_inhibit = threading.Event()
        self._inhibit_lock = threading.Lock()
        self._control_generation = 0
        self._control_generation_lock = threading.Lock()
        self._damping_owner_generation = None
        self._damping_owner = None
        self._damping_cancelled = threading.Event()
        self._command_watchdog: CommandWatchdog | None = None
        self.control_stop_error: str | None = None
        self._versions = None

    def motor_versions(self) -> list[dict]:
        if self._versions is not None:
            return self._versions.snapshot(connected=self._connected)
        return [{k: v for k, v in MotorVersions._empty(e).items() if k != "_monotonic"}
                for e in self.entries]

    @property
    def commands_inhibited(self) -> bool:
        return self._command_inhibit.is_set()

    def set_control_watchdog(self, timeout_s: float, on_fault: Any) -> None:
        def trip(message: str, generation: int) -> None:
            # Latch before acquiring any motor/controller lock. If a native
            # dispatch is stuck, future commands stay fenced when it returns.
            with self._inhibit_lock:
                if not self._command_watchdog.is_current(generation):
                    return
                self._command_inhibit.set()
                try:
                    on_fault(message)
                except Exception as exc:
                    self.control_stop_error = f"control fault notification failed: {exc}"
            try:
                with self._lock:
                    if not self._command_watchdog.is_current(generation):
                        return
                    self.disable()
            except Exception as exc:
                self.control_stop_error = str(exc)
        self._command_watchdog = CommandWatchdog(timeout_s, trip)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def motors_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    @abstractmethod
    def motors(self) -> list[Any]:
        """Raw motor handles used by the existing watchdog and recorder."""

    def _exclusive_resources(self) -> tuple[str, ...]:
        return ()

    def connect(self) -> None:
        """Backward-compatible transport-open plus motor-enable operation."""

        self.open_transport()
        try:
            self.enable_motors()
        except Exception:
            self.close_transport()
            raise

    def open_transport(self) -> None:
        with self._lock:
            if getattr(self, "_maintenance_active", False) or getattr(self, "_maintenance_quarantined", False):
                raise BackendInUseError("firmware maintenance owns or quarantines this backend")
            if self._connected:
                raise BackendInUseError("this backend is already connected")
            resources = tuple(sorted(set(self._exclusive_resources())))
            with _RESOURCE_LOCK:
                conflicts = [
                    resource
                    for resource in resources
                    if resource in _RESOURCE_OWNERS
                    and _RESOURCE_OWNERS[resource] != id(self)
                ]
                if conflicts:
                    raise BackendInUseError(
                        "motor interface already owned: " + ", ".join(conflicts)
                    )
                for resource in resources:
                    _RESOURCE_OWNERS[resource] = id(self)
            self._claimed_resources = resources
            try:
                self._open_transport_impl()
            except Exception:
                self._release_resources()
                raise
            self._connected = True

    def enable_motors(self) -> None:
        with self._lock:
            if getattr(self, "_maintenance_quarantined", False):
                raise MotorSafetyError("maintenance transport close failed; normal disconnect recovery required")
            self._require_connected()
            if self._enabled:
                raise BackendInUseError("motors are already enabled")
            self.prepare_motor_protocols()
            try:
                self._enable_motors_impl()
            except Exception:
                try:
                    self._disable_impl(None)
                except Exception:
                    pass
                self._enabled = False
                raise
            self._enabled = True
            with self._control_generation_lock:self._control_generation += 1
            self._damping_owner = None
            with self._inhibit_lock:
                self._command_inhibit.clear()
                self.control_stop_error = None
                if self._command_watchdog is not None:
                    self._command_watchdog.arm()

    def prepare_motor_protocols(self, check_cancelled: Callable[[], None] | None = None) -> None:
        """Confirm any drive-specific control protocols before enabling motors."""

    def disable_motors(self) -> None:
        self._revoke_damping()
        self._command_inhibit.set()
        if self._command_watchdog is not None:
            self._command_watchdog.disarm()
        with self._lock:
            self._require_connected()
            self._damping_owner = None
            if not self._enabled:
                return
            self._disable_impl(None)
            self._enabled = False

    def disconnect(self) -> None:
        self.close_transport()

    def close_transport(self) -> None:
        self._revoke_damping()
        if getattr(self, "_maintenance_active", False):
            raise BackendInUseError("cancel and join maintenance before closing transport")
        self._command_inhibit.set()
        if self._command_watchdog is not None:
            self._command_watchdog.disarm()
        with self._lock:
            self._damping_owner = None
            if not self._connected:
                self._release_resources()
                self._maintenance_owner = None
                return
            if self._enabled:
                self._disable_impl(None)
                self._enabled = False
            self._close_transport_impl()
            self._connected = False
            self._maintenance_quarantined = False
            self._release_resources()
            self._maintenance_owner = None

    def maintenance_transport(self):
        """A retryable claim held through the caller's terminal persistence."""
        return _MaintenanceTransport(self)

    def _begin_maintenance_transport(self, owner):
        with self._lock:
            if self._enabled or getattr(self, "_maintenance_active", False) or getattr(self, "_maintenance_quarantined", False):
                raise MotorSafetyError("maintenance requires disabled motors and an idle backend")
            resources = tuple(sorted(set(self._exclusive_resources())))
            with _RESOURCE_LOCK:
                if any(_RESOURCE_OWNERS.get(r) not in (None, id(self)) for r in resources):
                    raise BackendInUseError("maintenance interface is owned by another station")
                for resource in resources:
                    _RESOURCE_OWNERS[resource] = id(self)
            self._claimed_resources = resources
            self._maintenance_active = True
            self._maintenance_owner = owner
            try:
                if self._connected:
                    self._close_transport_impl()
                    self._connected = False
            except Exception:
                # Keep ownership but allow a subsequent normal close to retry.
                self._maintenance_active = False
                self._maintenance_quarantined = True
                raise

    def _finish_maintenance_transport(self, owner):
        with self._lock:
            if getattr(self, "_maintenance_owner", None) is not owner:
                return
            if getattr(self, "_maintenance_quarantined", False):
                # An entry close failed; retry normal cleanup before releasing.
                self.close_transport()
            else:
                # Keep the active fence if releasing ownership itself fails.
                self._release_resources()
            self._maintenance_active = False
            self._maintenance_owner = None

    def _release_resources(self) -> None:
        with _RESOURCE_LOCK:
            for resource in self._claimed_resources:
                if _RESOURCE_OWNERS.get(resource) == id(self):
                    del _RESOURCE_OWNERS[resource]
        self._claimed_resources = ()

    def _require_connected(self) -> None:
        if not self._connected:
            raise BackendNotConnectedError("motor backend is not connected")

    def _require_enabled(self) -> None:
        self._require_connected()
        if self._command_inhibit.is_set():
            raise MotorSafetyError("motor commands inhibited; explicit recovery and enable required")
        if not self._enabled:
            raise MotorSafetyError("motor command rejected while motors are disabled")

    def command_mit(
        self,
        target: np.ndarray | list[float],
        *,
        torque: np.ndarray | list[float] | None = None,
        kp_scale: float = 1.0,
        kd_scale: float = 1.0,
    ) -> None:
        target_array = np.asarray(target, dtype=np.float64).reshape(-1)
        if target_array.shape != (len(self.entries),):
            raise ValueError(
                f"MIT target must contain {len(self.entries)} values, got {target_array.size}"
            )
        if not np.all(np.isfinite(target_array)):
            raise ValueError("MIT target contains NaN or inf")
        torque_array = None
        if torque is not None:
            torque_array = np.asarray(torque, dtype=np.float64).reshape(-1)
            if torque_array.shape != target_array.shape:
                raise ValueError("MIT torque count does not match motor count")
            if not np.all(np.isfinite(torque_array)):
                raise ValueError("MIT torque contains NaN or inf")
        with self._lock:
            self._require_enabled()
            self._command_mit_impl(
                target_array,
                torque=torque_array,
                kp_scale=float(kp_scale),
                kd_scale=float(kd_scale),
            )
            if self._command_watchdog is not None:
                self._command_watchdog.touch()

    @property
    def control_generation(self):
        with self._control_generation_lock:return self._control_generation

    def _revoke_damping(self):
        self._damping_cancelled.set()
        with self._control_generation_lock:self._control_generation += 1

    def begin_protective_damping(self, expected_generation=None):
        """Fence normal commands; this never connects or enables a motor."""
        with self._lock:
            self._require_connected()
            if not self._enabled:
                raise MotorSafetyError("protective damping cannot enable disabled motors")
            if expected_generation is not None and expected_generation != self.control_generation:
                raise MotorSafetyError("protective damping request belongs to an old control generation")
            if self._damping_owner is not None and self._damping_cancelled.is_set():
                raise MotorSafetyError("protective damping was explicitly cancelled")
            if self._damping_owner is None:
                if self._command_inhibit.is_set():
                    raise MotorSafetyError("emergency inhibit cannot be overridden by damping")
                self._damping_owner = object()
                self._damping_owner_generation = self.control_generation
                self._damping_cancelled.clear()
            self._command_inhibit.set()
            return self._damping_owner

    def command_protective_damping(self, owner, kd: float = 1.0) -> None:
        # Absolute Kd, not a multiplier on the trajectory's configured gains.
        if not math.isfinite(kd) or not 0 < kd <= 1.0:
            raise ValueError("protective damping Kd must be finite and in (0, 1]")
        with self._lock:
            self._require_connected()
            if owner is None or owner is not self._damping_owner or not self._enabled or self._damping_cancelled.is_set() or self._damping_owner_generation != self.control_generation:
                raise MotorSafetyError("protective damping authority is no longer active")
            self._command_protective_damping_impl(kd)
            if self._command_watchdog is not None:
                self._command_watchdog.touch()

    def _command_protective_damping_impl(self, kd: float) -> None:
        raise MotorSafetyError("backend does not support protective damping")

    def cancel_protective_damping(self) -> None:
        """Latch explicit cancellation without waiting behind native I/O."""
        self._revoke_damping()

    def disable_protective_owner(self, owner):
        with self._lock:
            if (owner is None or owner is not self._damping_owner
                    or self._damping_owner_generation != self.control_generation
                    or self._damping_cancelled.is_set()):
                return False
            self.disable_motors()
            return True

    def end_protective_damping(self, owner) -> None:
        with self._lock:
            if owner is self._damping_owner:
                self._damping_owner = None
        # Normal commands remain inhibited until an explicit fresh enable.

    def read_samples(self) -> list[MotorSample]:
        with self._lock:
            self._require_connected()
            samples = self._read_samples_impl()
            if len(samples) != len(self.entries):
                raise MotorBackendError(
                    f"backend returned {len(samples)} samples for {len(self.entries)} motors"
                )
            return samples

    def refresh_feedback(self) -> None:
        """Request read-only feedback without enabling motor torque."""

        with self._lock:
            self._require_connected()
            self._refresh_feedback_impl()

    def zero_motor(self, index: int, *, force_on_error: bool = False) -> bool:
        with self._lock:
            # Set-zero is a maintenance command and the LRO protocol requires
            # the drive to be unlocked/disabled before it is issued.
            self._require_connected()
            self._validate_index(index)
            return bool(
                self._zero_motor_impl(int(index), force_on_error=force_on_error)
            )

    def submit_motor_zero(self, index: int, *, force_on_error: bool = False) -> MotorZeroResult:
        if self.zero_motor(index, force_on_error=force_on_error):
            return MotorZeroResult.CONFIRMED
        return MotorZeroResult.UNCONFIRMED

    def clear_errors(self, index: int | None = None) -> None:
        with self._lock:
            self._require_connected()
            if index is not None:
                self._validate_index(index)
            self._clear_errors_impl(index)

    def disable_feedback(self) -> str:
        """No transport acknowledgement is available; command flags are not feedback."""
        return "unknown"

    def disable(self, index: int | None = None) -> None:
        self._revoke_damping()
        if index is None:
            self._command_inhibit.set()
            if self._command_watchdog is not None:
                self._command_watchdog.disarm()
        with self._lock:
            self._require_connected()
            self._damping_owner = None
            if index is not None:
                self._validate_index(index)
            self._disable_impl(index)
            if index is None:
                self._enabled = False

    def _validate_index(self, index: int) -> None:
        if isinstance(index, bool) or not 0 <= int(index) < len(self.entries):
            raise IndexError(f"motor index {index} is out of range")

    @abstractmethod
    def _open_transport_impl(self) -> None:
        pass

    @abstractmethod
    def _enable_motors_impl(self) -> None:
        pass

    @abstractmethod
    def _close_transport_impl(self) -> None:
        pass

    @abstractmethod
    def _command_mit_impl(
        self,
        target: np.ndarray,
        *,
        torque: np.ndarray | None,
        kp_scale: float,
        kd_scale: float,
    ) -> None:
        pass

    @abstractmethod
    def _read_samples_impl(self) -> list[MotorSample]:
        pass

    def _refresh_feedback_impl(self) -> None:
        """Backend hook for a safe status request while motors are disabled."""

    @abstractmethod
    def _zero_motor_impl(self, index: int, *, force_on_error: bool) -> bool:
        pass

    @abstractmethod
    def _clear_errors_impl(self, index: int | None) -> None:
        pass

    @abstractmethod
    def _disable_impl(self, index: int | None) -> None:
        pass


def _optional_float(motor: Any, method: str) -> float:
    getter = getattr(motor, method, None)
    if getter is None:
        return float("nan")
    try:
        return float(getter())
    except Exception:
        return float("nan")


class MotorsPyBackend(MotorBackend):
    """Linux backend using the repository-bundled ``motors_py`` SDK."""

    _READ_ONLY_REFRESH_TYPES = frozenset({"DM", "LRO", "XYN"})
    _FEEDBACK_QUERY_TIMEOUT_S = 0.03

    def __init__(self, entries: Iterable[MotorEntry]) -> None:
        super().__init__(entries)
        self._motors: list[Any] = []
        self._motors_py: Any = None
        self._last_command = np.full(len(self.entries), np.nan, dtype=np.float64)
        self.init_error_ids: list[int] = []
        self._last_response_counts: list[int | None] = [None] * len(self.entries)
        self._last_feedback_counts: list[int | None] = [None] * len(self.entries)
        self._last_feedback_at = np.zeros(len(self.entries), dtype=np.float64)
        self._last_mit_command_at = 0.0
        self._protocol_observation_lock = threading.Lock()
        self._protocol_observations: list[dict | None] = [None] * len(self.entries)
        self._protocol_conflicts: list[str | None] = [None] * len(self.entries)
        self._protocol_conflict = threading.Event()
        self._selected_protocols: list[str | None] = [None] * len(self.entries)
        self._configured_v103_protocols = {
            entry.lro_protocol.protocol for entry in self.entries if entry.lro_protocol_profile == "v1.03"
        }
        self._pending_motor_zero: set[int] = set()

    @property
    def motors(self) -> list[Any]:
        return self._motors

    def _exclusive_resources(self) -> tuple[str, ...]:
        return tuple(
            f"{entry.backend}:{entry.interface_type}:{entry.interface}"
            for entry in self.entries
        )

    def _open_transport_impl(self) -> None:
        try:
            import motors_py
        except ImportError:
            # Development-only fallback; production builds embed motors_py.
            add_motors_python_paths()
            try:
                import motors_py
            except Exception as exc:
                raise MotorBackendError(
                    f"failed to import motors_py; rebuild the bundled SDK: {exc}"
                ) from exc

        self._motors_py = motors_py
        created: list[Any] = []
        try:
            for entry in self.entries:
                motor = create_motor_driver(entry, motors_py)
                created.append(motor)
        except Exception:
            for motor in reversed(created):
                try:
                    motor.deinit_motor()
                except Exception:
                    pass
            raise
        self._motors = created
        # Creating a driver only opens the transport.  It does not prove that a
        # motor has returned a frame, so keep feedback stale until a reply is
        # observed.
        self._last_feedback_at[:] = 0.0
        self._last_response_counts = [
            self._response_count(motor) for motor in self._motors
        ]
        self._last_feedback_counts = [
            self._feedback_count(entry, motor)
            for entry, motor in zip(self.entries, self._motors)
        ]
        # LRO's read-only status request is encoded as a zero-gain MIT frame.
        # Selecting MIT here only changes the SDK's local encoder; it does not
        # send an enable command or apply torque.
        for entry, motor in zip(self.entries, self._motors):
            if entry.motor_type.upper() != "LRO":
                continue
            setter = getattr(motor, "set_motor_control_mode", None)
            if setter is not None:
                setter(self._motors_py.MotorControlMode.MIT)

        self._protocol_observations = [None] * len(self.entries)
        self._protocol_conflicts = [None] * len(self.entries)
        self._protocol_conflict.clear()
        self._selected_protocols = [None] * len(self.entries)
        self._versions = MotorVersions(self.entries, self._motors, on_observation=self._observe_motor_protocol)
        self._versions.start()

    def motor_versions(self) -> list[dict]:
        rows = super().motor_versions()
        with self._protocol_observation_lock:
            for index, (entry, row) in enumerate(zip(self.entries, rows)):
                if entry.motor_type.upper() != "LRO":
                    continue
                row["configured_profile"] = entry.lro_protocol_profile
                row["active_profile"] = self._selected_protocols[index] if (
                    self._connected and not self._protocol_conflicts[index]
                ) else None
                row["profile_error"] = self._protocol_conflicts[index]
        return rows

    def _observe_motor_protocol(self, index: int, row: dict) -> None:
        entry = self.entries[index]
        if entry.motor_type.upper() != "LRO":
            return
        expected = entry.lro_protocol
        conflict = None
        generation = firmware_generation(row.get("raw_firmware"))
        if entry.lro_protocol_profile == "legacy" and (
            generation == 109 or row.get("protocol") in self._configured_v103_protocols
        ):
            conflict = "读到 109 固件或已核实的 V1.03 协议，需为该节点配置并核对 V1.03"
        elif expected is None and row.get("raw_firmware") is not None and generation != 108:
            conflict = "固件读回与默认的 108 范围不符，需为该节点配置并核对旧协议身份"
        elif expected is not None:
            mismatched = [
                name for name in ("raw_firmware", "numeric_software", "hardware", "protocol")
                if row.get(name) is not None and row[name] != getattr(expected, name)
            ]
            if mismatched:
                conflict = "LRO 身份与工位核实值不符（" + ", ".join(mismatched) + "）"
        with self._protocol_observation_lock:
            self._protocol_observations[index] = row
            if conflict is not None:
                self._protocol_conflicts[index] = conflict
                self._protocol_conflict.set()

    def _check_protocol_conflicts(self, index: int | None = None) -> None:
        if not self._protocol_conflict.is_set():
            return
        with self._protocol_observation_lock:
            for motor_index, reason in enumerate(self._protocol_conflicts):
                if reason and index in (None, motor_index):
                    entry = self.entries[motor_index]
                    raise MotorSafetyError(f"{entry.interface}#{entry.motor_id} {reason}")

    def _select_verified_protocol(self, index: int) -> bool:
        """Apply the profile only when the read-back identity supports it."""
        entry = self.entries[index]
        profile = entry.lro_protocol_profile
        if profile is None:
            return True
        expected = entry.lro_protocol
        with self._protocol_observation_lock:
            row = self._protocol_observations[index]
            if self._protocol_conflicts[index] or row is None or row.get("status") != "known":
                return False
            observed_at = row.get("_monotonic")
            if observed_at is None or time.monotonic() - observed_at > self._versions.stale:
                return False
            if expected is None:
                matches = firmware_generation(row.get("raw_firmware")) == 108
            else:
                matches = all(row.get(name) == getattr(expected, name)
                              for name in ("raw_firmware", "numeric_software", "hardware", "protocol"))
            if not matches:
                return False
            if self._selected_protocols[index] == profile:
                return True
        motor = self._motors[index]
        if profile == "v1.03":
            setter = getattr(motor, "set_lro_protocol_profile", None)
            if setter is None or not setter(profile):
                return False
            self._last_feedback_at[index] = 0.0
            self._last_feedback_counts[index] = self._feedback_count(entry, motor)
        with self._protocol_observation_lock:
            self._selected_protocols[index] = profile
        return True

    def prepare_motor_protocols(self, check_cancelled: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._require_connected()
            indices = [i for i, entry in enumerate(self.entries) if entry.motor_type.upper() == "LRO"]
            if not indices:
                return
            if self._versions is None:
                raise MotorSafetyError("LRO 版本查询未启动")
            deadline = time.monotonic() + 0.25 + 2 * self._versions.timeout * len(indices)
            while True:
                if check_cancelled is not None:
                    check_cancelled()
                self._check_protocol_conflicts()
                with self._protocol_observation_lock:
                    pending = [i for i in indices if self._protocol_observations[i] is None]
                if not pending or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            for index in indices:
                entry = self.entries[index]
                if index in pending or not self._select_verified_protocol(index):
                    raise MotorSafetyError(
                        f"{entry.interface}#{entry.motor_id} 电机协议未确认，"
                        "请核对固件、硬件、协议读回及当前 SDK"
                    )
            self._check_protocol_conflicts()

    def _enable_motors_impl(self) -> None:
        errors: list[int] = []
        initialized: list[Any] = []
        try:
            self._require_disabled_v103()
            for index, (entry, motor) in enumerate(zip(self.entries, self._motors)):
                self._check_protocol_conflicts()
                error_id = int(motor.init_motor())
                errors.append(error_id)
                if error_id != 0:
                    raise MotorSafetyError(
                        f"motor[{entry.index}] id={entry.motor_id} "
                        f"failed to enable with error_id={error_id} protocol_profile={self._selected_protocols[index]}"
                    )
                initialized.append(motor)
                if entry.lro_protocol_profile == "v1.03" and motor.get_motor_drive_enabled() is not True:
                    raise MotorSafetyError(f"{entry.interface}#{entry.motor_id} 未确认驱动器已使能")
                motor.set_motor_control_mode(self._motors_py.MotorControlMode.MIT)
        except Exception:
            for motor in reversed(initialized):
                try:
                    motor.deinit_motor()
                except Exception:
                    pass
            self.init_error_ids = errors
            raise
        self.init_error_ids = errors

    def _require_disabled_v103(self) -> None:
        if "v1.03" not in self._selected_protocols:
            return
        before = self._read_samples_impl()
        self._refresh_feedback_impl()
        for prior, sample in zip(before, self._read_samples_impl()):
            if sample.lro_protocol_profile == "v1.03" and (
                sample.drive_enabled is not False or sample.feedback_age_s > 0.1
                or sample.timestamp_s <= prior.timestamp_s
            ):
                raise MotorSafetyError(f"{sample.bus}#{sample.motor_id} 未收到新鲜的驱动失能反馈")

    def _close_transport_impl(self) -> None:
        first_error: Exception | None = None
        if self._versions is not None:
            self._versions.close()
        for motor in self._motors:
            try:
                motor.deinit_motor()
            except Exception as exc:
                first_error = first_error or exc
        self._motors = []
        if first_error is not None:
            raise MotorBackendError(f"failed to disable one or more motors: {first_error}")

    def _command_mit_impl(
        self,
        target: np.ndarray,
        *,
        torque: np.ndarray | None,
        kp_scale: float,
        kd_scale: float,
    ) -> None:
        self._check_protocol_conflicts()
        legacy_command_mit(
            self._motors,
            list(self.entries),
            target,
            torque=torque,
            kp_scale=kp_scale,
            kd_scale=kd_scale,
        )
        self._last_command[:] = target
        self._last_mit_command_at = time.monotonic()

    def _command_protective_damping_impl(self, kd: float) -> None:
        self._check_protocol_conflicts()
        count = len(self.entries)
        damping_entries = [replace(e, kp=0.0, kd=kd) for e in self.entries]
        legacy_command_mit(self._motors, damping_entries, np.zeros(count),
                           velocity=np.zeros(count), torque=np.zeros(count))
        self._last_command[:] = np.nan
        self._last_mit_command_at = time.monotonic()

    def _refresh_feedback_impl(self) -> None:
        if self._enabled:
            return

        pending: set[int] = set()
        for index, (entry, motor) in enumerate(zip(self.entries, self._motors)):
            # EVO's current SDK implementation of refresh_motor_status sends
            # an enable frame.  Never invoke it from this read-only path.
            if entry.motor_type.upper() not in self._READ_ONLY_REFRESH_TYPES:
                continue
            if entry.lro_protocol_profile is not None:
                self._select_verified_protocol(index)
            refresh = getattr(motor, "refresh_motor_status", None)
            feedback_count = self._feedback_count(entry, motor)
            if (
                refresh is None
                or (
                    feedback_count is None
                    and self._response_count(motor) is None
                )
            ):
                continue
            refresh()
            pending.add(index)

        deadline = time.monotonic() + self._FEEDBACK_QUERY_TIMEOUT_S
        while pending and time.monotonic() < deadline:
            received_at = time.monotonic()
            for index in tuple(pending):
                feedback_count = self._feedback_count(
                    self.entries[index], self._motors[index]
                )
                if (
                    feedback_count is not None
                    and feedback_count != self._last_feedback_counts[index]
                ):
                    self._last_feedback_counts[index] = feedback_count
                    self._last_feedback_at[index] = received_at
                    pending.remove(index)
                    continue
                response_count = self._response_count(self._motors[index])
                # motors_py increments this counter when requesting status and
                # resets it to zero only when that motor's feedback arrives.
                if feedback_count is None and response_count == 0:
                    self._last_response_counts[index] = 0
                    self._last_feedback_at[index] = received_at
                    pending.remove(index)
            if pending:
                time.sleep(0.001)

    def _read_samples_impl(self) -> list[MotorSample]:
        now = time.monotonic()
        result: list[MotorSample] = []
        for index, (entry, motor) in enumerate(zip(self.entries, self._motors)):
            feedback_count = self._feedback_count(entry, motor)
            response_count = self._response_count(motor)
            if (
                feedback_count is not None
                and feedback_count != self._last_feedback_counts[index]
            ):
                # LRO's receive callback increments a monotonic sequence for
                # every valid feedback frame. Unlike response_count, this
                # cannot be missed when commands are sent much faster than
                # Python samples status.
                self._last_feedback_counts[index] = feedback_count
                self._last_feedback_at[index] = now
            elif feedback_count is not None:
                pass
            elif response_count is None:
                # An absent/broken receive counter supplies no evidence of a
                # reply. Reading cached positions must not refresh its age.
                pass
            elif (
                self._enabled
                and response_count == 0
                and now - self._last_mit_command_at <= 0.1
            ):
                # The SDK increments every motor's counter when a batched MIT
                # command is sent and resets that motor's counter only when its
                # feedback frame arrives. A zero count after a recent command
                # therefore proves that this specific motor replied.
                self._last_response_counts[index] = response_count
                self._last_feedback_at[index] = now
            elif self._enabled:
                self._last_response_counts[index] = response_count
            feedback_at = float(self._last_feedback_at[index])
            is_lro = entry.motor_type.upper() == "LRO"
            profile = self._selected_protocols[index]
            if is_lro and self._versions is not None and (
                self._protocol_conflicts[index] or self._protocol_observations[index] is None
            ):
                profile = None
            v103 = profile == "v1.03"
            known_units = not is_lro or self._versions is None or profile is not None
            get_header = getattr(motor, "get_motor_feedback_header", None) if is_lro else None
            result.append(
                MotorSample(
                    pos_rad=float(motor.get_motor_pos()),
                    cmd_pos_rad=float(self._last_command[index]),
                    spd_rad_s=float(motor.get_motor_spd()) if known_units else float("nan"),
                    torque_nm=float(motor.get_motor_current()) if known_units and not v103 else float("nan"),
                    temp_c=float(motor.get_motor_temperature()),
                    error_id=int(motor.get_error_id()) if known_units else 0,
                    bus_voltage_v=_optional_float(motor, "get_motor_dc_bus_voltage") if known_units else float("nan"),
                    bus_current_a=_optional_float(motor, "get_motor_dc_bus_current") if known_units else float("nan"),
                    motor_id=int(entry.motor_id),
                    index=index,
                    joint_name=entry.joint_name,
                    bus=entry.interface,
                    timestamp_s=feedback_at,
                    feedback_age_s=max(0.0, now - feedback_at),
                    iq_current_a=_optional_float(motor, "get_motor_iq_current") if v103 else float("nan"),
                    drive_enabled=motor.get_motor_drive_enabled() if v103 else None,
                    feedback_header=get_header() if get_header is not None else None,
                    lro_protocol_profile=profile,
                    fault_code_available=known_units,
                )
            )
        return result

    @staticmethod
    def _response_count(motor: Any) -> int | None:
        getter = getattr(motor, "get_response_count", None)
        if getter is None:
            return None
        try:
            return int(getter())
        except Exception:
            return None

    @staticmethod
    def _feedback_count(entry: MotorEntry, motor: Any) -> int | None:
        if entry.motor_type.upper() != "LRO":
            return None
        getter = getattr(motor, "get_feedback_count", None)
        if getter is None:
            return None
        try:
            return int(getter())
        except Exception:
            return None

    def _zero_motor_impl(self, index: int, *, force_on_error: bool) -> bool:
        self._check_protocol_conflicts(index)
        if self.entries[index].lro_protocol_profile == "v1.03":
            raise MotorSafetyError("V1.03 标零必须使用支持待重新上电状态的接口")
        if (self.entries[index].lro_protocol_profile == "legacy" and self._versions is not None
                and not self._select_verified_protocol(index)):
            raise MotorSafetyError("LRO 旧协议身份未核实，不能标零")
        motor = self._motors[index]
        error_id = int(motor.get_error_id())
        if error_id != 0 and not force_on_error:
            raise MotorSafetyError(
                f"refusing to zero motor[{index}] while error_id={error_id}"
            )
        try:
            motor.unlock_motor()
        except Exception:
            pass
        time.sleep(0.05)
        if error_id:
            motor.clear_motor_error()
            time.sleep(0.05)
        result = bool(motor.set_motor_zero())
        time.sleep(0.05)
        return result

    def submit_motor_zero(self, index: int, *, force_on_error: bool = False) -> MotorZeroResult:
        with self._lock:
            self._require_connected()
            self._validate_index(index)
            index = int(index)
            self._check_protocol_conflicts(index)
            if self.entries[index].lro_protocol_profile != "v1.03":
                return super().submit_motor_zero(index, force_on_error=force_on_error)
            if self._enabled:
                raise MotorSafetyError("V1.03 标零要求工位驱动处于失能状态")
            if index in self._pending_motor_zero:
                raise MotorSafetyError("该节点已提交标零，等待重新上电和现场核验")
            if not self._select_verified_protocol(index):
                raise MotorSafetyError("未核实节点 V1.03 身份，不能标零")
            before = self._read_samples_impl()[index]
            self._refresh_feedback_impl()
            sample = self._read_samples_impl()[index]
            if (sample.drive_enabled is not False or sample.feedback_age_s > 0.1 or sample.error_id != 0
                    or sample.timestamp_s <= before.timestamp_s):
                raise MotorSafetyError("V1.03 标零要求新鲜的失能及无故障反馈")
            self._check_protocol_conflicts(index)
            self._pending_motor_zero.add(index)
            try:
                submitted = self._motors[index].request_lro_zero()
            except Exception as exc:
                raise MotorSafetyError("V1.03 标零发送状态不明，需在现场核验") from exc
            if not submitted:
                self._pending_motor_zero.remove(index)
                return MotorZeroResult.UNCONFIRMED
            return MotorZeroResult.SUBMITTED_PENDING_REBOOT

    def _clear_errors_impl(self, index: int | None) -> None:
        motors = self._motors if index is None else [self._motors[index]]
        for motor in motors:
            motor.clear_motor_error()

    def _disable_impl(self, index: int | None) -> None:
        motors = self._motors if index is None else [self._motors[index]]
        for motor in motors:
            try:
                motor.unlock_motor()
            except Exception:
                motor.deinit_motor()


# Descriptive alias used by callers selecting a platform backend.
LinuxMotorsPyBackend = MotorsPyBackend


class _FakeMotorHandle:
    def __init__(self, backend: "FakeMotorBackend", index: int) -> None:
        self.backend = backend
        self.index = index

    def refresh_motor_status(self) -> None:
        return None

    def get_motor_pos(self) -> float:
        return float(self.backend.positions[self.index])

    def get_motor_cmd_pos(self) -> float:
        return float(self.backend.commanded[self.index])

    def get_motor_spd(self) -> float:
        return float(self.backend.speeds[self.index])

    def get_motor_current(self) -> float:
        return float(self.backend.torques[self.index])

    def get_motor_temperature(self) -> float:
        return float(self.backend.temperatures[self.index])

    def get_motor_dc_bus_voltage(self) -> float:
        return float(self.backend.bus_voltages[self.index])

    def get_motor_dc_bus_current(self) -> float:
        return float(self.backend.bus_currents[self.index])

    def get_error_id(self) -> int:
        return int(self.backend.error_ids[self.index])


def _fake_entries(count: int) -> tuple[MotorEntry, ...]:
    return tuple(
        MotorEntry(
            index=index,
            motor_id=index + 1,
            interface_type="fake",
            interface="fake0",
            motor_type="FAKE",
            motor_model=0,
            master_id_offset=0,
            zero_offset=0.0,
            kp=1.0,
            kd=0.1,
            sign=1.0,
            joint_name=f"motor_{index}_joint",
        )
        for index in range(count)
    )


class FakeMotorBackend(MotorBackend):
    """In-memory backend with fault and stale-feedback injection hooks."""

    def __init__(
        self,
        entries: Iterable[MotorEntry] | int,
        *,
        initial_positions: Iterable[float] | None = None,
        response_ratio: float = 1.0,
    ) -> None:
        actual_entries = _fake_entries(entries) if isinstance(entries, int) else tuple(entries)
        super().__init__(actual_entries)
        count = len(self.entries)
        initial = (
            np.zeros(count, dtype=np.float64)
            if initial_positions is None
            else np.asarray(list(initial_positions), dtype=np.float64).reshape(-1)
        )
        if initial.shape != (count,) or not np.all(np.isfinite(initial)):
            raise ValueError(f"initial_positions must contain {count} finite values")
        if not math.isfinite(float(response_ratio)) or not 0.0 <= response_ratio <= 1.0:
            raise ValueError("response_ratio must be in [0, 1]")
        self.response_ratio = float(response_ratio)
        self.positions = initial.copy()
        self.commanded = initial.copy()
        self.speeds = np.zeros(count, dtype=np.float64)
        self.torques = np.zeros(count, dtype=np.float64)
        self.temperatures = np.full(count, 25.0, dtype=np.float64)
        self.bus_voltages = np.full(count, 48.0, dtype=np.float64)
        self.bus_currents = np.zeros(count, dtype=np.float64)
        self.error_ids = np.zeros(count, dtype=np.int64)
        self.enabled = np.zeros(count, dtype=np.bool_)
        self.stale = np.zeros(count, dtype=np.bool_)
        self.feedback_timestamps = np.zeros(count, dtype=np.float64)
        self.command_history: list[dict[str, Any]] = []
        self.zeroed_indices: list[int] = []
        self._motors = [
            _FakeMotorHandle(self, index) for index in range(len(self.entries))
        ]
        self._command_condition = threading.Condition(self._lock)

    @property
    def motors(self) -> list[Any]:
        return self._motors

    def _open_transport_impl(self) -> None:
        now = time.monotonic()
        self.enabled[:] = False
        self.feedback_timestamps[:] = now

    def _enable_motors_impl(self) -> None:
        self.enabled[:] = True

    def _refresh_feedback_impl(self) -> None:
        now = time.monotonic()
        self.feedback_timestamps[~self.stale] = now

    def _close_transport_impl(self) -> None:
        self.enabled[:] = False

    def _command_mit_impl(
        self,
        target: np.ndarray,
        *,
        torque: np.ndarray | None,
        kp_scale: float,
        kd_scale: float,
    ) -> None:
        if not np.all(self.enabled):
            raise MotorSafetyError("MIT command rejected while a fake motor is disabled")
        now = time.monotonic()
        old = self.positions.copy()
        self.commanded[:] = target
        for index in range(len(self.entries)):
            if not self.stale[index]:
                self.positions[index] += (
                    target[index] - self.positions[index]
                ) * self.response_ratio
                self.feedback_timestamps[index] = now
        self.speeds[:] = self.positions - old
        self.torques[:] = 0.0 if torque is None else torque
        self.command_history.append(
            {
                "timestamp_s": now,
                "target": target.copy(),
                "torque": self.torques.copy(),
                "kp_scale": float(kp_scale),
                "kd_scale": float(kd_scale),
            }
        )
        self._command_condition.notify_all()

    def _command_protective_damping_impl(self, kd: float) -> None:
        if not np.all(self.enabled):
            raise MotorSafetyError("protective damping rejected while a fake motor is disabled")
        now = time.monotonic()
        self.commanded[:] = np.nan
        self.torques[:] = -kd * self.speeds
        self.feedback_timestamps[~self.stale] = now
        if len(self.command_history) >= 4096:
            del self.command_history[:len(self.command_history)-4095]
        self.command_history.append({"timestamp_s": now, "mode": "protective_damping",
            "target": np.zeros(len(self.entries)), "velocity": np.zeros(len(self.entries)),
            "kp": np.zeros(len(self.entries)), "kd": np.full(len(self.entries), kd),
            "feedforward_torque": np.zeros(len(self.entries))})
        self._command_condition.notify_all()

    def _read_samples_impl(self) -> list[MotorSample]:
        now = time.monotonic()
        return [
            MotorSample(
                pos_rad=float(self.positions[index]),
                cmd_pos_rad=float(self.commanded[index]),
                spd_rad_s=float(self.speeds[index]),
                torque_nm=float(self.torques[index]),
                temp_c=float(self.temperatures[index]),
                error_id=int(self.error_ids[index]),
                bus_voltage_v=float(self.bus_voltages[index]),
                bus_current_a=float(self.bus_currents[index]),
                motor_id=int(entry.motor_id),
                index=index,
                joint_name=entry.joint_name,
                bus=entry.interface,
                timestamp_s=float(self.feedback_timestamps[index]),
                feedback_age_s=max(
                    0.0, now - float(self.feedback_timestamps[index])
                ),
            )
            for index, entry in enumerate(self.entries)
        ]

    def _zero_motor_impl(self, index: int, *, force_on_error: bool) -> bool:
        error_id = int(self.error_ids[index])
        if error_id != 0 and not force_on_error:
            raise MotorSafetyError(
                f"refusing to zero motor[{index}] while error_id={error_id}"
            )
        self.positions[index] = 0.0
        self.commanded[index] = 0.0
        self.zeroed_indices.append(index)
        self.feedback_timestamps[index] = time.monotonic()
        return True

    def _clear_errors_impl(self, index: int | None) -> None:
        if index is None:
            self.error_ids[:] = 0
        else:
            self.error_ids[index] = 0

    def disable_feedback(self) -> str:
        with self._lock:
            return "confirmed_mock" if not self._enabled and not self.enabled.any() else "not_disabled_mock"

    def _disable_impl(self, index: int | None) -> None:
        if index is None:
            self.enabled[:] = False
        else:
            self.enabled[index] = False

    def enable(self, index: int | None = None) -> None:
        with self._lock:
            self._require_connected()
            if index is None:
                self.enabled[:] = True
            else:
                self._validate_index(index)
                self.enabled[index] = True

    def inject_error(self, index: int, error_id: int) -> None:
        with self._lock:
            self._validate_index(index)
            self.error_ids[index] = int(error_id)

    def set_stale(self, indices: int | Iterable[int], stale: bool = True) -> None:
        selected = [indices] if isinstance(indices, int) else list(indices)
        with self._lock:
            for index in selected:
                self._validate_index(index)
                self.stale[index] = bool(stale)

    def wait_for_commands(self, count: int, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + float(timeout)
        with self._command_condition:
            while len(self.command_history) < int(count):
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._command_condition.wait(remaining)
            return True


__all__ = [
    "BackendInUseError",
    "BackendNotConnectedError",
    "FakeMotorBackend",
    "LinuxMotorsPyBackend",
    "MotorBackend",
    "MotorBackendError",
    "MotorSafetyError",
    "MotorsPyBackend",
]
