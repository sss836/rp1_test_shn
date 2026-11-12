import time
import threading
from types import SimpleNamespace
from uuid import UUID
import pytest
from factory_hmi.core.backend import FakeMotorBackend, MotorsPyBackend
from factory_hmi.core.motor_versions import MotorVersions, decode_version
from factory_hmi.gateway.motor_firmware import target_from_observation


def entry(**changes):
    return SimpleNamespace(**{**dict(index=0, interface="can2", motor_id=3, joint_name="joint", motor_type="LRO", motor_model=0, interface_type="canfd"), **changes})


def wait_row(monitor):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        row = monitor.snapshot()[0]
        if row["status"] != "unknown":
            return row
        time.sleep(.01)
    return monitor.snapshot()[0]


def test_exact_raw_numeric_and_no_108_guess():
    assert decode_version(b"\xa0\x31" + b"108".ljust(18, b"\0")) == {"raw_firmware": "108", "software": None}
    assert decode_version(bytes.fromhex("a01e01000801000201000000")) == dict(software="1.0.8", hardware="1.0.2", protocol="1.0.0.0")
    for raw in (b"", b"\x20\x31" + b"a"*18, b"\xa0\x31bad", b"\xa0\x1e" + b"0"*7):
        with pytest.raises(ValueError):
            decode_version(raw)


def test_background_version_observation_no_enable_and_stale():
    class Motor:
        def __init__(self): self.requests = []
        def request_version(self, code): self.requests.append(code); return True
        def version_payload(self, code):
            return b"\xa0\x31" + b"1.0.8".ljust(18, b"\0") if code == 0x31 else bytes.fromhex("a01e01000801000201000000")
        def init_motor(self): pytest.fail("version read enabled motor")
    motor = Motor()
    monitor = MotorVersions([entry()], [motor], stale=.02)
    monitor.start()
    row = wait_row(monitor)
    assert row["status"] == "known" and row["raw_firmware"] == "1.0.8"
    assert row["interface"] == "can2" and row["motor_id"] == 3 and row["observed_at"]
    assert motor.requests == [0x31, 0x1E]
    assert target_from_observation(row)["hardware"] == "1.0.2"
    time.sleep(.03)
    assert monitor.snapshot()[0]["status"] == "stale"
    with pytest.raises(ValueError): target_from_observation(monitor.snapshot()[0])
    monitor.close()
    assert not monitor._thread.is_alive()
    assert monitor.motors == ()


class DelayedVersionMotor:
    """Offline model of the native SDK's single pending version query."""
    def __init__(self, firmware_delay, numeric_delay=.01):
        self.firmware_delay, self.numeric_delay = firmware_delay, numeric_delay
        self.requested, self.events, self.dropped = [], [], []
        self.pending, self.pending_until, self.responses = None, 0, {}

    def request_version(self, code):
        self.requested.append(code)
        self.pending = code
        now = time.monotonic()
        self.pending_until = now + .5
        self.responses.pop(code, None)
        delay = self.firmware_delay if code == 0x31 else self.numeric_delay
        self.events.append((now + delay, code))
        return True

    def version_payload(self, code):
        now = time.monotonic()
        for due, received_code in self.events[:]:
            if due > now:
                continue
            self.events.remove((due, received_code))
            if received_code != self.pending or due > self.pending_until:
                self.dropped.append(received_code)
                continue
            self.responses[received_code] = (
                b"\xa0\x31" + b"108".ljust(18, b"\0") if received_code == 0x31
                else bytes.fromhex("a01e01000801000201000000")
            )
            self.pending = None
        return self.responses.get(code, [])

    def init_motor(self):
        pytest.fail("version query sent enable")


def test_delayed_firmware_reply_within_sdk_window():
    motor = DelayedVersionMotor(.40)
    monitor = MotorVersions([entry()], [motor])
    monitor.start()
    try:
        row = wait_row(monitor)
        assert row["status"] == "known" and row["raw_firmware"] == "108"
        assert not row["errors"] and motor.requested == [0x31, 0x1E]
    finally:
        monitor.close()


def test_late_firmware_reply_is_not_confused_with_numeric_reply():
    motor = DelayedVersionMotor(.60, numeric_delay=.20)
    monitor = MotorVersions([entry()], [motor])
    monitor.start()
    try:
        row = wait_row(monitor)
        assert row["status"] == "known" and row["raw_firmware"] is None
        assert row["errors"]["0x31"] == "版本查询超时"
        assert row["protocol"] == "1.0.0.0" and motor.dropped == [0x31]
    finally:
        monitor.close()


def test_close_cancels_pending_read_without_next_query():
    motor = DelayedVersionMotor(1.0)
    monitor = MotorVersions([entry()], [motor])
    monitor.start()
    deadline = time.monotonic() + 1
    while not motor.requested and time.monotonic() < deadline:
        time.sleep(.005)
    assert motor.requested == [0x31]
    started = time.monotonic()
    monitor.close()
    assert time.monotonic() - started < .3
    assert motor.requested == [0x31] and not monitor._thread.is_alive()


