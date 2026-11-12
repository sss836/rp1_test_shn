from dataclasses import replace
import math
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend, MotorProtocolPending, MotorSafetyError, MotorsPyBackend
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.service import FactoryService
from factory_hmi.core.playback import _RunStatistics
from factory_hmi.core.telemetry_hub import TelemetryHub
from factory_hmi.core.zeroing import MotorZeroingSession
from factory_hmi.models import MotorZeroResult, StationConfig, StationState
from scripts.log_motor_telemetry import csv_fieldnames, sample_row
from scripts.rp1_limb_common import LroProtocolExpectation


EXPECTED = LroProtocolExpectation("v1.03", "5.0.0.109", "5.0.1", "1.2.3", "1.0.3.0", "fixture")


class OfflineLro:
    def __init__(self, label, numeric=(5, 0, 1, 1, 2, 3, 1, 0, 3, 0)):
        self.label = label
        self.numeric = numeric
        self.profile = "legacy"
        self.enabled = False
        self.reply_count = 0
        self.enables = 0
        self.zero_requests = 0
        self.read_only_status_responds = True

    def request_version(self, code):
        return (self.label is not None if code == 0x31 else self.numeric is not None) if code in (0x31,0x1e) else False

    def version_payload(self, code):
        head = bytes((0xA0, code))
        if code == 0x31:
            return (head + self.label.encode("ascii")).ljust(20, b"\0")
        return head + bytes(self.numeric)

    def set_lro_protocol_profile(self, value):
        assert not self.enabled
        self.profile = value
        return True

    def set_motor_control_mode(self, _mode):
        pass

    def init_motor(self):
        self.enables += 1
        self.enabled = True
        self.reply_count += 1
        return 0

    def unlock_motor(self):
        self.enabled = False

    deinit_motor = unlock_motor

    def refresh_motor_status(self):
        if self.read_only_status_responds:
            self.reply_count += 1

    def get_feedback_count(self):
        return self.reply_count

    def get_motor_pos(self):
        return 0.1

    def get_motor_spd(self):
        return 0.2

    def get_motor_current(self):
        assert self.profile == "legacy"
        return 3.0

    def get_motor_temperature(self):
        return 31.0

    def get_error_id(self):
        return 0

    def get_motor_drive_enabled(self):
        return self.enabled

    def get_motor_feedback_header(self):
        return 0x30 if self.enabled else 0x20

    def get_motor_iq_current(self):
        return 10.0

    def get_motor_dc_bus_voltage(self):
        return 48.0 if self.profile == "v1.03" else math.nan

    def get_motor_dc_bus_current(self):
        return -2.0 if self.profile == "v1.03" else math.nan

    def request_lro_zero(self):
        assert self.profile == "v1.03" and not self.enabled
        self.zero_requests += 1
        return True


@pytest.fixture
def open_backend(monkeypatch):
    backends = []
    monkeypatch.setitem(sys.modules, "motors_py", SimpleNamespace(MotorControlMode=SimpleNamespace(MIT="MIT")))

    def open_for(*nodes):
        template = FakeMotorBackend(len(nodes)).entries
        entries = [replace(entry, interface_type="canfd", motor_type="LRO", motor_model=0, lro_protocol=expected)
                   for entry, (_, expected) in zip(template, nodes)]
        monkeypatch.setattr("factory_hmi.core.backend.create_motor_driver", lambda e, _: nodes[e.index][0])
        backend = MotorsPyBackend(entries)
        backend.open_transport()
        backends.append(backend)
        return backend

    yield open_for
    for backend in backends:
        backend.close_transport()


