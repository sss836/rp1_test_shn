"""Read-only, bounded version discovery on existing SDK handles.

No bus is opened here. A single cancellable worker per connected backend uses
the driver's existing receive callback; control and snapshot threads never wait
for a version response. No synthetic version is supplied by the fake backend.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import re
import threading
import time
from uuid import uuid4

from ..motor_ota_policy import MODEL_NAMES, firmware_generation
from .lro_query import valid_query_header


def decode_version(payload: bytes) -> dict:
    if len(payload) < 2 or not valid_query_header(payload[0]):
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


def version_relation(raw_firmware, numeric_software):
    if raw_firmware == numeric_software:return 'equal'
    generation=firmware_generation(raw_firmware)
    numeric_generation={'0.1.8':108,'0.1.9':109}.get(numeric_software)
    if generation is not None and numeric_generation is not None:
        return 'same_generation' if generation == numeric_generation else 'conflict'
    return 'unverified'


class MotorVersions:
    # Match the native SDK's 500 ms receive window. Starting the next command
    # earlier replaces the pending query and discards matching late replies.
    def __init__(self, entries, motors, *, timeout=.5, refresh=None, stale=180., on_observation=None, numeric_nodes=frozenset()):
        self.entries, self.motors = tuple(entries), tuple(motors)
        # refresh is accepted for callers of earlier releases. Only explicit
        # requests can start another pass after the initial connection scan.
        self.timeout, self.stale = timeout, stale
        self.numeric_nodes = numeric_nodes
        self._on_observation = on_observation
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._reading = True
        self._paused = False
        self._refresh = self.idle_refresh(len(self.entries))
        self._rows = [self._empty(e) for e in self.entries]
        self._events = deque(maxlen=512)
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

    @staticmethod
    def idle_refresh(total_motors=0):
        return {"request_id": None, "state": "idle", "completed_motors": 0, "total_motors": total_motors}

    def request_refresh(self):
        with self._lock:
            if self._paused:
                raise ValueError("使能期间已暂停版本查询，请先失能")
            if self._stop.is_set() or not self._thread.is_alive():
                raise ValueError("当前电机版本查询未运行，请先连接并扫描电机")
            if self._refresh["state"] not in {"queued", "reading"}:
                self._refresh.update(request_id=str(uuid4()), state="queued", completed_motors=0)
                self._wake.set()
            return dict(self._refresh)

    def pause_for_enable(self):
        with self._lock:
            if (self._reading or self._refresh["state"] == "queued" or self._stop.is_set()
                    or not self._thread.is_alive()):
                return False
            self._paused = True
            return True

    def resume_after_disable(self):
        with self._lock:
            self._paused = False

    def refresh_status(self):
        with self._lock:
            if self._stop.is_set() or not self._thread.is_alive():
                return self.idle_refresh(len(self.entries))
            return dict(self._refresh)

    @property
    def refresh_pending(self):
        return self.refresh_status()["state"] in {"queued", "reading"}

    def close(self):
        with self._lock:
            self._stop.set()
            self._wake.set()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("版本查询线程尚未退出，不能释放当前驱动")
        # Preserve observations, release native driver/socket ownership. Keeping
        # these handles would leave callbacks registered after backend close.
        self.motors = ()

    def snapshot(self, *, connected=True):
        with self._lock:
            rows = [{**r, "raw_responses": dict(r["raw_responses"]), "errors": dict(r["errors"])}
                    for r in self._rows]
        for r in rows:
            if "last_refresh_errors" in r:
                r["last_refresh_errors"] = dict(r["last_refresh_errors"])
            seen = r.pop("_monotonic")
            r["age_s"] = None if seen is None else max(0., time.monotonic() - seen)
            if seen is not None and (not connected or r["age_s"] > self.stale):
                r["status"] = "stale"
        return rows

    def events_snapshot(self, since=None):
        with self._lock:
            return [dict(event) for event in self._events if since is None or event["end"] >= since]

    @staticmethod
    def _merge_refresh(previous, row):
        if previous["status"] != "known" or previous.get("_monotonic") is None:
            return row
        # New identity or an explicit invalid response must reach the protocol fence.
        identity = ("raw_firmware", "numeric_software", "hardware", "protocol")
        if any(row.get(key) is not None and row[key] != previous.get(key) for key in identity):
            return row
        errors = row["errors"]
        if (any(message != "版本查询超时" for message in errors.values()) or
                row.get("kt_error") not in (None, "Kt读取失败：版本查询超时")):
            return row
        missing = previous["raw_responses"].keys() - row["raw_responses"].keys()
        if errors and (missing or row["status"] == "timeout"):
            cached = deepcopy(previous)
            cached.update(refresh_failures=previous.get("refresh_failures", 0) + 1,
                          last_refresh_errors=dict(errors), last_refresh_attempt_monotonic=time.monotonic())
            return cached
        if (row.get("kt_error") == "Kt读取失败：版本查询超时" and
                previous.get("torque_kt_nm_a") is not None):
            for key in ("torque_kt_nm_a", "kt_raw_response", "kt_monotonic", "kt_observed_at"):
                row[key] = previous[key]
            row["last_kt_error"] = row.pop("kt_error")
        return row

    def _query(self, motor, code, index, row):
        start = time.monotonic()
        outcome = "error"
        try:
            raw = self._read_payload(motor, code)
            if code == 0x16:
                from .torque_estimation import decode_kt
                fields = {"torque_kt_nm_a": decode_kt(raw), "kt_raw_response": raw.hex()}
            else:
                row["raw_responses"][hex(code)] = raw.hex()
                fields = decode_version(raw)
            outcome = "ok"
            return fields
        except TimeoutError:
            outcome = "timeout"
            raise
        except InterruptedError:
            outcome = "interrupted"
            raise
        finally:
            with self._lock:
                self._events.append({"index": index, "code": hex(code), "start": start,
                                     "end": time.monotonic(), "outcome": outcome})

    def _read_payload(self, motor, code):
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
            with self._lock:
                if self._stop.is_set():
                    return
                requested = self._refresh["state"] == "queued"
                self._reading = True
                if requested:
                    self._refresh["state"] = "reading"
                self._wake.clear()
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
                    previous = self._rows[i]
                    node = (e.interface, e.motor_id, e.motor_model, previous.get("numeric_software"),
                            previous.get("hardware"), previous.get("protocol"))
                    numeric_only = (requested and node in self.numeric_nodes
                                    and e.lro_protocol is None and e.lro_protocol_profile == "legacy"
                                    and previous.get("numeric_software") == "0.1.8"
                                    and previous.get("firmware_read_state") in {"timeout", "station_numeric"}
                                    and previous.get("status") == "known")
                    if numeric_only:
                        row["firmware_read_state"] = "station_numeric"
                    for code in ((0x1E,) if numeric_only else (0x31, 0x1E)):
                        if self._stop.is_set():
                            break
                        try:
                            fields = self._query(self.motors[i], code, i, row)
                            # Numeric software/hardware version is an independent
                            # observation; retain both if they disagree.
                            if code == 0x1E:
                                row["numeric_software"] = fields["software"]
                            row.update(fields)
                            successes += 1
                        except TimeoutError as exc:
                            row["errors"][hex(code)] = str(exc)
                            if code == 0x31:
                                row["firmware_read_state"] = "timeout"
                        except NotImplementedError as exc:
                            unsupported = True
                            row["errors"][hex(code)] = str(exc)
                        except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
                            row["errors"][hex(code)] = str(exc)
                    row["status"] = "known" if successes else ("unsupported" if unsupported else ("timeout" if not row["raw_responses"] else "unknown"))
                    if numeric_only and successes and any(row.get(field) != previous.get(field)
                            for field in ("numeric_software", "hardware", "protocol")):
                        row["status"] = "conflict"
                        row["errors"]["0x1e"] = "数值身份已变化，须重新连接核对完整版本"
                    if row.get("raw_firmware") and row.get("numeric_software"):
                        row["version_relation"] = version_relation(row["raw_firmware"], row["numeric_software"])
                    # Serialize this read with version requests on the same receive
                    # callback; no new sockets, writes, or control-thread waits.
                    from .lro_fixed_profiles import fixed_profile
                    from .torque_estimation import SUPPORTED_MODELS
                    if fixed_profile(row) == "v1.03" and e.motor_model in SUPPORTED_MODELS:
                        try:
                            row.update(self._query(self.motors[i], 0x16, i, row))
                            row["kt_monotonic"] = time.monotonic()
                            row["kt_observed_at"] = datetime.now(timezone.utc).isoformat()
                        except (OSError, ValueError, RuntimeError, TypeError, AttributeError) as exc:
                            row["kt_error"] = "Kt读取失败：" + str(exc)
                    if successes:
                        row["observed_at"] = datetime.now(timezone.utc).isoformat()
                        row["_monotonic"] = time.monotonic()
                with self._lock:
                    row = self._merge_refresh(self._rows[i], row)
                    self._rows[i] = row
                if self._on_observation is not None and not self._stop.is_set():
                    self._on_observation(i, deepcopy(row))
                if requested:
                    with self._lock:
                        self._refresh["completed_motors"] = i + 1
            with self._lock:
                if requested:
                    self._refresh["state"] = "complete"
                self._reading = False
            self._wake.wait()
