from __future__ import annotations

import hashlib
import json
import struct
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factory_hmi.core.device_tools import DeviceTools
from factory_hmi.core.dexhand import MockHand, snapshot, run_motion
from factory_hmi.core.imu_initialization import COMMANDS, initialize_imu, decode_measurement
from factory_hmi.gateway.app import create_app


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(0.01)
    raise AssertionError("worker did not reach the expected state")


class ImuFixture:
    def __init__(self, *, feedback=True, fail_at=None, restore_fail=False, bitrate=250000, up=True):
        self.now, self.sent, self.rates, self.cancelled = 0, [], [], False
        self.feedback, self.fail_at, self.restore_fail = feedback, fail_at, restore_fail
        self.link = SimpleNamespace(exists=True, up=up, bitrate=bitrate, dbitrate=None, fd=False)
        self.interfaces, self.receive_rates = [], []
    def status(self, names):
        self.interfaces.extend(names)
        return [deepcopy(self.link)]
    def connect(self, names, **timing):
        self.interfaces.extend(names)
        self.rates.append(timing["bitrate"])
        if self.restore_fail and len(self.rates) > 1: raise RuntimeError("helper rejected")
        self.link = SimpleNamespace(exists=True, up=True, **timing)
        return self.status(names)
    def disconnect(self, names):
        self.interfaces.extend(names)
        self.link.up = False
    def open(self): pass
    def close(self): pass
    def send(self, data):
        self.sent.append(data.hex().upper())
        if len(self.sent) == self.fail_at: raise RuntimeError("partial send")
    def wait(self, seconds):
        self.now += seconds
        return self.cancelled
    def is_set(self): return self.cancelled
    def receive(self):
        self.receive_rates.append(self.link.bitrate)
        self.now += .01
        if not self.feedback: return None
        i = int(round(self.now * 100)) % 3
        return [(0x18FF4608, struct.pack("<4h", 10000, 0, 0, 0)),
                (0x18FF3408, struct.pack("<4h", 0, 0, 2048, 0)),
                (0x18FF3708, struct.pack("<4h", 0, 0, 0, 0))][i]
    def run(self, action="initialize", interface="can2"):
        return initialize_imu({"interface": interface, "action": action}, self, lambda msg: None,
                              can=self, transport_factory=lambda name: self, clock=lambda: self.now)


def test_imu_initialize_sends_commands_and_leaves_read_bitrate_without_checking():
    f = ImuFixture()
    result = f.run(interface="can4")
    assert f.sent == [row[1] for row in COMMANDS]
    assert f.rates == [500000, 1000000]
    assert result["passed"] and result["command_sequence_sent"]
    assert f.link.bitrate == 1000000 and set(f.interfaces) == {"can4"}
    assert set(f.receive_rates) == {500000}  # only target identification before writes
    assert result["feedback_checked"] is False and not result["samples"]


@pytest.mark.parametrize("initial_rate,up,changes", [(500000, True, [1000000]),
                         (None, False, [1000000]), (1000000, True, [])])
def test_imu_standalone_check_sets_host_rate_and_sends_no_commands(initial_rate, up, changes):
    f = ImuFixture(bitrate=initial_rate, up=up)
    result = f.run("check", "can4")
    assert f.rates == changes and not f.sent and set(f.interfaces) == {"can4"}
    assert set(f.receive_rates) == {1000000}
    assert result["feedback_checked"] and result["passed"] and not result["command_sequence_sent"]
    assert result["observed_hz"]["quaternion_wxyz"] == pytest.approx(100 / 3, abs=.3)
    assert result["latest"]["acceleration_m_s2"][2] == pytest.approx(9.80665)
    assert decode_measurement(0x18FF4308, struct.pack("<h", 2534)) == {"temperature_c": 25.34}


def test_imu_without_target_never_writes_and_no_feedback_fails_check():
    f = ImuFixture(feedback=False)
    assert not f.run()["passed"] and not f.sent and f.link.bitrate == 250000
    result = f.run("check")
    assert result["feedback_checked"] and not result["passed"] and not f.sent


