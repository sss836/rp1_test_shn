"""Explicit version reads use the active station, without platform or motion."""

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from factory_hmi.gateway.app import create_app
from test_module_firmware_gateway import setup_service


@pytest.mark.parametrize("blocked", ["lease", "scan", "motion", "maintenance", "after_maintenance", None])
def test_gateway_version_refresh_requires_idle_scanned_station(tmp_path, monkeypatch, blocked):
    monkeypatch.delenv("RP1_FACTORY_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("RP1_HMI_UPDATE_MONITOR_ONLY", raising=False)
    service = setup_service(tmp_path)
    backend = service.controller.backend
    receipt = {"request_id": "fixture-refresh", "state": "queued", "completed_motors": 0, "total_motors": 2}
    backend.request_motor_version_refresh = Mock(return_value=receipt)
    backend.motor_version_refresh_status = Mock(return_value=receipt)
    if blocked == "scan":
        service.controller._motors_discovered = False
    elif blocked == "motion":
        backend._enabled = True
    elif blocked == "maintenance":
        service._maintenance = SimpleNamespace(active=True, cancel=lambda: None)
    if blocked == "after_maintenance":
        with backend.maintenance_transport():
            assert not backend.connected
        assert not backend.connected  # parameter reader intentionally released scanner
    with patch("factory_hmi.gateway.app.PlcCabinetService"):
        app = create_app(service, data_root=tmp_path / "gateway", lease_timeout_s=5)

    async def run():
        hub = app.state.runtime
        async with app.router.lifespan_context(app):
            runtime = hub.session("refresh-window")
            if blocked != "lease":
                runtime.lease.claim("refresh-window")
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://fixture",
                                         headers={"X-RP1-Client-ID": "refresh-window"}) as client:
                response = await client.post("/api/v1/motor-versions/refresh", json={})
                if blocked not in (None, "after_maintenance"):
                    assert response.status_code == 409, response.text
                    backend.request_motor_version_refresh.assert_not_called()
                else:
                    assert response.status_code == 200, response.text
                    assert response.json() == {"ok": True, "result": receipt}
                    backend.request_motor_version_refresh.assert_called_once_with()
                    assert backend.connected
                    status = await client.get("/api/v1/motor-versions/refresh")
                    assert status.json() == {"ok": True, "result": receipt}
                    with pytest.raises(RuntimeError, match="正在重新读取"):
                        service.maintenance_ready()
            service._maintenance = None
            backend._enabled = False

    asyncio.run(run())
