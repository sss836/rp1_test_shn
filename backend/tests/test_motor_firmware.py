"""Real signatures + ASGI + HMI cache, disposable files and synthetic actors."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
from uuid import uuid4
import pytest

from test_hmi_updates import materials, actor, call, app_for, run
from app.api.routes import motor_firmware as routes
from app.core.errors import ApiError
from app.schemas.motor_firmware import SignedMotorFirmware
from app.services.motor_firmware import MotorFirmwareStorage
from factory_hmi.motor_firmware import stage_firmware, verify_firmware

BASE = "/api/v1/hmi/motor-firmware"
TARGET = dict(model="LRO_5550", hardware="1.0.2", protocol="1.0.0.0", current_version="1.0.8")
QUERY = "?model=LRO_5550&hardware=1.0.2&protocol=1.0.0.0&current_version=1.0.8"


@pytest.fixture
def firmware(materials):
    root, settings, _, _, _ = materials
    settings = settings.model_copy(update={"motor_firmware_enabled": True,
        "motor_firmware_root": str(root / "motor-store"),
        "motor_firmware_public_key_file": str(root / "test-public.pem")})
    content = b"SYNTHETIC-MOTOR-FIRMWARE-ONLY" * 30
    def sign(**changes):
        m = dict(schema_version="rp1-motor-firmware-v1", artifact_type="motor_firmware", release_id=uuid4().hex,
            version="1.0.10", software_version="1.0.10", vendor="LRO", model="LRO_5550", interface_type="canfd",
            hardware_versions=["1.0.2"], protocol_versions=["1.0.0.0"], from_versions=["1.0.8"],
            filename="fixture.lsepbin", size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
            source_url="https://source.invalid/fixture", scope="仅隔离测试", notes="合成数据不能刷写",
            published_at="2026-10-30T02:00:00+08:00")
        m.update(changes)
        message = root / (uuid4().hex + ".json")
        message.write_bytes(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
        signature = run("openssl", "pkeyutl", "-sign", "-inkey", root / "test-private.pem", "-rawin", "-in", message).stdout
        return dict(manifest=m, signature=base64.b64encode(signature).decode())
    return root, settings, content, sign


def headers(release):
    return {"Content-Type": "application/octet-stream", "X-Motor-Release": base64.b64encode(json.dumps(release).encode()).decode()}


def app(settings, monkeypatch, identity):
    a = app_for(settings, monkeypatch, identity)
    a.include_router(routes.router)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    return a


def publish(fixture, release):
    _, settings, content, _ = fixture
    store = MotorFirmwareStorage(settings)
    stage = store.stage()
    (stage / release["manifest"]["filename"]).write_bytes(content)
    try:
        return store.publish(stage, SignedMotorFirmware.model_validate(release), actor_id="synthetic-admin", request_id="check")
    finally:
        import shutil
        shutil.rmtree(stage, ignore_errors=True)


@pytest.mark.parametrize("role,expected", [("SYSTEM_ADMIN", 201), ("TEST_EXECUTOR", 403), ("VIEWER", 403)])
def test_only_admin_publishes(firmware, monkeypatch, role, expected):
    _, settings, content, sign = firmware
    r = call(app(settings, monkeypatch, actor(role)), "POST", BASE + "/releases", headers=headers(sign()), content=content)
    assert r.status_code == expected, r.text


@pytest.mark.parametrize("role,expected", [("SYSTEM_ADMIN", 200), ("TEST_EXECUTOR", 200), ("VIEWER", 403)])
def test_download_roles_and_compatible_choice(firmware, monkeypatch, role, expected):
    _, settings, content, sign = firmware
    release = sign()
    publish(firmware, release)
    a = app(settings, monkeypatch, actor(role))
    catalog = call(a, "GET", BASE + "/releases" + QUERY)
    package = call(a, "GET", BASE + "/releases/" + release["manifest"]["release_id"] + "/package" + QUERY)
    assert catalog.status_code == package.status_code == expected
    if expected == 200:
        assert catalog.json()["data"]["releases"][0] == release
        assert package.content == content
        assert package.headers["x-artifact-type"] == "motor_firmware"
        assert package.headers["x-content-sha256"] == release["manifest"]["sha256"]
        assert call(a, "GET", BASE + "/releases" + QUERY.replace("1.0.2", "9.9.9")).json()["data"]["releases"] == []
        assert call(a, "GET", BASE + "/releases/" + release["manifest"]["release_id"] + "/package" + QUERY.replace("1.0.2", "9.9.9")).status_code == 409


def test_service_identity_rejected(firmware, monkeypatch):
    _, settings, content, sign = firmware
    a = app(settings, monkeypatch, actor(kind="SERVICE", session=False))
    assert call(a, "GET", BASE + "/releases" + QUERY).status_code == 403
    assert call(a, "POST", BASE + "/releases", headers=headers(sign()), content=content).status_code == 403


def test_numeric_order_and_unknown_compatibility(firmware):
    _, settings, _, sign = firmware
    for version in ("1.0.9", "1.0.10"):
        publish(firmware, sign(version=version, software_version=version))
    store = MotorFirmwareStorage(settings)
    assert [r.manifest.version for r in store.applicable(**TARGET)] == ["1.0.10", "1.0.9"]
    for field in ("model", "hardware", "protocol", "current_version"):
        assert store.applicable(**{**TARGET, field: "unknown"}) == []


def test_signature_hash_type_and_upload_failure(firmware, monkeypatch, materials):
    root, settings, content, sign = firmware
    a = app(settings, monkeypatch, actor())
    release = sign()
    release["manifest"]["source_url"] = "https://tampered.invalid"
    assert call(a, "POST", BASE + "/releases", headers=headers(release), content=content).status_code == 422
    assert call(a, "POST", BASE + "/releases", headers=headers(sign()), content=content[:-1]).status_code == 422
    deb = materials[2].read_bytes()
    disguised = sign(size_bytes=len(deb), sha256=hashlib.sha256(deb).hexdigest())
    assert call(a, "POST", BASE + "/releases", headers=headers(disguised), content=deb).status_code == 422
    assert call(a, "POST", BASE + "/releases", headers=headers(sign(filename="wrong.deb")), content=content).status_code == 422
    assert not list((root / "motor-store/.incoming").glob("upload-*"))
    assert call(a, "POST", BASE + "/releases", headers=headers(sign()), content=content).status_code == 201


def test_publish_idempotency_concurrency_immutable_metadata(firmware):
    _, settings, _, sign = firmware
    release = sign()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(firmware, release), range(2)))
    assert sorted(r["created"] for r in results) == [False, True]
    changed = sign(release_id=release["manifest"]["release_id"], hardware_versions=["9.9.9"])
    with pytest.raises(ApiError) as exc:
        publish(firmware, changed)
    assert exc.value.status_code == 409
    store = MotorFirmwareStorage(settings)
    path = store.releases / release["manifest"]["release_id"] / "fixture.lsepbin"
    path.write_bytes(b"corrupt")
    with pytest.raises(ApiError):
        store.package(release["manifest"]["release_id"])


def test_real_signature_cache_failure_recovery_and_separate_actual_version(firmware):
    root, _, content, sign = firmware
    release = sign()
    kwargs = dict(key=root / "test-public.pem", target={**TARGET, "observed_at": "fixture"}, cache_root=root / "cache")
    with pytest.raises(ValueError):
        stage_firmware(release, **kwargs, open_stream=lambda: io.BytesIO(content[:10]))
    assert not list((root / "cache/releases").glob(".partial-*"))
    first = stage_firmware(release, **kwargs, open_stream=lambda: io.BytesIO(content))
    assert first["status"] == "downloaded" and not first["installed"] and not first["actual_version_verified"]
    assert first["target_observation"]["current_version"] == "1.0.8"
    second = stage_firmware(release, **kwargs, open_stream=lambda: pytest.fail("cache hit opened network"))
    assert second["cache_hit"]
    Path(second["firmware_path"]).write_bytes(b"broken")
    repaired = stage_firmware(release, **kwargs, open_stream=lambda: io.BytesIO(content))
    assert not repaired["cache_hit"] and Path(repaired["firmware_path"]).read_bytes() == content
    assert list((root / "cache/releases").glob(".corrupt-*"))
    with pytest.raises(ValueError):
        verify_firmware(release, kwargs["key"], {**TARGET, "hardware": "unknown"})


def test_contracts_identical_and_storage_separate(firmware):
    from factory_hmi.motor_firmware_contract import SignedMotorFirmware as ClientContract
    assert ClientContract.model_json_schema() == SignedMotorFirmware.model_json_schema()
    _, settings, _, _ = firmware
    with pytest.raises(ApiError):
        MotorFirmwareStorage(settings.model_copy(update={"motor_firmware_root": settings.hmi_updates_root}))


def test_restart_partial_cleanup_and_failed_new_release_preserves_old(firmware):
    root, _, content, sign = firmware
    kwargs = dict(key=root / "test-public.pem", target=TARGET, cache_root=root / "cache")
    first = stage_firmware(sign(), **kwargs, open_stream=lambda: io.BytesIO(content))
    orphan = root / "cache/releases/.partial-crashed"
    orphan.mkdir(); (orphan / "incomplete").write_bytes(b"partial")
    newer = sign(version="1.0.11", software_version="1.0.11")
    with pytest.raises(ValueError):
        stage_firmware(newer, **kwargs, open_stream=lambda: io.BytesIO(b"truncated"))
    assert not orphan.exists()
    assert Path(first["firmware_path"]).read_bytes() == content
    recovered = stage_firmware(newer, **kwargs, open_stream=lambda: io.BytesIO(content))
    assert recovered["manifest"]["version"] == "1.0.11"
    assert Path(first["firmware_path"]).read_bytes() == content


def test_prepare_tool_signs_complete_provenance(firmware):
    import importlib.util
    root, _, content, sign = firmware
    path = Path(__file__).resolve().parents[2] / "hmi/tools/prepare_motor_firmware.py"
    # parents[2] is repository root for backend/tests/file.py.
    spec = importlib.util.spec_from_file_location("prepare_motor_firmware", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    file = root / "vendor.lsepbin"; file.write_bytes(content)
    metadata = {k:v for k,v in sign()["manifest"].items() if k not in {
        "release_id", "schema_version", "artifact_type", "filename", "size_bytes", "sha256", "published_at"}}
    result = module.prepare(file, metadata, root / "test-private.pem", root / "prepared.json")
    manifest = verify_firmware(result, root / "test-public.pem", TARGET)
    assert manifest["filename"] == "vendor.lsepbin" and manifest["source_url"] == metadata["source_url"]
