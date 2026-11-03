"""Fault injection for the explicitly published LRO script compatibility mode.

Every socket, clock, device value and frame is fake. The old unnumbered reply
ambiguity tests remain unchanged. A successful compatibility read is evidence
of one request's receive window, never evidence of a parameter-number echo.
"""
from copy import deepcopy
import json
import threading

import pytest

from factory_hmi.core import motor_parameter_transport as transport


PROFILE = "lro-pn-script-v1"


def target(motor_id=1, interface="can-pn-fixture"):
    return {"motor_id": motor_id, "interface": interface, "model": "LRO_5550"}


def entry(key="pn_0401", raw=200):
    return {"key": key, "expected_raw": [raw], "tolerance_raw": 0,
            "_protocol_profile": PROFILE}


def wire(data, motor_id=1):
    return transport.FRAME.pack(motor_id, len(data), 1, data)


def pn_reply(raw=200, motor_id=1):
    return wire(b"\xff\xfe\x11" + raw.to_bytes(4, "big"), motor_id)


class Clock:
    def __init__(self):
        self.value = 100.0

    def monotonic(self):
        return self.value

    def sleep(self, duration):
        self.value += max(0.0, duration)


class Bus:
    def __init__(self, clock, on_send=None):
        self.clock, self.on_send = clock, on_send
        self.sent, self.pending, self.options = [], [], []
        self.closed = False
        self.interface = None

    def queue(self, frame, delay=0.0):
        self.pending.append((self.clock.value + delay, frame))
        self.pending.sort(key=lambda row: row[0])

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.interface = address[0]

    def setblocking(self, enabled):
        assert enabled is False

    def send(self, frame):
        assert not self.closed
        self.sent.append(frame)
        if self.on_send:
            self.on_send(self, frame)
        return len(frame)

    def recv(self, size):
        assert not self.closed
        if not self.pending or self.pending[0][0] > self.clock.value:
            raise BlockingIOError
        return self.pending.pop(0)[1]

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Harness:
    def __init__(self, on_send=None):
        self.clock = Clock()
        self.bus = Bus(self.clock, on_send)
        self.opened = 0

    def socket_factory(self, *args):
        self.opened += 1
        return self.bus

    def ready(self, readers, writers, errors, timeout):
        assert self.bus in readers
        if self.bus.pending and self.bus.pending[0][0] <= self.clock.value:
            return ([self.bus], [], [])
        until = self.clock.value + max(0.0, timeout)
        if self.bus.pending:
            self.clock.value = min(until, self.bus.pending[0][0])
        else:
            self.clock.value = until
        return (([self.bus], [], []) if self.bus.pending and
                self.bus.pending[0][0] <= self.clock.value else ([], [], []))

    def session(self, **kwargs):
        return transport.ScriptParameterSession(
            socket_factory=self.socket_factory, ready=self.ready,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep, **kwargs)


def read_failed(session, row, item, stop):
    """A checked error result or an exception both stop the compatibility path."""
    try:
        result = session.read(row, item, stop)
    except (InterruptedError, RuntimeError, OSError, ValueError):
        return
    assert result.get("status") != "read", result


def test_script_profile_preserves_uint32_wire_value_and_old_default_refusal():
    plain = {"key": "pn_0401", "expected_raw": [200], "tolerance_raw": 0}
    with pytest.raises(ValueError):
        transport.write_payload(plain)
    assert transport.write_payload(plain, profile=PROFILE) == b"\xc0\x10\x04\x01\x00\x00\x00\xc8"
    maximum = dict(plain, expected_raw=[0xffffffff])
    assert transport.write_payload(maximum, profile=PROFILE) == b"\xc0\x10\x04\x01\xff\xff\xff\xff"
    raw = transport.decode("pn_0401", 1, b"\xff\xfe\x11\x00\x00\x00\xc8", target())
    assert raw["status"] == "association_unknown"
    assert raw["raw"] == [200]


