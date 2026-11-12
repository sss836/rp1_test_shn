"""Offline ASGI routes with synthetic signing keys, motors and PLC."""

import asyncio
import base64
import json
import subprocess
from unittest.mock import patch

import pytest

from factory_hmi.gateway import motor_maintenance
from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.motor_firmware import MotorFirmwareDownloads
from factory_hmi.motor_ota_policy import V2
from test_lro_108_109_maintenance import expected, seen
from test_module_firmware_gateway import setup_service
from test_module_firmware_maintenance import observation, signed
from test_module_parameters import record
from tools.prepare_local_motor_parameter_catalog import prepare


WINDOW = "paired-local-window"
CREDENTIAL = "synthetic-only-ephemeral-local-token"
WINDOW_HEADER = {"X-RP1-Client-Id": WINDOW}
LOCAL_HEADERS = {**WINDOW_HEADER, "X-RP1-Local-Maintenance": CREDENTIAL}


@pytest.fixture
def offline_app(tmp_path, monkeypatch, signed):
    def no_platform(_downloads):
        raise AssertionError("offline maintenance must not authorize against the platform")

    monkeypatch.setattr(motor_maintenance, "authorize", no_platform)
    monkeypatch.setenv("RP1_HMI_LOCAL_MAINTENANCE_TOKEN", CREDENTIAL)
    monkeypatch.delenv("RP1_FACTORY_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("RP1_HMI_UPDATE_MONITOR_ONLY", raising=False)
    service = setup_service(tmp_path)
    with patch("factory_hmi.gateway.app.PlcCabinetService"):
        app = create_app(service, data_root=tmp_path / "gateway", lease_timeout_s=5)
    app.state.runtime.motor_firmware = signed[0]
    return app


def test_local_status_and_verified_cache_available_before_platform_login(offline_app, signed):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=("127.0.0.1", 4000))
        async with offline_app.router.lifespan_context(offline_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=WINDOW_HEADER) as client:
                for name in ("motor-maintenance", "motor-parameters"):
                    response = await client.get(f"/api/v1/{name}", params={"local": "true"})
                    assert response.status_code == 200, response.text
                    result = response.json()["result"]
                    assert not result["write_authorized"]
                    assert [row["motor_id"] for row in result["targets"]] == [1, 2]
                cache_url = "/api/v1/motor-firmware/0/cached-releases"
                response = await client.get(cache_url)
                assert response.status_code == 200, response.text
                assert response.json()["result"]["releases"] == [{"manifest": signed[1]}]
                (signed[-1] / signed[1]["filename"]).write_bytes(b"x" * len(signed[2]))
                response = await client.get(cache_url)
                assert response.status_code == 200, response.text
                assert response.json()["result"]["releases"] == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("peer", "headers"),
    [("127.0.0.1", WINDOW_HEADER),
     ("127.0.0.1", {**WINDOW_HEADER, "X-RP1-Local-Maintenance": "wrong"}),
     ("127.0.0.1", {"X-RP1-Local-Maintenance": CREDENTIAL}),
     ("192.0.2.1", LOCAL_HEADERS)],
)
def test_local_mutations_and_imports_reject_missing_or_untrusted_credentials(offline_app, peer, headers):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=(peer, 4000))
        async with offline_app.router.lifespan_context(offline_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=headers) as client:
                for path in ("motor-maintenance/preview?local=true", "motor-parameters/read?local=true",
                             "motor-parameters/import", "motor-firmware/0/import"):
                    response = await client.post(f"/api/v1/{path}", json={})
                    assert response.status_code == 403, (path, response.text)

    asyncio.run(run())


def test_approved_local_window_needs_active_lease_for_preview(offline_app, signed):
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=("127.0.0.1", 4000))
        async with offline_app.router.lifespan_context(offline_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=LOCAL_HEADERS) as client:
                path = "/api/v1/motor-maintenance/preview?local=true"
                selections = [{"index": 0, "release_id": signed[1]["release_id"]}]
                response = await client.post(path, json={"selections": selections})
                assert response.status_code == 409, response.text
                assert "no active lease" in response.json()["detail"]
                runtime = offline_app.state.runtime.session(WINDOW)
                runtime.lease.claim(WINDOW)
                status = await client.get("/api/v1/motor-maintenance?local=true")
                assert status.json()["result"]["write_authorized"] is True
                response = await client.post(path, json={"selections": selections})
                assert response.status_code == 200, response.text
                preview = response.json()["result"]
                assert preview["local"] is True
                assert preview["targets"][0]["manifest"] == signed[1]
                assert not runtime.controller._maintenance.active
                rejected = await client.post("/api/v1/motor-maintenance/start",
                                             json={"preview_id": preview["preview_id"], "confirmed": True})
                assert rejected.status_code == 409, rejected.text
                assert not runtime.controller._maintenance.active

    asyncio.run(run())


