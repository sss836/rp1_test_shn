"""Native recording CSV through real worker/HTTP/RLS; no device access."""
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.services.archive_storage import ArchiveStorage
from monitor_recording_fixture import recording_fixture, encode_recording
from test_archives_integration import headers, upload
from test_archive_imports_integration import intake
from test_hmi_presence_integration import environment, signed_in
from test_setup_integration import data, post
from test_submissions_integration import grant, review
from test_factory_robot_results import result_csv

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get("HMI_TEST_ADMIN_URL"), reason="disposable database required")]


def whole_context(admin):
    tag = uuid4().hex[:8].upper()
    sequence = len([asset for asset in data(admin, "setup/options")["assets"] if asset["asset_kind"] == "WHOLE_MACHINE"]) + 1
    asset = post(admin, "assets", {"asset_code": f"RP1.3-SYS-{sequence:03d}", "asset_kind": "WHOLE_MACHINE", "target_part_code": "SYS",
        "product_family": "RP1.3", "serial_number": "QA-"+tag, "display_name": "Synthetic whole robot", "model": "QA", "configuration_fingerprint": "QA-"+tag})
    program = post(admin, "programs", {"program_code": "MON-"+tag, "name": "Synthetic native QA"})
    campaign = post(admin, "campaigns", {"program_id": program["id"], "campaign_code": "MON-"+tag, "name": "Synthetic native QA", "asset_kind": "WHOLE_MACHINE"})
    station = post(admin, "stations", {"site_code": "MON-"+tag, "site_name": "QA", "lab_code": "MON-"+tag, "lab_name": "QA", "station_code": "B-MON-"+tag, "name": "QA", "station_type": "WHOLE_MACHINE"})
    case = next(item for item in data(admin, "setup/catalog") if item["asset_kind"] == "WHOLE_MACHINE" and item["target_part_code"] == "SYS" and item["enabled"] and item["version_id"])
    return post(admin, f"campaigns/{campaign['id']}/contexts", {"asset_id": asset["id"], "configuration_id": asset["configuration_id"], "test_case_version_id": case["version_id"], "station_id": station["id"], "cycle_code": "MON-"+tag})["context"]


def import_recording(client, ctx, raw, record):
    payload = {"data_domain": "RESEARCH", "filename": "rp1-monitor-"+record["recording_id"]+".csv", "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "test_name": "Synthetic native recording", "actual_object": "Synthetic whole robot",
        "station_id": ctx["station_id"], "classification": None}
    return intake(client, payload)["archive"]


def native_intake(item, ctx, record):
    # Active time/cycle/failure counts are explicit human declarations. The CSV's
    # observed span is never promoted automatically to valid exposure.
    return {"verification": {"expected_revision": item["revision"], "tester": "Synthetic actual tester", "station_id": ctx["station_id"],
        "verdict": "passed", "notes": "Synthetic explicit human confirmation; observation span is not MTBF exposure",
        "declared_started_at": record["started_at"], "declared_ended_at": record["ended_at"],
        "declared_active_seconds": 1.5, "declared_completed_cycles": 2, "declared_exception_count": 0},
        "context": ctx, "expected_submission_revision": item.get("submission_revision") or 0}


def test_native_research_import_worker_curves_intake_review_and_no_mtbf_qualification(environment):
    engine, users = environment
    record, rows = recording_fixture()
    raw = encode_recording(record, rows)
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author, signed_in(users["viewer"]) as viewer:
        ctx = whole_context(admin)
        original = import_recording(author, ctx, raw, record)
        assert original["origin"] == "HISTORY" and original["facts"]["active_seconds"] is None
        item = upload(author, original, raw)
        assert item["processing_state"] == "READY", item
        assert item["manifest"]["monitor_recording"] == record
        assert item["manifest"]["monitor_validation"]["scope"] == "OBSERVATION_ONLY_NOT_QUALIFIED_EXPOSURE"
        assert item["detected_timing"] is None, "do not guess acquisition time from a monitor filename"
        curves = author.get(f"/api/v1/archives/{item['id']}/curves", params={"metrics": record["metrics"][0]})
        assert curves.status_code == 200, curves.text
        series = curves.json()["data"]["series"][0]
        assert series["unit"] == "°C" and [point[0] for point in series["points"] if point[1] is not None] == [1.25, 5.25]
        assert any(point[1] is None for point in series["points"])
        route = f"/api/v1/archives/{item['id']}/verify-and-submit"
        payload, key = native_intake(item, ctx, record), uuid4().hex
        pending = author.post(route, json=payload, headers=headers(key))
        assert pending.status_code == 200, pending.text
        pending = pending.json()["data"]
        assert pending["submission_status"] == "PENDING"
        assert author.post(route, json=payload, headers=headers(key)).json()["data"]["submission_id"] == pending["submission_id"]
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code == 404
        assert review(author, {"id": pending["submission_id"], "revision": pending["submission_revision"]}, "REJECTED").status_code == 200
        rejected = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        again = author.post(route, json=native_intake(rejected, ctx, record), headers=headers())
        assert again.status_code == 200, again.text
        pending2 = again.json()["data"]
        assert pending2["submission_revision"] == 2
        approved = review(author, {"id": pending2["submission_id"], "revision": pending2["submission_revision"]})
        assert approved.status_code == 200, approved.text
        final = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert final["submission_status"] == "APPROVED" and final["execution_id"]
        assert final["facts"] == original["facts"] and final["manifest"] == item["manifest"]
        storage = ArchiveStorage(get_settings().archive_root)
        assert storage.path(item["execution_uuid"], "raw.csv").read_bytes() == raw
        grant(admin, engine, users["viewer"], ctx["campaign_id"], "VIEW")
        assert viewer.get(f"/api/v1/archives/{item['id']}/curves", params={"metrics": record["metrics"][1]}).status_code == 200
        with engine.connect() as db:
            interval = db.execute(text("""SELECT ri.id,ri.active_seconds,ri.source_method FROM test.runtime_interval ri
                JOIN test.test_execution e ON e.id=ri.execution_id WHERE e.public_id=:id"""), {"id": final["execution_id"]}).one()
            assert float(interval.active_seconds) == 1.5 and interval.source_method == "approved-summary-unqualified"
            assert db.execute(text("SELECT count(*) FROM reliability.exposure_assessment WHERE runtime_interval_id=:id"), {"id": interval.id}).scalar_one() == 0
            assert db.execute(text("SELECT count(*) FROM reliability.current_mtbf_result WHERE campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:id)"), {"id": ctx["campaign_id"]}).scalar_one() == 0


@pytest.mark.parametrize("kind", ["simulated", "tampered", "factory-summary"])
def test_bad_source_never_ready_or_submit_but_original_bytes_remain(environment, kind):
    _, users = environment
    record, rows = recording_fixture("simulated" if kind == "simulated" else "ros2")
    if kind == "tampered": rows[0][3]["payload_hash"] = "f"*64
    raw = result_csv() if kind == "factory-summary" else encode_recording(record, rows)
    with signed_in(users["admin"]) as admin:
        ctx = whole_context(admin)
        item = upload(admin, import_recording(admin, ctx, raw, record), raw)
        assert item["processing_state"] == "FAILED", item
        assert item["submission_id"] is None
        base = f"/api/v1/archives/{item['id']}"
        assert admin.get(base+"/curves").status_code == 409
        assert admin.post(base+"/verify-and-submit", json=native_intake(item, ctx, record), headers=headers()).status_code == 409
        assert ArchiveStorage(get_settings().archive_root).path(item["execution_uuid"], "raw.csv").read_bytes() == raw
