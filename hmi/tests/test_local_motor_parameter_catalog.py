"""The offline catalog trusts signed files, never an API-supplied value."""

from copy import deepcopy
import json
import os
import subprocess

import pytest

from factory_hmi.gateway.motor_parameters import LocalTemplates
from factory_hmi.motor_parameter_catalog import KIND, catalog_body
from factory_hmi.motor_parameter_contract import SCHEMA_V1, SOURCE_SHA256, sha
from tools.prepare_local_motor_parameter_catalog import prepare
from test_module_parameters import local_numeric_observation, numeric_legacy_template, record as published_record


OBSERVATION = {"status": "known", "model": "LRO_5550", "raw_firmware": "5.0.0.108",
               "software": "0.1.8", "hardware": "0.1.0", "protocol": "0.0.1.17"}


def record():
    template = {
        "schema": SCHEMA_V1, "template_id": "a" * 32, "version": "synthetic-1", "model": "LRO_5550",
        "source_sha256": SOURCE_SHA256, "source": "synthetic test fixture",
        "applicability": [{key: OBSERVATION[key] for key in ("raw_firmware", "software", "hardware", "protocol")}],
        "entries": [{"key": "kt", "expected_raw": [174], "tolerance_raw": 1}],
        "confirmation": {"evidence_sha256": "c" * 64, "notes": "synthetic fixture only"},
    }
    return {"template": template, "sha256": sha(template), "published_by": "test-admin",
            "published_at": "2026-11-12T10:00:00+00:00", "request_id": "synthetic"}


def signing_key(root):
    private_key = root / "private.pem"
    public_key = root / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(private_key)], check=True)
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public_key)], check=True)
    public_key.chmod(0o644)
    return private_key, public_key


@pytest.fixture
def catalog(tmp_path):
    private_key, public_key = signing_key(tmp_path)

    class OfflineDownloads:
        def key(self):
            return public_key

    return LocalTemplates(OfflineDownloads(), root=tmp_path / "catalog"), private_key


def test_import_and_select_exact_applicability_without_platform(catalog, tmp_path):
    local, private_key = catalog
    assert local.query(OBSERVATION) == []
    source = tmp_path / "release.json"
    expected = record()
    prepare([expected], private_key, source)
    assert local.install(str(source)) == {"installed": True, "template_count": 1}
    source.unlink()
    assert local.query(OBSERVATION) == [expected]
    assert local.get("a" * 32, OBSERVATION) == expected
    assert local.query({**OBSERVATION, "raw_firmware": None}) == []
    with pytest.raises(ValueError, match="没有适用于"):
        local.get("a" * 32, {**OBSERVATION, "protocol": "0.0.1.18"})
    assert local.path.stat().st_mode & 0o077 == 0


def test_signed_numeric_108_catalog_matches_actual_device_identity(catalog, tmp_path):
    local, private_key = catalog
    source = tmp_path / "legacy-108.json"
    expected = published_record(numeric_legacy_template())
    prepare([expected], private_key, source)
    local.install(str(source))
    observation = local_numeric_observation()
    assert local.query(observation)[0]['execution_source']=='bundled-production-script'
    assert local.query(observation)[1:] == [expected]
    assert observation["raw_firmware"] is None
    assert local.query({**observation, "confirmed_firmware_generation": None})[1:] == [expected]
    assert local.query({**observation, "raw_responses": {}}) == []
    assert local.query({**observation, "hardware": "0.1.1"}) == []


def test_authentication_domain_and_signature_are_rechecked_on_every_read(catalog, tmp_path):
    local, private_key = catalog
    source = tmp_path / "release.json"
    envelope = prepare([record()], private_key, source)
    local.install(str(source))
    bad = deepcopy(envelope)
    bad["kind"] = "another.release.type"
    source.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="类型无效"):
        local.install(str(source))
    assert local.get("a" * 32, OBSERVATION)["template"]["entries"][0]["expected_raw"] == [174]
    assert envelope["kind"] == KIND
    changed = deepcopy(envelope)
    changed["templates"][0]["template"]["entries"][0]["expected_raw"] = [180]
    changed["templates"][0]["sha256"] = sha(changed["templates"][0]["template"])
    local.path.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="未通过可信签名"):
        local.get("a" * 32, OBSERVATION)


def test_wrong_key_and_immutable_id_cannot_replace_installed_template(catalog, tmp_path):
    local, private_key = catalog
    source = tmp_path / "release.json"
    prepare([record()], private_key, source)
    local.install(str(source))
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_key, _ = signing_key(foreign_root)
    foreign_path = foreign_root / "release.json"
    prepare([record()], foreign_key, foreign_path)
    with pytest.raises(ValueError, match="未通过可信签名"):
        local.install(str(foreign_path))
    changed = record()
    changed["template"]["entries"][0]["expected_raw"] = [180]
    changed["sha256"] = sha(changed["template"])
    new_source = tmp_path / "new-release.json"
    prepare([changed], private_key, new_source)
    with pytest.raises(ValueError, match="不可覆盖"):
        local.install(str(new_source))
    assert local.query(OBSERVATION) == [record()]


def test_symlinks_special_files_and_unsigned_values_are_rejected(catalog, tmp_path):
    local, private_key = catalog
    source = tmp_path / "release.json"
    prepare([record()], private_key, source)
    link = tmp_path / "catalog-link.json"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="符号链接"):
        local.install(str(link))
    fifo = tmp_path / "catalog-fifo.json"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="普通文件"):
        local.install(str(fifo))
    unsigned = tmp_path / "unsigned.json"
    unsigned.write_text(json.dumps(catalog_body([record()])))
    with pytest.raises(ValueError, match="签名封装"):
        local.install(str(unsigned))
    assert local.query(OBSERVATION) == []


def test_signer_rejects_unpublished_or_duplicate_templates():
    unpublished = record()
    unpublished["template"]["confirmation"] = None
    unpublished["sha256"] = sha(unpublished["template"])
    with pytest.raises(ValueError, match="尚未由管理员确认"):
        catalog_body([unpublished])
    with pytest.raises(ValueError, match="重复模板编号"):
        catalog_body([record(), record()])
