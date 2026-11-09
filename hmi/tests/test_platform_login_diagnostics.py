"""Platform login classification and explicit loopback transport, with synthetic peers."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import shutil
import socket
import ssl
import subprocess
import threading
from urllib import error, request
from unittest.mock import Mock

import pytest

from factory_hmi.client import GatewayClient, is_loopback_gateway
from factory_hmi.sync.reliability_platform import (
    PlatformLoginError, ReliabilityPlatformClient, platform_connection_diagnostics,
    reliability_worker_from_environment,
)


@contextmanager
def endpoint(*, tls=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.reply(b'{"status":"ok"}')

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            self.reply(b'{"data":{"user":{"username":"synthetic"}}}')

        def reply(self, body):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        if tls is not None:
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            scheme = "https" if tls else "http"
            yield f"{scheme}://127.0.0.1:{server.server_port}", calls
        finally:
            server.shutdown()
            thread.join(timeout=3)


@pytest.mark.parametrize("url,expected", [
    ("ws://localhost:8765/ws/status", True), ("https://127.0.3.1:8443", True),
    ("wss://[::1]:8765/", True), ("https://localhost.:8443", True),
    ("https://10.43.17.214:8443", False), ("http://localhost.example:8765", False),
    ("http://localhost@remote.invalid", False), ("https://[invalid", False),
])
def test_explicit_loopback_addresses(url, expected):
    assert is_loopback_gateway(url) is expected


def test_loopback_bypasses_proxy_and_remote_gateway_keeps_policy(monkeypatch):
    with endpoint() as (local, local_calls), endpoint() as (proxy, proxy_calls):
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("no_proxy", "")
        monkeypatch.setattr(request, "_opener", None)
        assert GatewayClient(local).health() == {"status": "ok"}
        assert ReliabilityPlatformClient(local).authenticate_operator("synthetic", "fixture")["user"] == {"username": "synthetic"}
        assert local_calls == ["/api/v1/health", "/api/v1/auth/login"]
        assert not proxy_calls
        assert GatewayClient("http://remote.invalid:8765").health() == {"status": "ok"}
        monkeypatch.delenv("RP1_FACTORY_PLATFORM_DIRECT", raising=False)
        remote = ReliabilityPlatformClient("http://remote.invalid:8765")
        assert remote.authenticate_operator("synthetic", "fixture")["user"] == {"username": "synthetic"}
        assert proxy_calls == ["http://remote.invalid:8765/api/v1/health", "http://remote.invalid:8765/api/v1/auth/login"]


def test_explicit_platform_direct_and_environment_diagnostics(monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_URL", "https://platform.invalid:8443")
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_DIRECT", "1")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/ca.crt")
    build = Mock(wraps=request.build_opener)
    monkeypatch.setattr(request, "build_opener", build)
    ReliabilityPlatformClient("https://platform.invalid:8443")
    assert any(isinstance(handler, request.ProxyHandler) and handler.proxies == {} for handler in build.call_args.args)
    assert platform_connection_diagnostics() == {"platform_origin": "https://platform.invalid:8443", "ca_source": "environment_ca"}


@pytest.mark.parametrize("failure,code,status", [
    (error.URLError(ssl.SSLCertVerificationError("synthetic private detail")), "platform_certificate_failed", 502),
    (error.URLError(ssl.SSLError("synthetic private detail")), "platform_tls_failed", 502),
    (error.URLError(socket.gaierror("synthetic private detail")), "platform_dns_failed", 502),
    (error.URLError(ConnectionRefusedError("synthetic private detail")), "platform_connection_failed", 502),
    (TimeoutError("synthetic private detail"), "platform_login_timeout", 504),
])
def test_login_transport_errors_are_layered_without_raw_exception(monkeypatch, failure, code, status):
    monkeypatch.setenv("RP1_HMI_STATE_ROOT", "/private-station")
    monkeypatch.setenv("SSL_CERT_FILE", "/private-station/config/platform-ca.crt")
    client = ReliabilityPlatformClient("https://platform.invalid:8443")
    client._operator_opener.open = Mock(side_effect=failure)
    with pytest.raises(PlatformLoginError) as caught:
        client.authenticate_operator("fixture-user", "fixture-password")
    exc = caught.value
    assert exc.gateway_status == status and exc.status is None
    assert exc.detail == {"code": code, "message": str(exc), "platform_origin": "https://platform.invalid:8443",
                          "ca_source": "workstation_ca_file", "upstream_status": None}
    assert all(value not in str(exc) for value in ("synthetic private detail", "private-station", "fixture-user", "fixture-password"))


@pytest.mark.parametrize("upstream,code,gateway", [
    (401, "platform_credentials_rejected", 401), (403, "platform_login_forbidden", 403),
    (404, "platform_login_route_missing", 404), (407, "platform_proxy_rejected", 407),
    (429, "platform_login_rate_limited", 429), (503, "platform_login_http_error", 502),
])
def test_login_uses_status_and_does_not_echo_upstream_reply(upstream, code, gateway):
    client = ReliabilityPlatformClient("https://fixture-user:fixture-password@platform.invalid:8443/private-path")
    reply = io.BytesIO(b'{"detail":"synthetic private detail"}')
    client._operator_opener.open = Mock(side_effect=error.HTTPError(client.base_url, upstream, "fixture", {}, reply))
    with pytest.raises(PlatformLoginError) as caught:
        client.authenticate_operator("fixture-user", "fixture-password")
    exc = caught.value
    assert exc.gateway_status == gateway and exc.status == upstream and reply.closed
    assert exc.detail["code"] == code and exc.detail["platform_origin"] == "https://platform.invalid:8443"
    assert all(value not in str(exc) for value in ("synthetic private detail", "private-path", "fixture-user", "fixture-password"))


def test_invalid_login_payload_and_unconfigured_platform(monkeypatch):
    client = ReliabilityPlatformClient("https://platform.invalid")
    client._call = Mock(return_value={"unrelated": True})
    with pytest.raises(PlatformLoginError) as invalid:
        client.authenticate_operator("synthetic", "fixture")
    assert invalid.value.detail["code"] == "platform_login_response_invalid"
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_URL", "")
    with pytest.raises(PlatformLoginError) as unconfigured:
        reliability_worker_from_environment(None)
    assert unconfigured.value.gateway_status == 503
    assert unconfigured.value.detail["code"] == "platform_not_configured"


def test_real_local_tls_rejects_untrusted_ca_then_accepts_provisioned_ca(tmp_path, monkeypatch):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL CLI is required for the local TLS fixture")
    config = tmp_path / "config"
    config.mkdir()
    ca, key = config / "platform-ca.crt", tmp_path / "ephemeral.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=Synthetic HMI test CA", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(ca)], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(ca, key)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setenv("RP1_HMI_STATE_ROOT", str(tmp_path))
    with endpoint(tls=tls) as (url, calls):
        with pytest.raises(PlatformLoginError) as rejected:
            ReliabilityPlatformClient(url, timeout=3).authenticate_operator("synthetic", "fixture")
        assert rejected.value.detail["code"] == "platform_certificate_failed"
        assert rejected.value.detail["ca_source"] == "system_ca"
        assert not calls
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        trusted = ReliabilityPlatformClient(url, timeout=3)
        assert trusted.connection_diagnostics()["ca_source"] == "workstation_ca_file"
        assert trusted.authenticate_operator("synthetic", "fixture")["user"] == {"username": "synthetic"}
        assert calls == ["/api/v1/auth/login"]
