"""Business partition contracts with synthetic evidence and fake devices only."""
from copy import deepcopy
import asyncio
import csv
import io
import json
import threading
from unittest import mock

import pytest
import httpx

from factory_hmi.data_domain import domain_metadata, archive_domain_fields
from factory_hmi.execution_history import history_csv
from factory_hmi.gateway.schemas import PlaybackStartRequest, QuickStartRequest
from factory_hmi.gateway.app import create_app
from factory_hmi.sync.presence import build_presence_snapshot
from factory_hmi.sync.uploader import PlatformError
from test_sample_selector import application, window, SAMPLES
from test_factory_local_recording import service, finish_report
from test_verified_csv_archive import local_record, ArchiveClient, worker_for, ID


@pytest.mark.parametrize("context,domain,source", [
    ({"data_domain": "RESEARCH", "bench_id": "A10"}, "RESEARCH", "EXPLICIT"),
    ({"data_domain": "FACTORY", "bench_id": "B20"}, "FACTORY", "EXPLICIT"),
    ({"bench_id": "a10"}, "FACTORY", "HISTORICAL_BENCH"),
    ({"bench_id": "B20"}, "RESEARCH", "HISTORICAL_BENCH"),
    ({}, "UNCLASSIFIED", "NO_EVIDENCE"),
    ({"bench_id": "unknown"}, "UNCLASSIFIED", "NO_EVIDENCE"),
    ({"bench_id": ID}, "UNCLASSIFIED", "NO_EVIDENCE"),
    ({"bench_id": "A" * 101}, "UNCLASSIFIED", "NO_EVIDENCE"),
])
def test_domain_evidence_and_explicit_precedence(context, domain, source):
    original = deepcopy(context)
    result = domain_metadata(context)
    assert (result["data_domain"], result["data_domain_source"]) == (domain, source)
    assert context == original
    sent = archive_domain_fields(context)
    assert ("data_domain" in sent) == (source == "EXPLICIT")


def test_factory_selector_is_online_and_accepts_unknown_sn_but_research_restores_gate(window):
    window._platform_operator = "submit-account"
    window._platform_samples_received({"online": True, "samples": SAMPLES})
    window.data_domain.setCurrentIndex(1)
    window.robot_id.setText("000abcDef09")
    assert window._sample_catalog_online() and window.robot_id.online
    assert window.robot_id.selection_allowed() and window.robot_id.text() == "000abcDef09"
    window.data_domain.setCurrentIndex(0)
    assert not window.robot_id.isEditable() and not window.robot_id.selection_allowed()


def test_explicit_factory_sn_unknown_is_text_not_a_missing_identifier():
    context={"data_domain":"FACTORY","serial_number":"unknown","bench_id":"B20"}
    assert domain_metadata(context)["serial_number"]=="unknown"
    assert archive_domain_fields(context)["serial_number"]=="unknown"


def test_factory_configuration_and_start_freeze_actual_context_without_research_ids(window):
    window._platform_operator = "submit-account"
    window._platform_samples_received({"online": True, "samples": SAMPLES})
    window.data_domain.setCurrentIndex(1)
    window.platform_station_id.setText("b20")
    window.robot_id.setText("000abcDef09")
    window.platform_case_code.addItem("factory-check", "INSPECT")
    window.platform_case_code.setCurrentIndex(window.platform_case_code.count() - 1)
    window.config_path.setText("synthetic.yaml")
    window._config_preview = {"entries": []}
    # Passing the same selection guard is required for configuration and start.
    assert window._sample_selection_allowed()
    with mock.patch.object(window, "_bus_bindings_from_table", return_value=[]), mock.patch.object(window, "_motor_overrides_from_table", return_value=[]), mock.patch.object(window, "_run_rest") as configure, mock.patch.object(window.client, "configure") as request:
        window._configure()
        assert configure.call_args.args[0] == "应用配置"
        configure.call_args.args[1]()
        assert request.called
    with mock.patch.object(window, "_save_local_bench"), mock.patch.object(window, "_run_rest") as run, mock.patch.object(window.client, "playback_start") as start:
        window._start_playback()
        deferred = run.call_args.args[1]
        window.platform_station_id.setText("A99")
        window.data_domain.setCurrentIndex(0)
        window._platform_operator = "another-account"
        deferred()
    payload = start.call_args.kwargs
    assert payload["robot_id"] == "000abcDef09"
    assert payload["data_domain"] == "FACTORY" and payload["bench_id"] == "B20"
    assert payload["operator_id"] == "submit-account" and payload["recording_scope"] == "local"
    assert all(payload[key] is None for key in ("campaign_id", "asset_id", "configuration_id", "test_case_version_id"))