def test_imu_check_refuses_unconfirmed_host_rate():
    f = ImuFixture(bitrate=500000)
    f.connect = lambda names, **timing: f.status(names)
    result = f.run("check")
    assert not result["passed"] and not result["feedback_checked"] and not f.receive_rates
    assert "1000000" in result["error"] and not f.sent


def test_imu_gateway_records_separate_operations(tmp_path):
    app = create_app(data_root=tmp_path)
    hw = ImuFixture()
    tools = app.state.runtime.device_access.tools
    tools.imu_runner = lambda cfg, cancel, publish: initialize_imu(
        cfg, hw, publish, can=hw, transport_factory=lambda interface: hw, clock=lambda: hw.now)
    with TestClient(app) as client:
        def run(action):
            response = client.post("/api/v1/device-tools/imu/" + action, json={"interface": "can4"})
            assert response.status_code == 200
            wait_for(lambda: tools.status("imu")["owner"] is None)
            report_id = tools.status("imu")["last_report"]["id"]
            return client.get(f"/api/v1/device-reports/{report_id}/report.json").json()
        init = run("initialize")
        check = run("check")
    assert init["id"] != check["id"]
    assert init["action"] == "initialize" and not init["result"]["feedback_checked"]
    assert check["action"] == "check" and check["result"]["feedback_checked"]
    assert check["result"]["passed"] and len(hw.sent) == 6


@pytest.mark.parametrize("fail_at,restored", [(2, 250000), (4, 1000000)])
def test_imu_partial_send_restores_host_and_preserves_events(fail_at, restored):
    f = ImuFixture(fail_at=fail_at)
    result = f.run()
    assert result["passed"] is False
    assert "partial send" in result["error"]
    assert result["events"] and f.rates[-1] == restored
    assert len(f.sent) == fail_at  # never retry uncertain device writes


def test_imu_restore_failure_is_explicit():
    result = ImuFixture(fail_at=4, restore_fail=True).run()
    assert result["restore_failed"] and not result["passed"]


def make_tools(tmp_path, hand=None, **kwargs):
    released = []
    service = DeviceTools(tmp_path, lambda kind, owner, cfg: (cfg, "claim"), released.append,
                          hand_factory=lambda _: hand or MockHand(), **kwargs)
    service.start()
    return service, released


def connect(service):
    state = service.submit("dexhand", "window-a", "connect", {"backend": "mock", "interface": "can1", "node_id": 1})
    wait_for(lambda: not service.status("dexhand")["busy"])
    return state["generation"]


def test_hand_run_local_evidence_and_no_auto_enable(tmp_path):
    service, released = make_tools(tmp_path)
    try:
        generation = connect(service)
        assert not any(snapshot(service.hand)["enabled"])
        settings = {"speed": 500, "current_limit": 200, "axis": -1, "amplitude": 300, "cycles": 1}
        for action in ("enable", "home", "run"):
            service.submit("dexhand", "window-a", action, settings, generation)
            wait_for(lambda: not service.status("dexhand")["busy"])
            assert service.status("dexhand")["last_report"]["passed"]
        assert not any(snapshot(service.hand)["enabled"])
        report = service.status("dexhand")["last_report"]
        raw = json.loads(service.report_path(report["id"], "report.json").read_text())
        data = service.report_path(report["id"], "samples.csv").read_bytes()
        assert raw["source_kind"] == "simulation" and raw["evidence_sha256"] == hashlib.sha256(data).hexdigest()
        assert raw["result"]["criterion"] == "movement_observed"
        assert len(raw["result"]["movements"]) == 12
        assert released == []  # connection retained until explicit disconnect
    finally:
        service.close()
    assert released == ["claim"]


