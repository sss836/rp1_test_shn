"""Synthetic wire fixtures shared with native-client integration tests."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from uuid import uuid4

from app.services.monitor_recording_csv import MONITOR_PREFIX


def point_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def recording_fixture(source_kind="ros2"):
    start = datetime.now(timezone.utc)-timedelta(minutes=10)
    metrics = ["joint.0.motor_temperature", "joint.0.torque"]
    record = {"schema_version": "rp1-monitor-recording-v1", "recording_id": str(uuid4()), "robot_id": "robot-qa",
        "source_kind": source_kind, "started_at": start.isoformat(), "ended_at": (start+timedelta(seconds=6)).isoformat(),
        "metrics": metrics, "source_context": {"catalog": [{"key": metrics[0], "label": "原始温度", "unit": "°C", "aggregation": "range"}, {"key": metrics[1], "label": "原始力矩", "unit": "N·m"}], "device_profile": {"note": "synthetic QA"}},
        "sample_count": 3, "high_water_sequence": 4, "run_number": "SYNTHETIC-ONLY"}
    rows = []
    for sequence, offset, disconnected in ((1, 1.25, False), (3, 3.25, True), (4, 5.25, False)):
        observed = (start+timedelta(seconds=offset)).isoformat()
        values = dict(zip(metrics, (None, None) if disconnected else (20.0+sequence, -0.25*sequence)))
        payload = {"values": values, "observed_at": observed, "source_kind": None if disconnected else source_kind,
            "connected": not disconnected, "ages_ms": {key: None if disconnected else 0 for key in metrics},
            "source_at": None if disconnected else observed, "stream_id": None if disconnected else "qa-stream",
            "time_basis": "pc_sampled", "clock_quality": "source_time_unverified", "configuration_revision": 1,
            "missing_reasons": {key: "disconnected_or_wrong_robot" for key in metrics} if disconnected else {}}
        sample = {"sequence": sequence, "observed_at": observed, "payload": payload, "payload_hash": point_hash(payload)}
        rows.append([repr(offset), str(sequence), record if not rows else None, sample, *["" if value is None else repr(value) for value in values.values()]])
    return record, rows


def encode_recording(record, rows, fields=None):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(fields or [*MONITOR_PREFIX, *record["metrics"]])
    for row in rows:
        writer.writerow([*row[:2], json.dumps(row[2], ensure_ascii=False) if row[2] is not None else "", json.dumps(row[3], ensure_ascii=False), *row[4:]])
    return b"\xef\xbb\xbf"+stream.getvalue().encode("utf-8")
