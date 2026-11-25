"""RP Hand adapters and bounded, feedback-checked motion tests."""
from __future__ import annotations

import importlib
import time
from types import SimpleNamespace

AXES = ("拇指侧摆", "拇指屈伸", "食指", "中指", "无名指", "小指")
SDK_REVISION = "3d2a5abb8f7299aea9ad7371ea94f37db87f79d6"
FIELDS = ("positions", "velocities", "currents", "enabled", "homed",
          "torque_reached", "fault_codes", "emergency_stopped", "stopped")


def build_hand(backend):
    if backend == "mock":
        return MockHand()
    try:
        module = importlib.import_module("rphand")
    except ImportError as exc:
        raise RuntimeError("未安装上位机配套 rphand SDK；请安装本次构建的 wheel 或新版上位机包") from exc
    # Upstream position commands can revoke an emergency stop implicitly.
    # Only accept our reproducible build with that branch disabled and explicit
    # emergency/stopped feedback exposed. Never silently load the unsafe ABI.
    if getattr(module, "HMI_SAFETY_API", None) != 1:
        raise RuntimeError("rphand SDK 缺少 HMI 安全接口 v1；请使用配套构建，禁止自动撤销急停")
    return module.Hand()


def snapshot(hand):
    state = hand.get_state()
    result = {key: list(getattr(state, key)) for key in FIELDS}
    if any(len(result[key]) != 6 for key in FIELDS):
        raise RuntimeError("灵巧手反馈不是六轴")
    result["feedback_valid"] = bool(state.feedback_valid)
    return result


def require_healthy(state, *, enabled=False, homed=False, stationary=False):
    if not state["feedback_valid"]:
        raise RuntimeError("灵巧手反馈缺失或超过 SDK 的 100 ms 新鲜度要求")
    if any(state["fault_codes"]):
        raise RuntimeError(f"灵巧手设备故障：{state['fault_codes']}")
    if any(state["emergency_stopped"]):
        raise RuntimeError("灵巧手处于急停状态；位置命令不会解除急停")
    if enabled and not all(state["enabled"]):
        raise RuntimeError("请先使能灵巧手全部六轴")
    if homed and not all(state["homed"]):
        raise RuntimeError("请先完成灵巧手回零")
    if stationary and not all(state["stopped"]):
        raise RuntimeError("灵巧手尚未静止")


def checked(hand, method, *args):
    if getattr(hand, method)(*args) is not True:
        raise RuntimeError(f"灵巧手 {method} 未确认成功")


def run_motion(hand, settings, cancel, publish, record, arm_guard=lambda: None, *, clock=time.monotonic):
    """Check that each commanded axis moves, without target-position acceptance.

    A changed integer position is movement evidence, not an accuracy or travel
    measurement. Still confirm stationary feedback before the next command;
    the caller always stops/disables the hand when the test ends.
    """
    state = snapshot(hand)
    require_healthy(state, enabled=True, homed=True, stationary=True)
    baseline = list(state["positions"])
    if any(not 0 <= x <= 10000 for x in baseline):
        raise RuntimeError("灵巧手当前行程超出 0..10000")
    axes = list(range(6)) if settings["axis"] == -1 else [settings["axis"]]
    movements = []
    for cycle in range(settings["cycles"]):
        for axis in axes:
            displaced = baseline.copy()
            amplitude = settings["amplitude"]
            displaced[axis] += amplitude if baseline[axis] + amplitude <= 10000 else -amplitude
            for leg, target in (("outward", displaced), ("return", baseline)):
                arm_guard()
                if cancel.is_set():
                    raise RuntimeError("灵巧手测试已取消")
                state = snapshot(hand)
                require_healthy(state, enabled=True, homed=True, stationary=True)
                start_position = state["positions"][axis]
                publish(f"第 {cycle+1}/{settings['cycles']} 轮 · {AXES[axis]} · 检查{'去程' if leg == 'outward' else '回程'}是否运动")
                checked(hand, "set_positions", target)
                deadline, settled, max_change = clock() + 10.0, 0, 0
                while clock() < deadline:
                    if cancel.wait(0.02):
                        raise RuntimeError("灵巧手测试已取消")
                    arm_guard()
                    state = snapshot(hand)
                    change = abs(state["positions"][axis] - start_position)
                    record({"target": target, "axis": axis, "cycle": cycle+1, "leg": leg,
                            "start_position": start_position, "position_change": change, **state})
                    require_healthy(state, enabled=True, homed=True)
                    max_change = max(max_change, change)
                    # Only the selected axis counts. Torque-reached feedback
                    # alone neither proves movement nor requires reaching target.
                    settled = settled + 1 if max_change > 0 and all(state["stopped"]) else 0
                    if settled >= 3:
                        movements.append({"cycle": cycle+1, "axis": axis, "leg": leg,
                                          "start_position": start_position,
                                          "end_position": state["positions"][axis],
                                          "requested_position": target[axis],
                                          "max_position_change": max_change, "movement_observed": True})
                        break
                else:
                    raise TimeoutError(f"{AXES[axis]} 10 秒内未检测到运动" if max_change == 0 else
                                       f"{AXES[axis]} 已检测到运动，但 10 秒内未确认停止")
    return {"passed": True, "criterion": "movement_observed", "cycles": settings["cycles"],
            "axes": axes, "movements": movements,
            "note": "受测轴去程、回程均观察到位置变化；不考核目标位置误差或实际运动幅度。"}


class MockHand:
    """Deterministic offline device. Its identity stays explicit in reports."""
    def __init__(self):
        self.connected = False
        self.state = SimpleNamespace(**{key: [False] * 6 for key in FIELDS}, feedback_valid=False)
        for key in ("positions", "velocities", "currents", "fault_codes"):
            setattr(self.state, key, [0] * 6)
        self.state.stopped = [True] * 6

    def connect(self, interface, node_id):
        self.connected = self.state.feedback_valid = True
        return True

    def disconnect(self):
        self.connected = self.state.feedback_valid = False

    def is_connected(self): return self.connected
    def get_state(self): return self.state
    def set_speed(self, value): return True
    def set_current_limit(self, value): return True
    def set_torque_reached_mode(self, value): return True
    def enable(self):
        self.state.enabled = [True] * 6
        return True
    def home(self):
        self.state.homed = [True] * 6
        self.state.positions = [0] * 6
        return True
    def set_positions(self, values):
        if any(self.state.emergency_stopped): return False
        self.state.positions = list(values)
        return True
    def stop(self):
        self.state.stopped = [True] * 6
        return True
    def disable(self):
        self.state.enabled = [False] * 6
        return True
    def prepare_disconnect(self):
        a = self.stop()
        b = self.disable()
        return a and b
