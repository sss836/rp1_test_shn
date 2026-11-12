"""Audited integration of the production LRO flasher; no power/enable commands."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
import select
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from .motor_versions import decode_version


# CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_SFF_MASK. CAN_ERR_FLAG in a receive
# mask would subscribe the socket to error frames instead of motor replies.
STANDARD_DATA_FRAME_MASK = 0xC00007FF


def protocol():
    name = "_rp1_lro_ota_protocol"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[2] / "motors/scripts/motor_ota.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def selfcheck_accepted(response):
    """The production parser owns response semantics, including extended 02 replies."""
    try:
        raw = bytes.fromhex(response) if isinstance(response, str) else response
        if not isinstance(raw, bytes):
            return False
        protocol().parse_selfcheck(raw)
        return True
    except (ValueError, TypeError, protocol().FlashError):
        return False


def cancelled(stop):
    if stop.is_set():
        raise InterruptedError("固件维护已取消；不自动重试")


def write_firmware(target, data, stop, progress, *, adapter_factory=None):
    """Use the production-script DeviceSession.flash, retaining HMI audit/cancel."""
    p = protocol()
    response_id = target["ota_response_id"] | p.CAN_EFF_FLAG
    diagnostics = {"stage": "open", "bytes_written": 0, "tx_frames": 0,
                   "rx_frames": 0, "ignored_frames": 0,
                   "interface": target["interface"], "motor_id": target["motor_id"],
                   "ota_request_id": target["ota_request_id"],
                   "ota_response_id": target["ota_response_id"]}

    class TargetAdapter(p.SocketcanAdapter):
        def open(self):
            super().open()
            # Filter at the kernel, before polling. Reuse the vendor decoder
            # for both classic CAN and CAN-FD frames without dropping either.
            self._sock.setsockopt(p._SOL_CAN_RAW, 1,
                                  struct.pack("=II", response_id, 0xDFFFFFFF))

    adapter = (adapter_factory or TargetAdapter)(target["interface"])
    deadline = time.monotonic() + 300
    receipt = {"reset_sent": False, "actual_version_verified": False}
    def check():
        cancelled(stop)
        if time.monotonic() > deadline:
            raise TimeoutError("单台OTA超过300秒预算；不自动重启刷写任务")

    stages = {p.CMD_CONNECT: "connect", p.CMD_GET_STATUS: "get_status",
              p.CMD_PROGRAM_START: "program_start", p.CMD_SET_MTA: "set_mta",
              p.CMD_PROGRAM_CLEAR: "erase", p.CMD_PROGRAM: "write",
              p.CMD_PROGRAM_BULK: "write", p.CMD_USER: "selfcheck"}
    class AuditedLink(p.IsoTpLink):
        def send_and_receive(self, payload, timeout_ms):
            diagnostics["stage"] = stages.get(payload[0], "command")
            response = super().send_and_receive(payload, timeout_ms)
            if payload[0] == p.CMD_USER:
                receipt["selfcheck_response"] = response.hex()
                # Do not impose a different three-byte-only contract here.
                # DeviceSession.flash calls the very same vendor parser.
                p.parse_selfcheck(response)
                receipt["selfcheck_parser"] = "production_motor_ota.parse_selfcheck"
                receipt["selfcheck_result_code"] = response[2]
            return response

        def send_and_receive_no_wait(self, payload):
            diagnostics["stage"] = "reset"
            super().send_and_receive_no_wait(payload)
            receipt["reset_sent"] = True

    try:
        adapter.open()
        def send(frame):
            check()
            adapter.send_frame(target["ota_request_id"], frame)
            diagnostics["tx_frames"] += 1
        def receive():
            check()
            got = adapter.recv_frame()
            if got is None:
                return None
            diagnostics["rx_frames"] += 1
            can_id, payload = got
            if can_id != response_id or len(payload) != 8:
                diagnostics["ignored_frames"] += 1
                return None
            return payload
        def report(written, total):
            diagnostics["bytes_written"] = written
            progress(written, total)

        # Same timeout defaults, connection and complete flash sequence used
        # by LinuxFlasher.flash in the successful ID 2 production-script run.
        timeouts = p.FlashTimeouts()
        link = AuditedLink(send, receive, cf_gap_ms=timeouts.cf_gap_ms)
        session = p.DeviceSession(link, timeouts)
        session.connect()
        session.flash(data, on_progress=report)
        diagnostics["stage"] = "awaiting_power"
        return {**receipt, "bytes_written": diagnostics["bytes_written"],
                "ota_diagnostics": dict(diagnostics)}
    except Exception as exc:
        exc.ota_diagnostics = dict(diagnostics)
        if isinstance(exc, p.ConnectError):
            exc.args = (f'{target["interface"]} ID {target["motor_id"]} 升级握手失败，未发送擦除或固件数据。'
                        f'产线协议连接尝试 {timeouts.connect_retries} 次未完成；'
                        f'接收 {diagnostics["rx_frames"]} 帧，忽略 {diagnostics["ignored_frames"]} 帧。'
                        '重新上下电仅恢复设备，需要重新发起更新。',)
        raise
    finally:
        adapter.close()


def read_version(target, stop, *, socket_factory=socket.socket, timeout=.5):
    frame = struct.Struct("<IBB2x64s")
    classic_frame = struct.Struct("<IB3x8s")
    values = {"index": target["index"], "interface": target["interface"],
              "motor_id": target["motor_id"], "model": target["model"],
              "status": "unknown", "raw_responses": {}, "errors": {}}
    with socket_factory(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
        bus.setsockopt(getattr(socket, "SOL_CAN_RAW", 101), getattr(socket, "CAN_RAW_FD_FRAMES", 5), 1)
        bus.setsockopt(getattr(socket, "SOL_CAN_RAW", 101), 1,
                       struct.pack("=II", target["motor_id"], STANDARD_DATA_FRAME_MASK))
        bus.bind((target["interface"],)); bus.setblocking(False)
        # Drop queued traffic before each query; exact code + ID + valid shape
        # must match the one active request. Only E0 31 / E0 1E are sent.
        for code in (0x31, 0x1e):
            command = f"0x{code:02x}"
            cancelled(stop)
            for _ in range(256):
                cancelled(stop)
                try: bus.recv(72)
                except BlockingIOError: break
            bus.send(frame.pack(target["motor_id"], 2, 1, bytes([0xe0, code])))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                cancelled(stop)
                if not select.select([bus], [], [], min(.02, max(0, deadline - time.monotonic())))[0]:
                    continue
                raw = bus.recv(72)
                if len(raw) == frame.size:
                    can_id, length, _, data = frame.unpack(raw)
                elif len(raw) == classic_frame.size:
                    can_id, length, data = classic_frame.unpack(raw)
                else:
                    continue
                if can_id != target["motor_id"] or not 2 <= length <= len(data) or data[:2] != bytes([0xa0, code]):
                    continue
                try:
                    decoded = decode_version(data[:length])
                except ValueError as exc:
                    values["errors"][command] = str(exc)
                    continue
                values["errors"].pop(command, None)
                values["raw_responses"][command] = data[:length].hex()
                if code == 0x1e and values.get("software") and decoded.get("software") != values["software"]:
                    values["status"] = "unknown"
                    values["error"] = "原始字符串与数值软件版本不一致；需精确映射"
                if code == 0x1e:
                    values["numeric_software"] = decoded["software"]
                values.update({k: v for k, v in decoded.items() if v is not None})
                break
            if command not in values["raw_responses"]:
                values["errors"].setdefault(command, "版本查询超时")
    values["observed_at"] = datetime.now(timezone.utc).isoformat()
    # Some legacy replies have only eight bytes. Keep the actual identity
    # available to read-only diagnostics; OTA still requires a protocol value.
    if all(values.get(k) for k in ("raw_firmware", "numeric_software", "hardware")) and not values.get("error"):
        values["status"] = "known"
    elif not values["raw_responses"]:
        values["status"] = "timeout"
    return values
