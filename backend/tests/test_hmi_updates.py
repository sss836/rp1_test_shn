"""Isolated real-signature/DEB and ASGI checks; no live DB, service or devices."""
from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import pytest

from app.api.routes import hmi_updates as routes
from app.core import auth as auth_module
from app.core.auth import RequestActor, get_request_actor
from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas.hmi_updates import SignedRelease
from app.services.hmi_updates import HmiUpdateStorage, canonical_manifest, parse_release


BASE = "/api/v1/hmi/updates"
QUERY = "?os_id=ubuntu&os_version=24.04&architecture=amd64&current_version=2.0.13"


def run(*args):
    return subprocess.run(list(map(str, args)), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, timeout=15)


@pytest.fixture
def materials():
    # These are disposable test-only keys; never provision production trust.
    with tempfile.TemporaryDirectory(prefix="rp1-ota-check-") as directory:
        root = Path(directory)
        key = root / "test-private.pem"
        public = root / "test-public.pem"
        run("openssl", "genpkey", "-algorithm", "ED25519", "-out", key)
        run("openssl", "pkey", "-in", key, "-pubout", "-out", public)
        public.chmod(0o644)
        settings = Settings(_env_file=None, hmi_updates_enabled=True,
                            hmi_updates_root=str(root / "store"),
                            hmi_updates_public_key_file=str(public),
                            archive_root=str(root / "measurement-archives"))

        def build(version="2.0.14-1", architecture="amd64", payload="fixture"):
            package_root = root / uuid4().hex
            (package_root / "DEBIAN").mkdir(parents=True)
            (package_root / "DEBIAN" / "control").write_text(
                f"Package: rp1-test-hmi\nVersion: {version}\nArchitecture: {architecture}\n"
                "Maintainer: RP1 Isolated Test <test@example.invalid>\nDescription: Disposable OTA fixture\n")
            (package_root / "payload.txt").write_text(payload)
            package = root / (uuid4().hex + ".deb")
            run("dpkg-deb", "--root-owner-group", "--build", package_root, package)
            return package

        def sign(package, **changes):
            manifest = {"schema_version": "rp1-hmi-release-v1", "release_id": uuid4().hex,
                        "version": "2.0.14-1", "os_id": "ubuntu", "os_version": "24.04",
                        "architecture": "amd64", "package_name": "rp1-test-hmi",
                        "filename": "rp1-test-hmi_2.0.14-1_amd64.deb", "size_bytes": package.stat().st_size,
                        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                        "notes": "隔离软件检查：中文说明", "published_at": "2026-10-28T12:00:00+08:00"}
            manifest.update(changes)
            message = root / "sign-input.json"
            message.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
            signature = run("openssl", "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", message).stdout
            return {"manifest": manifest, "signature": base64.b64encode(signature).decode()}

        package = build()
        yield root, settings, package, build, sign


def actor(role="SYSTEM_ADMIN", *, kind="HUMAN", session=True):
    return RequestActor(public_id=uuid4(), request_id="isolated-ota", change_reason="",
                        source_ip="127.0.0.1", username="fixture", display_name="Fixture", role=role,
                        principal_kind=kind, session_public_id=uuid4() if session else None,
                        session_type="BROWSER" if session else "DEVELOPMENT", expires_at=None,
                        must_change_password=False)


def app_for(settings, monkeypatch, identity=None):
    app = FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    if identity is not None:
        app.dependency_overrides[get_request_actor] = lambda: identity

    @app.exception_handler(ApiError)
    async def error(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code,
                            content={"error": {"code": exc.code, "message": exc.message}})
    return app


async def call_many(app, calls):
    async def pulse():
        while True:
            await asyncio.sleep(.01)
    timer = asyncio.create_task(pulse())
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
            return [await asyncio.wait_for(client.request(method, path, **kwargs), 10)
                    for method, path, kwargs in calls]
    finally:
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass


def call(app, method, path, **kwargs):
    return asyncio.run(call_many(app, [(method, path, kwargs)]))[0]


def upload_headers(release):
    return {"Content-Type": "application/octet-stream", "X-HMI-Release":
            base64.b64encode(json.dumps(release, ensure_ascii=False).encode()).decode()}


