"""Execute the copied command against an isolated software-only HTTP server."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shlex
import subprocess
import threading

import pytest

from factory_hmi.desktop.update_commands import gateway_download_command, package_install_command, validated_package_path


def test_download_receipt_never_grants_direct_package_install(tmp_path):
    package = str(tmp_path / "下载包 ' $(touch MUST_NOT_RUN); `id`.deb")
    assert validated_package_path(package) == package
    with pytest.raises(ValueError, match="不能授权安装"):
        package_install_command(package)
    assert not (tmp_path / "MUST_NOT_RUN").exists()


def test_install_command_rejects_missing_relative_or_multiline_paths():
    for invalid in (None, "", "relative.deb", "/tmp/not-a-package.txt", "/tmp/file\n.deb", "/tmp/file\0.deb"):
        with pytest.raises(ValueError, match="绝对路径"):
            package_install_command(invalid)


@contextmanager
def gateway(status=200, payload=None):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers.get("X-RP1-Operator-Token")))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def execute(command, token=""):
    return subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=5,
                          env={**os.environ, "RP1_FACTORY_OPERATOR_TOKEN": token})


def test_copied_command_uses_only_download_route_and_prints_real_receipt_path(tmp_path):
    path = str(tmp_path / "含空格的 下载目录" / "rp1-test.deb")
    with gateway(payload={"ok": True, "result": {"status": "staged", "package_path": path}}) as (url, calls):
        command = gateway_download_command(url, "a" * 32)
        result = execute(command, token="test-only-gateway-secret")
    assert result.returncode == 0, result.stderr
    assert path in result.stdout and "尚未安装" in result.stdout
    assert calls == [("/api/v1/software-updates/" + "a" * 32 + "/download", "test-only-gateway-secret")]
    assert "test-only-gateway-secret" not in command + result.stdout + result.stderr


@pytest.mark.parametrize(("status", "payload", "message"), [
    (401, {}, "操作令牌"),
    (409, {}, "登录执行员或管理员"),
    (302, {}, "HTTP 302"),
    (200, {"ok": False}, "未取得已校验"),
    (200, {"ok": True, "result": {"status": "staged", "package_path": "relative.deb"}}, "绝对路径"),
])
def test_failures_do_not_report_a_success_or_follow_redirects(status, payload, message):
    with gateway(status, payload) as (url, calls):
        result = execute(gateway_download_command(url, "b" * 32))
    assert result.returncode != 0
    assert "安装包已下载" not in result.stdout
    assert message in result.stderr
    assert len(calls) == 1


def test_shell_metacharacters_stay_literal_and_credentials_are_rejected():
    url = "http://localhost/prefix'$(touch /tmp/must-not-run);`id`"
    args = shlex.split(gateway_download_command(url, "c" * 32))
    assert args[0:2] == ["python3", "-c"]
    assert args[3] == url + "/api/v1/software-updates/" + "c" * 32 + "/download"
    for invalid in ("http://user:secret@localhost", "http://localhost/?token=secret", "file:///tmp/x", "http://localhost/\n"):
        with pytest.raises(ValueError):
            gateway_download_command(invalid, "c" * 32)
    with pytest.raises(ValueError):
        gateway_download_command("http://localhost", "$(id)")