def test_owner_generation_and_dead_gui_expiry(tmp_path):
    service, released = make_tools(tmp_path, lease_s=.15)
    generation = connect(service)
    with pytest.raises(RuntimeError): service.heartbeat("dexhand", "window-b", generation)
    with pytest.raises(RuntimeError): service.submit("dexhand", "window-a", "enable", {}, "old")
    wait_for(lambda: service.status("dexhand")["owner"] is None)
    assert released == ["claim"] and service.hand is None
    with pytest.raises(RuntimeError): service.heartbeat("dexhand", "window-a", generation)
    service.close()


def test_cleanup_failure_retains_resource_and_can_be_retried(tmp_path):
    hand = MockHand()
    service, released = make_tools(tmp_path, hand)
    generation = connect(service)
    hand.state.enabled = [True] * 6
    original = hand.prepare_disconnect
    hand.prepare_disconnect = lambda: False
    service.submit("dexhand", "window-a", "disconnect", {}, generation)
    wait_for(lambda: service.status("dexhand")["phase"] == "cleanup_failed")
    assert not released and service.status("dexhand")["owner"] == "window-a"
    hand.prepare_disconnect = original
    service.submit("dexhand", "window-a", "disconnect", {}, generation)
    wait_for(lambda: service.status("dexhand")["owner"] is None)
    service.close()


def test_emergency_blocks_motion_without_revoke():
    hand = MockHand()
    hand.connect("can1", 1)
    hand.enable()
    hand.home()
    hand.state.emergency_stopped[0] = True
    with pytest.raises(RuntimeError, match="急停"):
        run_motion(hand, {"axis": 0, "cycles": 1}, threading.Event(), lambda _: None, lambda _: None)
    assert hand.state.emergency_stopped[0] and hand.state.positions == [0] * 6


class Arm:
    def __init__(self):
        self.calls = []
        self.snap = {"configured": True, "config_revision": 3, "backend": "fake", "state": "connected",
                     "station": {"limb": "right_arm", "motors_enabled": True, "position_hold_active": True},
                     "configured_motors": [{"motor_id": i, "motor_type": "LRO", "bus": "can1"} for i in range(1, 8)],
                     "motors": [{"position_rad": 0., "speed_rad_s": 0., "feedback_age_s": .01} for _ in range(7)]}
    def snapshot(self): return deepcopy(self.snap)
    def close(self): self.calls.append("close")
    def disable(self): self.calls.append("disable")
    def can_disconnect(self): self.calls.append("can_down")
    def start(self): self.calls.append("start")


def test_shared_arm_default_reservation_no_bus_or_arm_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "test-only")
    arm = Arm()
    app = create_app(arm, data_root=tmp_path)
    headers = {"X-RP1-Client-ID": "window-a", "X-RP1-Operator-Token": "test-only"}
    with TestClient(app) as client:
        hub = app.state.runtime
        hub.session("window-a").lease.claim("window-a")
        hub._reserve_interfaces("shared", ["can1"])
        def post(action, **body):
            return client.post("/api/v1/device-tools/dexhand/" + action, json=body, headers=headers)
        assert client.post("/api/v1/device-tools/dexhand/connect", json={}).status_code == 401
        response = post("connect", backend="mock")
        assert response.status_code == 200, response.text
        generation = response.json()["generation"]
        wait_for(lambda: not hub.device_access.tools.status("dexhand")["busy"])
        assert hub.device_access.tools.status("dexhand")["config"]["interface"] == "can1"
        assert hub.interface_owners() == {"can1": "shared"}
        assert client.post("/api/v1/can/disconnect", headers=headers).status_code == 409
        assert post("run", generation=generation, amplitude=50000).status_code == 422
        assert post("run", generation=generation, amplitude=0).status_code == 422
        assert post("run", generation=generation, tolerance=100).status_code == 422
        assert post("enable", generation=generation, speed=7).status_code == 422
        for action in ("enable", "home", "run"):
            assert post(action, generation=generation, amplitude=1).status_code == 200
            wait_for(lambda: not hub.device_access.tools.status("dexhand")["busy"])
            assert hub.device_access.tools.status("dexhand")["last_report"]["passed"]
        assert arm.calls == []
        assert post("disconnect", generation=generation).status_code == 200
        wait_for(lambda: hub.device_access.tools.status("dexhand")["owner"] is None)
        assert hub.interface_owners() == {"can1": "shared"}
        assert arm.calls == []


