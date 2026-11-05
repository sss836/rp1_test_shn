#!/usr/bin/env python3
"""Real native-service HTTP acceptance, exclusively in the disposable QA cluster.

The source database contains synthetic monitor points. Exports use a SELECT-only
account and REPEATABLE READ; no real site, device or installed GUI is opened.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlencode
from uuid import uuid4

from argon2 import PasswordHasher
import httpx
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url


def main():
    admin_url = os.environ["HMI_TEST_ADMIN_URL"]
    if not make_url(admin_url).database.startswith("rp1_presence_native_domains") or os.environ.get("APP_ENV") != "test":
        raise RuntimeError("Only the disposable native-domains database is allowed")
    from app.services.archive_worker import run_once
    from rp1_monitor.factory_store import FactoryStore
    from rp1_monitor.platform_client import NativePlatformError, PlatformSessionClient
    from rp1_monitor.platform_workspace import NativePlatformService
    from rp1_monitor.recordings import samples, sessions
    from rp1_monitor.storage import robots
    from monitor_recording_fixture import recording_fixture, point_hash

    engine = create_engine(admin_url)
    password = os.environ["QA_NATIVE_PASSWORD"]
    accounts = {}
    with engine.begin() as db:
        for kind, role in (("admin", "SYSTEM_ADMIN"), ("author", "TEST_EXECUTOR"), ("other", "TEST_EXECUTOR")):
            name = "native-qa-"+kind+"-"+uuid4().hex[:10]
            identifier = db.execute(text("""INSERT INTO iam.app_user(username,display_name,role,principal_kind,must_change_password)
                VALUES(:name,:name,:role,'HUMAN',false) RETURNING id"""), {"name": name, "role": role}).scalar_one()
            db.execute(text("INSERT INTO iam.user_credential(user_id,password_hash) VALUES(:id,:hash)"),
                       {"id": identifier, "hash": PasswordHasher().hash(password)})
            accounts[kind] = name

    source_database = "rp1_presence_native_source"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as db:
        db.exec_driver_sql("CREATE DATABASE "+source_database)
    source_url = make_url(admin_url).set(database=source_database)
    source_engine = create_engine(source_url)
    source_records = []
    for number in range(3):
        record, rows = recording_fixture()
        delta = timedelta(minutes=30-number*10)
        original_start = datetime.fromisoformat(record["started_at"])
        start = datetime.now(timezone.utc)-delta
        record.update(started_at=start.isoformat(), ended_at=(start+timedelta(seconds=6)).isoformat())
        for row in rows:
            sample = row[3]
            shift = datetime.fromisoformat(sample["observed_at"])-original_start
            sample["observed_at"] = (start+shift).isoformat()
            sample["payload"]["observed_at"] = sample["observed_at"]
            if sample["payload"]["source_at"] is not None:
                sample["payload"]["source_at"] = sample["observed_at"]
            if number == 1 and not sample["payload"]["connected"]:
                sample["payload"].update(source_kind="ros2", connected=True, missing_reasons={})
                sample["payload"]["values"] = dict(zip(record["metrics"], (22.0, -0.5)))
            sample["payload_hash"] = point_hash(sample["payload"])
        source_records.append((record, rows))
    for table in (robots, sessions, samples):
        table.create(source_engine)
    with source_engine.begin() as db:
        db.execute(robots.insert(), {"robot_id": "robot-qa", "name": "Synthetic source", "source_kind": "ros2",
            "token_digest": hashlib.sha256(b"synthetic source only").hexdigest(), "enabled": True})
        for record, rows in source_records:
            db.execute(sessions.insert(), {"id": record["recording_id"], "run_number": "SYS-QA_"+record["recording_id"],
                "robot_id": record["robot_id"], "source_kind": "ros2", "case_number": "SYS-QA", "robot_number": "robot-qa",
                "site": "QA", "frequency_hz": 1.0, "metrics": record["metrics"], "source_context": record["source_context"],
                "started_at": datetime.fromisoformat(record["started_at"]), "ended_at": datetime.fromisoformat(record["ended_at"])})
            for row in rows:
                sample = row[3]
                db.execute(samples.insert(), {"recording_id": record["recording_id"], "sequence": sample["sequence"],
                    "observed_at": datetime.fromisoformat(sample["observed_at"]), "payload": sample["payload"], "payload_hash": sample["payload_hash"]})
        readonly = source_engine.dialect.identifier_preparer.quote(os.environ["RP1_READONLY_USER"])
        db.exec_driver_sql("GRANT SELECT ON ALL TABLES IN SCHEMA public TO "+readonly)

    def source_fingerprint():
        with source_engine.connect() as db:
            facts = [{"recording_id": row.recording_id, "sequence": row.sequence, "observed_at": row.observed_at.isoformat(),
                      "payload": row.payload, "payload_hash": row.payload_hash}
                     for row in db.execute(select(samples).order_by(samples.c.recording_id, samples.c.sequence))]
            counts = db.execute(text("SELECT (SELECT count(*) FROM monitor_recordings),(SELECT count(*) FROM monitor_recording_samples)")).one()
        return tuple(counts), hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()

    before_source = source_fingerprint()
    source_ro_url = source_url.set(username=os.environ["RP1_READONLY_USER"], password=os.environ["RP1_READONLY_PASSWORD"])
    with create_engine(source_ro_url).connect() as db:
        assert db.execute(text("SELECT has_table_privilege(current_user,'monitor_recording_samples','SELECT') AND NOT has_table_privilege(current_user,'monitor_recording_samples','INSERT')")).scalar_one()

    server = subprocess.Popen(["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning", "--no-access-log"])
    base = "http://127.0.0.1:8000"
    stop = threading.Event()
    worker_errors = []

    def worker():
        while not stop.is_set():
            try:
                if not run_once():
                    stop.wait(.05)
            except Exception:
                worker_errors.append("isolated_worker_failed")
                return

    thread = None
    try:
        for _ in range(100):
            try:
                if httpx.get(base+"/healthz", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if server.poll() is not None:
                raise RuntimeError("Isolated API did not start")
            time.sleep(.1)
        else:
            raise RuntimeError("Isolated API startup timed out")
        thread = threading.Thread(target=worker, name="synthetic-native-archive-worker", daemon=True)
        thread.start()
        admin = PlatformSessionClient(base, allow_http=True)
        admin.login(accounts["admin"], password)

        def write(path, value):
            return admin.call("POST", "/api/v1/setup/"+path, payload={**value, "reason": "Synthetic native acceptance"}, key=str(uuid4()))

        tag = uuid4().hex[:8].upper()
        catalog = admin.call("GET", "/api/v1/setup/catalog")
        case = next(item for item in catalog if item["asset_kind"] == "WHOLE_MACHINE" and item["target_part_code"] == "SYS" and item["enabled"] and item["version_id"])
        program = write("programs", {"program_code": "NATIVE-"+tag, "name": "Synthetic native QA"})
        campaign = write("campaigns", {"program_id": program["id"], "campaign_code": "NATIVE-"+tag, "name": "Synthetic native QA", "asset_kind": "WHOLE_MACHINE"})
        station = write("stations", {"site_code": "NATIVE-"+tag, "site_name": "QA", "lab_code": "NATIVE-"+tag, "lab_name": "QA", "station_code": "B-NATIVE-"+tag, "name": "Synthetic whole robot station", "station_type": "WHOLE_MACHINE"})
        contexts = []
        for sequence in (1, 2):
            asset = write("assets", {"asset_code": f"RP1.3-SYS-{sequence:03d}", "auto_number": True, "asset_kind": "WHOLE_MACHINE", "target_part_code": "SYS", "product_family": "RP1.3", "serial_number": "NATIVE-"+uuid4().hex,
                "display_name": "Synthetic whole robot", "model": "QA", "configuration_fingerprint": "NATIVE-"+uuid4().hex})
            configured = write(f"campaigns/{campaign['id']}/contexts", {"asset_id": asset["id"], "configuration_id": asset["configuration_id"], "test_case_version_id": case["version_id"], "station_id": station["id"], "cycle_code": f"NATIVE-{tag}-{sequence}"})
            contexts.append(configured["context"])
        with tempfile.TemporaryDirectory(prefix="native-domains-qa-") as temporary:
            root = Path(temporary)
            private_url = root/"readonly-source-pg.txt"
            private_url.write_text(source_ro_url.render_as_string(hide_password=False))
            private_url.chmod(0o600)
            service = NativePlatformService(root/"native")
            service.operation("configure", {"url": base, "allow_http": True})
            session = service.operation("login", {"username": accounts["author"], "password": password})["session"]
            assert session["can_submit_research"] and session["can_submit_factory"]
            setup = service.operation("setup")
            assert any(item["id"] == campaign["id"] for item in setup["campaigns"])
            assert service.operation("contexts", {"campaign_id": campaign["id"]})["items"]
            listed = service.operation("research_records", {"postgres_file": str(private_url)})["items"]
            assert {item["recording_id"] for item in listed} == {record["recording_id"] for record, _ in source_records}

            def export_record(number):
                record, _ = source_records[number]
                output = service.operation("research_export", {"postgres_file": str(private_url), "recording_id": record["recording_id"], "robot_id": record["robot_id"]})["export"]
                assert output["sample_count"] == 3 and output["high_water_sequence"] == 4
                assert hashlib.sha256(Path(output["path"]).read_bytes()).hexdigest() == output["sha256"]
                return output

            def submission(export, number):
                record, _ = source_records[number]
                return {"export_id": export["export_id"], "context": contexts[0], "test_name": "Synthetic real native-service workflow", "actual_object": "Synthetic whole robot",
                    "reason": "Local synthetic task note", "verified": True, "completed": True,
                    "verification": {"tester": "Synthetic actual tester", "station_id": station["id"], "verdict": "passed", "notes": "Synthetic human confirmation, no physical device used",
                        "declared_started_at": record["started_at"], "declared_ended_at": record["ended_at"], "declared_active_seconds": 1.5,
                        "declared_completed_cycles": 2, "declared_exception_count": 0}}

            def approve(archive):
                result = admin.call("POST", f"/api/v1/submissions/{archive['submission_id']}/review", payload={"revision": archive["submission_revision"], "decision": "APPROVED", "reason": "Synthetic central review"}, key=str(uuid4()))
                assert result["status"] == "APPROVED"
                return service.operation("research_detail", {"archive_id": archive["id"]})["archive"]

            output = export_record(0)
            request = submission(output, 0)
            pending = service.operation("research_submit", request)["archive"]
            assert pending["processing_state"] == "READY" and pending["submission_status"] == "PENDING"
            assert pending["classification"]["asset_id"] == contexts[0]["asset_id"]
            assert pending["facts"]["active_seconds"] is None and pending["current_verification"]["declared_active_seconds"] == 1.5
            approved = approve(pending)
            curve = service.client.call("GET", f"/api/v1/archives/{approved['id']}/curves?"+urlencode({"metrics": source_records[0][0]["metrics"][0]}))
            assert curve["series"][0]["unit"] == "°C"
            exact = service.client.call("GET", f"/api/v1/archives/{approved['id']}/samples?"+urlencode({"metrics": ",".join(source_records[0][0]["metrics"]), "limit": 50}))
            assert len(exact["rows"]) == 3 and exact["rows"][1][source_records[0][0]["metrics"][0]] is None
            assert [row["time_s"] for row in exact["rows"]] == [1.25, 3.25, 5.25]

            for expected, altered in (("archive_context_conflict", {**request, "context": contexts[1]}),
                                      ("archive_verification_conflict", {**request, "verification": {**request["verification"], "notes": "Different declaration"}})):
                try:
                    service.operation("research_submit", altered)
                except NativePlatformError as exc:
                    assert exc.code == expected, exc.code
                else:
                    raise AssertionError("Same-SHA relabelled evidence was accepted")
            unchanged = service.operation("research_detail", {"archive_id": approved["id"]})["archive"]
            assert unchanged["classification"] == approved["classification"] and unchanged["current_verification"] == approved["current_verification"]

            recovered = []
            for number, phase in ((1, "create"), (2, "intake")):
                exported = export_record(number)
                post = submission(exported, number)
                real_call = service.client.call
                dropped = []

                def lose_ack(method, path, **kwargs):
                    value = real_call(method, path, **kwargs)
                    target = path == "/api/v1/archive-imports" if phase == "create" else path.endswith("/verify-and-submit")
                    if method == "POST" and target and not dropped:
                        dropped.append(value["archive"]["id"] if phase == "create" else value["id"])
                        raise NativePlatformError("request_deadline_exceeded")
                    return value

                service.client.call = lose_ack
                try:
                    try:
                        service.operation("research_submit", post)
                    except NativePlatformError as exc:
                        assert exc.code == "request_deadline_exceeded", exc.code
                    else:
                        raise AssertionError("Lost ACK injection did not run")
                finally:
                    service.client.call = real_call
                assert len(dropped) == 1
                task = next(item for item in service.operation("tasks")["items"] if item.get("source_recording_id") == source_records[number][0]["recording_id"])
                assert task["status"] != "sent"
                retry = service.operation("retry", {"task_id": task["task_id"]})
                assert any(item.get("task_id") == task["task_id"] and item.get("status") == "sent" for item in retry["items"])
                assert retry["results"][0]["archive"]["id"] == dropped[0]
                task = next(item for item in service.operation("tasks")["items"] if item["task_id"] == task["task_id"])
                assert task["status"] == "sent" and task["archive_id"] == dropped[0]
                item = service.operation("research_detail", {"archive_id": task["archive_id"]})["archive"]
                assert item["submission_status"] == "PENDING" and item["submission_revision"] == 1
                assert len(item["verification_history"]) == len(item["classification_history"]) == 1
                if number == 1:
                    assert all(metric["missing"] == 0 for metric in item["manifest"]["analysis"]["metrics"])
                    assert item["manifest"]["quality_status"] == "PARTIAL"
                recovered.append(approve(item))

            source, robot, sn = "NATIVE-PC-QA", "factory-robot-qa", "000NATIVE-QA"
            with closing(FactoryStore(root/"factory-source")) as store:
                store.save_board_snapshot(sn, robot, "ros2", {})
                store.set_model_dof(sn, 24)
                store.save_estop_pairing(sn, "pass")
                row = next(item for item in store.report_rows(sn) if item["row_key"] == "SYS-012")
                store.save_report_edits(sn, [{"row_key": row["row_key"], "source_token": row["source_token"], "fields": {
                    "actual_result": "Synthetic native factory acceptance", "operator": "Synthetic tester", "verdict": "通过", "notes": "No physical device used"}}], station_id=source)
                file = Path(store.export_result_summary(sn, root/"factory-exports"))
                raw = file.read_bytes()
                receipt = service.operation("factory_submit", {"path": str(file), "source_id": source, "robot_id": robot, "source_kind": "real", "verified": True, "completed": True, "reason": "Synthetic explicit factory submission"})["receipt"]
                assert receipt["status"] == "PENDING" and receipt["formal_statistics_eligible"] is False
                detail = service.operation("factory_detail", {"archive_id": receipt["id"]})["archive"]
                assert not any(item["cases"] for item in service.client.call("GET", "/api/v1/factory/robots?"+urlencode({"source_id": source}))["items"])
                checked = service.operation("factory_review", {"archive_id": receipt["id"], "decision": "APPROVE", "expected_revision": detail["revision"], "reason": "Synthetic native factory review"})["archive"]
                assert checked["status"] == "APPROVED" and checked["formal_statistics_eligible"] is False
                cards = service.client.call("GET", "/api/v1/factory/robots?"+urlencode({"source_id": source}))["items"]
                assert {case["case_code"] for item in cards for case in item["cases"]} == {"SYS-012"}
                with httpx.Client(base_url=base, cookies=service.client.cookies, trust_env=False) as download:
                    response = download.get(f"/api/v1/factory/robot-result-archives/{receipt['id']}/file")
                    assert response.status_code == 200 and response.content == raw
                assert file.read_bytes() == raw
            assert len(recovered) == 2
            with engine.connect() as db:
                assert db.execute(text("SELECT count(*) FROM test.test_execution WHERE campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:id)"), {"id": campaign["id"]}).scalar_one() == 3
                assert db.execute(text("""SELECT count(*) FROM reliability.exposure_assessment WHERE runtime_interval_id IN
                    (SELECT ri.id FROM test.runtime_interval ri WHERE ri.campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:id))"""), {"id": campaign["id"]}).scalar_one() == 0
                assert db.execute(text("SELECT count(*) FROM reliability.current_mtbf_result WHERE campaign_id=(SELECT id FROM test.test_campaign WHERE public_id=:id)"), {"id": campaign["id"]}).scalar_one() == 0
            assert source_fingerprint() == before_source
            assert not worker_errors
            service.operation("logout")
            service.close()
        print(json.dumps({"result": "PASS", "transport": "real TCP HTTP, native operation, cookie/CSRF", "research": "read-only source PG export -> HISTORY -> real worker READY -> explicit intake -> central approve -> curves", "factory": "native 17-column export -> factory_submit -> native review -> approved SYS012 card", "faults": ["same SHA context conflict", "same SHA declaration conflict", "lost create ACK recovery", "lost intake ACK recovery"], "source_unchanged": True, "mtbf_qualified": False}), flush=True)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=5)
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        source_engine.dispose()
        engine.dispose()


if __name__ == "__main__":
    main()