def test_mixed_bus_uses_verified_109_and_legacy_108_telemetry(open_backend):
    old, new = OfflineLro("108", numeric=None), OfflineLro(EXPECTED.raw_firmware)
    backend = open_backend((old, None), (new, EXPECTED))
    backend.prepare_motor_protocols()
    assert (old.enables, new.enables) == (0, 0)
    backend.enable_motors()
    samples = backend.read_samples()
    legacy, verified = samples
    assert legacy.torque_nm == 3 and math.isnan(legacy.iq_current_a)
    assert legacy.drive_enabled is None
    assert verified.iq_current_a == 10 and verified.bus_current_a == -2
    assert verified.bus_voltage_v == 48 and verified.drive_enabled is True
    assert verified.feedback_header == 0x30 and verified.to_dict()["torque_nm"] is None
    rows = backend.motor_versions()
    assert [row["active_profile"] for row in rows] == ["legacy", "v1.03"]

    hub = TelemetryHub()
    hub.publish(samples)
    metrics = {metric["id"]: metric for metric in hub.capabilities()["metrics"]}
    assert metrics[f"{verified.joint_name}/iq_current_a"]["unit"] == "A"
    assert not metrics[f"{verified.joint_name}/torque_nm"]["available"]

    row = sample_row(backend.entries, samples, elapsed_s=0.0, sample=0)
    assert set(row) == set(csv_fieldnames(backend.entries))
    assert math.isnan(row[f"{legacy.joint_name}_iq_current_a"])
    assert row[f"{verified.joint_name}_iq_current_a"] == 10
    assert row[f"{verified.joint_name}_drive_enabled"] == 1
    assert math.isnan(row[f"{verified.joint_name}_torque_nm"])

    stats = _RunStatistics(StationConfig(limb="left_arm", entries=backend.entries, raw={}))
    stats.update(samples, np.zeros(2))
    joint = stats.snapshot()["joints"][1]
    assert joint["torque_peak_nm"] is None and joint["torque_mean_abs_nm"] is None


def test_service_pending_version_scan_never_enables_or_disconnects(open_backend, tmp_path, monkeypatch):
    class DelayedLro(OfflineLro):
        def request_version(self, code):
            self.started = time.monotonic()
            return True

        def version_payload(self, code):
            return [] if time.monotonic() - self.started < .4 else super().version_payload(code)

    motor = DelayedLro("108")
    backend = open_backend((motor, None))
    controller = FactoryController(StationConfig(limb="left_arm", entries=backend.entries, raw={}),
                                   backend, enable_watchdog=False)
    controller.state_machine.transition(StationState.CONNECTED)
    service = FactoryService(data_root=tmp_path)
    service._controller = controller
    assert service.display_snapshot()["station"]["motor_protocols_pending"] is True
    started = time.monotonic()
    with pytest.raises(MotorProtocolPending, match="本次未下发使能"):
        service.enable_motors(physical_estop_confirmed=True)
    assert time.monotonic() - started < .3
    assert backend.connected and not backend.motors_enabled and motor.enables == 0
    assert controller.state is StationState.CONNECTED and controller._fault is None
    deadline = time.monotonic() + 2
    while backend.motor_protocols_pending() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not backend.motor_protocols_pending()
    assert service.display_snapshot()["station"]["motor_protocols_pending"] is False
    # Avoid a position-hold thread, while exercising real protocol selection
    # and the full service/controller/backend enable sequence on a fake motor.
    monkeypatch.setattr(controller, "_confirm_motor_feedback", lambda *args, **kwargs: backend.read_samples())
    monkeypatch.setattr(controller, "_start_position_hold", lambda *args: None)
    service.enable_motors(physical_estop_confirmed=True)
    assert backend.motors_enabled and motor.enables == 1


@pytest.mark.parametrize("label,numeric,expected", [
    (None, None, None),
    ("1.0.8", None, None),
    ("5.0.0.109", None, EXPECTED),
    ("OTHER109", (5, 0, 1, 1, 2, 3, 1, 0, 3, 0), EXPECTED),
    ("5.0.0.109", (5, 0, 1, 1, 2, 4, 1, 0, 3, 0), EXPECTED),
    ("5.0.0.109", (5, 0, 1, 1, 2, 3, 1, 0, 2, 0), EXPECTED),
])
def test_mismatched_or_unconfirmed_109_never_initializes_any_node(open_backend, label, numeric, expected):
    old, new = OfflineLro("108", numeric=None), OfflineLro(label, numeric)
    backend = open_backend((old, None), (new, expected))
    with pytest.raises(MotorSafetyError):
        backend.enable_motors()
    assert (old.enables, new.enables) == (0, 0)
    sample = backend.read_samples()[1]
    assert math.isnan(sample.torque_nm) and sample.to_dict()["error_id"] is None
    assert sample.feedback_header == 0x20


def test_confirmed_numeric_018_automatically_uses_fixed_108_table(open_backend):
    motor = OfflineLro(None, (0, 1, 8, 0, 1, 0, 0, 0, 1, 17))
    backend = open_backend((motor, None))
    backend.enable_motors()
    assert motor.enables == 1 and backend._selected_protocols == ['legacy']
    row = backend.motor_versions()[0]
    assert row['raw_firmware'] is None and row['numeric_software'] == '0.1.8'