def test_shared_arm_protocol_mismatch_and_motion_are_rejected(tmp_path):
    arm = Arm()
    app = create_app(arm, data_root=tmp_path)
    with TestClient(app):
        hub = app.state.runtime
        hub.session("a").lease.claim("a")
        hub._reserve_interfaces("shared", ["can1"])
        settings = {"backend": "mock", "follow_arm": True, "node_id": 1}
        arm.snap["state"] = "running"
        with pytest.raises(RuntimeError, match="工位空闲"):
            hub.device_access.tools.submit("dexhand", "a", "read-state", settings)
        assert not hub.device_access.borrowed("a")
        arm.snap["state"] = "connected"
        arm.snap["configured_motors"][0]["motor_type"] = "EVO"
        with pytest.raises(RuntimeError, match="LRO"):
            hub.device_access.tools.submit("dexhand", "a", "connect", settings)
        arm.snap["configured_motors"][0]["motor_type"] = "LRO"
        arm.snap["motors"][0]["speed_rad_s"] = .2
        with pytest.raises(RuntimeError, match="位置变化"):
            hub.device_access.tools.submit("dexhand", "a", "connect", settings)
        assert arm.calls == []


def test_hardware_admission_only_reads_shared_can_and_imu_cannot_claim_it(tmp_path, monkeypatch):
    class ReadOnlyCan:
        bus_state = "bus-off"
        reads = []
        def status(self, names):
            self.reads.append(names)
            return [SimpleNamespace(exists=True, up=True, fd=True, bitrate=1000000, dbitrate=5000000, bus_state=self.bus_state)]
        def connect(self, *args, **kwargs): pytest.fail("must never reconfigure the arm bus")
        def disconnect(self, *args, **kwargs): pytest.fail("must never take down the arm bus")
    monkeypatch.setattr("factory_hmi.gateway.device_tools.CanManager", ReadOnlyCan)
    arm = Arm()
    arm.snap["backend"] = "motors_py"
    app = create_app(arm, data_root=tmp_path)
    with TestClient(app):
        hub = app.state.runtime
        hub.session("a").lease.claim("a")
        hub._reserve_interfaces("shared", ["can1"])
        hub.device_access.tools.hand_factory = lambda backend: MockHand()
        cfg = {"backend": "hardware", "follow_arm": True, "node_id": 1}
        with pytest.raises(RuntimeError, match="CAN FD"):
            hub.device_access.tools.submit("dexhand", "a", "connect", cfg)
        assert not hub.device_access.borrowed("a")
        ReadOnlyCan.bus_state = "ERROR-ACTIVE"
        state = hub.device_access.tools.submit("dexhand", "a", "connect", cfg)
        wait_for(lambda: not hub.device_access.tools.status("dexhand")["busy"])
        with pytest.raises(RuntimeError, match="已被工位占用"):
            hub.device_access.tools.submit("imu", "a", "initialize", {"backend": "hardware", "interface": "can1"})
        hub.device_access.tools.submit("dexhand", "a", "disconnect", {}, state["generation"])
        wait_for(lambda: hub.device_access.tools.status("dexhand")["owner"] is None)
        assert ReadOnlyCan.reads == [["can1"], ["can1"]]
        assert hub.interface_owners() == {"can1": "shared"} and not arm.calls


def test_stop_while_connecting_cleans_adapter_published_after_stop(tmp_path):
    entered, proceed = threading.Event(), threading.Event()
    hand = MockHand()
    def delayed_factory(backend):
        entered.set()
        assert proceed.wait(1)
        return hand
    released = []
    tools = DeviceTools(tmp_path, lambda k, o, c: (c, "claim"), released.append, hand_factory=delayed_factory)
    state = tools.submit("dexhand", "a", "connect", {"backend": "mock", "interface": "can1", "node_id": 1})
    assert entered.wait(1)
    tools.submit("dexhand", "a", "disconnect", {}, state["generation"])
    proceed.set()
    wait_for(lambda: tools.status("dexhand")["owner"] is None)
    assert not hand.is_connected() and not any(hand.state.enabled) and released == ["claim"]
    tools.close()


