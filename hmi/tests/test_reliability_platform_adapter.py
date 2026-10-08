from __future__ import annotations

import tempfile
from pathlib import Path

from factory_hmi.core.report import ReportManager
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import (
    ReliabilityUploaderWorker,
    build_telemetry_batches,
    load_test_profiles,
)


def test_profile_loader_marks_unfrozen_database_context(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(
        """
profiles:
  - key: pending
    label: 待冻结
    campaign_id: null
    stage_code: AGING
""",
        encoding="utf-8",
    )
    profiles = load_test_profiles(path)
    assert profiles[0]["ready"] is False
    assert "campaign_id" in profiles[0]["missing"]
    assert "station_id" in profiles[0]["missing"]


def test_raw_csv_is_mapped_to_current_ingestion_telemetry_contract() -> None:
    rows = [
        {
            "time_s": "0",
            "hip_cmd_pos_rad": "0",
            "hip_pos_rad": "0.1",
            "hip_torque_nm": "2.5",
            "hip_temp_c": "31",
        },
        {
            "time_s": "0.05",
            "hip_cmd_pos_rad": "0.2",
            "hip_pos_rad": "0.15",
            "hip_torque_nm": "2.8",
            "hip_temp_c": "31.2",
        },
    ]
    batches = build_telemetry_batches(
        rows,
        manifest={
            "started_at": "2026-09-22T00:00:00Z",
            "subject_map": {"hip": "UPPER-HIP"},
        },
        stage_id="00000000-0000-7000-8000-000000000001",
        producer_key="run-001",
    )
    assert len(batches) == 1
    series = batches[0]["series"]
    assert {item["metric_code"] for item in series} == {
        "JOINT-TARGET-POSITION",
        "JOINT-ACTUAL-POSITION",
        "JOINT-TORQUE",
        "JOINT-TEMPERATURE",
    }
    actual = next(
        item for item in series if item["metric_code"] == "JOINT-ACTUAL-POSITION"
    )
    assert actual["canonical_unit"] == "deg"
    assert 5.72 < actual["points"][0]["value"] < 5.74
    assert actual["sampling_interval_ms"] == 50.0


class _FakeReliabilityClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def authenticate_operator(self, username: str, _password: str) -> dict:
        return {
            "user": {"username": username},
            "expires_at": "2026-09-23T00:00:00Z",
        }

    def register_execution(self, payload, _key):
        self.calls.append(("register", dict(payload)))
        return {
            "execution_id": "00000000-0000-7000-8000-000000000011",
            "stage_id": "00000000-0000-7000-8000-000000000012",
        }

    def telemetry(self, _producer, payload, _key):
        self.calls.append(("telemetry", dict(payload)))
        return {"status": "RUNNING"}

    def events(self, _producer, payload, _key):
        self.calls.append(("events", dict(payload)))
        return {"status": "RUNNING"}

    def artifact(self, _producer, payload, _key):
        self.calls.append(("artifact", dict(payload)))
        return {"status": "RUNNING"}

    def finish(self, _producer, payload, _key):
        self.calls.append(("finish", dict(payload)))
        return {"status": "COMPLETED"}


def test_reliability_worker_registers_telemetry_event_artifact_and_finish() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        csv_path = root / "source.csv"
        csv_path.write_text(
            "time_s,hip_cmd_pos_rad,hip_pos_rad,hip_torque_nm,hip_temp_c,hip_error_id\n"
            "0,0,0.1,2.5,31,0\n"
            "1,0.2,0.15,2.8,31.2,0\n",
            encoding="utf-8",
        )
        ids = {
            "campaign_id": "00000000-0000-7000-8000-000000000001",
            "cycle_id": "00000000-0000-7000-8000-000000000002",
            "segment_id": "00000000-0000-7000-8000-000000000003",
            "asset_id": "00000000-0000-7000-8000-000000000004",
            "configuration_id": "00000000-0000-7000-8000-000000000005",
            "test_case_version_id": "00000000-0000-7000-8000-000000000006",
            "station_id": "00000000-0000-7000-8000-000000000007",
        }
        result = ReportManager(root / "data").finalize(
            csv_path,
            {
                "execution_uuid": "11111111-1111-4111-8111-111111111111",
                "test_id": "upper-aging_2609220000_RP1.3-UPPER-01_MP-01",
                "test_case_id": "upper-aging",
                "test_case_version": "1",
                "sample_id": "RP1.3-UPPER-01",
                "bench_id": "MP-01",
                "operator_id": "alice",
                "started_at": "2026-09-22T00:00:00Z",
                "ended_at": "2026-09-22T00:00:01Z",
                "active_seconds": 1,
                "completed_cycles": 1,
                "record_rate_hz": 1,
                "outcome": "completed",
                "entries": [
                    {"joint_name": "hip", "motor_id": 1, "module_name": "upper"}
                ],
                "subject_map": {"hip": "UPPER-HIP"},
                **ids,
                "config": {
                    "factory_hmi_acceptance": {
                        "criteria_version": "factory-v1",
                        "required_active_seconds": 1,
                        "required_cycles": 1,
                        "min_sample_completeness": 1,
                        "max_gap_seconds": 1,
                        "max_temperature_rise_c": 10,
                        "max_torque_p999_utilization": 1,
                        "max_tracking_rms_rad": 1,
                        "rated_torque_nm": {"hip": 10},
                    }
                },
            },
        )
        outbox = Outbox(root / "outbox.sqlite3")
        outbox.enqueue(result.manifest, result.bundle_path)
        client = _FakeReliabilityClient()
        worker = ReliabilityUploaderWorker(outbox, client)  # type: ignore[arg-type]
        worker.authenticate("alice", "password")

        assert worker.process_once() == 1
        kinds = [name for name, _ in client.calls]
        assert kinds[0] == "register"
        assert "telemetry" in kinds
        assert kinds[-3:] == ["events", "artifact", "finish"]
        item = outbox.get(result.manifest["execution_uuid"])
        assert item is not None
        assert item.state == "approved"
        artifact = next(payload for name, payload in client.calls if name == "artifact")
        assert artifact["availability_status"] == "MISSING"
        assert "binary object upload endpoint is not available" in artifact["metadata"]["reason"]