def test_quick_page_freezes_shared_mode_bench_operator_and_raw_sn(window):
    page = window.quick_page
    window.data_domain.setCurrentIndex(1)
    window.platform_station_id.setText("b20")
    window._platform_operator = "submit-account"
    page._phase = "ready"
    page.sample.setText("000abcDef09")
    with mock.patch.object(page, "runner") as run, mock.patch.object(page.client, "quick_start") as start:
        page.start(1, True)
        deferred = run.call_args.args[1]
        window.platform_station_id.setText("A99")
        window.data_domain.setCurrentIndex(0)
        page.sample.setText("changed")
        deferred()
    assert start.call_args.args == (1, "000abcDef09")
    assert start.call_args.kwargs["data_domain"] == "FACTORY"
    assert start.call_args.kwargs["bench_id"] == "B20"
    assert start.call_args.kwargs["operator_id"] == "submit-account"


def test_online_research_quick_rejects_unregistered_and_busy_sn(window):
    window._platform_operator = "submit-account"
    window._platform_samples_received({"online": True, "samples": SAMPLES})
    page = window.quick_page
    page._phase = "ready"
    page.selected = "left_arm"
    page.sample.setText("000unknown")
    with mock.patch.object(page, "runner") as run:
        page.start(1, True)
        assert not run.called and "研发在线" in page.detail.text()
    assert window._quick_sample_allowed("RP1.3-ARM-001", "left_arm")
    assert not window._quick_sample_allowed("RP1.3-ARM-002", "left_arm")


def test_quick_gateway_operation_forwards_context_without_real_devices():
    from factory_hmi.gateway.quick_run import QuickRun
    runtime = mock.Mock()
    runtime._motion_epoch_guard = threading.Lock()
    runtime._motion_epoch = 0
    runtime.snapshot.return_value = {"trajectory": {"sha256": "synthetic"}, "station": {"limb": "left_arm", "motion_readiness": {"test_allowed": True}}}
    quick = QuickRun(runtime, mock.Mock(), "isolated")
    quick._prepared = {"id": "left_arm", "label": "合成部位", "sha256": "synthetic"}
    quick._value["phase"] = "ready"
    with mock.patch.object(quick, "_power"), mock.patch.object(quick, "_check_power"), mock.patch.object(quick, "_checkpoint"), mock.patch.object(quick, "_watch"):
        quick.start(1, True, "000abcDef09", power_source="external", data_domain="FACTORY", bench_id="B20", operator_id="tester")
    quick._watcher.join(1)
    start = next(call for call in runtime._invoke.call_args_list if call.args == (("start",),))
    assert start.kwargs["data_domain"] == "FACTORY" and start.kwargs["bench_id"] == "B20"
    assert start.kwargs["robot_id"] == "000abcDef09" and start.kwargs["operator_id"] == "tester"
    assert start.kwargs["recording_scope"] == "local"


@pytest.mark.parametrize("domain,bench", [("FACTORY", "B20"), ("RESEARCH", "A10")])
def test_fake_recording_seals_partition_and_rebuild_keeps_baseline(service, domain, bench):
    service.start(test_case_id="INSPECT", data_domain=domain, bench_id=bench,
                  operator_id="tester", robot_id="000abcDef09", loop=False)
    manifest = finish_report(service)
    test_id = manifest["test_id"]
    before = service.report_manager.manifest(test_id)
    assert manifest["data_domain"] == domain and manifest["data_domain_source"] == "EXPLICIT"
    assert manifest["bench_id"] == bench and manifest["operator_id"] == "tester"
    assert manifest["sample_id"] == "000abcDef09"
    assert manifest["serial_number"] == ("000abcDef09" if domain == "FACTORY" else "")
    context = json.loads((service.report_manager.execution_dir(test_id) / "config/execution.json").read_text())
    assert context["data_domain"] == domain
    service.report_manager.rebuild(test_id, actor="tester", reason="isolated partition check")
    assert service.report_manager.manifest(test_id) == before
    service.report_manager.validate(test_id)
    live = build_presence_snapshot({"run": service.snapshot()}, {})["runs"][0]
    assert live["data_domain"] == domain and live["bench_id"] == bench
    assert live["serial_number"] == manifest["serial_number"] and live["operator_id"] == "tester"