def test_imu_recovery_verifies_host_without_changing_failed_report(tmp_path, monkeypatch):
    result = {"passed": False, "restore_failed": True, "error": "helper rejected",
              "restore_expected": {"up": True, "bitrate": 1000000, "fd": False}}
    tools, released = make_tools(tmp_path, imu_runner=lambda *args: deepcopy(result))
    state = tools.submit("imu", "a", "initialize", {"backend": "hardware", "interface": "can2"})
    wait_for(lambda: tools.status("imu")["phase"] == "cleanup_failed")
    report_id = tools.status("imu")["last_report"]["id"]
    before = tools.report_path(report_id, "report.json").read_bytes()
    status = SimpleNamespace(exists=True, up=True, bitrate=500000, fd=False, to_dict=lambda: {"up": True, "bitrate": 1000000, "fd": False})
    monkeypatch.setattr("factory_hmi.core.device_tools.CanManager", lambda: SimpleNamespace(status=lambda names: [status]))
    with pytest.raises(RuntimeError, match="尚未恢复"):
        tools.submit("imu", "a", "recover", {}, state["generation"])
    assert not released
    status.bitrate = 1000000
    tools.submit("imu", "a", "recover", {}, state["generation"])
    assert released == ["claim"] and tools.report_path(report_id, "report.json").read_bytes() == before
    assert len(list((tmp_path / report_id).glob("recovery-*.json"))) == 1
    tools.close()


def test_arm_motion_after_enable_stops_only_hand(tmp_path):
    arm = Arm()
    app = create_app(arm, data_root=tmp_path)
    with TestClient(app):
        hub = app.state.runtime
        hub.session("a").lease.claim("a")
        hub._reserve_interfaces("shared", ["can1"])
        tools = hub.device_access.tools
        state = tools.submit("dexhand", "a", "connect", {"backend": "mock", "follow_arm": True, "node_id": 1})
        wait_for(lambda: not tools.status("dexhand")["busy"])
        tools.submit("dexhand", "a", "enable", {"speed": 500, "current_limit": 200}, state["generation"])
        wait_for(lambda: not tools.status("dexhand")["busy"])
        assert all(tools.hand.state.enabled)
        arm.snap["motors"][0]["position_rad"] = .1
        wait_for(lambda: not any(tools.hand.state.enabled))
        wait_for(lambda: not tools.status("dexhand")["busy"])
        assert not arm.calls and hub.interface_owners() == {"can1": "shared"}


def test_lost_feedback_does_not_release_unknown_hardware(tmp_path):
    hand = MockHand()
    tools, released = make_tools(tmp_path, hand)
    generation = connect(tools)
    hand.state.feedback_valid = False
    wait_for(lambda: tools.status("dexhand")["phase"] == "cleanup_failed")
    assert not released
    hand.state.feedback_valid = True
    tools.submit("dexhand", "window-a", "disconnect", {}, generation)
    wait_for(lambda: tools.status("dexhand")["owner"] is None)
    tools.close()


class MotionClock:
    def __init__(self, cancel_at=float("inf")):
        self.now, self.cancel_at = 0., cancel_at
    def is_set(self): return self.now >= self.cancel_at
    def wait(self, seconds):
        self.now += seconds
        return self.is_set()


def exercise_motion(hand, *, axis=0, cancel_at=float("inf")):
    hand.connect("can1", 1)
    hand.enable()
    hand.home()
    clock = MotionClock(cancel_at)
    records = []
    result = run_motion(hand, {"axis": axis, "amplitude": 300, "cycles": 1}, clock,
                        lambda text: None, records.append, clock=lambda: clock.now)
    return result, records


