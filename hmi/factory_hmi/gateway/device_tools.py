"""Peripheral routes and shared-arm CAN admission; no arm commands here."""
from __future__ import annotations

import os
from pathlib import Path
from .station_process import lock_can_interface
import math
import time
from typing import Literal
import uuid

from fastapi import Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from factory_hmi.core.can_manager import CanManager
from factory_hmi.core.device_tools import DeviceTools


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    generation: str | None = None
    interface: str = Field(default="can2", pattern=r"^can[0-9]+$")
    backend: Literal["hardware", "mock"] = "hardware"
    follow_arm: bool = True
    node_id: int = Field(default=1, ge=1, le=127, strict=True)
    speed: int = Field(default=500, ge=10, le=20000, multiple_of=10, strict=True)
    current_limit: int = Field(default=200, ge=1, le=1000, strict=True)
    amplitude: int = Field(default=300, ge=1, le=2000, strict=True)
    cycles: int = Field(default=1, ge=1, le=10, strict=True)
    axis: int = Field(default=0, ge=-1, le=5, strict=True)
    duration_s: float = Field(default=5, ge=1, le=60)


class DeviceAccess:
    def __init__(self, hub):
        self.hub = hub
        self.file_locks = {}
        self.claims = {}
        self.cache = {}
        self.tools = DeviceTools(hub.data_root / "device-tools", self.acquire, self.release, self.arm_guard)

    def acquire(self, kind, owner, cfg):
        # Never wait under the device status lock behind a long arm command.
        shared = kind == "dexhand" and cfg["follow_arm"]
        runtime = self.hub.session(owner, create=False) if shared else None
        if shared and not runtime._lease_guard.acquire(blocking=False):
            raise RuntimeError("当前手臂正在接受控制命令，请稍后再连接设备")
        try:
            if kind == "dexhand" and cfg["backend"] == "hardware" and os.environ.get("RP1_FACTORY_CAN_DISABLED") == "1":
                raise RuntimeError("离线实例禁止访问真实灵巧手")
            if shared:
                runtime.lease.refresh(owner)
                snap = runtime._invoke(("snapshot", "get_snapshot"))
                motors = snap.get("configured_motors", [])
                buses = {m.get("bus") for m in motors}
                if (len(motors) != 7 or len(buses) != 1 or None in buses
                        or {m.get("motor_type") for m in motors} != {"LRO"}
                        or {m.get("motor_id") for m in motors} != set(range(1, 8))
                        or (snap.get("station") or {}).get("limb") not in {"left_arm", "right_arm"}):
                    raise RuntimeError("共线测试要求当前窗口已配置单 CAN、ID 1..7 的 LRO 七自由度手臂")
                if (snap.get("backend") == "fake") != (cfg["backend"] == "mock"):
                    raise RuntimeError("手臂与灵巧手必须同时使用真实设备，或同时使用模拟设备")
                cfg.update(interface=next(iter(buses)), config_revision=snap.get("config_revision"),
                           arm_positions=[m.get("position_rad") for m in snap.get("motors", [])])
                self.arm_guard(owner, cfg, fresh=True)
            elif kind == "dexhand" and cfg["backend"] != "mock":
                raise RuntimeError("真实灵巧手必须跟随当前七自由度手臂；独立模式仅供离线模拟")
            if kind == "imu" and cfg["backend"] != "hardware":
                raise ValueError("IMU 模拟仅通过测试注入，不连接伪装为硬件的设备")
            token = "device:" + str(uuid.uuid4())
            interface = cfg["interface"]
            with self.hub._lock:
                current = self.hub._interface_owners.get(interface)
                if shared:
                    if current != self.hub._key(owner):
                        raise RuntimeError("手臂 CAN 所有权已变化，请重新配置当前窗口")
                elif cfg["backend"] != "mock" and current is not None:
                    raise RuntimeError(f"{interface} 已被工位占用，请为 IMU 选择空闲接口")
                if cfg["backend"] != "mock" and not shared:
                    self.file_locks[token] = lock_can_interface(Path("/tmp"), interface)
                    self.hub._interface_owners[interface] = token
                self.claims[token] = (owner, interface, shared)
            # Reads only. In shared mode even a wrong bitrate must not cause
            # down/up or timing changes underneath a holding arm.
            try:
                if kind == "dexhand" and cfg["backend"] == "hardware":
                    status = CanManager().status([interface])[0]
                    if not (status.exists and status.up and status.fd and status.bitrate == 1000000
                            and status.dbitrate == 5000000 and status.bus_state != "BUS-OFF"):
                        raise RuntimeError("手臂 CAN 必须已运行 CAN FD 1 Mbps / 5 Mbps；灵巧手不会重配共享总线")
            except Exception:
                self.release(token)
                raise
            return cfg, token
        finally:
            if shared:
                runtime._lease_guard.release()

    def release(self, token):
        with self.hub._lock:
            descriptor = self.file_locks.pop(token, None)
            if descriptor is not None:
                os.close(descriptor)
            claim = self.claims.pop(token, None)
            if claim and claim[2]:
                self.cache.pop(claim[0], None)
            if claim and self.hub._interface_owners.get(claim[1]) == token:
                self.hub._interface_owners.pop(claim[1])

    def borrowed(self, owner):
        with self.hub._lock:
            return any(v[0] == owner and v[2] for v in self.claims.values())

    def check_arm_command(self, owner, path):
        if not self.borrowed(owner):
            return
        if path in {"/api/v1/motors/disable", "/api/v1/playback/disable", "/api/v1/manual/stop",
                    "/api/v1/playback/stop", "/api/v1/quick/abort", "/api/v1/lease/protective-stop/disable"}:
            self.tools.stop_owner(owner)
            return
        raise RuntimeError("灵巧手共用当前手臂 CAN；请先停止并断开灵巧手，再改变手臂配置或运动")

    def arm_guard(self, owner, cfg, *, fresh=False):
        if not cfg.get("follow_arm"):
            return
        now = time.monotonic()
        cached = self.cache.get(owner)
        if fresh or cached is None or now - cached[0] > 0.1:
            runtime = self.hub.session(owner, create=False)
            lease = runtime.lease.snapshot()
            if lease.owner != owner or lease.revoked or lease.expired:
                raise RuntimeError("手臂控制权已失效，停止灵巧手")
            if runtime.quick is not None and runtime.quick.snapshot().get("active"):
                raise RuntimeError("请先结束工厂快速验证，再测试末端灵巧手")
            snap = runtime._invoke(("snapshot", "get_snapshot"))
            self.cache[owner] = (now, snap)
        else:
            snap = cached[1]
        station = snap.get("station") or {}
        if snap.get("config_revision") != cfg["config_revision"]:
            raise RuntimeError("手臂配置发生变化，停止灵巧手")
        if (not station.get("motors_enabled") or not station.get("position_hold_active")
                or station.get("manual_control_active") or snap.get("state") in {"running", "armed", "stopping"}
                or (station.get("protective_damping") or {}).get("active")
                or (snap.get("motor_maintenance") or {}).get("active")
                or (snap.get("motor_parameters") or {}).get("active")
                or snap.get("fault") or station.get("commands_inhibited")):
            raise RuntimeError("灵巧手测试要求手臂已使能并静止保持；当前手臂状态不满足")
        motors = snap.get("motors", [])
        baseline = cfg["arm_positions"]
        if len(motors) != 7 or len(baseline) != 7:
            raise RuntimeError("手臂七轴反馈不完整")
        for m, position in zip(motors, baseline):
            age, velocity, current = m.get("feedback_age_s"), m.get("speed_rad_s"), m.get("position_rad")
            if (any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in (age, velocity, current, position))
                    or not 0 <= age < 0.2 or abs(velocity) > 0.03 or abs(current-position) > 0.03
                    or m.get("error_id", 0)):
                raise RuntimeError("手臂反馈过期、故障或位置变化，停止灵巧手")