def test_clean_script_read_labels_timing_and_no_number_echo():
    def reply(bus, frame):
        bus.queue(pn_reply(200), .001)
    h = Harness(reply)
    session, stop = h.session(), threading.Event()
    try:
        result = session.read(target(), entry(), stop)
        assert result["status"] == "read" and result["raw"] == [200]
        assert result["association_method"] == "single_flight_time_window"
        assert result["parameter_number_echoed"] is False
        _, length, _, payload = transport.FRAME.unpack(h.bus.sent[0])
        assert payload[:length] == b"\xc0\x11\x04\x01"
        assert len(h.bus.sent) == 1
    finally:
        session.close()
    assert h.bus.closed


def test_unknown_or_missing_profile_cannot_make_unnumbered_reply_trusted():
    for profile in (None, "made-up-profile"):
        h = Harness(lambda bus, frame: bus.queue(pn_reply()))
        session, item = h.session(), entry()
        if profile is None:
            item.pop("_protocol_profile")
        else:
            item["_protocol_profile"] = profile
        try:
            read_failed(session, target(), item, threading.Event())
        finally:
            session.close()


@pytest.mark.parametrize("profile", [None, "made-up-profile"])
def test_session_write_without_verified_profile_sends_nothing(profile):
    h = Harness()
    session, item = h.session(), entry()
    if profile is None:
        item.pop("_protocol_profile")
    else:
        item["_protocol_profile"] = profile
    try:
        with pytest.raises((ValueError, RuntimeError)):
            session.write(target(), item, threading.Event())
        assert h.bus.sent == []
    finally:
        session.close()


@pytest.mark.parametrize("value", [-1, 0x100000000, True, 2.5])
def test_script_encoder_rejects_non_uint32_raw_values(value):
    with pytest.raises((ValueError, OverflowError, TypeError)):
        transport.write_payload(entry(raw=value), profile=PROFILE)


def test_read_write_read_preserves_actual_old_value_and_sends_only_explicit_write():
    actual = [100]
    def respond(bus, frame):
        _, length, _, padded = transport.FRAME.unpack(frame)
        payload = padded[:length]
        if payload[:2] == b"\xc0\x10":
            assert length == 8
            actual[0] = int.from_bytes(payload[4:8], "big")
        elif payload[:2] == b"\xc0\x11":
            bus.queue(pn_reply(actual[0]), .001)
        else:
            raise AssertionError(payload.hex())
    h = Harness(respond)
    session, stop = h.session(), threading.Event()
    try:
        old = session.read(target(), entry(), stop)
        assert old["status"] == "read" and old["raw"] == [100]
        assert len(h.bus.sent) == 1  # Reading/comparison never writes 200.
        receipt = session.write(target(), entry(), stop)
        assert receipt["request_hex"] == "c0100401000000c8"
        new = session.read(target(), entry(), stop)
        assert new["status"] == "read" and new["raw"] == [200]
        assert len(h.bus.sent) == 3
        assert [transport.FRAME.unpack(packet)[3][1] for packet in h.bus.sent] == [0x11, 0x10, 0x11]
    finally:
        session.close()


@pytest.mark.parametrize("next_target,next_entry", [
    (target(), entry("pn_0402")),
    (target(2), entry()),
])
def test_timeout_quarantines_channel_across_keys_and_motor_ids(next_target, next_entry):
    h = Harness()  # First query has no reply at all.
    session, stop = h.session(), threading.Event()
    try:
        read_failed(session, target(), entry(), stop)
        assert len(h.bus.sent) == 1
        # A late old answer would otherwise be accepted as the next parameter.
        h.bus.queue(pn_reply(200))
        read_failed(session, next_target, next_entry, stop)
        assert len(h.bus.sent) == 1
    finally:
        session.close()


