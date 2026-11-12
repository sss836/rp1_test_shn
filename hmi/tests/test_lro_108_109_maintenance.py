"""Version-scoped maintenance tests; every device callback is simulated."""
import base64
from contextlib import nullcontext
from copy import deepcopy
import json
import struct
import subprocess
import threading
from unittest.mock import Mock

import pytest

from factory_hmi.core.motor_maintenance import (
    MODULE_109, SELECTED_109, ModuleFirmwareMaintenance, digest,
)
from factory_hmi.core.motor_parameters import ModuleParameterMaintenance
from factory_hmi.core import motor_ota_transport
from factory_hmi.core.motor_versions import firmware_generation
from factory_hmi.gateway.motor_maintenance import artifact, cached_109_releases, cached_releases
from factory_hmi.motor_ota_policy import V2, validate_policy

from test_module_firmware_maintenance import observation, signed, target, wait


def seen(index, generation, *, protocol=None):
    row = observation(index)
    row.update(raw_firmware=f"5.0.0.{generation}", software="5.0.0",
               protocol=protocol or ("1.0.0.0" if generation == 108 else "1.0.3.0"))
    return row


def expected(dest):
    return dict(hardware=dest["hardware"], protocol=dest["protocol"], raw_version=dest["raw_firmware"],
                software_version=dest["software"], firmware_sha256="c"*64)


def make_ota(tmp_path, observed, *, reader=None):
    targets = [target(row["index"]) for row in observed]
    written = []
    def read(row, stop):
        index = row["index"]
        return seen(index, 109) if index in written else deepcopy(observed[index])
    def write(row, data, stop, progress):
        written.append(row["index"])
        progress(len(data), len(data))
        return {"selfcheck_response": "ff0201"}
    def signed_package(release_id, before):
        dest = seen(before["index"], 109)
        return {"manifest": {"version": dest["raw_firmware"], "software_version": dest["software"]},
                "policy": {"schema": V2, "entry": {"target": expected(dest)}}, "data": b"fixture"}
    task = ModuleFirmwareMaintenance(tmp_path, lambda: targets, lambda: observed, lambda: {"username": "operator"},
                                     signed_package, nullcontext, write, reader or read)
    return task, targets, written


def select(*indices):
    return [{"index": i, "release_id": "a"*32} for i in indices]


def test_mixed_module_requires_every_108_skips_109_and_verifies_target_protocol(tmp_path):
    task, targets, written = make_ota(tmp_path, [seen(0, 108), seen(1, 109), seen(2, 108)])
    try:
        with pytest.raises(ValueError, match="所有且仅覆盖"):
            task.preview(select(0), mode=MODULE_109)
        with pytest.raises(ValueError, match="有效配置"):
            task.preview(select(0, 2), mode=MODULE_109, configuration_sha256="old")
        preview = task.preview(select(0, 2), mode=MODULE_109, configuration_sha256=digest(targets))
        assert [r["index"] for r in preview["targets"]] == [0, 2]
        assert [(r["index"], r["status"]) for r in preview["skipped_targets"]] == [(1, "already_109")]
        job = task.start(preview["preview_id"], True)
        for index, row_count in ((0, 1), (2, 2)):
            current = wait(task, "awaiting_power")
            assert len(written) == row_count and written[-1] == index
            task.acknowledge(job["job_id"])
            task.readback(job["job_id"])
        done = wait(task, "verified")
        assert all(r["readback"]["protocol"] == "1.0.3.0" for r in done["targets"])
        assert done["skipped_targets"][0]["skip_readback"]["raw_firmware"] == "5.0.0.109"
    finally:
        task.cancel()


def test_unknown_neighbor_blocks_module_but_not_selected_motor(tmp_path):
    observed = [seen(0, 108), dict(seen(1, 109), status="timeout")]
    task, _, written = make_ota(tmp_path, observed)
    try:
        with pytest.raises(ValueError, match="整个模块"):
            task.preview(select(0), mode=MODULE_109)
        with pytest.raises(ValueError, match="未确认是 108"):
            task.preview(select(1), mode=SELECTED_109)
        preview = task.preview(select(0), mode=SELECTED_109)
        job = task.start(preview["preview_id"], True)
        wait(task, "awaiting_power")
        task.acknowledge(job["job_id"]); task.readback(job["job_id"])
        assert wait(task, "verified")["skipped_targets"] == [] and written == [0]
    finally:
        task.cancel()


def test_changed_second_motor_aborts_entire_module_before_first_write(tmp_path):
    observed = [seen(0, 108), seen(1, 108)]
    task, _, written = make_ota(tmp_path, observed, reader=lambda row, stop: seen(row["index"], 108 + row["index"]))
    try:
        preview = task.preview(select(0, 1), mode=MODULE_109)
        task.start(preview["preview_id"], True)
        assert all(r["status"] == "not_run" for r in wait(task, "version_unknown")["targets"])
        assert written == []
    finally:
        task.cancel()


