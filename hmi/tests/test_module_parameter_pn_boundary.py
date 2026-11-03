"""Pn ambiguity must remain unverified through transport and write preview.

All frames, versions, accounts and parameter values here are synthetic. No CAN,
PLC, network endpoint or downloaded upstream program is opened or executed.
"""
from copy import deepcopy
import threading

import pytest

from factory_hmi.core.motor_parameter_transport import FRAME, decode, exchange, query_payload, write_payload
from factory_hmi.motor_parameter_contract import compare, sha
from test_module_firmware_maintenance import target
from test_module_parameters import finish, make


@pytest.mark.parametrize("key", ["pn_0401", "pn_0402", "pn_0e71"])
def test_same_unnumbered_value_is_not_proof_of_requested_pn(key):
    # Identical bytes could have come from any of these Pn requests. A matching
    # expected number is not evidence of which parameter actually responded.
    result = decode(key, target()["motor_id"], b"\xff\xfe\x11\x00\x00\x00\xc8", target())
    entry = dict(key=key, expected_raw=[200], tolerance_raw=0)
    assert result["raw"] == [200]
    assert compare(entry, result) == "association_unknown"
    with pytest.raises(ValueError, match="禁止Pn写入"):
        write_payload(entry)


def test_late_other_pn_reply_after_new_request_stays_unverified():
    stop = threading.Event()
    sent = []

    class FakeSocket:
        def __init__(self):
            self.replies = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def setsockopt(self, *args):
            pass
        def bind(self, *args):
            pass
        def setblocking(self, *args):
            pass
        def send(self, frame):
            sent.append(frame)
            # The old pn_0401 reply arrives only after the new pn_0402 request.
            # Draining the socket before send cannot eliminate this case.
            self.replies.append(FRAME.pack(target()["motor_id"], 7, 1, b"\xff\xfe\x11\x00\x00\x00\xc8"))
            return len(frame)
        def recv(self, size):
            if not self.replies:
                raise BlockingIOError
            return self.replies.pop(0)

    bus = FakeSocket()
    result = exchange(target(), query_payload("pn_0402"), stop, "pn_0402",
                      socket_factory=lambda *args: bus,
                      ready=lambda *args: ([bus], [], []) if bus.replies else ([], [], []), timeout=.1)
    assert len(sent) == 1
    assert FRAME.unpack(sent[0])[3][:4] == b"\xc0\x11\x04\x02"
    assert result["status"] == "association_unknown"
    assert result["response_hex"] == "fffe11000000c8"
    assert compare(dict(key="pn_0402", expected_raw=[200], tolerance_raw=0), result) != "passed"


@pytest.mark.parametrize("raw_firmware", ["synthetic-v108", "synthetic-v109"])
def test_pn_ambiguity_blocks_whole_write_preview_even_when_expected_value_matches(tmp_path, raw_firmware):
    task, selections, writes, values, actor, record, targets = make(tmp_path)
    template = record["template"]
    template["entries"] = [dict(key="kt", expected_raw=[174], tolerance_raw=0),
                           dict(key="pn_0402", expected_raw=[200], tolerance_raw=0)]
    template["applicability"][0]["raw_firmware"] = raw_firmware
    record["sha256"] = sha(template)
    base_observations = task.observations
    base_versions = task.version_reader
    task.observations = lambda: [dict(row, raw_firmware=raw_firmware) for row in base_observations()]
    task.version_reader = lambda row, stop: dict(base_versions(row, stop), raw_firmware=raw_firmware)
    values[0]["kt"] = [174]
    base_reader = task.reader
    def reader(row, item, stop):
        if item["key"].startswith("pn_"):
            return decode(item["key"], row["motor_id"], b"\xff\xfe\x11\x00\x00\x00\xc8", row)
        return base_reader(row, item, stop)
    task.reader = reader
    try:
        task.read(selections)
        job = finish(task)
        assert job["status"] == "checked_with_differences"
        kt, pn = job["targets"][0]["items"]
        assert kt["status"] == "passed"
        assert pn["status"] == "association_unknown" and pn["actual"]["raw"] == [200]
        with pytest.raises(ValueError, match="禁止写入"):
            task.preview(job["job_id"])
        assert not writes
        assert job["summary"]["not_written"] == 2
        assert not job["targets"][0]["power_cycle_required"]
        # The device never receives a partial KT write just because one valid
        # parameter precedes an unverified Pn item in the template.
        assert deepcopy(task.snapshot()["targets"]) == job["targets"]
    finally:
        task.cancel()
