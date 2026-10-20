"""Real session/RLS checks for every raw inspection and CSV download route."""
import csv
import io
import os

from fastapi.testclient import TestClient
import pytest

from app.main import app
import app.services.curve_analysis as analysis
from test_archives import archive_payload
from test_archives_integration import create, upload, verify, submit
from test_hmi_presence_integration import environment, signed_in
from test_submissions_integration import context, grant, review


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated archive database required")]
RAW = b"time_s,stage,cycle,joint_pos_rad\n2,b,2,20\n1,a,1,\n1,a,1,10\n3,b,2,-4\n"
ENDPOINTS = ("statistics", "samples", "export.csv")


def test_raw_routes_auth_ready_state_rls_and_exact_values(environment, monkeypatch):
    _, users = environment
    with signed_in(users["first"]) as owner, signed_in(users["viewer"]) as viewer:
        item = create(owner, archive_payload(origin="OTHER", raw=RAW))
        base = f"/api/v1/archives/{item['id']}"
        with TestClient(app) as anonymous:
            for endpoint in ENDPOINTS:
                assert anonymous.get(base + "/" + endpoint).status_code == 401
                assert viewer.get(base + "/" + endpoint).status_code == 404
                assert owner.get(base + "/" + endpoint).status_code == 409
        item = upload(owner, item, RAW)
        assert item["processing_state"] == "READY"
        before = owner.get(base).json()["data"]
        params = {"metrics": "pos_rad", "start": 1, "end": 2}
        stats = owner.get(base + "/statistics", params=params)
        assert stats.status_code == 200, stats.text
        data = stats.json()["data"]
        assert data["total_rows"] == 3 and data["resolution"] == "raw"
        assert data["series"][0]["mean"] == 15 and data["series"][0]["count"] == 2 and data["series"][0]["missing"] == 1
        samples = owner.get(base + "/samples", params={**params, "offset": 1, "limit": 1}).json()["data"]
        assert samples["rows"][0]["_row"] == 3 and samples["rows"][0]["joint_pos_rad"] == 10
        assert samples["has_more"] is True
        download = owner.get(base + "/export.csv", params=params)
        assert download.status_code == 200 and download.headers["x-archive-row-count"] == "3"
        assert download.headers["content-disposition"].endswith('-raw.csv"')
        exported = list(csv.DictReader(io.StringIO(download.content.decode("utf-8-sig"))))
        assert [row["joint_pos_rad [rad]"] for row in exported] == ["", "10.0", "20.0"]
        for endpoint in ENDPOINTS:
            assert viewer.get(base + "/" + endpoint, params=params).status_code == 404
            for invalid in ({"metrics": "../../raw.csv"}, {"start": "NaN"}, {"end": "Infinity"},
                            {"start": -1}, {"end": 315360001}, {"start": 3, "end": 1}, {"stage": "unknown"}):
                rejected = owner.get(base + "/" + endpoint, params={**params, **invalid})
                assert rejected.status_code == 422, rejected.text
        for pagination in ({"offset": -1}, {"limit": 501}, {"limit": 0}):
            assert owner.get(base + "/samples", params={**params, **pagination}).status_code == 422
        monkeypatch.setattr(analysis, "MAX_EXPORT_ROWS", 2)
        rejected = owner.get(base + "/export.csv", params=params)
        assert rejected.status_code == 413 and rejected.json()["error"]["code"] == "export_too_large"
        assert "content-disposition" not in rejected.headers
        assert owner.get(base).json()["data"] == before


def test_approved_reader_reuses_existing_permissions_for_each_endpoint(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as owner, signed_in(users["viewer"]) as viewer:
        ctx = context(admin)
        initial = {key: ctx[key] for key in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}
        item = upload(owner, create(owner, archive_payload(context=initial, raw=RAW)), RAW)
        item = verify(owner, item, ctx["station_id"]).json()["data"]
        item = submit(owner, item).json()["data"]
        approved = review(owner, {"id": item["submission_id"], "revision": item["submission_revision"]})
        assert approved.status_code == 200, approved.text
        grant(admin, engine, users["viewer"], ctx["campaign_id"], "VIEW")
        base = f"/api/v1/archives/{item['execution_uuid']}"
        before = owner.get(base).json()["data"]
        for endpoint in ENDPOINTS:
            result = viewer.get(base + "/" + endpoint, params={"metrics": "pos_rad", "start": 1, "end": 1, "stage": "a", "cycle": "1"})
            assert result.status_code == 200, result.text
        assert owner.get(base).json()["data"] == before
