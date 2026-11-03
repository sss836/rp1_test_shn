"""Strict adapter around the checked-in LRO protocol primitives.

No connect retry, reset command, shell interface setup, power or enable command.
"""
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


def cancelled(stop):
    if stop.is_set():
        raise InterruptedError("固件维护已取消；不自动重试")


def write_firmware(target, data, stop, progress, *, adapter_factory=None):
    p = protocol()
    class StrictAdapter(p.SocketcanAdapter):
        def recv_frame(self):
            try:
                raw = self._sock.recv(72)
            except BlockingIOError:
                return None
            if len(raw) != 72:
                return None
            can_id, length, flags, data = p._CANFD_FRAME.unpack(raw)
            if length != 8:
                return None
            return can_id, data[:length]
    adapter = (adapter_factory or StrictAdapter)(target["interface"])
    deadline = time.monotonic() + 300
    def check():
        cancelled(stop)
        if time.monotonic() > deadline:
            raise TimeoutError("单台OTA超过300秒预算；不自动重试")
    # Open failure also closes a partially-created socket.
    try:
        adapter.open()
        def send(frame):
            check()
            adapter.send_frame(target["ota_request_id"], frame)
        def receive():
            check()
            got = adapter.recv_frame()
            if got is None:
                return None
            can_id, payload = got
            # Reject standard, remote, error and other target responses.
            if can_id != target["ota_response_id"] | p.CAN_EFF_FLAG:
                return None
            return payload
        link = p.IsoTpLink(send, receive, cf_gap_ms=1000)
        session = p.DeviceSession(link, p.FlashTimeouts(connect_retries=1))
        session.connect()
        session.get_status(); session.program_start()
        session.set_mta(p.FIRMWARE_BASE_ADDRESS)
        session.program_clear(len(data))
        written = 0
        for offset in range(0, len(data), p.BLOCK_SIZE):
            block = data[offset:offset+p.BLOCK_SIZE]
            cancelled(stop)
            session._write_block(block)
            written += len(block)
            progress(written, len(data))
        session.set_mta(p.FIRMWARE_BASE_ADDRESS)
        response = link.send_and_receive(p.build_selfcheck(len(data)), 10000)
        # 0x02 was provisionally accepted by the reference. Its meaning is not
        # established: preserve evidence and refuse to continue here.
        if response != b"\xff\x02\x01":
            raise p.VerifyError(f"未支持的设备校验响应: {response.hex()}")
        return {"bytes_written": written, "selfcheck_response": response.hex(),
                "reset_sent": False, "actual_version_verified": False}
    finally:
        adapter.close()


def read_version(target, stop, *, socket_factory=socket.socket, timeout=.5):
    frame = struct.Struct("<IBB2x64s")
    values = {"index": target["index"], "interface": target["interface"],
              "motor_id": target["motor_id"], "model": target["model"],
              "status": "unknown", "raw_responses": {}}
    with socket_factory(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as bus:
        bus.setsockopt(getattr(socket, "SOL_CAN_RAW", 101), getattr(socket, "CAN_RAW_FD_FRAMES", 5), 1)
        bus.setsockopt(getattr(socket, "SOL_CAN_RAW", 101), 1,
                       struct.pack("=II", target["motor_id"], 0xffffffff))
        bus.bind((target["interface"],)); bus.setblocking(False)
        # Drop queued traffic before each query; exact code + ID + valid shape
        # must match the one active request. Only E0 31 / E0 1E are sent.
        for code in (0x31, 0x1e):
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
                if len(raw) != 72:
                    continue
                can_id, length, flags, data = frame.unpack(raw)
                if can_id != target["motor_id"] or not 2 <= length <= 64 or data[:2] != bytes([0xa0, code]):
                    continue
                try:
                    decoded = decode_version(data[:length])
                except ValueError:
                    continue
                values["raw_responses"][f"0x{code:02x}"] = data[:length].hex()
                if code == 0x1e and values.get("software") and decoded.get("software") != values["software"]:
                    values["status"] = "unknown"
                    values["error"] = "原始字符串与数值软件版本不一致；需精确映射"
                values.update({k: v for k, v in decoded.items() if v is not None})
                break
    values["observed_at"] = datetime.now(timezone.utc).isoformat()
    if all(values.get(k) for k in ("raw_firmware", "software", "hardware", "protocol")) and not values.get("error"):
        values["status"] = "known"
    elif not values["raw_responses"]:
        values["status"] = "timeout"
    return values
