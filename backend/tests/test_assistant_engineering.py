"""Engineering capability boundaries, tested without production data."""
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import pytest
from PIL import Image
from app.core.errors import ApiError
from app.services import assistant_attachments as attachments,assistant_sources as sources
from app.services.assistant_privacy import public_evidence,redact_text
from app.services.assistant_web import public_url

def test_private_evidence_not_forwarded():
    result=public_evidence({"operator_name":"私人姓名","owner_name":"私人姓名","metric":"torque_nm","value":0,
                           "note":"password=not-for-model user@example.com Bearer synthetictoken"})
    assert "operator_name" not in result and "owner_name" not in result
    assert result["value"]==0 and result["metric"]=="torque_nm"
    assert all(x not in result["note"] for x in ("not-for-model","user@example.com","synthetictoken"))

def test_attachments_are_owned_immutable_and_validated(tmp_path,monkeypatch):
    monkeypatch.setattr(attachments,"get_settings",lambda:SimpleNamespace(assistant_attachment_root=str(tmp_path)))
    actor=SimpleNamespace(public_id=uuid4());other=SimpleNamespace(public_id=uuid4())
    image=Image.new("RGB",(80,40),"white");output=io.BytesIO();image.save(output,format="PNG")
    meta=attachments.upload(actor,output.getvalue(),"截图.png","image/png")
    assert meta["kind"]=="image"
    frozen=attachments.freeze(actor,[meta["id"]])
    assert attachments.model_content(actor,frozen)[0]["type"]=="image"
    with pytest.raises(ApiError) as caught:attachments.read(other,meta["id"])
    assert caught.value.status_code==404
    (tmp_path/str(actor.public_id)/meta["id"]/"content").write_bytes(b"changed")
    with pytest.raises(ApiError):attachments.model_content(actor,frozen)
    with pytest.raises(ApiError):attachments.upload(actor,b"not an image","fake.png","image/png")
    with pytest.raises(ApiError):attachments.upload(actor,b"measurement","raw.csv","text/plain")
    log=attachments.upload(actor,b"password=synthetic-private","error.log","text/plain")
    assert b"synthetic-private" not in attachments.read(actor,log["id"])[1]

def test_expired_attachment_cleanup(tmp_path,monkeypatch):
    monkeypatch.setattr(attachments,"get_settings",lambda:SimpleNamespace(assistant_attachment_root=str(tmp_path)))
    actor=SimpleNamespace(public_id=uuid4());meta=attachments.upload(actor,b"trace","error.log","text/plain")
    path=tmp_path/str(actor.public_id)/meta["id"]/"metadata.json"
    data=json.loads(path.read_text());data["expires_at"]="2000-01-01T00:00:00+00:00";path.write_text(json.dumps(data))
    attachments.cleanup_expired()
    assert not path.parent.exists()

def test_torque_absolute_peak_raw_range_and_approval(tmp_path,monkeypatch):
    from test_archives import processed
    from app.services import assistant_business as business
    _,archive=processed(tmp_path,b"time_s,a_torque_nm,b_torque_nm\n0,-12,7\n1,3,9\n")
    archive["submission_status"]="APPROVED"
    monkeypatch.setattr(business,"get_settings",lambda:SimpleNamespace(archive_root=str(tmp_path),archive_query_memory_mb=128))
    monkeypatch.setattr(business,"ArchiveRepository",lambda session:SimpleNamespace(get=lambda ref:archive))
    result=business.compare_torque(None,{"archives":["synthetic"]},{})["data"]
    assert result["largest_in_scanned_scope"]["subject"]=="a"
    assert result["largest_in_scanned_scope"]["peak_abs"]==12
    assert result["largest_in_scanned_scope"]["range"]=={"start":0,"end":1}
    assert result["complete"]
    scoped=business.compare_torque(None,{"archives":["synthetic"]},{"archive_id":"synthetic","start":1,"end":1})["data"]
    assert scoped["largest_in_scanned_scope"]["subject"]=="b" and scoped["largest_in_scanned_scope"]["peak_abs"]==9
    with pytest.raises(ApiError):business.compare_torque(None,{"archives":["synthetic"]},{"archive_id":"synthetic","selection_empty":True})
    archive["submission_status"]="PENDING"
    assert not business.compare_torque(None,{"archives":["synthetic"]},{})["data"]["items"]
    assert business.compare_torque(None,{"archives":["synthetic"],"scope":"raw"},{})["data"]["items"]

def test_runtime_converts_seconds_and_keeps_distinct_buckets(monkeypatch):
    from app.services import assistant_business as business
    item={"asset_kind":"MODULE","target_part_code":"ARM","part_asset_count":2,
          "part_durations":{"total_duration_seconds":7200,"effective_exposure_seconds":3600,"pending_seconds":1800,"excluded_seconds":1800,"active_elapsed_seconds":0}}
    data={"groups":[{"objects":[item]}],"freshness":{"status":"FRESH"}}
    monkeypatch.setattr(business,"ReadModelRepository",lambda session:SimpleNamespace(dashboard_home=lambda:SimpleNamespace(model_dump=lambda **kw:data)))
    monkeypatch.setattr(business,"part_codes",lambda session,part:["ARM"])
    got=business.runtime_summary(None,{"part":"上肢"},{})["data"]["items"][0]
    assert got["hours"]["total_duration_seconds"]==2
    assert got["hours"]["effective_exposure_seconds"]==1
    assert got["hours"]["pending_seconds"]==.5 and got["part_asset_count"]==2

def test_source_version_hash_search_and_path_boundary(tmp_path,monkeypatch):
    folder=tmp_path/"hmi";folder.mkdir();raw=b"raise TimeoutError('SYNTHETIC PLC deadline')\n"
    (folder/"client.py").write_bytes(raw)
    (tmp_path/"manifest.json").write_text(json.dumps({"version":"synthetic-version","snapshot_sha256":"synthetic-snapshot",
        "files":[{"path":"hmi/client.py","sha256":hashlib.sha256(raw).hexdigest()}]}))
    monkeypatch.setattr(sources,"get_settings",lambda:SimpleNamespace(assistant_source_root=str(tmp_path)))
    found=sources.source_query({"query":"PLC deadline"})
    assert found["version"]=="synthetic-version" and found["matches"][0]["line"]==1
    assert "TimeoutError" in sources.source_query({"path":"hmi/client.py","start":1})["text"]
    with pytest.raises(ApiError):sources.source_query({"path":"../../etc/passwd"})
    (folder/"client.py").write_text("changed")
    with pytest.raises(ApiError):sources.source_query({"path":"hmi/client.py"})

@pytest.mark.parametrize("url",["http://example.com","https://user:pass@example.com","https://127.0.0.1","https://[::1]","https://169.254.169.254"])
def test_web_rejects_private_and_credential_urls(url):
    with pytest.raises(ApiError):public_url(url)