def test_parameter_catalog_import_and_query_uses_local_signature(offline_app, tmp_path):
    import httpx

    approved = record()
    source = tmp_path / "signed-parameter-catalog.json"
    prepare([approved], tmp_path / "private.pem", source)

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=("127.0.0.1", 4000))
        async with offline_app.router.lifespan_context(offline_app):
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=LOCAL_HEADERS) as client:
                path = "/api/v1/motor-parameters/0/templates?local=true"
                empty = await client.get(path)
                assert empty.status_code == 200, empty.text
                assert empty.json()["result"]["templates"] == []
                installed = await client.post("/api/v1/motor-parameters/import", json={"manifest_path": str(source)})
                assert installed.status_code == 200, installed.text
                assert installed.json()["result"] == {"installed": True, "template_count": 1}
                source.unlink()
                loaded = await client.get(path)
                assert loaded.status_code == 200, loaded.text
                assert loaded.json()["result"]["templates"] == [approved]

    asyncio.run(run())


def test_local_parameters_write_only_confirmed_selection_and_independently_verify(offline_app, tmp_path, monkeypatch):
    import httpx
    from factory_hmi.core import motor_ota_transport, motor_parameter_transport

    approved = record()
    source = tmp_path / "signed-parameter-catalog.json"
    prepare([approved], tmp_path / "private.pem", source)
    values = {(index, key): [100] if key == "kt" else [0, 400]
              for index in (0, 1) for key in ("kt", "kp")}
    writes = []

    def read_parameter(row, item, _stop):
        return {"status": "read", "raw": list(values[row["index"], item["key"]])}

    def write_parameter(row, item, _stop):
        target = (row["index"], item["key"])
        writes.append(target)
        values[target] = list(item["expected_raw"])
        return {"synthetic": True}

    monkeypatch.setattr(motor_parameter_transport, "read_parameter", read_parameter)
    monkeypatch.setattr(motor_parameter_transport, "write_parameter", write_parameter)
    monkeypatch.setattr(motor_ota_transport, "read_version", lambda row, _stop: observation(row["index"]))

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=("127.0.0.1", 4000))
        async with offline_app.router.lifespan_context(offline_app):
            runtime = offline_app.state.runtime.session(WINDOW)
            runtime.lease.claim(WINDOW)
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=LOCAL_HEADERS) as client:
                async def action(name, **body):
                    response = await client.post(f"/api/v1/motor-parameters/{name}?local=true", json=body)
                    assert response.status_code == 200, response.text
                    return response.json()["result"]

                async def wait_for(status):
                    while True:
                        response = await client.get("/api/v1/motor-parameters?local=true")
                        assert response.status_code == 200, response.text
                        job = response.json()["result"]["job"]
                        if job["status"] == status and (status == "awaiting_power" or not job["active"]):
                            return job
                        await asyncio.sleep(0.005)

                await action("import", manifest_path=str(source))
                selections = [{"index": index, "template_id": approved["template"]["template_id"]} for index in (0, 1)]
                await action("read", selections=selections)
                checked = await asyncio.wait_for(wait_for("checked_with_differences"), 3)
                assert not writes
                preview = await action("preview", read_job_id=checked["job_id"], selections=[selections[1]])
                assert [row["index"] for row in preview["targets"]] == [1]
                await action("start", preview_id=preview["preview_id"], confirmed=True)
                awaiting = await asyncio.wait_for(wait_for("awaiting_power"), 3)
                assert writes == [(1, "kt"), (1, "kp")]
                assert not awaiting["targets"][0]["post_power_verified"]
                await action("acknowledge", job_id=awaiting["job_id"], confirmed=True)
                await action("readback", job_id=awaiting["job_id"])
                verified = await asyncio.wait_for(wait_for("verified"), 3)
                assert verified["targets"][0]["post_power_verified"]

    asyncio.run(run())


