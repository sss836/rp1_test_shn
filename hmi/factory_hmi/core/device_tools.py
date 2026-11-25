"""Gateway-owned auxiliary device workers, leases and local evidence.

The GUI never owns a CAN socket. Long SDK calls run outside the status/lease
lock; a separate safety worker can cancel them. Failed cleanup keeps ownership.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import dexhand
from .imu_initialization import initialize_imu
from .can_manager import CanManager

logger = logging.getLogger(__name__)


class DeviceTools:
    def __init__(self, root: Path, acquire, release, arm_guard=lambda owner, config: None,
                 *, hand_factory=dexhand.build_hand, imu_runner=initialize_imu, lease_s=5.0):
        self.root, self.acquire, self.release, self.arm_guard = root, acquire, release, arm_guard
        self.hand_factory, self.imu_runner, self.lease_s = hand_factory, imu_runner, lease_s
        self.lock = threading.RLock()
        self.slots = {kind: self._empty(kind) for kind in ("imu", "dexhand")}
        self.hand = None
        self.closed = False
        self.shutdown_event = threading.Event()
        self.monitor = None

    @staticmethod
    def _empty(kind):
        return dict(kind=kind, phase="disconnected", owner=None, generation=None,
                    busy=False, connected=False, detail="未连接", config={}, feedback=None,
                    last_report=None, thread=None, safety_thread=None, cancel=threading.Event(),
                    last_heartbeat=0.0, resource=None, feedback_at=0.0, expired=False)

    def start(self):
        with self.lock:
            if self.monitor is None:
                self.monitor = threading.Thread(target=self._monitor, name="device-tools-monitor", daemon=True)
                self.monitor.start()

    def status(self, kind):
        with self.lock:
            slot = self.slots[kind]
            result = deepcopy({k: v for k, v in slot.items() if k not in
                               {"thread", "safety_thread", "cancel", "resource", "last_heartbeat", "feedback_at", "finish_wave"}})
            age = None if not slot["feedback_at"] else time.monotonic() - slot["feedback_at"]
            result["feedback_age_s"] = age
            if result["feedback"] is not None and (age is None or age > 0.3):
                result["feedback"]["feedback_valid"] = False
            return result

    def _owner(self, slot, owner, generation):
        if slot["owner"] != owner or slot["generation"] != generation:
            raise RuntimeError("设备由其他窗口控制或连接代次已失效，请刷新状态")

    def heartbeat(self, kind, owner, generation):
        with self.lock:
            slot = self.slots[kind]
            self._owner(slot, owner, generation)
            if slot["expired"] or time.monotonic() - slot["last_heartbeat"] > self.lease_s:
                slot["expired"] = True
                raise RuntimeError("设备控制心跳已过期，请等待安全收尾后重新连接")
            slot["last_heartbeat"] = time.monotonic()
        return self.status(kind)

    def submit(self, kind, owner, action, config, generation=None):
        with self.lock:
            if self.closed and action not in {"stop", "disconnect", "recover", "finish-wave"}:
                raise RuntimeError("设备工具正在关闭")
            slot = self.slots[kind]
            if kind == "imu" and action == "recover":
                self._owner(slot, owner, generation)
                if slot["phase"] != "cleanup_failed":
                    raise RuntimeError("只有 IMU 接口恢复失败后才需要核对恢复")
                expected = ((slot.get("last_report") or {}).get("result") or {}).get("restore_expected")
                if not expected:
                    raise RuntimeError("缺少预期接口状态，不能自动释放占用")
                state = CanManager().status([slot["config"]["interface"]])[0]
                if not state.exists or any(getattr(state, key) != value for key, value in expected.items()):
                    raise RuntimeError(f"接口尚未恢复到预期配置：{expected}")
                report = slot["last_report"]
                recovery = {"created_at": datetime.now(timezone.utc).isoformat(), "owner": owner,
                            "interface": state.to_dict(), "original_result_unchanged": True}
                (self.root / report["id"] / f"recovery-{uuid.uuid4()}.json").write_text(json.dumps(recovery, ensure_ascii=False, indent=2))
                self._release_locked(slot)
                slot.update(phase="disconnected", detail="已核对主机接口恢复；原失败记录保留")
                return self.status(kind)
            if kind == "dexhand" and action == "finish-wave":
                self._owner(slot, owner, generation)
                if slot["phase"] == "waving":
                    slot["finish_wave"].set()
                    slot["detail"] = "正在停止并核对失能反馈"
                    return self.status(kind)
                if slot["config"].get("operation") != "wave":
                    raise RuntimeError("当前没有手指运动")
                action = "disconnect"
            if action in {"stop", "disconnect"}:
                self._owner(slot, owner, generation)
                self._stop_locked(kind, disconnect=action == "disconnect", reason="操作员请求停止")
                return self.status(kind)
            if slot["busy"] or slot["phase"] == "cleanup_failed":
                raise RuntimeError("设备仍在操作或安全收尾失败；请先停止并确认")
            if kind == "imu" or action in {"connect", "read-state", "wave"}:
                if slot["owner"] is not None:
                    raise RuntimeError("设备仍被占用，请先断开")
                resolved, resource = self.acquire(kind, owner, {**config, "operation": action})
                if action == "wave":
                    slot["finish_wave"] = threading.Event()
                slot.update(owner=owner, generation=str(uuid.uuid4()), config=resolved, resource=resource,
                            expired=False, last_heartbeat=time.monotonic(), feedback=None, feedback_at=0,
                            disconnect_requested=False, fault_latched=False, parameters=None)
            else:
                self._owner(slot, owner, generation)
                if slot["expired"] or time.monotonic() - slot["last_heartbeat"] > self.lease_s:
                    raise RuntimeError("设备控制心跳已过期")
                if not slot["connected"]:
                    raise RuntimeError("请先连接设备")
            slot.update(busy=True, phase=action, detail="操作已受理", cancel=threading.Event(), fault_latched=False,
                        last_report=None)
            worker = threading.Thread(target=self._execute, args=(kind, action, dict(config)),
                                      name=f"device-{kind}-{action}", daemon=True)
            slot["thread"] = worker
            worker.start()
        return self.status(kind)

    def _publish(self, kind, text):
        with self.lock:
            self.slots[kind]["detail"] = text

    def _execute(self, kind, action, settings):
        slot = self.slots[kind]
        cancel = slot["cancel"]
        report_id = str(uuid.uuid4())
        folder = self.root / report_id
        result, error, stream = {}, None, None
        started = time.monotonic()
        try:
            folder.mkdir(parents=True, exist_ok=False)
            stream = (folder / "samples.csv").open("x", encoding="utf-8", newline="")
            writer = csv.writer(stream)
            writer.writerow(["elapsed_s", "raw_snapshot_json"])
            def record(sample):
                writer.writerow([time.monotonic() - started, json.dumps(sample, ensure_ascii=False, allow_nan=False)])
            if kind == "imu":
                result = self.imu_runner({**slot["config"], "action": action}, cancel,
                                         lambda text: self._publish(kind, text))
                for row in result.pop("samples", []): record(row)
            else:
                result = self._hand_action(action, settings, cancel, record)
            if cancel.is_set():
                raise RuntimeError("操作已取消，不记为通过")
            if result.get("passed") is not True:
                error = result.get("error") or result.get("note") or "设备反馈未通过检查"
        except Exception as exc:
            error = str(exc)
            result = {**result, "passed": False, "error": error, "cancelled": cancel.is_set()}
            logger.warning("device operation failed: kind=%s action=%s error=%s", kind, action, exc)
        finally:
            if stream is not None:
                stream.close()
        try:
            evidence = folder / "samples.csv"
            with evidence.open("rb") as sample_file:
                evidence_sha = hashlib.file_digest(sample_file, "sha256").hexdigest()
            report = {"schema": "rp1-device-tools-v1", "id": report_id, "kind": kind, "action": action,
                      "created_at": datetime.now(timezone.utc).isoformat(), "duration_s": time.monotonic()-started,
                      "config": slot["config"], "settings": settings, "owner": slot["owner"],
                      "applied_parameters": slot.get("parameters"),
                      "source_kind": "simulation" if slot["config"].get("backend") == "mock" else "hardware",
                      "result": result, "evidence_sha256": evidence_sha}
            (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            with self.lock:
                slot["last_report"] = {"id": report_id, "path": str(folder / "report.json"),
                                       "passed": result.get("passed", False), "action": action,
                                       "source_kind": report["source_kind"], "result": result}
        except Exception as exc:
            error = f"记录保存失败：{exc}" + (f"；{error}" if error else "")
        with self.lock:
            # Do not replace the state of a concurrently running safety task.
            if slot["phase"] not in {"stopping", "cleanup_failed"}:
                slot.update(busy=False, phase="fault" if error else "connected" if kind == "dexhand" else "completed",
                            detail=error or result.get("note", "操作完成"))
                if kind == "dexhand" and action in {"read-state", "wave"} and self.hand is None:
                    slot["phase"] = "fault" if error else "completed"
                    self._release_locked(slot)
                elif kind == "dexhand" and error and self.hand is not None:
                    self._stop_locked(kind, disconnect=action in {"read-state", "wave"}, reason=error)
            if kind == "imu":
                if result.get("restore_failed"):
                    slot.update(phase="cleanup_failed", busy=False, detail=result.get("error"))
                else:
                    self._release_locked(slot)
            elif action == "connect" and self.hand is None:
                self._release_locked(slot)

    def _hand_action(self, action, settings, cancel, record):
        slot = self.slots["dexhand"]
        cfg = slot["config"]
        if action in {"connect", "read-state", "wave"}:
            hand = self.hand_factory(cfg["backend"])
            with self.lock: self.hand = hand
            dexhand.checked(hand, "connect", cfg["interface"], cfg["node_id"])
            if cancel.is_set(): raise RuntimeError("连接已取消")
            with self.lock: slot["connected"] = True
            current = dexhand.snapshot(hand)
            record(current)
            if action == "read-state":
                valid = current["feedback_valid"]
                fault = any(current["fault_codes"])
                result = {"feedback": current, "passed": valid and not fault,
                          "note": "设备反馈无效" if not valid else "已读取六轴状态，设备报告故障" if fault else "已读取六轴状态"}
                hand.disconnect()
                with self.lock:
                    self.hand = None
                    slot["connected"] = False
                return result
            if action == "wave":
                return self._wave_session(hand, slot, cancel, record)
            return {"passed": current["feedback_valid"], "note": "连接完成；未自动使能或回零"}
        hand = self.hand
        guard = lambda: self.arm_guard(slot["owner"], cfg)
        state = dexhand.snapshot(hand)
        if action in {"enable", "home", "run"}:
            guard()
            dexhand.require_healthy(state, enabled=action != "enable", stationary=True)
        if action == "enable":
            for method, value in (("set_speed", settings["speed"]), ("set_current_limit", settings["current_limit"]),
                                  ("set_torque_reached_mode", True)):
                if cancel.is_set(): raise RuntimeError("使能已取消")
                dexhand.checked(hand, method, value)
            if cancel.is_set(): raise RuntimeError("使能已取消")
            dexhand.checked(hand, "enable")
            state = dexhand.snapshot(hand)
            dexhand.require_healthy(state, enabled=True)
            with self.lock:
                slot["parameters"] = {"speed": settings["speed"], "current_limit": settings["current_limit"]}
        elif action == "home":
            if cancel.is_set(): raise RuntimeError("回零已取消")
            dexhand.checked(hand, "home")
            state = dexhand.snapshot(hand)
            dexhand.require_healthy(state, enabled=True, homed=True, stationary=True)
        elif action == "run":
            try:
                if slot.get("parameters") != {"speed": settings["speed"], "current_limit": settings["current_limit"]}:
                    raise RuntimeError("测试参数与已设置参数不同；请停止手，再设置参数并使能")
                result = dexhand.run_motion(hand, settings, cancel,
                    lambda text: self._publish("dexhand", text), record, guard)
            finally:
                # Always attempt both via the native single-priority operation.
                dexhand.checked(hand, "prepare_disconnect")
                self._confirm_disabled(hand)
            return result
        elif action == "check":
            deadline = time.monotonic() + settings["duration_s"]
            while time.monotonic() < deadline:
                if cancel.wait(0.02): raise RuntimeError("检查已取消")
                state = dexhand.snapshot(hand)
                record(state)
                dexhand.require_healthy(state)
        else:
            raise ValueError("未知灵巧手操作")
        record(state)
        return {"passed": True, "note": "反馈检查完成；速度和电流限幅为发送及本地缓存确认，力矩停止模式只读回首轴，未证明六轴参数均已设备端读回。"}

    def _wave_session(self, hand, slot, cancel, record):
        cfg = slot["config"]
        def guard():
            if cancel.is_set():
                raise RuntimeError("手指运动已中断")
            self.arm_guard(slot["owner"], cfg)
        guard()
        dexhand.require_healthy(dexhand.snapshot(hand))
        self._publish("dexhand", "正在准备并回零")
        for method, args in (("disable", ()), ("set_speed", (16000,)), ("set_current_limit", (400,)),
                             ("set_torque_reached_mode", (False,)), ("enable", ()), ("home", ())):
            guard()
            dexhand.checked(hand, method, *args)
        with self.lock:
            if cancel.is_set():
                raise RuntimeError("手指运动已中断")
            slot.update(phase="waving", detail="手指正在循环往返")
        result = dexhand.run_wave(hand, cancel, slot["finish_wave"], guard, record)
        dexhand.checked(hand, "prepare_disconnect")
        result["feedback"] = self._confirm_disabled(hand)
        with self.lock:
            slot["connected"] = False
        hand.disconnect()
        with self.lock:
            self.hand = None
            slot["connected"] = False
        return result

    @staticmethod
    def _confirm_disabled(hand):
        state = dexhand.snapshot(hand)
        if not state["feedback_valid"] or any(state["enabled"]) or not all(state["stopped"]):
            raise RuntimeError("未获得灵巧手已停止且六轴失能的有效反馈")
        return state

    def _stop_locked(self, kind, *, disconnect, reason):
        slot = self.slots[kind]
        slot["cancel"].set()
        slot["disconnect_requested"] = slot.get("disconnect_requested", False) or disconnect or slot["config"].get("operation") in {"wave", "read-state"}
        if slot["phase"] == "stopping" and slot["safety_thread"] is not None and slot["safety_thread"].is_alive():
            return
        slot.update(phase="stopping", busy=True, detail=reason)
        worker = threading.Thread(target=self._cleanup, args=(kind,), name=f"device-{kind}-safety", daemon=True)
        slot["safety_thread"] = worker
        worker.start()

    def _cleanup(self, kind):
        slot = self.slots[kind]
        try:
            hand = self.hand if kind == "dexhand" else None
            # Native priority cancellation must happen before joining a home.
            if hand is not None and hand.is_connected() and slot["config"].get("operation") != "read-state":
                hand.prepare_disconnect()
            worker = slot["thread"]
            if worker is not None and worker is not threading.current_thread():
                worker.join(3.0)
                if worker.is_alive(): raise RuntimeError("设备操作仍未退出，保留 CAN 占用")
            # connect may have published its adapter while this worker joined.
            hand = self.hand if kind == "dexhand" else None
            if hand is not None and slot["connected"] and not hand.is_connected():
                raise RuntimeError("灵巧手连接已丢失，无法确认停止与失能，保留 CAN 占用")
            if kind == "imu" and (slot.get("last_report") or {}).get("result", {}).get("restore_failed"):
                raise RuntimeError("IMU 主机 CAN 恢复失败，请先在工位恢复接口并重启网关核对")
            if hand is not None and hand.is_connected() and slot["config"].get("operation") != "read-state":
                dexhand.checked(hand, "prepare_disconnect")
                self._confirm_disabled(hand)
            with self.lock:
                disconnect = slot.get("disconnect_requested", False) or slot["expired"]
                if kind == "dexhand" and not disconnect:
                    slot.update(phase="connected", busy=False, detail="灵巧手已停止并确认失能")
                    return
            if hand is not None and disconnect:
                hand.disconnect()
            with self.lock:
                if kind == "dexhand":
                    self.hand = None
                slot.update(connected=False, phase="disconnected", feedback=None)
                self._release_locked(slot)
                slot.update(busy=False, detail="已停止" if kind == "imu" else "灵巧手已停止并确认失能",
                            disconnect_requested=False)
        except Exception as exc:
            with self.lock:
                slot.update(busy=False, phase="cleanup_failed", detail=f"安全收尾未完成：{exc}")
            logger.error("device cleanup failed: kind=%s error=%s", kind, exc)

    def _release_locked(self, slot):
        if slot["resource"] is not None:
            self.release(slot["resource"])
        slot.update(owner=None, resource=None, connected=False)

    def _monitor(self):
        while not self.shutdown_event.wait(0.05):
            with self.lock:
                hand = self.hand
                slot = self.slots["dexhand"]
                connected = slot["connected"] and slot["config"].get("operation") != "read-state"
            if hand is not None and connected:
                try:
                    feedback = dexhand.snapshot(hand)
                    with self.lock:
                        if hand is not self.hand or not slot["connected"]:
                            continue
                        slot.update(feedback=feedback, feedback_at=time.monotonic())
                        if (not feedback["feedback_valid"] or any(feedback["fault_codes"]) or any(feedback["emergency_stopped"])) and not slot.get("fault_latched") and slot["phase"] not in {"stopping", "cleanup_failed"}:
                            slot["fault_latched"] = True
                            self._stop_locked("dexhand", disconnect=False, reason="设备反馈异常，停止并失能")
                    if any(feedback["enabled"]) and slot["phase"] not in {"stopping", "cleanup_failed"}:
                        self.arm_guard(slot["owner"], slot["config"])
                except Exception as exc:
                    with self.lock:
                        if hand is self.hand and slot["connected"] and slot["phase"] not in {"stopping", "cleanup_failed"}:
                            self._stop_locked("dexhand", disconnect=False, reason=str(exc))
            with self.lock:
                for kind, slot in self.slots.items():
                    if slot["owner"] and not slot["expired"] and time.monotonic() - slot["last_heartbeat"] > self.lease_s:
                        slot["expired"] = True
                        self._stop_locked(kind, disconnect=True, reason="界面心跳丢失，停止并断开")

    def hand_uses_arm(self, owner):
        with self.lock:
            slot = self.slots["dexhand"]
            return slot["owner"] == owner and slot["config"].get("follow_arm", False)

    def stop_owner(self, owner):
        with self.lock:
            slot = self.slots["dexhand"]
            if owner is not None and slot["owner"] == owner:
                self._stop_locked("dexhand", disconnect=False, reason="手臂停止请求，同时停止灵巧手")

    def close(self, *, owner=None):
        with self.lock:
            if owner is None: self.closed = True
            workers = []
            for kind, slot in self.slots.items():
                if slot["owner"] is not None and (owner is None or slot["owner"] == owner):
                    self._stop_locked(kind, disconnect=True, reason="关闭设备工具")
                    workers.append((kind, slot["safety_thread"]))
        for kind, worker in workers:
            worker.join(6.0)
            if worker.is_alive() or self.status(kind)["phase"] == "cleanup_failed":
                raise RuntimeError(f"{kind} 安全关闭尚未完成，保留连接与占用")
        if owner is None:
            self.shutdown_event.set()
            if self.monitor is not None: self.monitor.join(1.0)

    def report_path(self, report_id, filename):
        if str(uuid.UUID(report_id)) != report_id or filename not in {"report.json", "samples.csv"}:
            raise ValueError("无效报告路径")
        path = self.root / report_id / filename
        if not path.is_file(): raise ValueError("报告尚未保存或不存在")
        return path
