"""NPZ size, streaming, rollback and launcher checks; no device transports."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest import mock

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from factory_hmi.client import GatewayClient, GatewayHTTPError
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.trajectory_upload import (
    DEFAULT_TRAJECTORY_MAX_BYTES,
    configured_trajectory_limit,
    receive_trajectory,
)
from launch import gateway_environment


def request_with_chunks(chunks, *, length=None, disconnect=False):
    iterator = iter(chunks)

    async def receive():
        try:
            chunk = next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"} if disconnect else {
                "type": "http.request", "body": b"", "more_body": False,
            }
        return {"type": "http.request", "body": chunk, "more_body": True}

    headers = [] if length is None else [(b"content-length", str(length).encode())]
    return Request({"type": "http", "headers": headers}, receive)


def test_default_is_one_gib_and_environment_can_override(monkeypatch):
    monkeypatch.delenv("RP1_FACTORY_TRAJECTORY_MAX_BYTES", raising=False)
    assert configured_trajectory_limit() == DEFAULT_TRAJECTORY_MAX_BYTES == 1024**3
    monkeypatch.setenv("RP1_FACTORY_TRAJECTORY_MAX_BYTES", str(512 * 1024**2))
    assert configured_trajectory_limit() == 512 * 1024**2
    assert configured_trajectory_limit(0) == 0


@pytest.mark.parametrize("value", ["-1", "unlimited", "1.5", "NaN"])
def test_invalid_environment_limit_fails_at_startup(monkeypatch, value):
    monkeypatch.setenv("RP1_FACTORY_TRAJECTORY_MAX_BYTES", value)
    with pytest.raises(ValueError, match="non-negative integer"):
        create_app(data_root=Path("unused"))


def test_stream_accepts_exact_limit(tmp_path):
    path, size = asyncio.run(receive_trajectory(
        request_with_chunks([b"ab", b"cd"], length=4), tmp_path, 4,
    ))
    assert size == 4 and path.read_bytes() == b"abcd"


@pytest.mark.parametrize("length", [None, 3])
def test_actual_stream_limit_is_checked_and_partial_removed(tmp_path, length):
    with pytest.raises(HTTPException) as error:
        asyncio.run(receive_trajectory(
            request_with_chunks([b"abc", b"de"], length=length), tmp_path, 4,
        ))
    assert error.value.status_code == 413
    assert not list(tmp_path.glob(".trajectory-*"))


@pytest.mark.parametrize("length,status", [(5, 413), (0, 413), (-1, 400), ("bad", 400)])
def test_invalid_declared_length_rejected_before_reading(tmp_path, length, status):
    def must_not_read():
        raise AssertionError("body should not be consumed")
        yield

    with pytest.raises(HTTPException) as error:
        asyncio.run(receive_trajectory(
            request_with_chunks(must_not_read(), length=length), tmp_path, 4,
        ))
    assert error.value.status_code == status
    assert not list(tmp_path.glob(".trajectory-*"))


@pytest.mark.parametrize("chunks,length,status", [([], None, 413), ([b"ab"], 3, 400)])
def test_empty_or_truncated_stream_is_removed(tmp_path, chunks, length, status):
    with pytest.raises(HTTPException) as error:
        asyncio.run(receive_trajectory(request_with_chunks(chunks, length=length), tmp_path, 4))
    assert error.value.status_code == status
    assert not list(tmp_path.glob(".trajectory-*"))


def test_client_disconnect_removes_partial_upload(tmp_path):
    with pytest.raises(ClientDisconnect):
        asyncio.run(receive_trajectory(
            request_with_chunks([b"ab"], disconnect=True), tmp_path, 4,
        ))
    assert not list(tmp_path.glob(".trajectory-*"))


def test_unlimited_setting_still_rejects_empty_file(tmp_path):
    path, size = asyncio.run(receive_trajectory(request_with_chunks([b"large"]), tmp_path, 0))
    assert size == 5
    path.unlink()
    with pytest.raises(HTTPException) as error:
        asyncio.run(receive_trajectory(request_with_chunks([]), tmp_path, 0))
    assert error.value.status_code == 413


def test_stream_larger_than_old_128_mib_cap(tmp_path):
    # Reuse a 1 MiB buffer; this exercises actual disk reception of 129 MiB,
    # without constructing a full-size HTTP body or a real motor trajectory.
    chunk = bytes(1024**2)
    size = 129 * len(chunk)
    path, received = asyncio.run(receive_trajectory(
        request_with_chunks((chunk for _ in range(129)), length=size),
        tmp_path, DEFAULT_TRAJECTORY_MAX_BYTES,
    ))
    try:
        assert received == path.stat().st_size == size
    finally:
        path.unlink()


class UploadController:
    def __init__(self):
        self.state = "disconnected"
        self.reject = False

    def configure(self, **kwargs):
        return kwargs

    def snapshot(self):
        return {"state": self.state, "motors": []}

    def import_trajectory(self, path):
        if self.reject:
            raise ValueError("invalid NPZ")
        return {"path": path, "size": Path(path).stat().st_size}

    def disconnect(self):
        self.state = "disconnected"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    config = tmp_path / "arm.yaml"
    config.write_text("{}")
    monkeypatch.setenv("RP1_FACTORY_ALLOWED_CONFIG_ROOT", str(tmp_path))
    monkeypatch.delenv("RP1_FACTORY_OPERATOR_TOKEN", raising=False)
    controller = UploadController()
    with TestClient(create_app(controller, data_root=tmp_path, trajectory_max_bytes=8)) as client:
        headers = {"X-RP1-Client-ID": "npz-test"}
        configured = client.post("/api/v1/configure", headers=headers, json={
            "config_path": str(config), "limb": "left_arm", "backend": "fake",
        })
        assert configured.status_code == 200, configured.text
        yield client, controller, tmp_path / "trajectories", {
            **headers, "X-Filename": "aging.npz", "Content-Type": "application/octet-stream",
        }


def test_failed_replacement_preserves_previous_file(gateway):
    client, controller, directory, headers = gateway
    assert client.post("/api/v1/trajectory/upload", headers=headers, content=b"valid").status_code == 200
    controller.reject = True
    failed = client.post("/api/v1/trajectory/upload", headers=headers, content=b"invalid")
    assert failed.status_code == 409
    assert (directory / "aging.npz").read_bytes() == b"valid"
    assert [p.name for p in directory.iterdir()] == ["aging.npz"]


def test_first_invalid_file_is_removed_and_can_be_retried(gateway):
    client, controller, directory, headers = gateway
    controller.reject = True
    assert client.post("/api/v1/trajectory/upload", headers=headers, content=b"bad").status_code == 409
    assert not list(directory.iterdir())
    controller.reject = False
    assert client.post("/api/v1/trajectory/upload", headers=headers, content=b"valid").status_code == 200


def test_oversize_or_wrong_extension_does_not_replace_existing_file(gateway):
    client, _, directory, headers = gateway
    (directory / "aging.npz").write_bytes(b"valid")
    assert client.post("/api/v1/trajectory/upload", headers=headers, content=b"x" * 9).status_code == 413
    assert client.post("/api/v1/trajectory/upload", headers={**headers, "X-Filename": "bad.zip"}, content=b"x").status_code == 400
    assert (directory / "aging.npz").read_bytes() == b"valid"
    assert [p.name for p in directory.iterdir()] == ["aging.npz"]


def test_client_streams_and_closes_file_on_http_failure(tmp_path):
    source = tmp_path / "input.npz"
    source.write_bytes(b"npz")
    streams = []

    def reject(req, **kwargs):
        from urllib.error import HTTPError

        streams.append(req.data)
        assert req.data.read(2) == b"np"
        assert req.get_header("Content-length") == "3"
        assert kwargs["timeout"] == 240
        raise HTTPError(req.full_url, 413, "too large", {}, io.BytesIO(b'{"detail":"limit"}'))

    with mock.patch("factory_hmi.client.request.urlopen", side_effect=reject), mock.patch.object(
        Path, "read_bytes", side_effect=AssertionError("whole-file upload is forbidden"),
    ):
        with pytest.raises(GatewayHTTPError) as error:
            GatewayClient("http://127.0.0.1:8766", trajectory_upload_timeout=240).trajectory_upload(source)
    assert error.value.status_code == 413 and streams[0].closed


@pytest.mark.parametrize("timeout", [-1, 0, float("inf"), float("nan")])
def test_invalid_upload_timeout_is_rejected(timeout):
    with pytest.raises(ValueError, match="positive finite"):
        GatewayClient("http://127.0.0.1:8766", trajectory_upload_timeout=timeout)


def test_launcher_preserves_only_explicit_upload_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("RP1_FACTORY_TRAJECTORY_MAX_BYTES", "1073741824")
    monkeypatch.setenv("RP1_FACTORY_TRAJECTORY_UPLOAD_TIMEOUT_S", "240")
    monkeypatch.setenv("RP1_FACTORY_UNTRUSTED_SETTING", "ignored")
    environment = gateway_environment(tmp_path, None, offline=True, driver="mock", mode="control")
    assert environment["RP1_FACTORY_TRAJECTORY_MAX_BYTES"] == "1073741824"
    assert environment["RP1_FACTORY_TRAJECTORY_UPLOAD_TIMEOUT_S"] == "240"
    assert "RP1_FACTORY_UNTRUSTED_SETTING" not in environment
    assert environment["RP1_FACTORY_PLC_CONFIG"] == "config/plc_cabinet.yaml"
