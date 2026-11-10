"""Exercise the actual isolated cabinet child and local IPC using only mock PLCs."""
from dataclasses import replace
import json
import os
import signal
import time

import pytest

from factory_hmi.plc.config import PlcCabinetConfig, PlcSetpointConfig, PlcTimingConfig
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.plc.process import PlcCabinetClient, PlcCabinetProcess
from factory_hmi.plc.service import PlcControlDisabledError


def config():
    return PlcCabinetConfig(
        driver="mock",
        timings=PlcTimingConfig(poll_ms=20),
        setpoints=PlcSetpointConfig(enabled=True, voltage_min_v=1, voltage_max_v=60,
                                  current_min_a=.1, current_max_a=10),
    )


def execute(client, operation, request_id, *, payload=None, context=None, window="window-a"):
    seen = context or client.snapshot(include_history=False)["coordination"]
    return client.coordination.execute(
        operation, payload or {}, user=window, client_id=window, request_id=request_id,
        expected_epoch=seen["epoch"], expected_revision=seen["revision"],
    )


def wait_for(client, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = client.snapshot(include_history=False)
        if predicate(value):
            return value
        time.sleep(.02)
    raise AssertionError(value)


def test_multiple_callers_share_a_single_cabinet_and_ordered_receipts(tmp_path):
    process = PlcCabinetProcess(config(), data_root=tmp_path)
    process.start()
    try:
        assert process.pid != os.getpid()
        info = json.loads(json.dumps(process.connection_info()))
        assert "protocol" not in info["config"]
        left = PlcCabinetClient.attach(info)
        right = PlcCabinetClient.attach(info)
        initial = left.snapshot()
        assert initial["communication_state"] == "DISABLED"
        assert initial["communication_enabled"] is False
        assert right.config.dut_can_map["DUT2"] == "can1"
        with pytest.raises(PlcRequestConflict, match="权限") as error:
            right._request("close", {}, timeout=.5)
        assert error.value.code == "cabinet_forbidden"

        connected = execute(left, "connect", "connect")
        assert connected["request"]["state"] == "confirmed"
        seen = right.snapshot()["coordination"]
        assert seen["epoch"] == connected["result"]["coordination"]["epoch"]
        payload = {"voltage": 12.0, "current": 1.0}
        accepted = execute(right, "setpoints", "setpoints", payload=payload, context=seen, window="window-b")
        assert accepted["request"]["accepted"] is True
        with pytest.raises(PlcRequestConflict) as conflict:
            execute(left, "setpoints", "competing", payload={"voltage": 24.0, "current": 1.0}, context=seen)
        assert conflict.value.code == "stale_revision"
        final = wait_for(left, lambda value: value["coordination"]["last_command"]["state"] == "confirmed")
        assert final["status"]["PS1SetVoltage"] == 12.0
        assert "trend" not in final and "operation_log" not in final
        assert "trend" in right.snapshot()
        replay = execute(right, "setpoints", "setpoints", payload=payload, context=seen, window="window-b")
        assert replay["replayed"] is True and replay["request"]["state"] == "confirmed"
    finally:
        process.close()
    assert process.shutdown_status["state"] == "confirmed"
    stale = left.snapshot()
    assert stale["stale"] and stale["status"] is None and not stale["output_admission"]["allowed"]
    assert stale["coordination"]["epoch"] == seen["epoch"]


def test_worker_is_exclusive_and_monitor_mode_remains_read_only(tmp_path):
    monitor = PlcCabinetProcess(replace(config(), access_mode="monitor"), data_root=tmp_path)
    monitor.start()
    try:
        other = PlcCabinetProcess(config(), data_root=tmp_path)
        with pytest.raises(RuntimeError, match="active owner"):
            other.start()
        assert monitor.read_only
        execute(monitor, "connect", "observe")
        assert monitor.snapshot()["phase"] == "MONITORING"
        with pytest.raises(PlcControlDisabledError):
            execute(monitor, "all-stop", "deny-write")
    finally:
        monitor.close()
    assert monitor.shutdown_status["state"] == "unverified"


def test_owner_channel_loss_closes_only_after_mock_feedback(tmp_path):
    process = PlcCabinetProcess(config(), data_root=tmp_path)
    process.start()
    try:
        execute(process, "connect", "enable")
        execute(process, "start", "start", payload={"channels": [1]})
        wait_for(process, lambda value: value["phase"] == "RUNNING")
        process._owner.close()
        assert process._process.wait(timeout=5) == 0
        assert process.snapshot()["communication_state"] == "COMM_LOST"
    finally:
        if process._process.poll() is None:
            process.close()


def test_stalled_mock_worker_is_preserved_until_close_confirmation(tmp_path):
    process = PlcCabinetProcess(config(), data_root=tmp_path)
    process.start()
    try:
        execute(process, "connect", "enable")
        original_timeout = process._close_timeout
        process._close_timeout = .2
        os.kill(process.pid, signal.SIGSTOP)
        try:
            with pytest.raises(RuntimeError, match="停机未确认"):
                process.close()
            assert process._process.poll() is None
            assert process.snapshot()["stale"]
        finally:
            os.kill(process.pid, signal.SIGCONT)
            process._close_timeout = original_timeout
        wait_for(process, lambda value: process.shutdown_status.get("ready") is True)
    finally:
        process.close()
    assert process.shutdown_status["state"] == "confirmed"
