"""Exercise the real gateway worker process boundary using only fake hardware."""

import os
from pathlib import Path
import signal
import socket
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
    return create_app(data_root=tmp_path / "state", isolate_stations=True)


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
