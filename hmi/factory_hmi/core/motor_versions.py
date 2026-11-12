"""Read-only, bounded version discovery on existing SDK handles.

No bus is opened here. A single cancellable worker per connected backend uses
the driver's existing receive callback; control and snapshot threads never wait
for a version response. No synthetic version is supplied by the fake backend.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import threading
import time

from ..motor_ota_policy import MODEL_NAMES, firmware_generation


def decode_version(payload: bytes) -> dict:
    if len(payload) < 2 or payload[0] != 0xA0:
        raise ValueError("版本响应头无效或设备返回错误")
    if payload[1] == 0x31:
        if len(payload) < 20:
            raise ValueError("固件字符串响应不足20字节")
        raw = payload[2:20].split(b"\0", 1)[0].decode("ascii")
        if not raw.strip() or not raw.isprintable():
            raise ValueError("固件字符串为空或不可打印")
        # Preserve exact string; never interpret 108 as 1.0.8.
        return {"raw_firmware": raw,
                "software": raw if re.fullmatch(r"\d+\.\d+\.\d+", raw) else None}
    if payload[1] == 0x1E and len(payload) in (8, 12):
        result = {"software": ".".join(map(str, payload[2:5])),
                  "hardware": ".".join(map(str, payload[5:8]))}
        if len(payload) == 12:
            result["protocol"] = ".".join(map(str, payload[8:12]))
        return result
    raise ValueError("版本响应命令或长度不支持")


class MotorVersions:
    def __init__(self, entries, motors, *, timeout=.35, refresh=60., stale=180., on_observation=None):
        self.entries, self.motors = tuple(entries), tuple(motors)
        self.timeout, self.refresh, self.stale = timeout, refresh, stale
        self._on_observation = on_observation
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._rows = [self._empty(e) for e in self.entries]
        self._thread = threading.Thread(target=self._run, name="motor-versions", daemon=True)

    @staticmethod
    def _empty(e):
        return {"index": e.index, "interface": e.interface, "motor_id": e.motor_id,
                "joint_name": e.joint_name, "motor_type": e.motor_type,
                "model": MODEL_NAMES.get(e.motor_model) if e.motor_type.upper() == "LRO" else None,
                "status": "unknown", "raw_firmware": None, "software": None,
                "hardware": None, "protocol": None, "observed_at": None,
                "raw_responses": {}, "errors": {}, "_monotonic": None}

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("版本查询线程尚未退出，不能释放当前驱动")
        # Preserve observations, release native driver/socket ownership. Keeping
        # these handles would leave callbacks registered after backend close.
        self.motors = ()

    def snapshot(self, *, connected=True):
        with self._lock:
            rows = [{**r, "raw_responses": dict(r["raw_responses"]), "errors": dict(r["errors"])} for r in self._rows]
        for r in rows:
            seen = r.pop("_monotonic")
            r["age_s"] = None if seen is None else max(0., time.monotonic() - seen)
            if seen is not None and (not connected or r["age_s"] > self.stale):
                r["status"] = "stale"
        return rows

    def _query(self, motor, code):
        if not motor.request_version(code):
            raise NotImplementedError("SDK或驱动不支持版本查询")
        deadline = time.monotonic() + self.timeout
        while not self._stop.is_set():
            raw = bytes(motor.version_payload(code))
            if raw:
                return raw
            if time.monotonic() >= deadline:
                raise TimeoutError("版本查询超时")
            self._stop.wait(.01)
        raise InterruptedError("连接已关闭")

    def _run(self):
        while not self._stop.is_set():
            for i, e in enumerate(self.entries):
                if self._stop.is_set():
                    return
                row = self._empty(e)
                if (e.motor_type.upper() != "LRO" or e.interface_type not in {"canfd", "ethercanfd"}
                        or i >= len(self.motors) or not hasattr(self.motors[i], "request_version")):
                    row["status"] = "unsupported"
                else:
                    successes = 0
                    unsupported = False
                    for code in (0x31, 0x1E):
                        try:
                            raw = self._query(self.motors[i], code)
                            row["raw_responses"][hex(code)] = raw.hex()
                            fields = decode_version(raw)
                            # Numeric software/hardware version is an independent
                            # observation; retain both if they disagree.
                            if code == 0x1E:
                                row["numeric_software"] = fields["software"]
                            row.update(fields)
                            successes += 1
                        except NotImplementedError as exc:
                            unsupported = True
                            row["errors"][hex(code)] = str(exc)
                        except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
                            row["errors"][hex(code)] = str(exc)
                    row["status"] = "known" if successes else ("unsupported" if unsupported else ("timeout" if not row["raw_responses"] else "unknown"))
                    if row.get("raw_firmware") and row.get("numeric_software"):
                        row["version_relation"] = ("equal" if row["raw_firmware"] == row["numeric_software"] else "unverified")
                    if successes:
                        row["observed_at"] = datetime.now(timezone.utc).isoformat()
                        row["_monotonic"] = time.monotonic()
                with self._lock:
                    self._rows[i] = row
                if self._on_observation is not None and not self._stop.is_set():
                    self._on_observation(i, {**row, "raw_responses": dict(row["raw_responses"]),
                                             "errors": dict(row["errors"])})
            if self._stop.wait(self.refresh):
                return