def test_repeat_sn_has_distinct_execution_identity_and_history_filters(service):
    service.start(test_case_id="INSPECT", data_domain="FACTORY", bench_id="B20", robot_id="000abcDef09", loop=False)
    first = finish_report(service)
    trajectory = service.controller.trajectory.path
    service.prepare_next_run()
    service.import_trajectory(str(trajectory))
    service.arm()
    service.start(test_case_id="INSPECT", data_domain="FACTORY", bench_id="B20", robot_id="000abcDef09", loop=False)
    service.controller.wait(timeout=3)
    for thread in list(service._report_threads):
        thread.join(5)
    page = service.execution_history_page(data_domain="FACTORY", sn="abcDef", bench="b20")
    assert page["total"] == 2
    assert len({item["execution_uuid"] for item in page["items"]}) == 2
    assert len({item["test_id"] for item in page["items"]}) == 2
    assert first["sample_id"] == "000abcDef09"
    rows = list(csv.DictReader(io.StringIO(b"".join(history_csv(page["items"])).decode("utf-8-sig"))))
    assert all(row["工厂SN"] == "000abcDef09" and row["数据分区"] == "工厂" for row in rows)
    assert service.execution_history_page(data_domain="RESEARCH")["total"] == 0


def test_legacy_history_partition_is_a_read_view_not_manifest_rewrite(service, tmp_path):
    raw = tmp_path / "legacy.csv"
    raw.write_text("time_s,hip_pos_rad\n0,0\n1,1\n")
    original = service.report_manager._manifest_identity
    def legacy_identity(context, summary):
        return {key: value for key, value in original(context, summary).items()
                if key not in {"data_domain", "data_domain_source", "serial_number"}}
    # Build a valid old-format fixture before publication; never rewrite evidence.
    with mock.patch("factory_hmi.core.report.domain_metadata", return_value={}), mock.patch.object(service.report_manager, "_manifest_identity", side_effect=legacy_identity):
        report = service.report_manager.finalize(raw, {"test_id": "legacy", "test_case_id": "INSPECT",
            "sample_id": "000abcDef09", "bench_id": "A10", "started_at": "2026-10-01T00:00:00Z"})
    path = report.manifest_path
    baseline = path.read_bytes()
    assert service.execution_history_page(data_domain="FACTORY")["total"] == 1
    assert path.read_bytes() == baseline
    service.report_manager.validate("legacy")


class FactoryArchiveClient(ArchiveClient):
    def __init__(self):
        super().__init__()
        self.formal_context_available = True

    def create_archive(self, payload, key):
        result = super().create_archive(payload, key)
        if self.formal_context_available:
            # Simulate a server receipt with explicitly supplied formal context.
            from test_verified_csv_archive import CTX
            self.detail.update(classification_state="CLASSIFIED", classification=deepcopy(CTX))
        self.detail.update(data_domain="FACTORY", is_owner=True, serial_number=payload.get("serial_number"), bench_id=payload.get("bench_id"))
        return deepcopy(self.detail)

    def submission(self, _id):
        raise AssertionError("factory approval must not call research submissions")


def test_factory_verified_archive_uses_platform_classification_and_polls_own_receipt(local_record):
    service, report, review = local_record
    client = FactoryArchiveClient()
    worker = worker_for(service, review, client)
    worker.process_once()
    item = service.outbox.get(ID)
    assert item.state == "pending_review"
    create = next(call[1] for call in client.calls if call[0] == "create")
    assert "classification" not in create and create["bench_id"] == "A10"
    assert create["serial_number"] == "合成样机" and "data_domain" not in create
    client.detail.update(submission_status="APPROVED", execution_code="FACTORY_E1")
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "approved"
    assert len([call for call in client.calls if call[0] == "submit"]) == 1
    assert bytes(client.raw) == (report.execution_dir / "telemetry/raw.csv").read_bytes()


