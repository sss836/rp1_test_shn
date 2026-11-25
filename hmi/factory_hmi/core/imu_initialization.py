"""Independent HIPNUC configuration and passive feedback checks on SocketCAN."""
from __future__ import annotations

import math
import os
import socket
import struct
import time

from factory_hmi.core.can_manager import CanManager

UPSTREAM = "https://github.com/Roboparty/roboparty_imu/blob/8da01fc4ca37f6e89cad4ff3ba817c038de20a0f/init_imu.sh"
COMMANDS = (
    ("四元数输出配置", "4601060005000000", 0.5),
    ("航向输出配置", "4101060000000000", 0.5),
    ("横滚俯仰输出配置", "3D01060000000000", 0.5),
    ("设备速率改为 1 Mbps", "9A00060000000000", 0.5),
    ("保存设备配置", "0000060000000000", 1.0),
    ("设备复位", "00000600FF000000", 1.5),
)


class ImuCan:
    def __init__(self, interface: str):
        self.interface = interface
        self.socket = None

    def open(self):
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            sock.settimeout(0.1)
            sock.bind((self.interface,))
        except Exception:
            sock.close()
            raise
        self.socket = sock

    def send(self, payload: bytes):
        frame = struct.pack("=IB3x8s", 0x80000000 | 0x0CEF0808, 8, payload)
        if self.socket.send(frame) != 16:
            raise RuntimeError("IMU CAN 发送不完整")

    def receive(self):
        try:
            frame = self.socket.recv(72)
        except socket.timeout:
            return None
        if len(frame) != 16:
            return None
        identifier, size, data = struct.unpack("=IB3x8s", frame)
        if size != 8 or identifier & 0x60000000 or not identifier & 0x80000000:
            return None
        if identifier & 0xFF != 8:
            return None
        return identifier & 0x1FFFFFFF, data[:size]

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None


def decode_measurement(identifier: int, data: bytes) -> dict:
    """J1939 PGNs from the same upstream revision; no cached-value freshness."""
    pgn = (identifier >> 8) & 0x3FFFF
    if pgn in (0xFF34, 0xFF37) and len(data) >= 6:
        scale = 9.80665 / 2048 if pgn == 0xFF34 else math.radians(2000 / 32768)
        return {"acceleration_m_s2" if pgn == 0xFF34 else "angular_velocity_rad_s":
                [x * scale for x in struct.unpack_from("<3h", data)]}
    if pgn == 0xFF46 and len(data) == 8:
        return {"quaternion_wxyz": [x / 10000 for x in struct.unpack("<4h", data)]}
    if pgn == 0xFF43 and len(data) >= 2:
        return {"temperature_c": struct.unpack_from("<h", data)[0] / 100}
    return {}


FACTORY_BITRATE = 500_000
READ_BITRATE = 1_000_000
FACTORY_PGNS = {0xFF34, 0xFF3D, 0xFF41}


def _classic_at(state, bitrate):
    return state.exists and state.up and state.bitrate == bitrate and state.fd is False


def _set_classic_rate(can, interface, bitrate):
    state = can.connect([interface], bitrate=bitrate, dbitrate=None, fd=False)[0]
    if not _classic_at(state, bitrate):
        raise RuntimeError(f"{interface} 未确认以经典 CAN {bitrate} bit/s 启动")


def _ensure_active(cancel):
    if cancel.is_set():
        raise RuntimeError("用户取消了 IMU 操作")


def _identify_target(transport, cancel, clock):
    """Recognize address 8 before writing, as in the supplied bench tool."""
    counts = {}
    deadline = clock() + 5.0
    while clock() < deadline:
        _ensure_active(cancel)
        frame = transport.receive()
        if frame is None:
            continue
        identifier, data = frame
        if identifier & 0xFF != 8 or len(data) != 8:
            continue
        pgn = (identifier >> 8) & 0x3FFFF
        if pgn == 0xFF46:
            quat = decode_measurement(identifier, data)["quaternion_wxyz"]
            if not 0.95 <= math.sqrt(sum(x*x for x in quat)) <= 1.05:
                continue
        counts[pgn] = counts.get(pgn, 0) + 1
        gyro = counts.get(0xFF37, 0)
        if ((gyro and counts.get(0xFF46))
                or (gyro >= 2 and any(counts.get(p, 0) >= 2 for p in FACTORY_PGNS))):
            return
    raise RuntimeError("500 kbit/s 未识别到地址 8 的 IMU 广播，未发送配置。已初始化的设备可直接选择“检查回传”。")