def test_older_legacy_identity_can_be_configured_without_aliasing_108(open_backend):
    expected = replace(EXPECTED, profile="legacy", raw_firmware="1.0.8", protocol="1.0.0.0")
    motor = OfflineLro("1.0.8", (5, 0, 1, 1, 2, 3, 1, 0, 0, 0))
    backend = open_backend((motor, expected))
    backend.enable_motors()
    assert motor.enables == 1
    assert backend.read_samples()[0].lro_protocol_profile == "legacy"


def test_late_observed_109_fences_default_legacy_commands(open_backend):
    motor = OfflineLro("108", numeric=None)
    backend = open_backend((motor, None))
    backend.enable_motors()
    backend._observe_motor_protocol(0, {"raw_firmware": "V5.0.0.109"})
    with pytest.raises(MotorSafetyError, match="连接期间固件版本变化"):
        backend.command_mit([0.0])


def test_109_enable_needs_new_disabled_feedback(open_backend):
    motor = OfflineLro(EXPECTED.raw_firmware)
    backend = open_backend((motor, EXPECTED))
    backend.prepare_motor_protocols()
    backend.refresh_feedback()
    motor.read_only_status_responds = False
    with pytest.raises(MotorSafetyError, match="新鲜的驱动失能反馈"):
        backend.enable_motors()
    assert motor.enables == 0


def test_109_zero_submission_keeps_verification_pending(open_backend):
    motor = OfflineLro(EXPECTED.raw_firmware)
    backend = open_backend((motor, EXPECTED))
    backend.prepare_motor_protocols()
    session = MotorZeroingSession(backend)
    session.prepare(0)
    assert session.confirm() is MotorZeroResult.SUBMITTED_PENDING_REBOOT
    assert session.snapshot()["zeroed_indices"] == []
    assert session.snapshot()["pending_reboot_indices"] == [0]
    with pytest.raises(MotorSafetyError, match="等待重新上电"):
        backend.submit_motor_zero(0)
    assert motor.zero_requests == 1

class DelayedEnableLro(OfflineLro):
    def __init__(self, delay=.08, *, no_feedback=False, hardware_error=0):
        super().__init__('V5.0.0.109', numeric=(0,1,9,0,1,0,0,0,1,17))
        self.delay=delay;self.no_feedback=no_feedback;self.hardware_error=hardware_error
        self.enable_at=None;self.disables=0;self.on_pending=None
    def init_motor(self):
        self.enables+=1;self.enable_at=time.monotonic()
        return 0
    def refresh_motor_status(self):
        if self.enable_at is None:return super().refresh_motor_status()
        if self.on_pending:
            callback,self.on_pending=self.on_pending,None;callback()
        if self.no_feedback:
            self.enabled=True  # cached flag alone must never count as confirmation
            return
        self.reply_count+=1
        if time.monotonic()-self.enable_at >= self.delay:self.enabled=True
    def get_error_id(self):return self.hardware_error if self.enable_at is not None else 0
    def deinit_motor(self):
        self.disables+=1;self.enabled=False;self.enable_at=None

@pytest.mark.parametrize('count',[1,7])
def test_109_delayed_enable_feedback_waits_without_reissuing_enable(open_backend,count):
    motors=[DelayedEnableLro(.04+i*.01) for i in range(count)]
    backend=open_backend(*[(m,None) for m in motors]);backend.prepare_motor_protocols()
    started=time.monotonic();backend.enable_motors()
    assert backend.motors_enabled and all(m.enabled and m.enables==1 for m in motors)
    assert .03 <= time.monotonic()-started < .7

@pytest.mark.parametrize('fault',['never_enabled','stale_flag','hardware_error','stop'])
def test_109_enable_failure_rolls_back_and_preserves_guard(open_backend,fault):
    first=DelayedEnableLro(.005)
    second=DelayedEnableLro(10 if fault=='never_enabled' else .08,
                            no_feedback=fault=='stale_flag',hardware_error=3 if fault=='hardware_error' else 0)
    backend=open_backend((first,None),(second,None));backend.prepare_motor_protocols()
    if fault=='stop':second.on_pending=backend._revoke_damping
    started=time.monotonic()
    with pytest.raises(MotorSafetyError) as error:backend.enable_motors()
    assert time.monotonic()-started < .8
    assert not backend.motors_enabled and all(not m.enabled and m.disables for m in (first,second))
    assert all(m.enables==1 for m in (first,second))
    if fault in ('never_enabled','stale_flag'):
        assert '300毫秒' in str(error.value) and 'feedback=' in str(error.value)
    elif fault=='stop':assert '停止请求' in str(error.value)
    else:assert '硬件故障' in str(error.value)