@pytest.mark.parametrize("source_kind", ["raw", "numeric", "numeric_changed"])
def test_local_firmware_import_and_108_to_109_run_without_platform(offline_app, signed, tmp_path, monkeypatch, source_kind):
    import httpx
    from factory_hmi.core import motor_ota_transport
    from factory_hmi.motor_ota_policy import NUMERIC_LEGACY_108, V3

    _, manifest, content, policy, save_policy, _ = signed
    source, destination = seen(0, 108), seen(0, 109)
    numeric_only = source_kind != "raw"
    if numeric_only:
        source.update(raw_firmware=None, software="0.1.8", numeric_software="0.1.8",
                      hardware="0.1.0", protocol="0.0.1.17",
                      raw_responses={"0x1e": bytes((0xa0, 0x1e, 0, 1, 8, 0, 1, 0, 0, 0, 1, 17)).hex()},
                      errors={"0x31": "版本查询超时"}, confirmed_firmware_generation=108,
                      firmware_confirmation_source="local_numeric_evidence")
        destination["hardware"] = source["hardware"]
    manifest.update(version=destination["raw_firmware"], software_version=destination["software"],
                    from_versions=[source["raw_firmware"] or source["software"]],
                    hardware_versions=[source["hardware"]], protocol_versions=[source["protocol"]])
    policy.update(schema=V3 if numeric_only else V2, preferred_109_sha256=manifest["sha256"])
    policy["entries"][0].update(raw_version=source["raw_firmware"], software_version=source["software"],
                                hardware=source["hardware"], protocol=source["protocol"],
                                target=dict(expected(destination), firmware_sha256=manifest["sha256"]))
    if numeric_only:
        policy["entries"][0].pop("raw_version")
        policy["entries"][0]["source_identity"] = NUMERIC_LEGACY_108
    save_policy()
    approved_policy = tmp_path / "policy.json"
    monkeypatch.delenv("RP1_MOTOR_OTA_POLICY")
    monkeypatch.setenv("RP1_MOTOR_FIRMWARE_PUBLIC_KEY", str(tmp_path / "public.pem"))
    monkeypatch.setenv("RP1_HMI_STATE_ROOT", str(tmp_path / "state"))
    (tmp_path / "state" / "config").mkdir(parents=True, mode=0o700)

    imports = tmp_path / "import"
    imports.mkdir()
    (imports / manifest["filename"]).write_bytes(content)
    (imports / "motor-ota-policy.json").write_bytes(approved_policy.read_bytes())
    canonical_manifest = tmp_path / "to-sign.json"
    canonical_manifest.write_bytes(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    signature = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(tmp_path / "private.pem"),
                                "-rawin", "-in", str(canonical_manifest)], capture_output=True, check=True).stdout
    release = imports / "release.json"
    release.write_text(json.dumps({"manifest": manifest, "signature": base64.b64encode(signature).decode()}))

    hub = offline_app.state.runtime
    hub.motor_firmware = MotorFirmwareDownloads(None, tmp_path / "firmware-storage")
    runtime = hub.session(WINDOW)
    backend = runtime.controller.controller.backend
    monkeypatch.setattr(backend, "motor_versions", lambda: [source, seen(1, 109)])
    writes = []

    def write(row, data, _stop, progress):
        writes.append((row["index"], data))
        progress(len(data), len(data))
        return {"selfcheck_response": "ff0201"}

    monkeypatch.setattr(motor_ota_transport, "write_firmware", write)
    def read(row, _stop):
        if row["index"] == 1:
            return seen(1, 109)
        if writes:
            return dict(destination)
        fresh = {**source, "observed_at": seen(0, 108)["observed_at"]}
        if numeric_only:
            fresh.pop("confirmed_firmware_generation")
            fresh.pop("firmware_confirmation_source")
            fresh["status"] = "unknown"
        if source_kind == "numeric_changed":
            fresh["raw_responses"] = {"0x1e": bytes((0xa0, 0x1e, 0, 1, 8, 0, 1, 0, 0, 0, 1, 18)).hex()}
            fresh["protocol"] = "0.0.1.18"
        return fresh

    monkeypatch.setattr(motor_ota_transport, "read_version", read)

    async def run():
        transport = httpx.ASGITransport(app=offline_app, client=("127.0.0.1", 4000))
        async with offline_app.router.lifespan_context(offline_app):
            runtime.lease.claim(WINDOW)
            async with httpx.AsyncClient(transport=transport, base_url="http://local", headers=LOCAL_HEADERS) as client:
                async def action(name, **body):
                    response = await client.post(f"/api/v1/motor-maintenance/{name}?local=true", json=body)
                    assert response.status_code == 200, response.text
                    return response.json()["result"]

                async def wait_for(status):
                    while True:
                        response = await client.get("/api/v1/motor-maintenance?local=true")
                        assert response.status_code == 200, response.text
                        result = response.json()["result"]
                        if result["job"]["status"] == status:
                            return result["job"]
                        await asyncio.sleep(0.005)

                imported = await client.post("/api/v1/motor-firmware/0/import", json={"manifest_path": str(release)})
                assert imported.status_code == 200, imported.text
                assert imported.json()["result"]["policy_imported"] is True
                response = await client.get("/api/v1/motor-maintenance?local=true")
                result = response.json()["result"]
                assert result["targets"][0]["upgrade_109_releases"] == [manifest]
                if numeric_only:
                    assert result["targets"][0]["confirmed_firmware_generation"] == 108
                    assert result["targets"][0]["firmware_generation"] is None
                    assert result["targets"][0]["observation"]["raw_firmware"] is None
                assert result["targets"][1]["firmware_generation"] == 109
                preview = await action("preview", mode="module_108_to_109",
                                       configuration_sha256=result["configuration_sha256"],
                                       selections=[{"index": 0, "release_id": manifest["release_id"]}])
                assert not writes
                await action("start", preview_id=preview["preview_id"], confirmed=True)
                if source_kind == "numeric_changed":
                    rejected = await asyncio.wait_for(wait_for("version_unknown"), 3)
                    assert not writes and "回读与核实记录不符" in rejected["error"]
                    return
                awaiting = await asyncio.wait_for(wait_for("awaiting_power"), 3)
                assert writes == [(0, content)]
                await action("acknowledge", job_id=awaiting["job_id"], confirmed=True)
                await action("readback", job_id=awaiting["job_id"])
                verified = await asyncio.wait_for(wait_for("verified"), 3)
                assert verified["targets"][0]["actual_version_verified"] is True
                assert verified["skipped_targets"][0]["index"] == 1

    asyncio.run(run())
