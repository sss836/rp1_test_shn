"""Synthetic local history, pagination, ledger and CSV tests; no devices/network."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from factory_hmi.core.service import FactoryService
from factory_hmi.execution_history import history_csv, select_history
from factory_hmi.gateway.app import create_app


@pytest.fixture
def service(tmp_path):
    value = FactoryService(data_root=tmp_path / "window", report_root=tmp_path / "shared")
    for thread in list(value._report_threads):
        thread.join(2)
    yield value


def seed(service, test_id, started_at, sn="000012345678901234567890", *, missing=False):
    root = service.report_manager.executions_root / test_id
    (root / "analysis").mkdir(parents=True)
    (root / "telemetry").mkdir()
    manifest = {"execution_uuid": test_id, "test_id": test_id, "sample_id": sn,
                "started_at": started_at, "ended_at": started_at, "operator_id": "合成人员",
                "timestamp_provenance": "missing" if missing else "recorded"}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "analysis/summary.json").write_text('{"verdict":{"status":"passed"}}')
    (root / "telemetry/raw.csv").write_text("synthetic immutable acquisition\n")
    # Seed the persistent read model as a production publication/rebuild does.
    service.report_manager.refresh_history_index(test_id)
    service.report_manager.write_ledger(service.report_manager.iter_history())
    return root


def decode_csv(data):
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


async def request_all(app, calls):
    async def pulse():
        while True:
            await asyncio.sleep(.005)
    timer = asyncio.create_task(pulse())
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
                return [await asyncio.wait_for(client.get(url, **kwargs), 30) for url, kwargs in calls]
    finally:
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass


def test_date_bounds_use_local_midnight_inclusive_end_day_and_keep_unknown_only_in_all(service):
    for name, when, missing in (
        ("before", "2026-10-27T15:59:59Z", False),
        ("begin", "2026-10-27T16:00:00Z", False),
        ("end", "2026-10-28T15:59:59Z", False),
        ("after", "2026-10-28T16:00:00Z", False),
        ("unknown", "1970-01-01T00:00:00Z", True),
    ):
        seed(service, name, when, missing=missing)
    result = service.execution_history_page(start_at="2026-10-28T00:00:00+08:00", end_at="2026-10-29T00:00:00+08:00")
    assert [item["test_id"] for item in result["items"]] == ["end", "begin"]
    assert result["total"] == 2
    all_items = service.execution_history_page()["items"]
    assert len(all_items) == 5 and all_items[-1]["started_at"] == ""
    # The persistent ledger always covers all local executions, even after a filter.
    ledger = decode_csv(Path(result["ledger_path"]).read_bytes())
    assert len(ledger) == 5


def test_sn_is_text_partial_case_insensitive_and_csv_keeps_long_leading_zero_identifier(service):
    root = seed(service, "SN-CASE", "2026-10-28T01:00:00Z", "000012345678901234567890-AbC")
    seed(service, "other", "2026-10-28T02:00:00Z", "different")
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    page = service.execution_history_page(sn="567890-abc")
    assert page["total"] == 1
    rows = decode_csv(b"".join(history_csv(page["items"])))
    assert rows[0]["模块编号/SN"] == "000012345678901234567890-AbC"
    assert rows[0]["记录目录"] == str(root)
    assert rows[0]["原始采集CSV"] == str(root / "telemetry/raw.csv")
    assert all(path.read_bytes() == content for path, content in before.items())
    assert service.outbox.list() == []


def test_ledger_write_failure_does_not_hide_records_or_change_evidence(service, monkeypatch):
    root = seed(service, "untouched", "2026-10-28T00:00:00Z")
    before = (root / "manifest.json").read_bytes()
    calls=[]
    def fail(_items):
        calls.append(True)
        raise OSError("synthetic read-only ledger directory")
    monkeypatch.setattr(service.report_manager, "write_ledger", fail)
    page = service.execution_history_page()
    assert page["total"] == 1 and page["ledger_error"] == ""
    assert calls == []  # Opening history must not rewrite or scan the whole ledger.
    assert (root / "manifest.json").read_bytes() == before


def test_csv_neutralizes_formula_cells_without_changing_plain_sn():
    rows = decode_csv(b"".join(history_csv([{"sample_id": "000000000000000001", "review_reason": " =1+1"}])))
    assert rows[0]["模块编号/SN"] == "000000000000000001"
    assert rows[0]["审批意见"].startswith("'")


def test_normal_finalization_publishes_named_folder_and_persistent_ledger(service, tmp_path):
    source = tmp_path / "TEST-DONE.csv"
    raw = "time_s,hip_pos_rad,hip_cmd_pos_rad,hip_torque_nm,hip_temp_c\n0,0,0,1,25\n1,0.1,0.2,2,26\n"
    source.write_text(raw)
    controller = SimpleNamespace(wait=lambda: None, snapshot=lambda: {"playback": {
        "ended_at": "2026-10-28T01:00:01Z", "progress": {"active_seconds": 1, "completed_cycles": 1, "outcome": "duration_complete"}}})
    service._finalize_after_run(controller, source, {"test_id": "TEST-DONE", "sample_id": "00012345678901234567890",
        "started_at": "2026-10-28T01:00:00Z", "recording_scope": "local", "test_case_id": "OTHER"})
    assert "TEST-DONE" not in service._report_errors
    folder = service.report_manager.executions_root / "TEST-DONE"
    assert (folder / "manifest.json").is_file()
    assert (folder / "telemetry/raw.csv").read_text() == raw
    rows = decode_csv((service.report_manager.executions_root / "执行台账.csv").read_bytes())
    assert len(rows) == 1 and rows[0]["本地记录ID"] == "TEST-DONE"
    assert rows[0]["模块编号/SN"] == "00012345678901234567890"


@pytest.mark.parametrize("start,end", [("2026-10-28", ""), ("wrong", ""),
                                        ("2026-10-29T00:00:00Z", "2026-10-28T00:00:00Z")])
def test_invalid_bounds_rejected(start, end):
    with pytest.raises(ValueError):
        select_history([], start_at=start, end_at=end)


def test_api_export_includes_more_than_1000_rows_and_shared_location_and_auth(service, monkeypatch):
    start = datetime(2026, 10, 28, tzinfo=timezone.utc)
    for number in range(1007):
        seed(service, f"TEST-{number:04d}", (start + timedelta(seconds=number)).isoformat())
    with service.outbox._connect() as database:
        database.executemany("""INSERT INTO execution_outbox
            (execution_uuid,test_id,bundle_path,manifest_json,state,created_at,updated_at)
            VALUES (?,?,?,'{}','approved',0,?)""",
            [(f"synthetic-{number}", f"TEST-{number:04d}", "/synthetic/no-upload.tar.gz", number) for number in range(1007)])
    app = create_app(service, data_root=service.report_manager.data_root)
    monkeypatch.setenv("RP1_FACTORY_OPERATOR_TOKEN", "synthetic-secret")
    headers = {"X-RP1-Operator-Token": "synthetic-secret", "X-RP1-Client-ID": "a-different-window"}
    responses = asyncio.run(request_all(app, [
        ("/api/v1/executions?offset=1000&limit=100&sn=0000123", {"headers": headers}),
        ("/api/v1/executions/export.csv?sn=0000123", {"headers": headers}),
        ("/api/v1/records/location?test_id=TEST-0001", {"headers": headers}),
        ("/api/v1/records/location", {"headers": headers}),
        ("/api/v1/records/location?test_id=../escape", {"headers": headers}),
        ("/api/v1/executions/export.csv", {}),
        ("/api/v1/executions?start_at=2026-10-28", {"headers": headers}),
    ]))
    page, exported, location, home, traversal, denied, invalid = responses
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 1007 and len(page.json()["items"]) == 7
    assert all(item["sync_state"] == "approved" for item in page.json()["items"])
    assert exported.status_code == 200
    assert len(decode_csv(exported.content)) == 1007
    assert len(decode_csv(Path(page.json()["ledger_path"]).read_bytes())) == 1007
    assert location.json()["result"]["directory"] == str(service.report_manager.executions_root / "TEST-0001")
    assert home.json()["result"]["directory"] == str(service.report_manager.executions_root)
    assert traversal.status_code == 404 and denied.status_code == 401 and invalid.status_code == 422


def test_shared_ledger_stays_complete_under_concurrent_filtered_refreshes(service, tmp_path):
    second = FactoryService(data_root=tmp_path / "other-window", report_root=service.report_manager.data_root, outbox=service.outbox)
    for thread in list(second._report_threads):
        thread.join(2)
    for number in range(12):
        seed(service, f"BATCH-{number}", "2026-10-28T01:00:00Z", f"SN-{number % 2}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: (service if n % 2 else second).execution_history_page(sn=f"SN-{n % 2}"), range(8)))
    assert all(result["total"] == 6 for result in results)
    ledger = service.report_manager.executions_root / "执行台账.csv"
    assert len(decode_csv(ledger.read_bytes())) == 12
    assert not list(service.report_manager.executions_root.glob(".ledger-*"))
