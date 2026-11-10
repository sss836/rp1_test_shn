"""Private process boundary for a single station's controller and lease.

The pipes are inherited only by the gateway and its child. HTTP clients see
the existing gateway routes; they never send or receive pickled objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import logging
import multiprocessing
import os
from pathlib import Path
import pickle
import signal
import socket
import stat
import struct
import threading
import time

from .lease import LeaseConflict

logger = logging.getLogger(__name__)

_CONTROL = frozenset({
    "configure", "clear_configuration", "can_connect", "can_disconnect", "can_recover",
    "connect", "discover_motors", "enable_motors", "disable_motors", "disconnect",
    "import_trajectory", "arm", "start", "pause", "resume", "finish", "stop", "disable",
    "prepare_next_run", "manual_move", "manual_stop", "reset", "reset_motor_fault",
    "zero_motor", "zero_all_motors", "zero_prepare", "zero_read", "zero_confirm", "zero_skip", "zero_abort",
})
_RECORDS = frozenset({
    "record_file", "start_receipt", "execution_history_page", "execution_location",
    "execution_local_corrections", "correct_execution_locally", "execution_summary",
    "execution_analysis_versions", "execution_report_file", "execution_bundle_file",
    "rebuild_report", "submit_execution", "retry_submission", "import_history", "sync_status",
    "adopt_platform_execution_code", "preview_record_identity",
})
_READS = frozenset({"snapshot", "display_snapshot", "collect_display_snapshot", "can_status", "can_interfaces",
                   "maintenance_targets", "maintenance_ready"})
_SAFETY = frozenset({"request_motion_stop", "protective_damping_snapshot"})
_LEASE = frozenset({"claim", "refresh", "release", "snapshot", "expired"})
_MAX_MESSAGE = 32 * 1024 * 1024
_RECOVERY_COMMANDS = frozenset({"stop", "disable", "disable_motors", "disconnect", "can_disconnect", "manual_stop"})


class StationUnavailable(RuntimeError):
    pass


class StationOutcomeUnknown(StationUnavailable):
    pass


_ERROR_TYPES = {error.__name__: error for error in
                (LeaseConflict, ValueError, KeyError, TypeError, FileNotFoundError, PermissionError,
                 TimeoutError, OSError, RuntimeError)}


def _encode(value):
    data = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    if len(data) > _MAX_MESSAGE:
        raise ValueError("工位进程消息超过大小限制")
    return struct.pack("!I", len(data)) + data


def _send(connection, value) -> None:
    connection.sendall(_encode(value))


def _receive(connection, buffer, deadline=None):
    target = 4
    while True:
        while len(buffer) < target:
            if deadline is not None:
                connection.settimeout(max(0, deadline - time.monotonic()))
            chunk = connection.recv(min(65536, target - len(buffer)))
            if not chunk:
                raise EOFError("station socket closed")
            buffer.extend(chunk)
        if target > 4:
            data = pickle.loads(buffer[4:])
            buffer.clear()
            return data
        size, = struct.unpack_from("!I", buffer)
        if not 0 < size <= _MAX_MESSAGE:
            raise ValueError("工位进程消息大小无效")
        target = 4 + size


class _LocalPlatform:
    def snapshot(self):
        return {"uploader_running": False}


def _run_station(channels, ready, data_root, report_root, outbox_path, lease_timeout_s):
    from factory_hmi.core.config import load_station_config
    from factory_hmi.core.service import FactoryService
    from factory_hmi.plc.process import PlcCabinetClient
    from factory_hmi.sync.outbox import Outbox
    from .app import GatewayRuntime
    from .quick_run import require_plc_power

    # Keep the device process alive until its own safe close has succeeded.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    outbox = Outbox(outbox_path, recover_interrupted=False)
    service = FactoryService(data_root=data_root, report_root=report_root, outbox=outbox)
    released = threading.Event()
    finished = threading.Event()
    close_lock = threading.Lock()
    permit_lock = threading.Lock()
    can_claims = {}
    permit = None

    def release_can(keep=()):
        for key in set(can_claims).difference(keep):
            os.close(can_claims.pop(key))

    def reserve_can(backend, interfaces):
        if backend == "fake":
            root = Path(report_root) / "can-locks"
            root.mkdir(parents=True, exist_ok=True)
        else:
            root = Path("/tmp")
        previous = set(can_claims)
        wanted = {(backend, interface) for interface in interfaces}
        try:
            for key in sorted(wanted.difference(previous)):
                digest = hashlib.sha256(key[1].encode()).hexdigest()[:24]
                path = root / f"rp1-hmi-can-{digest}.lock"
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                try:
                    metadata = os.fstat(fd)
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
                        raise LeaseConflict(f"CAN 接口 {key[1]} 的锁不属于当前服务账号")
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except Exception as exc:
                    os.close(fd)
                    if isinstance(exc, BlockingIOError):
                        raise LeaseConflict(f"CAN 接口 {key[1]} 已由另一个工位占用") from exc
                    raise
                can_claims[key] = fd
        except Exception:
            release_can(previous)
            raise
        return wanted

    def clear_permit():
        nonlocal permit
        with permit_lock:
            permit = None

    def confirm_release():
        clear_permit()
        release_can()
        released.set()

    runtime = GatewayRuntime(service, lease_timeout_s=lease_timeout_s, data_root=data_root,
                             outbox=outbox, platform=_LocalPlatform(), expiry_callback=confirm_release)
    transport = None

    def owns_current_can():
        if not can_claims:
            return False
        required = {(service._backend_name, interface) for interface in service._can_interfaces}
        if set(can_claims) != required:
            raise LeaseConflict("当前 CAN 配置与工位持有的设备锁不一致，禁止自动下线")
        return True

    def cleanup_station(reason="operator lease expired"):
        if transport is not None:
            runtime._cleanup_error = "工位维护传输未确认释放，保留 CAN 占用"
            return False
        if service._controller is None and not can_claims:
            runtime.lease.release()
            confirm_release()
            return True
        return runtime._safe_release(reason=reason, disconnect_can=owns_current_can())

    def close_station():
        service.request_motion_stop()
        with close_lock, runtime._lease_guard:
            if transport is not None:
                raise RuntimeError("工位维护传输尚未安全释放")
            runtime.close(disconnect_can=owns_current_can())
            clear_permit()
            release_can()

    def watch_safety():
        while not finished.wait(.1):
            reason = None
            revoked = runtime.lease.revoke_expired()
            if revoked is not None:
                reason = "operator lease expired"
                generation = revoked.generation
            with permit_lock:
                current = permit
            if reason is None and current is not None:
                client, permit_channels, generation = current
                try:
                    require_plc_power(client.snapshot(include_history=False), permit_channels)
                except Exception:
                    with permit_lock:
                        if permit is current and runtime.lease.revoke(generation=generation):
                            reason = "PLC supply permit lost"
            if reason is None:
                continue
            logger.warning("Station control revoked: %s", reason)
            service.request_motion_stop()
            with runtime._lease_guard:
                lease = runtime.lease.snapshot()
                if not finished.is_set() and lease.revoked and lease.generation == generation:
                    try:
                        cleanup_station(reason)
                    except Exception as exc:
                        runtime._cleanup_error = str(exc)
                        logger.exception("Station safety cleanup not confirmed")

    def execute(channel, name, args, kwargs):
        nonlocal permit, transport
        if channel == "lease":
            if name == "status":
                return runtime.lease_snapshot(), released.is_set()
            if name == "clear_release_notice":
                released.clear()
                return None
            if name in _LEASE:
                if name == "claim" and runtime._cleanup_error is not None:
                    raise LeaseConflict("工位清理未确认")
                return getattr(runtime.lease, name)(*args, **kwargs)
        elif channel == "safety":
            if name in _SAFETY:
                return getattr(service, name)(*args, **kwargs)
            if name == "close":
                close_station()
                return None
            if name == "recover":
                with runtime._lease_guard:
                    if not cleanup_station():
                        raise LeaseConflict(runtime._cleanup_error)
                return True
            if name == "disable_protection":
                with runtime._lease_guard:
                    service.disable_motors()
                    runtime._cleanup_job = None
                    if not cleanup_station():
                        raise LeaseConflict(runtime._cleanup_error)
                return True
        elif channel == "control":
            if name == "watch_plc":
                client = PlcCabinetClient.attach(args[0])
                permit_channels, generation = args[1:]
                require_plc_power(client.snapshot(include_history=False), permit_channels)
                with permit_lock:
                    permit = (client, permit_channels, generation)
                return None
            if name in _CONTROL:
                generation, args = args[0], args[1:]
                with runtime._lease_guard:
                    lease = runtime.lease.snapshot()
                    if lease.owner is None or lease.revoked or lease.expired or lease.generation != generation:
                        raise LeaseConflict("工位控制租约已失效")
                    if runtime._cleanup_error is not None or transport is not None:
                        raise LeaseConflict("工位正在清理或维护")
                    if name == "configure":
                        previous = set(can_claims)
                        if previous and any(row["up"] for row in service.can_status()["interfaces"]):
                            raise LeaseConflict("重新配置前须确认当前 CAN 接口全部断开")
                        config = load_station_config(kwargs["limb"], Path(kwargs["config_path"]),
                                                     bus_bindings=kwargs.get("bus_bindings"),
                                                     motor_overrides=kwargs.get("motor_overrides"))
                        wanted = reserve_can(kwargs["backend"], (entry.interface for entry in config.entries
                                             if "can" in entry.interface_type.lower()))
                        try:
                            result = service.configure(*args, station_config=config, **kwargs)
                        except Exception:
                            release_can(previous)
                            raise
                        release_can(wanted)
                        return result
                    if name in {"can_connect", "can_recover", "connect", "discover_motors", "enable_motors",
                                "can_disconnect", "clear_configuration"} and service._can_interfaces:
                        reserve_can(service._backend_name, service._can_interfaces)
                    result = getattr(service, name)(*args, **kwargs)
                    if name in {"can_disconnect", "clear_configuration"}:
                        confirm_release()
                    return result
            if name == "maintenance_open":
                with runtime._lease_guard, service._lifecycle_lock:
                    service.maintenance_ready()
                    if transport is not None:
                        raise RuntimeError("工位维护已占用设备")
                    reserve_can(service._backend_name, service._can_interfaces)
                    transport = service.controller.backend.maintenance_transport()
                    transport.__enter__()
                return None
            if name == "maintenance_close":
                with runtime._lease_guard, service._lifecycle_lock:
                    if transport is not None:
                        transport.close()
                        transport = None
                return None
        elif channel == "records" and name in _RECORDS:
            return getattr(service, name)(*args, **kwargs)
        elif channel == "read":
            if name in _READS:
                return getattr(service, name)(*args, **kwargs)
            if name == "controller_state":
                return service.controller.state
            if name == "motor_versions":
                return service.controller.backend.motor_versions()
            if name in {"preview_run", "preview_read", "preview_capabilities"}:
                cache = service.controller.telemetry_hub
                if name == "preview_run":
                    return cache.run_id
                if name == "preview_capabilities":
                    return cache.capabilities()
                return cache.read(*args, **kwargs)
            if name == "validate_mujoco_return":
                from factory_hmi.core.mujoco_completion import plan_return
                controller = service.controller
                trajectory = controller.trajectory
                if trajectory is None:
                    raise ValueError("尚未载入工厂轨迹")
                velocity = (trajectory.motor_pos[-1] - trajectory.motor_pos[-2]) / (
                    trajectory.timestamps[-1] - trajectory.timestamps[-2])
                plan_return(controller.config, trajectory.motor_pos[-1], velocity)
                return None
        raise ValueError("不支持的工位进程操作")

    def serve(channel, connection):
        buffer = bytearray()
        try:
            while True:
                name, args, kwargs = _receive(connection, buffer)
                try:
                    result = execute(channel, name, args, kwargs)
                    _send(connection, ("ok", result))
                except Exception as exc:
                    kind = next((name for name, error in _ERROR_TYPES.items() if isinstance(exc, error)), "RuntimeError")
                    _send(connection, (kind, str(exc)))
                else:
                    if name == "close" and channel == "safety":
                        finished.set()
                        return
        except (EOFError, OSError):
            # The inherited lease pipe disappearing means the gateway has
            # gone away. The device still owns its physical shutdown outcome.
            if channel == "lease" and not finished.is_set():
                try:
                    lease = runtime.lease.snapshot()
                    if lease.owner is not None:
                        runtime.lease.revoke(generation=lease.generation)
                        service.request_motion_stop()
                        with runtime._lease_guard:
                            if not cleanup_station("gateway connection lost"):
                                logger.error("Orphan station remains isolated: %s", runtime._cleanup_error)
                                return
                    close_station()
                    finished.set()
                except Exception:
                    logger.exception("Orphan station safe shutdown could not be confirmed")
        finally:
            connection.close()

    runtime.trajectory_root.mkdir(parents=True, exist_ok=True)
    runtime.config_root.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_safety, name="station-safety-watch", daemon=True).start()
    for name, connection in channels.items():
        threading.Thread(target=serve, args=(name, connection), name=f"station-{name}", daemon=True).start()
    ready.set()
    finished.wait()


@dataclass
class _Channel:
    connection: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: str | None = None
    buffer: bytearray = field(default_factory=bytearray)

    def call(self, name, args, kwargs, *, timeout):
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=min(timeout, 0.5)):
            raise StationUnavailable("工位通道繁忙，请核对当前状态")
        try:
            if self.pending is not None:
                try:
                    status, result = _receive(self.connection, self.buffer, time.monotonic())
                except BlockingIOError as exc:
                    raise StationOutcomeUnknown("工位仍在处理此前请求，结果未确认") from exc
                previous, self.pending = self.pending, None
                if name == previous == "close" and status == "ok":
                    return result
            data = _encode((name, args, kwargs))
            self.connection.settimeout(max(0, deadline - time.monotonic()))
            self.pending = name
            try:
                self.connection.sendall(data)
            except OSError:
                # A partial request can never be followed by another frame.
                self.connection.close()
                raise
            status, result = _receive(self.connection, self.buffer, deadline)
            self.pending = None
        except TimeoutError as exc:
            raise StationOutcomeUnknown("工位进程请求超时，结果未确认") from exc
        except (EOFError, OSError) as exc:
            raise StationOutcomeUnknown("工位进程失联，设备状态未确认") from exc
        finally:
            self.lock.release()
        if status != "ok":
            error = _ERROR_TYPES.get(status, RuntimeError)
            raise error(result)
        return result


class _RemoteLease:
    def __init__(self, station):
        self.station = station

    def __getattr__(self, name):
        if name not in _LEASE:
            raise AttributeError(name)
        return lambda *args, **kwargs: self.station._call("lease", name, *args, **kwargs)


class _MaintenanceHandle:
    def __init__(self, station):
        self.station = station

    def __enter__(self):
        self.station._call("control", "maintenance_open")
        return self

    def close(self):
        self.station._call("control", "maintenance_close")


class _ControllerView:
    def __init__(self, station):
        self.station = station
        self.backend = self

    @property
    def state(self):
        return self.station._call("read", "controller_state")

    def snapshot(self):
        return self.station.display_snapshot()

    def motor_versions(self):
        return self.station._call("read", "motor_versions")

    def maintenance_transport(self):
        return _MaintenanceHandle(self.station)


class _PreviewView:
    def __init__(self, station):
        self.station = station

    @property
    def run_id(self):
        return self.station._call("read", "preview_run")

    def read(self, **kwargs):
        return self.station._call("read", "preview_read", **kwargs)

    def capabilities(self):
        return self.station._call("read", "preview_capabilities")


class StationProcess:
    def __init__(self, data_root: Path, report_root: Path, outbox, lease_timeout_s: float):
        self.data_root = data_root
        self.report_root = report_root
        self.outbox = outbox
        self.lease = _RemoteLease(self)
        self.controller = _ControllerView(self)
        self.telemetry_hub = _PreviewView(self)
        self._maintenance = self._parameters = None
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._close_confirmed = False
        self._control_unknown = False
        self._startup_lock = threading.Lock()
        self._lease_timeout_s = lease_timeout_s
        self._report_manager = None
        self._process = None
        self._channels = {}

    @property
    def pid(self):
        return self._process.pid if self._process else None

    def _start(self):
        with self._startup_lock:
            self._require_open()
            if self._process is not None:
                if not self._process.is_alive():
                    raise StationUnavailable("工位进程已退出，设备状态未确认")
                return
            ctx = multiprocessing.get_context("spawn")
            endpoints = {name: socket.socketpair() for name in ("control", "safety", "lease", "read", "records")}
            ready = ctx.Event()
            process = ctx.Process(target=_run_station, name="rp1-hmi-station", args=(
                {name: pair[1] for name, pair in endpoints.items()}, ready, self.data_root, self.report_root,
                self.outbox.path, self._lease_timeout_s))
            try:
                process.start()
            except BaseException:
                for pair in endpoints.values():
                    for connection in pair:
                        connection.close()
                raise
            self._process = process
            self._channels = {name: _Channel(pair[0]) for name, pair in endpoints.items()}
            for pair in endpoints.values():
                pair[1].close()
            if not ready.wait(8):
                raise StationUnavailable("工位进程初始化未确认")

    def _call(self, channel, name, *args, **kwargs):
        self._start()
        timeout = {"read": 1.0, "lease": 0.3, "safety": 8.0, "control": 120.0, "records": 120.0}[channel]
        try:
            return self._channels[channel].call(name, args, kwargs, timeout=timeout)
        except StationOutcomeUnknown:
            if channel == "control":
                self._control_unknown = True
            raise

    def __getattr__(self, name):
        if name in _CONTROL:
            def control(*args, **kwargs):
                if self.close_pending:
                    raise StationUnavailable("工位关闭结果未确认，请重新核对关闭结果")
                if self._control_unknown and name not in _RECOVERY_COMMANDS:
                    raise StationUnavailable("上一条工位指令结果未确认，请先执行安全恢复")
                if any(task.active for task in self.maintenance_tasks()):
                    raise RuntimeError("模块固件或参数维护占用当前实例")
                return self._call("control", name, self.lease.snapshot().generation, *args, **kwargs)
            return control
        for names, channel in ((_READS, "read"), (_RECORDS, "records"), (_SAFETY, "safety")):
            if name in names:
                return lambda *args, **kwargs: self._call(channel, name, *args, **kwargs)
        raise AttributeError(name)

    def _require_open(self):
        if self._closing:
            raise StationUnavailable("工位正在关闭")

    def maintenance_tasks(self):
        return tuple(task for task in (self._maintenance, self._parameters) if task is not None)

    @property
    def report_manager(self):
        if self._report_manager is None:
            from factory_hmi.core.report import ReportManager
            self._report_manager = ReportManager(self.report_root)
        return self._report_manager

    def _annotate_execution_history(self, items):
        from factory_hmi.core.service import FactoryService
        FactoryService._annotate_execution_history(self, items)

    def iter_execution_history(self, **filters):
        from factory_hmi.core.service import FactoryService
        return FactoryService.iter_execution_history(self, **filters)

    def validate_mujoco_return(self):
        return self._call("read", "validate_mujoco_return")

    def lease_status(self):
        value, released = self._call("lease", "status")
        if self._control_unknown and not value.get("cleanup_error"):
            value.update(quarantined=True, cleanup_error="上一条工位指令结果未确认，请先执行安全恢复")
        return value, released

    def clear_release_notice(self):
        self._call("lease", "clear_release_notice")

    def recover(self):
        result = self._call("safety", "recover")
        self._control_unknown = False
        return result

    def disable_protection(self):
        result = self._call("safety", "disable_protection")
        self._control_unknown = False
        return result

    def watch_plc(self, info, channels):
        return self._call("control", "watch_plc", info, tuple(channels), self.lease.snapshot().generation)

    @property
    def close_pending(self):
        channel = self._channels.get("safety")
        return self._close_confirmed or (channel is not None and channel.pending == "close")

    def close(self):
        with self._lifecycle_lock:
            for task in self.maintenance_tasks():
                if task.active:
                    task.cancel()
                    if task.active:
                        raise RuntimeError("工位维护尚未退出")
            if self._process is None:
                self._closing = True
                return
            if not self._close_confirmed:
                if not self._process.is_alive() and not self.close_pending:
                    raise StationUnavailable("工位进程已退出，设备状态未确认")
                self._channels["safety"].call("close", (), {}, timeout=8)
                self._close_confirmed = True
            self._process.join(2)
            if self._process.is_alive():
                raise StationUnavailable("工位进程退出未确认")
            self._closing = True
            for channel in self._channels.values():
                channel.connection.close()