def test_109_exact_signed_transition_can_have_same_numeric_software(signed):
    downloads, manifest, data, policy, save, root = signed
    source, dest = seen(0, 108), seen(0, 109)
    policy["schema"] = V2
    policy["entries"][0].update(raw_version=source["raw_firmware"], software_version=source["software"],
                                 target=dict(expected(dest), firmware_sha256=manifest["sha256"]))
    manifest.update(version=dest["raw_firmware"], software_version=dest["software"],
                    from_versions=[source["raw_firmware"]])
    private = root.parents[2]/"private.pem"
    payload = json.dumps(manifest,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
    # Ed25519 openssl one-shot operations require a seekable input.
    manifest_path = root.parents[2]/"fixture-manifest"
    manifest_path.write_bytes(payload)
    signature = subprocess.run(["openssl","pkeyutl","-sign","-inkey",str(private),"-rawin","-in",str(manifest_path)],
                               capture_output=True,check=True).stdout
    (root/"release.json").write_text(json.dumps({"manifest":manifest,"signature":base64.b64encode(signature).decode()}))
    save()
    with pytest.raises(ValueError, match="不是当前批准"):
        artifact(downloads, manifest["release_id"], source)
    policy["preferred_109_sha256"] = manifest["sha256"]
    save()
    assert cached_109_releases(downloads, source, cached_releases(downloads, source)) == [manifest]
    assert artifact(downloads, manifest["release_id"], source)["policy"]["entry"]["target"]["protocol"] == "1.0.3.0"
    policy["entries"][0]["target"]["firmware_sha256"] = "b"*64
    save()
    with pytest.raises(ValueError, match="缺少精确版本适用条目"):
        artifact(downloads, manifest["release_id"], source)
    policy["entries"].append(deepcopy(policy["entries"][0]))
    with pytest.raises(ValueError, match="重复"):
        validate_policy(policy)


@pytest.mark.parametrize("label,expected_generation", [
    ("108",108),("V5.0.0.109",109),("5.0.0.109",109),("1.0.9",None),("test109",None),("5.0.0.1097",None),
])
def test_generation_is_not_inferred_from_numeric_or_substrings(label, expected_generation):
    assert firmware_generation(label) == expected_generation


def test_108_classic_version_reply_keeps_protocol_unknown(monkeypatch):
    class Bus:
        def __enter__(self): return self
        def __exit__(self, *exc): pass
        def setsockopt(self, *args): pass
        def bind(self, address): pass
        def setblocking(self, enabled): pass
        def send(self, frame):
            command = frame[9]
            if command == 0x31:
                payload = b"\xa0\x31" + b"108".ljust(18, b"\0")
                self.response = struct.pack("<IBB2x64s",1,20,1,payload)
            else:
                self.response = struct.pack("<IB3x8s",1,8,bytes.fromhex("a01e050000010002"))
        def recv(self, size):
            response = getattr(self,"response",None)
            self.response = None
            if response is None:
                raise BlockingIOError
            return response
    monkeypatch.setattr(motor_ota_transport.select,"select",lambda reads,*args:(reads,[],[]))
    row = motor_ota_transport.read_version(target(), threading.Event(), socket_factory=lambda *args:Bus())
    assert row["status"] == "known" and row["raw_firmware"] == "108" and row["numeric_software"] == "5.0.0"
    assert not row.get("protocol")


class _PnFixture:
    def __init__(self, *, quarantine_root):
        self.keys = []
    def ensure_available(self,*args): pass
    def check_idle(self,*args,**kwargs): pass
    def close(self): pass
    def read(self,row,item,stop):
        self.keys.append((row["index"],item["key"]))
        return {"status":"read","raw":item["expected_raw"],"association_method":"single_flight_time_window",
                "parameter_number_echoed":False,"protocol_profile":"lro-pn-script-v1"}


def test_reference_checks_selected_motor_without_templates_or_writes(tmp_path):
    targets = [target(0),target(1),target(2)]
    observations = [dict(seen(0,108),protocol=None),seen(1,109),dict(seen(2,108),status="unknown")]
    reads = []
    def reader(row,item,stop):
        reads.append((row["index"],item["key"]))
        return {"status":"read","raw":[item["expected_raw"][0]+2] if item["key"]=="kt" else item["expected_raw"]}
    template, writer = Mock(side_effect=AssertionError("no template")), Mock(side_effect=AssertionError("no write"))
    task = ModuleParameterMaintenance(tmp_path, lambda:targets, lambda:observations, lambda:{"username":"operator"},
        template, nullcontext, writer, reader, lambda row, stop:observations[row["index"]],script_session_factory=_PnFixture)
    try:
        with pytest.raises(ValueError, match="有效配置"):
            task.read_reference([0], "old")
        job = task.read_reference([0], digest(targets))
        current = wait(task,"reference_checked")
        assert all(item["status"]=="read" and not item["key"].startswith("pn_") for item in current["targets"][0]["items"])
        assert all(index==0 for index,key in reads)
        with pytest.raises(ValueError, match="禁止写入"):
            task.preview(job["job_id"])
        task.read_reference([1], digest(targets))
        current = wait(task,"reference_checked_with_findings")
        assert current["targets"][0]["items"][0]["status"]=="reference_difference"
        assert all(index==1 for index,key in task._script_session.keys)
        assert task._script_session.keys and current["summary"]["written_verified"]==0
        template.assert_not_called()
        writer.assert_not_called()
    finally:
        task.cancel()
