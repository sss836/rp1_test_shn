"""Platform connection diagnostics cross the local gateway without secrets."""

from fastapi.testclient import TestClient

from factory_hmi.gateway.app import GatewayRuntime, create_app
from factory_hmi.sync.reliability_platform import PlatformLoginError


def test_gateway_reports_effective_target_and_safe_tls_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_URL", "https://test-platform.invalid:8443")
    monkeypatch.setenv("RP1_HMI_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "config/platform-ca.crt"))
    connection = {
        "platform_origin": "https://test-platform.invalid:8443",
        "ca_source": "workstation_ca_file",
    }

    def fail_login(_runtime, _body):
        raise PlatformLoginError(
            "platform_certificate_failed", "平台证书校验失败。", connection, gateway_status=502
        )

    monkeypatch.setattr(GatewayRuntime, "platform_login", fail_login)
    app = create_app(data_root=tmp_path / "data")
    client = TestClient(app)  # No lifespan, worker or device startup.
    try:
        assert client.get("/api/v1/health").json()["platform_connection"] == connection
        response = client.post("/api/v1/platform/login", json={
            "username": "synthetic-operator", "password": "synthetic-password",
        })
        assert response.status_code == 502
        assert response.json()["detail"]["code"] == "platform_certificate_failed"
        assert response.json()["detail"]["platform_origin"] == connection["platform_origin"]
        assert "synthetic-password" not in response.text
        assert str(tmp_path) not in response.text
    finally:
        client.close()
        app.state.runtime.close()
