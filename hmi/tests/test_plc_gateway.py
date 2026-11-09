from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from factory_hmi.gateway.app import create_app

def _post(client, path, payload):
    import uuid
    ctx = client.get("/api/v1/plc/snapshot").json()["coordination"]
    return client.post(path, headers={"X-RP1-Client-ID": "test-window"}, json=dict(
        payload, request_id=str(uuid.uuid4()), expected_epoch=ctx["epoch"], expected_revision=ctx["revision"],
    ))



def _wait_phase(client: TestClient, expected: str) -> dict:
    deadline = time.monotonic() + 2.0
    snapshot: dict = {}
    while time.monotonic() < deadline:
        response = client.get("/api/v1/plc/snapshot")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["phase"] == expected:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"PLC did not reach {expected}: {snapshot}")


def test_gateway_exposes_protocol_neutral_plc_sequence(tmp_path: Path) -> None:
    with TestClient(create_app(data_root=tmp_path)) as client:
        snapshot = client.get("/api/v1/plc/snapshot").json()
        assert snapshot["communication_enabled"] is False
        assert snapshot["communication_state"] == "DISABLED"
        assert _post(client, "/api/v1/plc/connect", {"user": "gateway-test"}).status_code == 200
        assert snapshot["driver"] == "mock"
        assert snapshot["dut_can_map"] == {
            "DUT1": "can0",
            "DUT2": "can1",
            "DUT3": "can2",
            "DUT4": "can3",
        }

        response = _post(client,
            "/api/v1/plc/start",
            {
                "user": "gateway-test",
                "channels": [1, 2, 3, 4],
                "voltage": 3.0,
                "current": 0.5,
            },
        )
        assert response.status_code == 200
        running = _wait_phase(client, "RUNNING")
        assert running["status"]["PS1ActualOutput"] is True
        assert running["status"]["ChannelPermit"] == [True] * 4
        assert running["status"]["PS1SetVoltage"] == 3.0
        assert running["status"]["PS1SetCurrent"] == 0.5

        response = _post(client,
            "/api/v1/plc/stop",
            {"user": "gateway-test"},
        )
        assert response.status_code == 200
        stopped = _wait_phase(client, "OFF")
        # Power-off invalidates PS1's prior-cycle feedback. TCP still works,
        # but the unavailable device status is never presented as actual OFF.
        assert stopped["status"]["PS1ActualOutput"] is None
        assert stopped["status"]["PS1FeedbackValid"] is False
        assert stopped["status"]["MainContactorFB"] is False
        assert any(
            item["user"] == "gateway-test" and item["confirmation"] == "confirmed"
            for item in stopped["operation_log"]
        )


def test_update_monitor_can_explicitly_enable_readonly_plc(tmp_path, monkeypatch):
    import yaml
    from unittest.mock import patch
    from factory_hmi.plc.config import DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw.update(driver="mock", access_mode="monitor")
    config = tmp_path / "monitor.yaml"
    config.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("RP1_FACTORY_PLC_CONFIG", str(config))
    monkeypatch.setenv("RP1_HMI_UPDATE_MONITOR_ONLY", "1")
    app = create_app(data_root=tmp_path / "state")
    service = app.state.runtime.plc
    with patch.object(service.client, 'write_command', side_effect=AssertionError('readonly must never write')):
        with TestClient(app) as client:
            assert client.get('/api/v1/plc/snapshot').json()['communication_enabled'] is False
            enabled = _post(client, '/api/v1/plc/connect', {'user':'monitor'})
            assert enabled.status_code == 200
            assert enabled.json()['result']['read_only'] is True
            assert enabled.json()['result']['communication_state'] == 'LIVE'
            assert _post(client, '/api/v1/plc/start', {'user':'monitor','channels':[], 'voltage':3.,'current':.5}).status_code == 403
            assert _post(client, '/api/v1/plc/disconnect', {'user':'monitor'}).status_code == 200


def test_update_monitor_rejects_control_mode_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_HMI_UPDATE_MONITOR_ONLY", "1")
    with TestClient(create_app(data_root=tmp_path)) as client:
        assert _post(client, '/api/v1/plc/connect', {'user':'monitor'}).status_code == 403
        assert client.get('/api/v1/plc/snapshot').json()['communication_enabled'] is False