def stage_publish(storage, package, release):
    stage = storage.stage()
    signed = SignedRelease.model_validate(release)
    shutil.copyfile(package, stage / signed.manifest.filename)
    try:
        return storage.publish(stage, signed, actor_id="test-actor", request_id="test-request")
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def test_real_signed_publish_query_download_and_idempotency(materials, monkeypatch):
    root, settings, package, _, sign = materials
    release = sign(package)
    app = app_for(settings, monkeypatch, actor())
    publish, latest, download, retry = asyncio.run(call_many(app, [
        ("POST", BASE + "/releases", {"content": package.read_bytes(), "headers": upload_headers(release)}),
        ("GET", BASE + "/latest" + QUERY, {}),
        ("GET", BASE + "/releases/" + release["manifest"]["release_id"] + "/package", {}),
        ("POST", BASE + "/releases", {"content": package.read_bytes(), "headers": upload_headers(release)}),
    ]))
    assert publish.status_code == 201, publish.text
    assert publish.json()["data"]["created"] is True
    assert latest.json()["data"] == {"available": True, "can_update": True,
                                       "current_version": "2.0.13", "release": release}
    assert download.status_code == 200 and download.content == package.read_bytes()
    assert download.headers["cache-control"] == "no-store"
    assert retry.status_code == 200 and retry.json()["data"]["created"] is False
    stored = root / "store" / "releases" / release["manifest"]["release_id"]
    assert json.loads((stored / "publication.json").read_text())["request_id"] == "isolated-ota"
    assert list((root / "store" / ".incoming").iterdir()) == []
    # Exact same package retry returns the first immutable envelope.
    result = stage_publish(HmiUpdateStorage(settings), package, sign(package, notes="retry"))
    assert result == {"release": release, "created": False}


def test_container_layout_accepts_external_store_and_rejects_application_tree(materials, monkeypatch):
    from app.services import hmi_updates as service_module
    root, settings, _, _, _ = materials
    monkeypatch.setattr(service_module, "__file__", "/app/app/services/hmi_updates.py")
    storage = HmiUpdateStorage(settings)
    assert storage.root == root / "store"
    for invalid in ("/app/ota", "/app/app/ota", "/", str(root / "measurement-archives/ota")):
        with pytest.raises(ApiError) as error:
            HmiUpdateStorage(settings.model_copy(update={"hmi_updates_root": invalid}))
        assert error.value.status_code == 503


@pytest.mark.parametrize("role,can_update,download_status,publish_status", [
    ("VIEWER", False, 403, 403), ("TEST_EXECUTOR", True, 200, 403),
    ("SYSTEM_ADMIN", True, 200, 201),
])
def test_server_role_gates(materials, monkeypatch, role, can_update, download_status, publish_status):
    _, settings, package, build, sign = materials
    release = sign(package)
    stage_publish(HmiUpdateStorage(settings), package, release)
    next_package = build(version="2.0.15")
    next_release = sign(next_package, version="2.0.15")
    app = app_for(settings, monkeypatch, actor(role))
    latest = call(app, "GET", BASE + "/latest" + QUERY)
    assert latest.json()["data"]["can_update"] is can_update
    assert call(app, "GET", BASE + "/releases/" + release["manifest"]["release_id"] + "/package").status_code == download_status
    assert call(app, "POST", BASE + "/releases", content=next_package.read_bytes(),
                headers=upload_headers(next_release)).status_code == publish_status


@pytest.mark.parametrize("identity", [actor(kind="SERVICE"), actor(session=False)])
def test_human_real_session_required(materials, monkeypatch, identity):
    _, settings, package, _, sign = materials
    app = app_for(settings, monkeypatch, identity)
    response = call(app, "GET", BASE + "/latest" + QUERY, headers={"X-Role": "SYSTEM_ADMIN"})
    assert response.status_code == 403
    response = call(app, "POST", BASE + "/releases", content=package.read_bytes(), headers=upload_headers(sign(package)))
    assert response.status_code == 403


def test_no_login_and_bearer_rejected_before_storage(materials, monkeypatch):
    _, settings, _, _, _ = materials
    app = app_for(settings, monkeypatch)
    assert call(app, "GET", BASE + "/latest" + QUERY).status_code == 401
    response = call(app, "GET", BASE + "/latest" + QUERY, headers={"Authorization": "Bearer service"})
    assert response.status_code == 403 and response.json()["error"]["code"] == "bearer_not_allowed"


def test_real_session_dependency_enforces_csrf_without_database(materials, monkeypatch):
    from app.core import db as db_module
    from app.schemas.security import Principal
    from app.services.auth import AuthService
    _, settings, package, _, sign = materials
    principal = Principal(id=uuid4(), username="admin", display_name="Admin", role="SYSTEM_ADMIN", kind="HUMAN",
                          session_id=uuid4(), session_type="BROWSER", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                          csrf_token_hash=AuthService.secret_hash("test-csrf"))

    class FakeSession:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def commit(self):
            pass

    monkeypatch.setattr(db_module, "SessionLocal", FakeSession)
    monkeypatch.setattr(AuthService, "resolve", lambda self, token: principal if token == "session" else None)
    app = app_for(settings, monkeypatch)
    release = sign(package)
    headers = upload_headers(release)
    headers["Cookie"] = "rp1_session=session; rp1_csrf=test-csrf"
    denied = call(app, "POST", BASE + "/releases", content=package.read_bytes(), headers=headers)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "csrf_invalid"
    headers["X-CSRF-Token"] = "test-csrf"
    accepted = call(app, "POST", BASE + "/releases", content=package.read_bytes(), headers=headers)
    assert accepted.status_code == 201, accepted.text
    principal.must_change_password = True
    assert call(app, "GET", BASE + "/latest" + QUERY, headers={"Cookie": "rp1_session=session"}).json()["error"]["code"] == "password_change_required"


