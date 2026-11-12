"""Serial, journalled module OTA. No enable, power switching or automatic retry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
import uuid

from .motor_versions import MODEL_NAMES, firmware_generation
from ..motor_ota_policy import V2


MODULE_109 = "module_108_to_109"
SELECTED_109 = "selected_108_to_109"


def _node_identity(row):
    return tuple(row.get(key) for key in ("index", "interface", "motor_id", "model"))


def _version_identity(row):
    return tuple(row.get(key) for key in ("raw_firmware", "software", "hardware", "protocol"))


def generation_for_target(target, observation, *, require_protocol=True):
    """The label is insufficient on its own: require a fresh complete identity."""
    if observation.get("status") != "known" or _node_identity(target) != _node_identity(observation):
        return None
    identity = _version_identity(observation)
    if not all(identity if require_protocol else identity[:3]):
        return None
    try:
        seen = datetime.fromisoformat(observation["observed_at"])
        age = (datetime.now(timezone.utc) - seen).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None
    if not 0 <= age <= 180:
        return None
    return firmware_generation(observation["raw_firmware"])


def _require_109_transition(package):
    if (firmware_generation(package["manifest"]["version"]) != 109
            or package["policy"].get("schema") != V2):
        raise ValueError("108→109 需要精确签名的源版本、目标版本、协议和固件哈希映射")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def effective_targets(config):
    rows, physical = [], set()
    for entry in config.entries:
        if (entry.motor_type != "LRO" or entry.interface_type.lower() != "canfd"
                or entry.backend != "socketcan" or entry.motor_model not in MODEL_NAMES
                or type(entry.motor_id) is not int or not 1 <= entry.motor_id <= 0x7ff):
            raise ValueError("首版维护只支持已知型号的 LRO / SocketCAN / CAN-FD 有效目标")
        key = (entry.interface, entry.motor_id)
        if key in physical or any(r["index"] == entry.index for r in rows):
            raise ValueError("有效配置包含重复目标或索引")
        physical.add(key)
        rows.append({"index": entry.index, "module": config.limb, "joint_name": entry.joint_name,
                     "interface": entry.interface, "motor_id": entry.motor_id,
                     "model": MODEL_NAMES[entry.motor_model], "ota_request_id": 0x1000 + 2 * entry.motor_id - 1,
                     "ota_response_id": 0x1000 + 2 * entry.motor_id,
                     "effective_entry": asdict(entry)})
    if not rows:
        raise ValueError("没有有效维护目标")
    return rows


SERIAL_LOCK = threading.Lock()


class VersionUnknownError(RuntimeError):
    pass


class ModuleFirmwareMaintenance:
    """The service owns this worker and must cancel/join it before releasing CAN.

    Each write pauses for a human acknowledgement AND an explicit fresh readback.
    Only a matched readback advances to the next target. Downloads remain separate.
    Callbacks are trusted local adapters, never request-provided executable code.
    """
    def __init__(self, root, targets, observations, authorize, artifact, reserve, writer, reader):
        self.root = Path(root)
        self.targets, self.observations = targets, observations
        self.authorize, self.artifact, self.reserve = authorize, artifact, reserve
        self.writer, self.reader = writer, reader
        self._lock = threading.RLock()
        self._stop, self._read = threading.Event(), threading.Event()
        self._thread = None
        self._preview = None
        self._job = None
        self._reservation = None
        self._serial_owned = False
        self._cleanup_error = None
        self._cleanup_lock = threading.Lock()
        self._unrecoverable_release = False
        self.root.mkdir(parents=True, exist_ok=True)
        # A restart is evidence of interruption, never permission to replay writes.
        for path in sorted(self.root.glob("*.json")):
            value = json.loads(path.read_text())
            if value.get("active"):
                self._recover_interrupted_job(value)
                self._job = value
                self._save()

    def _recover_interrupted_job(self, value):
        """Normalize each journal before its atomic save, without device access."""
        value.update(active=False, status="interrupted", error="服务重启；未自动恢复写入", ended_at=now())
        for row in value["targets"]:
            if row["status"] in {"writing", "awaiting_power", "readback_requested", "verifying"}:
                row["status"] = "interrupted_unverified"
            elif row["status"] == "pending":
                row["status"] = "not_run"

    @property
    def active(self):
        with self._lock:
            return bool(self._job and (self._job.get("active") or self._thread is not None and self._thread.is_alive() or self._serial_owned or self._cleanup_error is not None))

    def snapshot(self):
        with self._lock:
            value = deepcopy(self._job or {"active": False, "status": "idle", "targets": []})
            value["active"] = self.active
            return value

    def preview(self, selections, *, mode=None, configuration_sha256=None):
        with self._lock:
            if self.active:
                raise RuntimeError("已有模块固件维护任务")
            self._preview = None
            actor = self.authorize()
            targets = self.targets()
            configuration = digest(targets)
            if configuration_sha256 is not None and configuration_sha256 != configuration:
                raise ValueError("当前有效配置已变化，请刷新电机列表")
            indices = [s["index"] for s in selections]
            if mode not in (None, MODULE_109, SELECTED_109):
                raise ValueError("未知的固件升级范围")
            if any(type(i) is not int for i in indices) or len(indices) != len(set(indices)):
                raise ValueError("请选择非空且不重复的目标")
            by_index = {r["index"]: r for r in targets}
            observed = {r["index"]: r for r in self.observations()}
            skipped = []
            if mode == MODULE_109:
                pending = set()
                for target in targets:
                    index = target["index"]
                    observation = observed.get(index, {})
                    generation = generation_for_target(target, observation)
                    if generation == 108:
                        pending.add(index)
                    elif generation == 109:
                        skipped.append({**target, "before": observation, "status": "already_109"})
                    else:
                        raise ValueError(f'{target["joint_name"]} 的实际版本未确认；无法确定整个模块的升级范围')
                if not pending:
                    raise ValueError("整个模块已确认是 109，无需刷写")
                if set(indices) != pending:
                    raise ValueError("整模块升级必须覆盖所有且仅覆盖当前的 108 电机")
            if not indices:
                raise ValueError("请选择非空且不重复的目标")
            rows = []
            for selection in selections:
                index = selection["index"]
                if index not in by_index:
                    raise ValueError("所选目标不属于当前模块")
                target = by_index[index]
                observation = observed.get(index, {})
                if _node_identity(observation) != _node_identity(target):
                    raise ValueError("实际版本与最终有效配置不一致")
                if mode is not None and generation_for_target(target, observation) != 108:
                    raise ValueError(f'{target["joint_name"]} 未确认是 108，不能加入此批次')
                package = self.artifact(selection["release_id"], observation)
                if mode is not None:
                    _require_109_transition(package)
                rows.append({**target, "release_id": selection["release_id"], "before": observation,
                             "manifest": package["manifest"], "policy": package["policy"]})
            preview = {"preview_id": uuid.uuid4().hex, "actor": actor, "created_at": now(),
                       "configuration_sha256": configuration, "targets": rows,
                       "selections": deepcopy(selections), "expires_monotonic": time.monotonic() + 120}
            if mode is not None:
                preview.update(mode=mode, skipped_targets=skipped)
            self._preview = preview
            return deepcopy({k: v for k, v in preview.items() if k != "expires_monotonic"})

    def start(self, preview_id, confirmed):
        with self._lock:
            if self.active:
                raise RuntimeError("已有模块固件维护任务")
            p = self._preview
            if confirmed is not True or p is None or p["preview_id"] != preview_id or time.monotonic() > p["expires_monotonic"]:
                raise ValueError("请重新预览并明确确认维护目标")
            if self.authorize() != p["actor"] or digest(self.targets()) != p["configuration_sha256"]:
                self._preview = None
                raise ValueError("账号或最终配置已变化，请重新预览")
            # Revalidate signed files/policy before creating any device task.
            for row in p["targets"]:
                package = self.artifact(row["release_id"], row["before"])
                if package["manifest"] != row["manifest"] or package["policy"] != row["policy"]:
                    raise ValueError("固件或兼容映射已变化，请重新预览")
            self._job = {"job_id": uuid.uuid4().hex, "actor": p["actor"], "configuration_sha256": p["configuration_sha256"],
                         "active": True, "status": "queued", "created_at": now(),
                         "targets": [{**deepcopy(r), "status": "pending", "bytes_written": 0,
                                      "power_cycle_required": False, "human_acknowledged_at": None,
                                      "readback": None, "actual_version_verified": False} for r in p["targets"]]}
            if p.get("mode"):
                self._job.update(mode=p["mode"], skipped_targets=deepcopy(p["skipped_targets"]))
            self._preview = None
            self._stop.clear(); self._read.clear()
            try:
                self._save()
            except OSError as exc:
                self._job.update(active=False, status="failed", error=f"维护记录无法落盘，未启动设备任务: {exc}")
                raise
            self._thread = threading.Thread(target=self._run, name="factory-motor-maintenance", daemon=True)
            self._thread.start()
            return self.snapshot()

    def _save(self):
        if self._job is None:
            return
        path = self.root / (self._job["job_id"] + ".json")
        temp = path.with_suffix(".tmp")
        with temp.open("w") as stream:
            json.dump(self._job, stream, ensure_ascii=False, indent=2)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temp.replace(path)

    def _check(self):
        if self._stop.is_set():
            raise InterruptedError("用户取消或服务关闭；不自动重试")
        if self.authorize() != self._job["actor"]:
            raise RuntimeError("登录或维护权限已变化")

    def _read_planned_identity(self, row, generation):
        self._check()
        actual = self.reader(row, self._stop)
        if (generation_for_target(row, actual) != generation
                or _version_identity(actual) != _version_identity(row["before"])):
            raise VersionUnknownError(f'{row["joint_name"]} 的实际版本与预览不符；请重新检查')
        return actual

    def _run(self):
        current = None
        terminal_status, terminal_error = "verified", None
        try:
            if not SERIAL_LOCK.acquire(blocking=False):
                raise RuntimeError("本机已有串行固件维护任务")
            self._serial_owned = True
            # Retain a retryable reservation even if __enter__ fails part-way.
            self._reservation = self.reserve()
            self._reservation.__enter__()
            mode = self._job.get("mode")
            if mode:
                # Confirm the entire plan before the first irreversible write.
                for row in self._job["targets"]:
                    self._read_planned_identity(row, 108)
                for row in self._job["skipped_targets"]:
                    actual = self._read_planned_identity(row, 109)
                    with self._lock:
                        row["skip_readback"] = actual
                        self._save()
            for row in self._job["targets"]:
                current = row
                self._check()
                # Re-read actual identity/version at the last boundary before erase.
                before = self._read_planned_identity(row, 108) if mode else self.reader(row, self._stop)
                if before.get("status") != "known":
                    raise VersionUnknownError("写前实际版本未知，停止后续目标")
                package = self.artifact(row["release_id"], before)
                if package["manifest"] != row["manifest"] or package["policy"] != row["policy"]:
                    raise ValueError("待写固件或版本适用性发生变化")
                self._check()
                with self._lock:
                    row.update(before_write=before, status="writing", power_cycle_required=True, write_started_at=now())
                    self._job["status"] = "writing"
                    self._save()
                last_saved = time.monotonic()
                def progress(written, total):
                    nonlocal last_saved
                    with self._lock:
                        row.update(bytes_written=written, total_bytes=total)
                        if written == total or time.monotonic() - last_saved >= .25:
                            self._save()
                            last_saved = time.monotonic()
                receipt = self.writer(row, package["data"], self._stop, progress)
                if receipt.get("selfcheck_response") != "ff0201":
                    raise RuntimeError("设备自校验未得到已支持的明确通过响应")
                with self._lock:
                    row.update(write_receipt=receipt, write_finished_at=now(), status="awaiting_power")
                    self._job["status"] = "awaiting_power"
                    self._save()
                while not self._read.wait(.1):
                    if self._stop.is_set():
                        raise InterruptedError("等待人工操作期间取消；写入未核验")
                self._read.clear()
                self._check()
                with self._lock:
                    row["status"] = self._job["status"] = "verifying"
                    self._save()
                after = self.reader(row, self._stop)
                with self._lock:
                    row["readback"] = after
                    row["readback_at"] = now()
                    self._save()
                if after.get("status") != "known":
                    raise VersionUnknownError("版本未知，停止后续目标")
                m = row["manifest"]
                dest = row["policy"].get("entry", {}).get("target") if row["policy"].get("schema") == V2 else None
                expected = (m["version"], m["software_version"],
                            dest["hardware"] if dest else row["before"]["hardware"],
                            dest["protocol"] if dest else row["before"]["protocol"])
                if _version_identity(after) != expected or (mode and generation_for_target(row, after) != 109):
                    raise RuntimeError("回读版本不符，停止后续目标")
                with self._lock:
                    row.update(status="verified", actual_version_verified=True)
                    self._save()
        except Exception as exc:
            terminal_status = "interrupted" if isinstance(exc, InterruptedError) else ("version_unknown" if isinstance(exc, VersionUnknownError) else "failed")
            terminal_error = str(exc)
        finally:
            # Publish the terminal batch and every remaining target atomically.
            # Final state and its journal share one lock boundary: a poll must
            # not see a terminal outcome before the atomic save has completed.
            def finalize():
                self._job["status"] = terminal_status
                if terminal_error is not None:
                    self._job["error"] = terminal_error
                    if current and current["status"] != "verified":
                        current.update(status=terminal_status, error=terminal_error)
                for row in self._job["targets"]:
                    if row["status"] == "pending":
                        row["status"] = "not_run"
                self._job.update(active=False, ended_at=now())
            try:
                self._persist_and_release(finalize=finalize)
            except Exception as exc:
                self._retain_cleanup_failure(exc)
                logging.getLogger(__name__).exception("motor maintenance finalization quarantined")

    def _retain_cleanup_failure(self, exc):
        with self._lock:
            self._cleanup_error = str(exc)
            self._job["cleanup_error"] = str(exc)
            self._job["cleanup_quarantined"] = True

    def _persist_and_release(self, finalize=None):
        """The journal must succeed before any reservation is released.

        A failed save/close keeps the handle and the global serial claim. Only
        an explicit cancel/service close retries cleanup; it never replays OTA.
        """
        with self._cleanup_lock:
            with self._lock:
                if finalize is not None:
                    finalize()
                self._job.pop("cleanup_error", None)
                self._job.pop("cleanup_quarantined", None)
                self._save()
            if self._reservation is not None:
                close = getattr(self._reservation, "close", None)
                if callable(close):
                    close()
                else:
                    if self._unrecoverable_release:
                        raise RuntimeError("维护资源退出失败；需要检查原资源，不自动解除隔离")
                    try:
                        self._reservation.__exit__(None, None, None)
                    except Exception:
                        self._unrecoverable_release = True
                        raise
                self._reservation = None
            if self._serial_owned:
                SERIAL_LOCK.release()
                self._serial_owned = False
            with self._lock:
                self._cleanup_error = None

    def acknowledge(self, job_id):
        with self._lock:
            self._require_job(job_id)
            if self._job["status"] != "awaiting_power":
                raise ValueError("当前没有等待重新上下电的目标")
            row = next(r for r in self._job["targets"] if r["status"] == "awaiting_power")
            row["human_acknowledged_at"] = now()
            row["human_acknowledged_by"] = self.authorize()
            self._save()
            return self.snapshot()

    def readback(self, job_id):
        with self._lock:
            self._require_job(job_id)
            row = next((r for r in self._job["targets"] if r["status"] == "awaiting_power"), None)
            if row is None or not row["human_acknowledged_at"] or self._read.is_set():
                raise ValueError("先人工确认重新上下电，再单独请求回读")
            row["status"] = self._job["status"] = "readback_requested"
            self._save()
            self._read.set()
            return self.snapshot()

    def _require_job(self, job_id):
        if not self._job or self._job["job_id"] != job_id or self.authorize() != self._job["actor"]:
            raise ValueError("维护任务或账号不匹配")

    def cancel(self, timeout=5):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise TimeoutError("维护线程尚未退出，保留总线占用；请稍后重试关闭")
        if self._cleanup_error is not None:
            try:
                self._persist_and_release()
            except Exception as exc:
                self._retain_cleanup_failure(exc)
                raise RuntimeError(f"维护收尾失败，保留资源隔离: {exc}") from exc
        return self.snapshot()