@pytest.mark.parametrize("incoming", [
    [pn_reply(200), pn_reply(201)],
    [pn_reply(200), pn_reply(200)],
    [wire(b"\xff\xfe\x11\x00")],
    [wire(b"\xc0\x11\x04\x02")],
    [pn_reply(999, motor_id=2), pn_reply(200)],
    [pn_reply(200, motor_id=0x80000001)],  # Extended CAN ID is not standard 1.
    [pn_reply(200, motor_id=0x40000001)],  # RTR is never a data reply.
    [pn_reply(200, motor_id=0x20000001)],  # CAN error flag must not be masked off.
])
def test_conflict_short_reply_or_foreign_query_prevents_followup(incoming):
    def reply(bus, frame):
        for index, packet in enumerate(incoming):
            bus.queue(packet, .001 * (index + 1))
    h = Harness(reply)
    session, stop = h.session(), threading.Event()
    try:
        read_failed(session, target(), entry(), stop)
        sent = len(h.bus.sent)
        assert sent == 1
        read_failed(session, target(), entry("pn_0402"), stop)
        assert len(h.bus.sent) == sent
    finally:
        session.close()


def test_preexisting_pn_response_is_not_silently_discarded_then_trusted():
    h = Harness(lambda bus, frame: bus.queue(pn_reply(200), .001))
    h.bus.queue(pn_reply(999))
    session, stop = h.session(), threading.Event()
    try:
        read_failed(session, target(), entry(), stop)
        assert h.bus.sent == []
        read_failed(session, target(), entry("pn_0402"), stop)
        assert h.bus.sent == []
    finally:
        session.close()


def test_cancellation_after_send_stops_next_query_without_replay():
    stop = threading.Event()
    def cancel(bus, frame):
        stop.set()
        bus.queue(pn_reply(200), .001)
    h = Harness(cancel)
    session = h.session()
    try:
        read_failed(session, target(), entry(), stop)
        assert len(h.bus.sent) == 1
        stop.clear()  # A cleared UI cancel flag is not a clean bus boundary.
        read_failed(session, target(), entry("pn_0402"), stop)
        assert len(h.bus.sent) == 1
    finally:
        session.close()


@pytest.mark.parametrize("send_failure", ["short", "error"])
def test_uncertain_send_never_retries_or_allows_next_parameter(send_failure):
    h = Harness()
    def uncertain(packet):
        h.bus.sent.append(packet)
        if send_failure == "error":
            raise OSError("synthetic send delivery unknown")
        return len(packet) - 1
    h.bus.send = uncertain
    session, stop = h.session(), threading.Event()
    try:
        read_failed(session, target(), entry(), stop)
        assert len(h.bus.sent) == 1
        read_failed(session, target(), entry("pn_0402"), stop)
        assert len(h.bus.sent) == 1
    finally:
        session.close()


def test_streaming_telemetry_and_wrong_id_do_not_become_pn_value():
    def reply(bus, frame):
        bus.queue(wire(b"\x20\x00\x00\x00\x00\x00\x47"), .001)
        bus.queue(wire(b"\x20\x00\x00\x00\x00\x00\x7d", motor_id=2), .002)
        bus.queue(pn_reply(200), .003)
    h = Harness(reply)
    session = h.session()
    try:
        result = session.read(target(), entry(), threading.Event())
        assert result["status"] == "read" and result["raw"] == [200]
        assert len(h.bus.sent) == 1
    finally:
        session.close()


def test_session_close_never_sends_reset_enable_or_power_command():
    h = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    session = h.session()
    session.read(target(), entry(), threading.Event())
    before = deepcopy(h.bus.sent)
    session.close()
    session.close()
    assert h.bus.sent == before and h.bus.closed


def test_quarantine_survives_close_and_new_session_at_same_state_root(tmp_path):
    h = Harness()
    session = h.session(quarantine_root=tmp_path)
    read_failed(session, target(), entry(), threading.Event())
    assert len(h.bus.sent) == 1
    session.close()
    # Closing descriptors or creating a new task cannot establish whether an
    # old device response is still in flight. No hardware reset is simulated.
    fresh = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    again = fresh.session(quarantine_root=tmp_path)
    try:
        read_failed(again, target(2), entry("pn_0402"), threading.Event())
        assert fresh.bus.sent == []
    finally:
        again.close()