def test_factory_pending_receipt_rejects_wrong_owner_and_keeps_original_queue(local_record):
    service, _report, review = local_record
    client = FactoryArchiveClient()
    worker = worker_for(service, review, client)
    worker.process_once()
    frozen = service.outbox.get(ID).submission_payload
    client.detail["is_owner"] = False
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "sync_failed"
    assert service.outbox.get(ID).submission_payload == frozen
    assert "账号不一致" in service.outbox.get(ID).last_error


def test_factory_rejection_requires_new_review_and_preserves_frozen_identity(local_record):
    service, report, review = local_record
    client = FactoryArchiveClient()
    worker = worker_for(service, review, client)
    worker.process_once()
    original = json.loads(service.outbox.get(ID).submission_payload)["create"]
    baseline = report.manifest_path.read_bytes()
    client.detail["submission_status"] = "REJECTED"
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "rejected"
    with pytest.raises(ValueError, match="重新核实"):
        service.retry_submission("synthetic-test")
    service.submit_execution("synthetic-test", review={**review, "notes": "按驳回意见重新核实"})
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"
    assert json.loads(service.outbox.get(ID).submission_payload)["create"] == original
    submits = [call for call in client.calls if call[0] == "submit"]
    assert submits[-1][1]["expected_submission_revision"] == 1
    assert report.manifest_path.read_bytes() == baseline
    assert len(list((report.execution_dir / "analysis").glob("operator_review.v*.json"))) == 2


def test_factory_lost_submit_response_retries_identical_frozen_payload(local_record):
    service, _report, review = local_record
    client = FactoryArchiveClient()
    action = client.archive_action
    lost = [False]
    def lose_once(archive_id, name, payload, key):
        result = action(archive_id, name, payload, key)
        if name == "submit" and not lost[0]:
            lost[0] = True
            raise PlatformError("lost factory submit receipt")
        return result
    client.archive_action = lose_once
    worker = worker_for(service, review, client)
    worker.process_once()
    assert service.outbox.get(ID).state == "sync_failed"
    frozen = service.outbox.get(ID).submission_payload
    service.retry_submission("synthetic-test")
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"
    assert service.outbox.get(ID).submission_payload == frozen
    submits = [call for call in client.calls if call[0] == "submit"]
    assert len(submits) == 2 and submits[0][1:] == submits[1][1:]
    assert len([call for call in client.calls if call[0] == "create"]) == 1


def test_old_factory_waiting_classification_requires_explicit_reverification_before_submit(local_record):
    service, _report, review = local_record
    client = ArchiveClient()  # Simulates the old server's unclassified receipt.
    worker = worker_for(service, review, client)
    worker.process_once()
    assert service.outbox.get(ID).state == "awaiting_classification"
    client.detail.update(data_domain="FACTORY", is_owner=True, classification_state="CLASSIFIED")
    service.outbox.update(ID, next_attempt_at=0)
    count = len([call for call in client.calls if call[0] == "submit"])
    worker.process_once()
    assert service.outbox.get(ID).state == "needs_verification"
    assert len([call for call in client.calls if call[0] == "submit"]) == count
    with pytest.raises(ValueError, match="重新核实"):
        service.retry_submission("synthetic-test")
    service.submit_execution("synthetic-test", review=review)
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"


def test_filtered_history_and_export_carry_same_domain_bench_and_raw_sn(window):
    window.history_domain.setCurrentIndex(window.history_domain.findData("FACTORY"))
    window.history_bench.setText("b20")
    window.history_sn.setText("000abcDef09")
    filters = window._history_date_query()
    assert filters == {"data_domain": "FACTORY", "bench": "b20", "sn": "000abcDef09"}
    with mock.patch.object(window.client, "get") as get:
        window.client.execution_history(**filters)
    assert "data_domain=FACTORY" in get.call_args.args[0] and "bench=b20" in get.call_args.args[0]
    with mock.patch.object(window.client, "download") as download:
        window.client.export_execution_history("synthetic.csv", **filters)
    assert "sn=000abcDef09" in download.call_args.args[0] and "data_domain=FACTORY" in download.call_args.args[0]