def _check_feedback(transport, cancel, clock, result, started):
    first = clock()
    last, counts = {}, {}
    while clock() < first + 5.0:
        _ensure_active(cancel)
        frame = transport.receive()
        if frame is None:
            continue
        identifier, data = frame
        values = decode_measurement(identifier, data)
        if not values:
            continue
        now = clock()
        if len(result["samples"]) < 10000:
            result["samples"].append({"elapsed_s": now - started, "can_id": f"{identifier:08X}",
                                      "payload": data.hex(), **values})
        for key, value in values.items():
            last[key] = (now, value)
            counts[key] = counts.get(key, 0) + 1
    now = clock()
    required = ("quaternion_wxyz", "acceleration_m_s2", "angular_velocity_rad_s")
    fresh = all(key in last and now - last[key][0] < 0.5 for key in required)
    quat = last.get("quaternion_wxyz", (0, [0] * 4))[1]
    valid = fresh and 0.9 <= math.sqrt(sum(x*x for x in quat)) <= 1.1
    result.update(passed=valid, feedback_checked=True, frame_counts=counts,
                  observed_hz={key: round(n / (now - first), 2) for key, n in counts.items()},
                  latest={key: v for key, (_, v) in last.items()},
                  note="IMU 回传检查通过。" if valid else "回传缺失、超时或四元数异常。")


def _restore_interface(can, interface, previous, rate_command_attempted, result, stage):
    # A partial device-rate write may already have taken effect.
    target = READ_BITRATE if rate_command_attempted else previous.bitrate
    up = rate_command_attempted or previous.up
    fd = False if rate_command_attempted else previous.fd
    expected = {"up": up}
    if target is not None:
        expected["bitrate"] = target
    if fd is not None:
        expected["fd"] = fd
    if fd and previous.dbitrate is not None:
        expected["dbitrate"] = previous.dbitrate
    result["restore_expected"] = expected
    stage(f"恢复 {interface} 主机接口")

    def matches():
        state = can.status([interface])[0]
        return state.exists and all(getattr(state, k) == v for k, v in expected.items())

    try:
        if matches():
            return
        if target is not None:
            can.connect([interface], bitrate=target,
                        dbitrate=previous.dbitrate if fd else None, fd=bool(fd))
        if not up:
            can.disconnect([interface])
        if not matches():
            raise RuntimeError("接口状态回读不匹配")
    except Exception as exc:
        result.update(passed=False, restore_failed=True,
                      error=f"{result.get('error', '')}；主机 CAN 恢复失败：{exc}")


def initialize_imu(config, cancel, publish, *, can=None, transport_factory=ImuCan,
                   clock=time.monotonic):
    """Run one explicit action on an exclusively reserved IMU interface."""
    action = config["action"]
    if action not in {"initialize", "check"}:
        raise ValueError("未知 IMU 操作")
    init = action == "initialize"
    interface = config["interface"]
    if can is None:
        if os.environ.get("RP1_FACTORY_CAN_DISABLED") == "1":
            raise RuntimeError("离线实例禁止访问真实 IMU")
        can = CanManager()
        for bitrate in ((FACTORY_BITRATE, READ_BITRATE) if init else (READ_BITRATE,)):
            if not can.authorize_reuse(interface, bitrate=bitrate, dbitrate=None, fd=False):
                raise RuntimeError("当前 CAN 授权配置未允许此 IMU 接口及波特率")
    transport = transport_factory(interface)
    previous = can.status([interface])[0]
    started = clock()
    result = {"action": action, "interface": interface, "passed": False,
              "command_sequence_sent": False, "feedback_checked": False,
              "upstream": UPSTREAM, "events": [], "samples": []}

    def stage(message):
        publish(message)
        result["events"].append({"elapsed_s": clock() - started, "message": message})

    restore = False
    rate_command_attempted = False
    try:
        _ensure_active(cancel)
        if not previous.exists:
            raise RuntimeError(f"未找到 IMU 接口 {interface}")
        rate = FACTORY_BITRATE if init else READ_BITRATE
        stage(f"{interface} 准备使用 {rate} bit/s")
        if not _classic_at(previous, rate):
            restore = True
            _set_classic_rate(can, interface, rate)
        _ensure_active(cancel)
        transport.open()
        if init:
            if cancel.wait(1.0):
                raise RuntimeError("用户取消了 IMU 操作")
            stage("识别 500 kbit/s 下地址 8 的 IMU")
            _identify_target(transport, cancel, clock)
            for message, payload, delay in COMMANDS:
                _ensure_active(cancel)
                stage(message)
                if payload == "9A00060000000000":
                    rate_command_attempted = True
                    restore = True
                transport.send(bytes.fromhex(payload))
                result["events"][-1].update(can_id="0CEF0808", payload=payload, sent=True)
                if cancel.wait(delay):
                    raise RuntimeError("用户取消了 IMU 操作")
            result["command_sequence_sent"] = True
            transport.close()
            stage(f"{interface} 自动切换到 1 Mbit/s")
            _set_classic_rate(can, interface, READ_BITRATE)
            result.update(passed=True, note="初始化指令已发完，主机已切至 1 Mbit/s。可单独运行回传检查。")
        else:
            stage("在 1 Mbit/s 下采集回传（5 秒）")
            _check_feedback(transport, cancel, clock, result, started)
        restore = False
    except Exception as exc:
        result.update(passed=False, error=str(exc), cancelled=cancel.is_set())
    finally:
        try:
            transport.close()
        finally:
            if restore:
                _restore_interface(can, interface, previous, rate_command_attempted, result, stage)
    return result