def register_device_routes(app, hub, require_token, owner_id, monitor_only):
    access = hub.device_access

    def authenticate(request, client_id, token, *, control=False):
        require_token(token)
        if control and monitor_only:
            raise HTTPException(403, "监控实例禁止控制设备")
        return owner_id(request, client_id)

    @app.get("/api/v1/device-tools/{kind}")
    def status(kind: Literal["imu", "dexhand"], request: Request,
               x_rp1_client_id: str | None = Header(default=None), x_rp1_operator_token: str | None = Header(default=None)):
        authenticate(request, x_rp1_client_id, x_rp1_operator_token)
        return access.tools.status(kind)

    @app.post("/api/v1/device-tools/{kind}/{action}")
    def action(kind: Literal["imu", "dexhand"], action: str, body: DeviceRequest, request: Request,
               x_rp1_client_id: str | None = Header(default=None), x_rp1_operator_token: str | None = Header(default=None)):
        owner = authenticate(request, x_rp1_client_id, x_rp1_operator_token, control=action not in {"stop", "disconnect"})
        allowed = {"initialize", "check", "stop", "recover", "heartbeat"} if kind == "imu" else {"connect", "enable", "home", "check", "run", "stop", "disconnect", "heartbeat"}
        if action not in allowed:
            raise HTTPException(404, "未知设备操作")
        try:
            if action == "heartbeat":
                return access.tools.heartbeat(kind, owner, body.generation)
            return access.tools.submit(kind, owner, action, body.model_dump(exclude={"generation"}), body.generation)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/device-reports/{report_id}/{filename}")
    def report(report_id: str, filename: str, x_rp1_operator_token: str | None = Header(default=None)):
        require_token(x_rp1_operator_token)
        try:
            return FileResponse(access.tools.report_path(report_id, filename), filename=filename)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