@pytest.mark.parametrize("changes", [{"hmi_updates_enabled": False}, {"hmi_updates_root": ""},
                                     {"hmi_updates_public_key_file": ""}, {"hmi_updates_root": "relative"},
                                     {"hmi_updates_root": str(Path(__file__).resolve().parents[2] / "ota") }])
def test_missing_or_unsafe_configuration_fails_closed(materials, changes):
    _, settings, _, _, _ = materials
    with pytest.raises(ApiError) as error:
        HmiUpdateStorage(settings.model_copy(update=changes))
    assert error.value.status_code == 503


def test_wrong_key_and_archive_overlap_fail_closed(materials):
    root, settings, _, _, _ = materials
    with pytest.raises(ApiError):
        HmiUpdateStorage(settings.model_copy(update={"hmi_updates_root": settings.archive_root + "/software"}))
    rsa = root / "rsa.pem"
    run("openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:1024", "-out", root / "rsa-private.pem")
    run("openssl", "pkey", "-in", root / "rsa-private.pem", "-pubout", "-out", rsa)
    with pytest.raises(ApiError):
        HmiUpdateStorage(settings.model_copy(update={"hmi_updates_public_key_file": str(rsa)}))
    Path(settings.hmi_updates_public_key_file).chmod(0o666)
    with pytest.raises(ApiError):
        HmiUpdateStorage(settings)


@pytest.mark.parametrize("change", [{"filename": "../escape.deb"}, {"release_id": "A" * 32},
                                   {"extra": "unsupported"}, {"published_at": "2026-10-28"},
                                   {"size_bytes": True}, {"package_name": "another-package"}])
def test_exact_manifest_schema_rejects_unsafe_fields(materials, change):
    _, _, package, _, sign = materials
    with pytest.raises(ApiError):
        parse_release(json.dumps(sign(package, **change)).encode())


def test_duplicate_json_key_rejected():
    with pytest.raises(ApiError):
        parse_release(b'{"manifest":{},"manifest":{},"signature":""}')


def test_invalid_signature_and_hash_never_publish(materials, monkeypatch):
    root, settings, package, _, sign = materials
    app = app_for(settings, monkeypatch, actor())
    release = sign(package)
    release["manifest"]["notes"] = "tampered"
    response = call(app, "POST", BASE + "/releases", content=package.read_bytes(), headers=upload_headers(release))
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_hmi_signature"
    wrong_hash = sign(package, sha256="0" * 64)
    response = call(app, "POST", BASE + "/releases", content=package.read_bytes(), headers=upload_headers(wrong_hash))
    assert response.status_code == 422 and response.json()["error"]["code"] == "hmi_package_hash_mismatch"
    assert not list((root / "store" / "releases").iterdir())
    assert not list((root / "store" / ".incoming").iterdir())


@pytest.mark.parametrize("changes", [{"version": "2.0.99"}, {"architecture": "arm64"}])
def test_signed_manifest_must_match_deb_metadata(materials, changes):
    _, settings, package, _, sign = materials
    with pytest.raises(ApiError) as error:
        stage_publish(HmiUpdateStorage(settings), package, sign(package, **changes))
    assert error.value.code == "hmi_package_metadata_mismatch"


def test_chunked_limit_short_upload_and_interruption_cleanup(materials, monkeypatch):
    root, settings, package, _, sign = materials
    app = app_for(settings, monkeypatch, actor())
    release = sign(package)

    async def oversized():
        yield package.read_bytes()
        yield b"excess"

    async def incomplete():
        yield b"partial"

    async def broken():
        yield b"partial"
        raise RuntimeError("injected transport interruption")

    response = call(app, "POST", BASE + "/releases", content=oversized(), headers=upload_headers(release))
    assert response.status_code == 413
    response = call(app, "POST", BASE + "/releases", content=incomplete(), headers=upload_headers(release))
    assert response.status_code == 422
    with pytest.raises(RuntimeError, match="injected"):
        call(app, "POST", BASE + "/releases", content=broken(), headers=upload_headers(release))
    assert list((root / "store" / ".incoming").iterdir()) == []
    assert list((root / "store" / "releases").iterdir()) == []


