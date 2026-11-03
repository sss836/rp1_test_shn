import time
from types import SimpleNamespace
import pytest
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


def test_timeout_unsupported_bad_response_and_close():
    motor = SimpleNamespace(request_version=lambda code: True, version_payload=lambda code: [])
    m = MotorVersions([entry()], [motor], timeout=.02)
    m.start()
    assert wait_row(m)["status"] == "timeout"
    m.close()
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