def test_concurrent_operation_and_close_cannot_interleave_with_pending_query():
    entered, release = threading.Event(), threading.Event()
    def delayed_reply(bus, frame):
        entered.set()
        assert release.wait(2)
        bus.queue(pn_reply(), .001)
    h = Harness(delayed_reply)
    session, stop, outcome = h.session(), threading.Event(), []
    def run():
        try:
            outcome.append(session.read(target(), entry(), stop))
        except Exception as exc:
            outcome.append(exc)
    worker = threading.Thread(target=run, name="fake-pn-single-flight")
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(RuntimeError):
            session.read(target(), entry("pn_0402"), stop)
        with pytest.raises(RuntimeError):
            session.write(target(), entry(), stop)
        with pytest.raises(RuntimeError):
            session.close()
        assert len(h.bus.sent) == 1 and not h.bus.closed
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()
        session.close()
    assert len(outcome) == 1 and isinstance(outcome[0], dict), outcome
    assert outcome[0]["status"] == "read" and len(h.bus.sent) == 1


def test_close_cannot_discard_late_pn_queued_after_last_success(tmp_path):
    h = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    session = h.session(quarantine_root=tmp_path)
    session.read(target(), entry(), threading.Event())
    h.bus.queue(pn_reply(999))
    try:
        with pytest.raises(RuntimeError):
            session.close()
        assert not h.bus.closed
        assert list(tmp_path.glob("*.json")), "Late Pn uncertainty must survive task close"
        assert len(h.bus.sent) == 1
    finally:
        session.close()  # Explicit cleanup may close dirty handles, not clear evidence.
    assert h.bus.closed and list(tmp_path.glob("*.json"))
    again_h = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    again = again_h.session(quarantine_root=tmp_path)
    try:
        read_failed(again, target(2), entry("pn_0402"), threading.Event())
        assert again_h.bus.sent == []
    finally:
        again.close()


def test_idle_boundary_detects_pn_retained_during_ordinary_parameter_operation(tmp_path):
    h = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    session = h.session(quarantine_root=tmp_path)
    stop = threading.Event()
    session.read(target(), entry(), stop)
    # A normal-parameter socket receives its own copy. It cannot drain this
    # task-level socket's queue; an ordinary send/read is outside this session.
    h.bus.queue(pn_reply(999))
    try:
        with pytest.raises(RuntimeError):
            session.check_idle(stop)
        assert list(tmp_path.glob("*.json"))
        read_failed(session, target(2), entry("pn_0402"), stop)
        assert len(h.bus.sent) == 1
    finally:
        session.close()


def test_inflight_marker_is_durable_before_send_and_crash_does_not_rearm(tmp_path):
    h = Harness()
    def crash_after_send_attempt(packet):
        # SystemExit simulates termination outside ordinary Exception cleanup.
        markers = list(tmp_path.glob("*.json"))
        assert len(markers) == 1
        marker = json.loads(markers[0].read_text())
        assert marker["status"] == "in_flight"
        assert marker["request_hex"] == "c0110401"
        h.bus.sent.append(packet)
        raise SystemExit("synthetic abrupt process termination")
    h.bus.send = crash_after_send_attempt
    session = h.session(quarantine_root=tmp_path)
    with pytest.raises(SystemExit):
        session.read(target(), entry(), threading.Event())
    session.close()
    assert len(h.bus.sent) == 1 and list(tmp_path.glob("*.json"))
    fresh_h = Harness(lambda bus, frame: bus.queue(pn_reply(), .001))
    fresh = fresh_h.session(quarantine_root=tmp_path)
    try:
        read_failed(fresh, target(2), entry("pn_0402"), threading.Event())
        assert fresh_h.bus.sent == []
    finally:
        fresh.close()


def test_preflight_opens_passive_channel_and_rejects_existing_pn_without_send(tmp_path):
    h = Harness()
    h.bus.queue(pn_reply(999))
    session = h.session(quarantine_root=tmp_path)
    try:
        with pytest.raises(transport.SessionQuarantinedError):
            session.ensure_available(target(), entry(), threading.Event())
        assert h.opened == 1 and h.bus.interface == "can-pn-fixture"
        assert h.bus.sent == []
        markers = list(tmp_path.glob("*.json"))
        assert len(markers) == 1
        assert "残留" in json.loads(markers[0].read_text())["error"]
    finally:
        session.close()
