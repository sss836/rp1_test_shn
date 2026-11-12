"""Exercise the real gateway worker process boundary using only fake hardware."""

import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import numpy as np
import pytest

from factory_hmi.core._legacy import motor_default_positions
from factory_hmi.core.config import load_station_config
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.station_process import StationOutcomeUnknown, _Channel, _encode, _receive


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    mock_plc = tmp_path / "mock-plc.yaml"
    mock_plc.write_text(
        "driver: mock\naccess_mode: control\ntimings: {poll_ms: 100}\n"
        "dut_can_map: {DUT1: can0, DUT2: can1, DUT3: can2, DUT4: can3}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RP1_FACTORY_PLC_CONFIG", str(mock_plc))
    monkeypatch.setenv("RP1_FACTORY_ALLOWED_CONFIG_ROOT", str(Path(__file__).resolve().parents[1] / "scripts/config"))
    monkeypatch.setenv("RP1_FACTORY_CAN_DISABLED", "1")
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_URL", "")
    monkeypatch.delenv("RP1_FACTORY_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("RP1_FACTORY_SHUTDOWN_TOKEN", raising=False)
    monkeypatch.delenv("RP1_HMI_UPDATE_MONITOR_ONLY", raising=False)
    monkeypatch.setenv("RP1_HMI_LOCAL_MAINTENANCE_TOKEN", "fixture-local-maintenance")
    return create_app(data_root=tmp_path / "state", isolate_stations=True)


def test_firmware_maintenance_reads_live_scan_state_from_station_process(isolated_app):
    owner = "firmware-maintenance-worker"
    headers = {"X-RP1-Client-ID": owner, "X-RP1-Local-Maintenance": "fixture-local-maintenance"}
    path = "/api/v1/motor-maintenance"
    params = {"local": "true"}
    with TestClient(isolated_app, client=("127.0.0.1", 50000),
                    raise_server_exceptions=False) as client:
        configured = client.post("/api/v1/configure", headers=headers, json={
            "config_path": "left_arm_motors.yaml", "limb": "left_arm", "backend": "fake",
            "bus_bindings": [{"index": 0, "interface": "can0", "mode": "canfd",
                              "bitrate": 1_000_000, "dbitrate": 5_000_000}],
        })
        assert configured.status_code == 200, configured.text
        headers["X-RP1-Lease-Generation"] = str(configured.json()["snapshot"]["lease"]["generation"])
        worker = isolated_app.state.runtime.session(owner, create=False)._process
        assert worker.pid != os.getpid()

        def scan_required():
            response = client.get(path, headers=headers, params=params)
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert len(result["targets"]) == 7 and result["write_authorized"] is True
            return result["scan_required"]

        def preview():
            return client.post(path + "/preview", headers=headers, params=params,
                               json={"selections": []})

        assert scan_required() is True
        can = client.post("/api/v1/can/connect", headers=headers)
        assert can.status_code == 200, can.text
        rejected = preview()
        assert rejected.status_code == 409 and "刷新版本" in rejected.text
        scanned = client.post("/api/v1/motors/discover", headers=headers)
        assert scanned.status_code == 200, scanned.text
        assert scan_required() is False
        empty = preview()
        assert empty.status_code == 409 and "请选择非空" in empty.text

        transport = worker.controller.backend.maintenance_transport()
        transport.__enter__()
        try:
            assert scan_required() is True
        finally:
            transport.close()
        rescanned = client.post("/api/v1/motors/discover", headers=headers)
        assert rescanned.status_code == 200, rescanned.text
        assert rescanned.json()["snapshot"]["station"]["motors_enabled"] is False
        assert scan_required() is False


def test_quick_arm_finishes_one_cycle_then_disables_and_seals_its_record(isolated_app, tmp_path):
    headers = {"X-RP1-Client-ID": "quick-arm-record"}

    def wait_for(read, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = read()
            if result:
                return result
            time.sleep(.05)
        pytest.fail("mock station did not reach the expected phase")

    with TestClient(isolated_app) as client:
        prepared = client.post("/api/v1/quick/prepare", headers=headers,
                               json={"profile": "left_arm", "backend": "fake"})
        assert prepared.status_code == 200, prepared.text
        assert prepared.json()["result"]["profile"]["test_case_id"] == "UNIT-AGING-006"
        started = client.post("/api/v1/quick/start", headers=headers, json={
            "duration_hours": 1, "sample_id": "fake-arm-001", "data_domain": "FACTORY",
            "bench_id": "TEST-01", "physical_estop_confirmed": True,
            "parking_pose_confirmed": True, "power_source": "external",
        })
        assert started.status_code == 200, started.text
        test_id = started.json()["snapshot"]["playback"]["test_id"]
        assert test_id.startswith("LOCAL_UNIT-AGING-006_")

        def snapshot():
            response = client.get("/api/v1/snapshot", headers=headers)
            return response.json() if response.status_code == 200 else {}

        wait_for(lambda: (snapshot().get("playback") or {}).get("test_started_at"), 3)
        ended = client.post("/api/v1/quick/end", headers=headers)
        assert ended.status_code == 200, ended.text
        assert ended.json()["result"]["phase"] == "finishing_cycle"
        assert ended.json()["snapshot"]["station"]["motors_enabled"]

        def terminal_quick_status():
            status = client.get("/api/v1/quick/status", headers=headers).json()
            return status if not status["active"] else None

        finished = wait_for(terminal_quick_status, 24)
        assert finished["phase"] == "completed", finished

        def released_snapshot():
            current = snapshot()
            return (current.get("station", {}).get("motors_enabled") is False
                    and current.get("can", {}).get("all_up") is False)

        wait_for(released_snapshot, 2)
        report = tmp_path / "state" / "executions" / test_id
        wait_for(lambda: (report / "manifest.json").is_file(), 5)
        context = json.loads((report / "config/execution.json").read_text())
        assert context["test_case_id"] == "UNIT-AGING-006"
        assert context["completed_cycles"] == 1 and context["outcome"] == "stopped"
        assert context["run_request"]["completion_mode"] == "mujoco_hanging"
        assert context["motion_completion"]["safe_disabled"] is True
        assert context["motion_completion"]["finish_cycle_requested"] is True
        assert (report / "telemetry/raw.csv").stat().st_size > 0


def test_stopped_fake_station_does_not_block_other_station_or_mock_cabinet(isolated_app):
    app = isolated_app
    arm = {"X-RP1-Client-ID": "fake-arm-window"}
    leg = {"X-RP1-Client-ID": "fake-leg-window"}

    def config(limb, interface):
        return {"config_path": f"{limb}_motors.yaml",
                "limb": limb, "backend": "fake", "bus_bindings": [
                    {"index": 0, "interface": interface, "mode": "canfd",
                     "bitrate": 1_000_000, "dbitrate": 5_000_000},
                ]}

    with TestClient(app) as client:
        def configure(headers, limb, interface):
            return client.post("/api/v1/configure", headers=headers, json=config(limb, interface))

        def get_snapshot(headers):
            return client.get("/api/v1/snapshot", headers=headers)

        def wait_for_snapshot(headers, status_code):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                response = get_snapshot(headers)
                if response.status_code == status_code:
                    return response
                time.sleep(.05)
            raise AssertionError(f"mock station did not reach snapshot status {status_code}")

        def get_plc():
            return client.get("/api/v1/plc/snapshot", params={"include_history": False}).json()

        response = configure(arm, "left_arm", "can0")
        assert response.status_code == 200, response.text
        arm["X-RP1-Lease-Generation"] = str(response.json()["snapshot"]["lease"]["generation"])
        assert configure(leg, "right_leg", "can0").status_code == 409
        response = configure(leg, "right_leg", "can1")
        assert response.status_code == 200, response.text
        leg["X-RP1-Lease-Generation"] = str(response.json()["snapshot"]["lease"]["generation"])
        hub = app.state.runtime
        arm_pid = hub.session(arm["X-RP1-Client-ID"], create=False)._process.pid
        leg_pid = hub.session(leg["X-RP1-Client-ID"], create=False)._process.pid
        assert len({arm_pid, leg_pid, os.getpid()}) == 3
        assert set(client.get("/api/v1/health").json()["station_processes"]) == {arm_pid, leg_pid}
        stale_arm = {**arm, "X-RP1-Lease-Generation": str(int(arm["X-RP1-Lease-Generation"]) + 1)}
        assert client.post("/api/v1/station/close", headers=stale_arm).status_code == 409

        before = get_plc()
        assert before["driver"] == "mock" and before["communication_state"] == "DISABLED"
        coordination = before["coordination"]
        connected = client.post("/api/v1/plc/connect", headers=leg, json={
            "user": "isolated mock test", "request_id": str(uuid4()),
            "expected_epoch": coordination["epoch"], "expected_revision": coordination["revision"],
        })
        assert connected.status_code == 200, connected.text
        assert get_plc()["communication_state"] == "LIVE"
        assert wait_for_snapshot(arm, 200).json()["station"]["limb"] == "left_arm"
        leg_sequence = wait_for_snapshot(leg, 200).json()["_display"]["sequence"]

        # Signal only the proven, isolated fake station child. Always resume it
        # before TestClient waits for verified gateway shutdown.
        os.kill(arm_pid, signal.SIGSTOP)
        try:
            wait_for_snapshot(arm, 503)
            started = time.monotonic()
            leg_view = get_snapshot(leg)
            assert leg_view.status_code == 200 and leg_view.json()["station"]["limb"] == "right_leg"
            assert leg_view.json()["_display"]["sequence"] > leg_sequence
            renewed = client.post("/api/v1/lease/heartbeat", headers=leg)
            assert renewed.status_code == 200 and renewed.json()["ok"] is True
            cabinet = get_plc()
            assert cabinet["communication_state"] == "LIVE" and cabinet["coordination"]["epoch"] == coordination["epoch"]
            assert time.monotonic() - started < 2.0
        finally:
            os.kill(arm_pid, signal.SIGCONT)

        closed = client.post("/api/v1/station/close", headers=arm)
        assert closed.status_code == 200, closed.text
        assert closed.json() == {"ok": True, "result": {"released": True}}
        assert client.get("/api/v1/health").json()["station_processes"] == [leg_pid]
        assert not Path(f"/proc/{arm_pid}").exists()
        assert get_snapshot(leg).status_code == 200
        assert client.post("/api/v1/station/close", headers=leg).status_code == 200


def test_worker_revokes_control_after_mock_plc_permit_loss(isolated_app):
    app = isolated_app
    owner = "permit-worker"
    headers = {"X-RP1-Client-ID": owner}

    with TestClient(app) as client:
        configured = client.post("/api/v1/configure", headers=headers, json={
            "config_path": "left_arm_motors.yaml", "limb": "left_arm", "backend": "fake",
            "bus_bindings": [{"index": 0, "interface": "can0", "mode": "canfd",
                              "bitrate": 1_000_000, "dbitrate": 5_000_000}],
        })
        assert configured.status_code == 200, configured.text
        generation = configured.json()["snapshot"]["lease"]["generation"]
        headers["X-RP1-Lease-Generation"] = str(generation)
        can = client.post("/api/v1/can/connect", headers=headers)
        assert can.status_code == 200 and can.json()["result"]["can"]["all_up"]
        hub = app.state.runtime
        plc = hub.plc

        def command(operation, payload):
            context = plc.snapshot(include_history=False)["coordination"]
            return plc.coordination.execute(operation, payload, user=owner, client_id=owner,
                                            request_id=str(uuid4()), expected_epoch=context["epoch"],
                                            expected_revision=context["revision"])

        def wait_for(check):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                if check():
                    return
                time.sleep(.05)
            pytest.fail("mock cabinet or worker did not reach the expected state")

        command("connect", {})
        command("start", {"channels": [1]})
        wait_for(lambda: plc.snapshot(include_history=False)["phase"] == "RUNNING")
        # Arm the same worker-local watcher as a PLC-powered QuickRun. No
        # QuickRun or gateway display reader participates in the stop.
        runtime = hub.session(owner, create=False)
        worker = runtime._process
        worker.watch_plc(plc.connection_info(), [1])
        command("channel", {"channel": 1, "enabled": False})
        wait_for(lambda: worker.lease.snapshot().owner is None)
        assert worker.display_snapshot()["can"]["all_up"] is False
        assert client.post("/api/v1/lease/heartbeat", headers=headers).status_code == 409
        assert client.post("/api/v1/station/close", headers=headers).status_code == 200


def test_partial_response_resumes_and_close_is_not_replayed():
    client, worker = socket.socketpair()
    with client, worker:
        channel = _Channel(client)
        # Write the response header and part of the payload only. A bounded
        # read must retain them across the timeout instead of blocking on IPC.
        response = _encode(("ok", None))
        worker.sendall(response[:7])
        with pytest.raises(StationOutcomeUnknown):
            channel.call("close", (), {}, timeout=.05)
        assert _receive(worker, bytearray()) == ("close", (), {})
        worker.sendall(response[7:])
        assert channel.call("close", (), {}, timeout=.05) is None
        worker.setblocking(False)
        with pytest.raises(BlockingIOError):
            worker.recv(1)


def test_can_connect_waits_for_the_same_late_partial_reply():
    client, worker = socket.socketpair()
    with client, worker:
        channel = _Channel(client)
        received = []
        reply = _encode(("ok", {"can": {"all_up": True}}))

        def send_reply():
            received.append(_receive(worker, bytearray(), time.monotonic() + 1))
            worker.sendall(reply[:7])
            time.sleep(.12)
            worker.sendall(reply[7:])

        thread = threading.Thread(target=send_reply)
        thread.start()
        args = {"command_id": "can-late-001"}
        assert channel.call("can_connect", (1,), args, timeout=.05, late_reply_timeout=.5) == {
            "can": {"all_up": True}
        }
        thread.join()
        assert received == [("can_connect", (1,), args)]
        assert channel.pending is None
        worker.setblocking(False)
        with pytest.raises(BlockingIOError):
            worker.recv(1)


def test_can_receipt_survives_delayed_connect_and_failed_snapshot(isolated_app, monkeypatch):
    arm, leg = ({"X-RP1-Client-ID": owner} for owner in ("receipt-arm", "receipt-leg"))
    hub = isolated_app.state.runtime
    original = hub.connect_can
    entered, release = threading.Event(), threading.Event()
    attempts = []

    def connect(owner, runtime, bindings, **identity):
        attempts.append((owner, identity["command_id"]))
        if identity["command_id"] == "arm-can-004":
            raise StationOutcomeUnknown("injected missing control reply")
        if identity["command_id"] == "arm-can-001":
            entered.set()
            assert release.wait(8)
        return original(owner, runtime, bindings, **identity)

    def unavailable_snapshot(**_kwargs):
        raise StationOutcomeUnknown("injected read channel failure")

    def configure(client, headers, limb, interface, command_id):
        configured = client.post("/api/v1/configure", headers=headers, json={
            "config_path": f"{limb}_motors.yaml", "limb": limb, "backend": "fake",
            "bus_bindings": [{"index": 0, "interface": interface, "mode": "canfd",
                              "bitrate": 1_000_000, "dbitrate": 5_000_000}],
        })
        assert configured.status_code == 200, configured.text
        snapshot = configured.json()["snapshot"]
        return {"command_id": command_id, "expected_config_revision": snapshot["config_revision"],
                "expected_lease_generation": snapshot["lease"]["generation"]}

    def receipt(client, headers, cmd):
        response = client.get(f"/api/v1/can/connect/receipts/{cmd}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def terminal(client, headers, cmd):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            value = receipt(client, headers, cmd)
            if value["status"] != "pending":
                return value
            time.sleep(.02)
        pytest.fail(f"missing CAN connection receipt for {cmd}")

    path = "/api/v1/can/connect"
    with TestClient(isolated_app) as client:
        arm_body = configure(client, arm, "left_arm", "can0", "arm-can-001")
        leg_body = configure(client, leg, "left_short_leg", "can1", "leg-can-001")
        monkeypatch.setattr(hub, "connect_can", connect)
        monkeypatch.setattr(hub.session(arm["X-RP1-Client-ID"]), "snapshot", unavailable_snapshot)
        try:
            first = client.post(path, headers=arm, json=arm_body)
            assert first.status_code == 202, first.text
            assert entered.wait(2)
            assert receipt(client, arm, "arm-can-001")["status"] == "pending"
            assert client.post(path, headers=arm, json=arm_body).status_code == 202
            assert client.post(path, headers=arm, json={**arm_body, "command_id": "arm-can-002"}).status_code == 409
            assert client.post(path, headers=arm, json={**arm_body, "expected_config_revision": "old"}).status_code == 409
            assert receipt(client, leg, "arm-can-001")["status"] == "not_found"
            heartbeat = client.post("/api/v1/lease/heartbeat", headers={
                **arm, "X-RP1-Lease-Generation": str(arm_body["expected_lease_generation"]),
            })
            assert heartbeat.status_code == 200, heartbeat.text
            assert client.post(path, headers=leg, json=leg_body).status_code == 202
            assert terminal(client, leg, "leg-can-001")["status"] == "connected"
            assert receipt(client, arm, "arm-can-001")["status"] == "pending"
        finally:
            release.set()
        result = terminal(client, arm, "arm-can-001")
        assert result["status"] == "connected", result
        assert result["result"]["can"]["all_up"] is True
        assert result["result"]["station"]["motors_enabled"] is False
        assert client.post(path, headers=arm, json=arm_body).json()["result"] == result
        assert attempts == [("receipt-arm", "arm-can-001"), ("receipt-leg", "leg-can-001")]

        stale = {**arm_body, "command_id": "arm-can-003", "expected_config_revision": "old"}
        assert client.post(path, headers=arm, json=stale).status_code == 202
        failure = terminal(client, arm, "arm-can-003")
        assert failure["status"] == "failed" and "配置已变化" in failure["error"]

        unknown = {**arm_body, "command_id": "arm-can-004"}
        assert client.post(path, headers=arm, json=unknown).status_code == 202
        result = terminal(client, arm, "arm-can-004")
        assert result["status"] == "unknown" and result["result"] is None
        assert client.post(path, headers=arm, json=unknown).json()["result"] == result
        assert attempts.count(("receipt-arm", "arm-can-004")) == 1


def test_two_fake_stations_play_and_record_concurrently(tmp_path, isolated_app):
    app = isolated_app
    with TestClient(app) as client:
        arm = {"X-RP1-Client-ID": "play-arm"}
        leg = {"X-RP1-Client-ID": "play-leg"}
        config_root = Path(__file__).resolve().parents[1] / "scripts/config"
        for headers, limb in ((arm, "left_arm"), (leg, "right_leg")):
            config_name = f"{limb}_motors.yaml"
            configured = client.post("/api/v1/configure", headers=headers, json={
                "config_path": config_name, "limb": limb, "backend": "fake",
            })
            assert configured.status_code == 200, configured.text
            generation = configured.json()["snapshot"]["lease"]["generation"]
            headers["X-RP1-Lease-Generation"] = str(generation)
            config = load_station_config(limb, config_root / config_name)
            positions = motor_default_positions(config.raw, config.limb)
            trajectory = tmp_path / f"{limb}.npz"
            np.savez(trajectory, motor_pos=np.tile(positions, (3, 1)), fps=[10.], time=[0., .1, .2], limb=[limb])
            imported = client.post("/api/v1/trajectory/upload", headers={**headers, "X-Filename": trajectory.name},
                                   content=trajectory.read_bytes())
            assert imported.status_code == 200, imported.text[:2000]
            for route, body in (("can/connect", None), ("motors/discover", None),
                                ("motors/enable", {"physical_estop_confirmed": True}), ("arm", None)):
                prepared = client.post(f"/api/v1/{route}", headers=headers, json=body)
                assert prepared.status_code == 200, prepared.text[:2000]

        for headers in (arm, leg):
            started = client.post("/api/v1/playback/start", headers=headers, json={
                "duration_hours": .01, "recording_scope": "local", "test_case_id": "OTHER",
                "test_name": "mock dual process", "robot_id": "synthetic-isolation", "bench_id": "fake-bench",
            })
            assert started.status_code == 200, started.text[:2000]

        def station_progress(headers):
            response = client.get("/api/v1/snapshot", headers=headers)
            assert response.status_code == 200, response.text[:2000]
            value = response.json()
            assert value["state"] == "running" and value["station"]["motors_enabled"] is True
            return value["progress"]["command_count"], value["playback"]["recording"]["row_count"]

        time.sleep(.25)  # Allow a fresh display slot after the second start.
        before = [station_progress(headers) for headers in (arm, leg)]
        time.sleep(.5)
        after = [station_progress(headers) for headers in (arm, leg)]
        for (commands, rows), (new_commands, new_rows) in zip(before, after, strict=True):
            assert new_commands > commands and new_rows > rows
        assert app.state.runtime.plc.snapshot(include_history=False)["communication_state"] == "DISABLED"

        assert client.post("/api/v1/station/close", headers=arm).status_code == 200
        assert station_progress(leg)[0] > before[1][0]
        assert client.post("/api/v1/station/close", headers=leg).status_code == 200
        records = list((tmp_path / "state" / "sessions").glob("*/records/*.csv"))
        assert len(records) == 2 and all(path.stat().st_size > 0 for path in records)


def test_gateway_channel_loss_preserves_active_protection(isolated_app):
    app = isolated_app
    owner = "orphan-fake-arm"
    headers = {"X-RP1-Client-ID": owner}
    with TestClient(app) as client:
        configured = client.post("/api/v1/configure", headers=headers, json={
            "config_path": "left_arm_motors.yaml", "limb": "left_arm", "backend": "fake",
        })
        assert configured.status_code == 200, configured.text
        connected = client.post("/api/v1/connect", headers=headers)
        assert connected.status_code == 200 and connected.json()["snapshot"]["station"]["motors_enabled"]
        worker = app.state.runtime.session(owner, create=False)._process

        # Drop only the fake worker's gateway lease channel, keeping its
        # safety channel accessible to check the actual protection state.
        worker._channels["lease"].connection.shutdown(socket.SHUT_WR)
        try:
            deadline = time.monotonic() + 4
            protection = None
            while time.monotonic() < deadline:
                protection = worker.protective_damping_snapshot()
                if protection and protection.get("active"):
                    break
                time.sleep(.05)
            assert protection and protection["active"] is True
            assert worker._process.is_alive()
            assert app.state.runtime.interface_owners() == {"can0": owner}
            with pytest.raises(RuntimeError, match="保护阻尼"):
                worker.close()
        finally:
            if worker._process.is_alive():
                protection = worker.protective_damping_snapshot()
                if protection and protection.get("active"):
                    worker.disable_protection()
                worker.close()