def test_timeout_unsupported_bad_response_and_close():
    motor = SimpleNamespace(request_version=lambda code: True, version_payload=lambda code: [])
    m = MotorVersions([entry()], [motor], timeout=.02)
    m.start()
    assert wait_row(m)["status"] == "timeout"
    m.close()


class RefreshVersionMotor:
    def __init__(self, *, blocked=False):
        self.requests = []
        self.raw_firmware = "108"
        self.requested = threading.Event()
        self.release = threading.Event()
        if not blocked:
            self.release.set()

    def request_version(self, code):
        self.requests.append(code)
        self.requested.set()
        return True

    def version_payload(self, code):
        if not self.release.is_set():
            return b""
        if code == 0x31:
            return b"\xa0\x31" + self.raw_firmware.encode().ljust(18, b"\0")
        return bytes.fromhex("a01e01000801000201000000")


def wait_refresh(monitor, request_id):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        receipt = monitor.refresh_status()
        if receipt["state"] == "complete" and receipt["request_id"] == request_id:
            return receipt
        time.sleep(.005)
    pytest.fail(f"refresh did not complete: {monitor.refresh_status()}")


def test_manual_refresh_wakes_worker_and_replaces_cached_observation():
    motor = RefreshVersionMotor()
    monitor = MotorVersions([entry()], [motor], refresh=3600)
    monitor.start()
    try:
        assert wait_row(monitor)["raw_firmware"] == "108"
        assert monitor.refresh_status()["state"] == "idle"
        motor.raw_firmware = "109"
        request = monitor.request_refresh()
        assert UUID(request["request_id"]).version == 4
        completed = wait_refresh(monitor, request["request_id"])
        assert completed["completed_motors"] == completed["total_motors"] == 1
        assert motor.requests == [0x31, 0x1E, 0x31, 0x1E, 0x16]
        assert monitor.snapshot()[0]["raw_firmware"] == "109"
    finally:
        monitor.close()


def test_manual_refresh_during_auto_pass_coalesces_behind_it():
    motor = RefreshVersionMotor(blocked=True)
    monitor = MotorVersions([entry()], [motor], refresh=3600, timeout=2)
    monitor.start()
    try:
        assert motor.requested.wait(1)
        first = monitor.request_refresh()
        assert first["state"] == "queued" and first["completed_motors"] == 0
        assert monitor.request_refresh() == first
        assert motor.requests == [0x31]
        motor.release.set()
        wait_refresh(monitor, first["request_id"])
        assert motor.requests == [0x31, 0x1E, 0x31, 0x1E]
    finally:
        monitor.close()


def test_backend_refresh_preserves_enable_guard_and_connection_lifetime():
    backend = MotorsPyBackend(FakeMotorBackend(1).entries)
    with pytest.raises(ValueError, match="先连接并扫描"):
        backend.request_motor_version_refresh()
    assert backend.motor_version_refresh_status()["state"] == "idle"

    motor = RefreshVersionMotor(blocked=True)
    monitor = MotorVersions([entry()], [motor], refresh=3600, timeout=2)
    backend._versions = monitor
    backend._connected = True
    backend._protocol_observations = [{}]
    monitor.start()
    try:
        assert motor.requested.wait(1)
        backend._enabled = True
        with pytest.raises(ValueError, match="失能"):
            backend.request_motor_version_refresh()
        backend._enabled = False
        request = backend.request_motor_version_refresh()
        assert backend.motor_protocols_pending()
        assert backend.motor_version_refresh_status() == request
    finally:
        monitor.close()
    assert backend.motor_version_refresh_status()["state"] == "idle"
    assert not backend.motor_protocols_pending()
    assert motor.requests == [0x31]
    m = MotorVersions([entry(motor_type="DM")], [motor])
    m.start(); assert wait_row(m)["status"] == "unsupported"; m.close()
    motor.version_payload = lambda code: [0xA0, code, 1]
    m = MotorVersions([entry()], [motor])
    m.start()
    deadline = time.monotonic() + 1
    while not m.snapshot()[0]["errors"] and time.monotonic() < deadline: time.sleep(.01)
    assert m.snapshot()[0]["status"] == "unknown"
    assert m.snapshot()[0]["raw_responses"]
    m.close()


@pytest.mark.parametrize('raw,numeric,expected',[
    ('V5.0.0.109','0.1.9','same_generation'),
    ('V5.0.0.108','0.1.8','same_generation'),
    ('V5.0.0.109','0.1.8','conflict'),
    ('V5.0.0.108','0.1.9','conflict'),
    ('1.0.8','1.0.8','equal'),
    ('V5.0.0.110','0.1.9','unverified'),
])
def test_full_numeric_version_relation_and_ui(raw,numeric,expected):
    from factory_hmi.core.motor_versions import version_relation
    from factory_hmi.desktop.motor_firmware_dialog import MotorFirmwareDialog
    relation=version_relation(raw,numeric)
    assert relation==expected
    row=dict(interface='can-fixture',motor_id=1,status='known',raw_firmware=raw,
             software=numeric,hardware='0.1.0',protocol='0.0.1.17',version_relation=relation)
    text=MotorFirmwareDialog._version_text(row,False)
    if expected=='same_generation':
        assert '属于同一' in text and '尚未核实' not in text
    elif expected=='conflict':assert '代际冲突' in text