@pytest.mark.parametrize("torque_reached", [False, True])
def test_partial_movement_passes_without_reaching_target(torque_reached):
    class PartialHand(MockHand):
        def set_positions(self, target):
            for i, desired in enumerate(target):
                delta = desired - self.state.positions[i]
                if delta:
                    self.state.positions[i] += (1 if delta > 0 else -1) * max(1, abs(delta) // 4)
            self.state.torque_reached = [torque_reached] * 6
            return True
    result, records = exercise_motion(PartialHand(), axis=-1)
    assert result["passed"] and result["criterion"] == "movement_observed"
    assert len(result["movements"]) == 12
    assert all(step["max_position_change"] > 0 for step in result["movements"])
    assert all(step["end_position"] != step["requested_position"] for step in result["movements"])
    assert result["movements"][0]["end_position"] == 75  # 225 short of requested 300
    assert records[0]["axis"] == 0 and records[0]["leg"] == "outward"


@pytest.mark.parametrize("torque_reached", [False, True])
def test_acknowledged_command_without_movement_fails(torque_reached):
    class StuckHand(MockHand):
        def set_positions(self, target):
            self.state.torque_reached = [torque_reached] * 6
            return True
    with pytest.raises(TimeoutError, match="未检测到运动"):
        exercise_motion(StuckHand())


def test_movement_of_another_axis_does_not_pass_selected_axis():
    class WrongAxisHand(MockHand):
        def set_positions(self, target):
            self.state.positions[1] += 100
            return True
    with pytest.raises(TimeoutError, match="拇指侧摆.*未检测到运动"):
        exercise_motion(WrongAxisHand())


def test_return_requires_new_movement_after_its_own_command():
    class OneWayHand(MockHand):
        def set_positions(self, target):
            if target[0] > 0: self.state.positions[0] = 75
            return True
    with pytest.raises(TimeoutError, match="未检测到运动"):
        exercise_motion(OneWayHand())


def test_movement_does_not_bypass_stop_confirmation():
    class NeverStopsHand(MockHand):
        def set_positions(self, target):
            self.state.positions[0] += 10
            self.state.stopped = [False] * 6
            return True
    with pytest.raises(TimeoutError, match="已检测到运动.*未确认停止"):
        exercise_motion(NeverStopsHand())


def test_cancel_still_interrupts_movement_check():
    with pytest.raises(RuntimeError, match="已取消"):
        exercise_motion(MockHand(), cancel_at=.03)


def test_offline_gateway_cannot_open_physical_imu(monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_CAN_DISABLED", "1")
    with pytest.raises(RuntimeError, match="离线实例"):
        initialize_imu({"action": "check", "interface": "can2"}, threading.Event(), lambda _: None)


def test_initialized_imu_must_pass_installed_bus_policy(monkeypatch):
    calls = []
    class DeniedInterface:
        def authorize_reuse(self, interface, **timing):
            calls.append((interface, timing))
            return False
    monkeypatch.delenv("RP1_FACTORY_CAN_DISABLED", raising=False)
    monkeypatch.setattr("factory_hmi.core.imu_initialization.CanManager", DeniedInterface)
    with pytest.raises(RuntimeError, match="授权配置"):
        initialize_imu({"action": "check", "interface": "can8"}, threading.Event(), lambda _: None)
    assert calls == [("can8", {"bitrate": 1000000, "dbitrate": None, "fd": False})]


def test_single_state_read_never_enables_or_stops_the_hand(tmp_path):
    class Observer(MockHand):
        def prepare_disconnect(self): pytest.fail("snapshot issued a stop")
        def enable(self): pytest.fail("snapshot enabled the hand")
        def home(self): pytest.fail("snapshot homed the hand")
        def set_positions(self, targets): pytest.fail("snapshot moved the hand")
    hand = Observer()
    tools, released = make_tools(tmp_path, hand)
    try:
        settings = {"backend": "mock", "follow_arm": False, "interface": "can3", "node_id": 1}
        tools.submit("dexhand", "a", "read-state", settings)
        wait_for(lambda: tools.status("dexhand")["owner"] is None)
        report = tools.status("dexhand")["last_report"]
        assert report["passed"] and report["result"]["feedback"]["positions"] == [0] * 6
        assert released == ["claim"] and not hand.is_connected()
    finally:
        tools.close()


def test_continuous_wave_follows_demo_and_requires_measured_round_trip():
    from factory_hmi.core.dexhand import run_wave, wave_target
    assert wave_target(1) == [10000, 0, 0, 0, 0, 0]
    assert wave_target(3) == [0, 10000, 0, 0, 0, 0]
    assert wave_target(5) == [0, 0, 0, 3000, 6000, 3000]
    assert wave_target(10) == [0] * 6
    class Finish:
        now = 0.
        def is_set(self): return self.now >= 20.1
        def wait(self, duration): self.now += max(.000001, duration)
    hand = MockHand()
    hand.connect("can3", 1)
    hand.enable()
    hand.home()
    finish = Finish()
    result = run_wave(hand, threading.Event(), finish, lambda: None, lambda s: None, clock=lambda: finish.now)
    assert result["passed"] and result["rounds"] == 2
    hand.set_positions = lambda target: True
    finish = Finish()
    with pytest.raises(RuntimeError, match="未观察到往返"):
        run_wave(hand, threading.Event(), finish, lambda: None, lambda s: None, clock=lambda: finish.now)


def test_wave_button_stops_disables_and_releases(tmp_path):
    hand = MockHand()
    tools, released = make_tools(tmp_path, hand)
    settings = {"backend": "mock", "follow_arm": False, "interface": "can3", "node_id": 1}
    try:
        state = tools.submit("dexhand", "a", "wave", settings)
        wait_for(lambda: tools.status("dexhand")["phase"] == "waving")
        tools.submit("dexhand", "a", "finish-wave", {}, state["generation"])
        wait_for(lambda: tools.status("dexhand")["owner"] is None)
        result = tools.status("dexhand")["last_report"]["result"]
        assert not result["passed"] and not any(result["feedback"]["enabled"])
        assert released == ["claim"] and not hand.is_connected()
    finally:
        tools.close()


@pytest.mark.parametrize("state_at", ["top", "station"])
def test_running_arm_rejects_new_shared_read_before_sdk_connect(tmp_path, state_at):
    arm = Arm()
    app = create_app(arm, data_root=tmp_path)
    headers = {"X-RP1-Client-ID": "window-a"}
    with TestClient(app) as client:
        hub = app.state.runtime
        hub.session("window-a").lease.claim("window-a")
        hub._reserve_interfaces("shared", ["can1"])
        (arm.snap if state_at == "top" else arm.snap["station"])["state"] = "running"
        hub.device_access.tools.hand_factory = lambda _: pytest.fail("busy arm opened the hand SDK")
        response = client.post("/api/v1/device-tools/dexhand/read-state", headers=headers, json={"backend": "mock"})
        assert response.status_code == 409 and "共线" in response.json()["detail"]
        assert not hub.device_access.borrowed("window-a") and not arm.calls


def test_shared_read_after_confirmed_stop_does_not_require_reenable(tmp_path):
    arm = Arm()
    arm.snap["station"].update(motors_enabled=False, position_hold_active=False, commands_inhibited=True)
    arm.snap["motor_disable"] = {"state": "confirmed", "scope": "all"}
    app = create_app(arm, data_root=tmp_path)
    with TestClient(app) as client:
        hub = app.state.runtime
        hub.session("a").lease.claim("a")
        hub._reserve_interfaces("shared", ["can1"])
        def read():
            return client.post("/api/v1/device-tools/dexhand/read-state", headers={"X-RP1-Client-ID": "a"},
                               json={"backend": "mock"})
        assert read().status_code == 200
        wait_for(lambda: hub.device_access.tools.status("dexhand")["owner"] is None)
        arm.snap["motor_disable"]["state"] = "unconfirmed"
        assert read().status_code == 409 and not arm.calls