def test_request_models_keep_legacy_optional_mode_and_reject_unknown_modes():
    old = PlaybackStartRequest(test_case_id="INSPECT", robot_id="text")
    assert old.data_domain is None
    assert QuickStartRequest(duration_hours=1, physical_estop_confirmed=True, sample_id="000a").data_domain is None
    with pytest.raises(ValueError):
        PlaybackStartRequest(test_case_id="INSPECT", data_domain="UNCLASSIFIED")


async def with_isolated_gateway(app, operation):
    # A timer also exercises event-loop responsiveness while sync handlers run.
    async def pulse():
        while True:
            await asyncio.sleep(.005)
    task = asyncio.create_task(pulse())
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://isolated") as client:
                return await operation(client)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def wait_for_reports(service, seconds=5):
    for _ in range(int(seconds / .005)):
        if not any(thread.is_alive() for thread in list(service._report_threads)):
            assert not service._report_errors
            return
        await asyncio.sleep(.005)
    raise AssertionError(f"isolated report did not finish within {seconds} seconds")


def test_asgi_factory_start_and_filtered_list_export_use_real_payload_with_fake_motors(service, tmp_path):
    async def operation(client):
        response = await client.post("/api/v1/playback/start", json={"test_case_id": "INSPECT", "data_domain": "FACTORY",
            "robot_id": "000abcDef09", "bench_id": "B20", "operator_id": "tester", "recording_scope": "local", "loop": False})
        assert response.status_code == 200, response.text
        assert response.json()["snapshot"]["playback"]["serial_number"] == "000abcDef09"
        await wait_for_reports(service)
        params = {"data_domain": "FACTORY", "sn": "000abcDef09", "bench": "B20"}
        page = await client.get("/api/v1/executions", params=params)
        assert page.status_code == 200 and page.json()["total"] == 1
        export = await client.get("/api/v1/executions/export.csv", params=params)
        assert export.status_code == 200 and "000abcDef09" in export.text
        assert (await client.get("/api/v1/executions", params={"data_domain": "RESEARCH"})).json()["total"] == 0
        assert (await client.get("/api/v1/executions", params={"data_domain": "INVALID"})).status_code == 422
    asyncio.run(with_isolated_gateway(create_app(controller=service, data_root=tmp_path / "gateway"), operation))


def test_asgi_quick_start_seals_explicit_factory_context_with_fake_motors(tmp_path):
    app = create_app(data_root=tmp_path / "gateway", lease_timeout_s=30)
    async def operation(client):
        headers = {"X-RP1-Client-ID": "isolated-quick"}
        prepared = await client.post("/api/v1/quick/prepare", headers=headers, json={"profile": "left_arm", "backend": "fake"})
        assert prepared.status_code == 200, prepared.text
        response = await client.post("/api/v1/quick/start", headers=headers, json={"duration_hours": .0001,
            "physical_estop_confirmed": True, "power_source": "external", "sample_id": "000abcDef09",
            "data_domain": "FACTORY", "bench_id": "B20", "operator_id": "tester"})
        assert response.status_code == 200, response.text
        runtime = app.state.runtime.session("isolated-quick")
        live = app.state.runtime.presence_snapshot()["runs"][0]
        assert live["data_domain"] == "FACTORY" and live["serial_number"] == "000abcDef09"
        # Normal completion finishes the current built-in cycle (~12.6 s).
        await wait_for_reports(runtime.controller, seconds=20)
        manifests = list(runtime.controller.report_manager.executions_root.glob("*/manifest.json"))
        assert len(manifests) == 1
        metadata = json.loads(manifests[0].read_text())
        assert metadata["data_domain"] == "FACTORY" and metadata["serial_number"] == "000abcDef09"
        assert metadata["bench_id"] == "B20" and metadata["operator_id"] == "tester"
    asyncio.run(with_isolated_gateway(app, operation))


def test_factory_unclassified_curve_only_never_submits(local_record):
    service, _report, review = local_record
    client = FactoryArchiveClient()
    client.formal_context_available = False
    worker = worker_for(service,review,client)
    worker.process_once()
    assert service.outbox.get(ID).state == "awaiting_classification"
    assert not any(call[0] == "submit" for call in client.calls)