def test_exact_target_debian_order_and_no_downgrade(materials):
    _, settings, package, build, sign = materials
    storage = HmiUpdateStorage(settings)
    stage_publish(storage, package, sign(package))
    pre = build(version="2.0.15~rc1")
    stage_publish(storage, pre, sign(pre, version="2.0.15~rc1"))
    other = build(version="9.0", architecture="arm64")
    stage_publish(storage, other, sign(other, version="9.0", architecture="arm64"))
    assert storage.latest("ubuntu", "24.04", "amd64", "2.0.13").manifest.version == "2.0.15~rc1"
    assert storage.latest("ubuntu", "24.04", "amd64", "2.0.15") is None
    assert storage.latest("ubuntu", "22.04", "amd64", "2.0.13") is None
    assert storage.latest("debian", "24.04", "amd64", "2.0.13") is None
    with pytest.raises(ApiError):
        storage.latest("ubuntu", "24.04", "amd64", "2.0;touch /tmp/injection")


def test_conflicting_content_serialized_and_same_content_concurrent_retry(materials):
    _, settings, package, build, sign = materials
    storage = HmiUpdateStorage(settings)
    release = sign(package)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: stage_publish(storage, package, release), range(2)))
    assert sorted(item["created"] for item in results) == [False, True]
    changed = build(payload="different immutable package")
    with pytest.raises(ApiError) as error:
        stage_publish(storage, changed, sign(changed))
    assert error.value.status_code == 409
    assert len(list(storage.releases.iterdir())) == 1


def test_corrupt_or_symlink_package_and_manifest_refused(materials):
    root, settings, package, _, sign = materials
    storage = HmiUpdateStorage(settings)
    release = sign(package)
    stage_publish(storage, package, release)
    release_dir = storage.releases / release["manifest"]["release_id"]
    stored_package = release_dir / release["manifest"]["filename"]
    stored_package.write_bytes(b"corrupt")
    with pytest.raises(ApiError) as error:
        storage.package(release["manifest"]["release_id"])
    assert error.value.code == "hmi_release_corrupt"
    stored_package.unlink()
    stored_package.symlink_to(package)
    with pytest.raises(ApiError):
        storage.package(release["manifest"]["release_id"])
    manifest = release_dir / "release.json"
    tampered = json.loads(manifest.read_text())
    tampered["manifest"]["version"] = "9999.0"
    manifest.write_text(json.dumps(tampered))
    with pytest.raises(ApiError) as error:
        storage.latest("ubuntu", "24.04", "amd64", "2.0.13")
    assert error.value.code == "hmi_release_corrupt"


def test_missing_release_and_oversized_manifest_or_package(materials, monkeypatch):
    _, settings, package, _, sign = materials
    app = app_for(settings, monkeypatch, actor())
    assert call(app, "GET", BASE + "/releases/" + "a" * 32 + "/package").status_code == 404
    assert call(app, "GET", BASE + "/releases/not-an-id/package").status_code == 404
    response = call(app, "POST", BASE + "/releases", content=b"", headers={
        "Content-Type": "application/octet-stream", "X-HMI-Release": "a" * 100000})
    assert response.status_code == 413
    storage = HmiUpdateStorage(settings)
    with pytest.raises(ApiError) as error:
        storage.from_header(upload_headers(sign(package, size_bytes=settings.hmi_updates_max_package_bytes + 1))["X-HMI-Release"])
    assert error.value.status_code == 413


def test_hmi_updater_and_platform_share_signed_contract(materials, monkeypatch):
    root, settings, package, _, sign = materials
    # Load the stdlib-only updater directly, avoiding factory_hmi's controller
    # package initializer. No GUI, gateway, CAN or PLC object is created.
    source = Path(__file__).resolve().parents[2] / "hmi" / "factory_hmi" / "updates.py"
    spec = importlib.util.spec_from_file_location("_isolated_hmi_updater", source)
    updater = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, updater)
    spec.loader.exec_module(updater)
    release = sign(package)
    host = updater.HostInfo("ubuntu", "24.04", "amd64", "2.0.13")
    assert updater.canonical_manifest(release["manifest"]) == canonical_manifest(SignedRelease.model_validate(release))
    assert updater.verify_release(release, settings.hmi_updates_public_key_file, host) == release["manifest"]
    receipt = updater.stage_release(release, public_key_path=settings.hmi_updates_public_key_file,
                                    cache_dir=root / "hmi-cache", host=host,
                                    package_stream=io.BytesIO(package.read_bytes()))
    assert Path(receipt["package_path"]).read_bytes() == package.read_bytes()
    hmi_release = updater.prepare_release(package, private_key_path=root / "test-private.pem",
                                          os_id="ubuntu", os_version="24.04", output=root / "hmi-signed.json")
    decoded = HmiUpdateStorage(settings).from_header(upload_headers(hmi_release)["X-HMI-Release"])
    assert decoded.model_dump() == hmi_release
