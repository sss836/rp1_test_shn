import csv
import io

import pytest

from app.services.curve_analysis import CurveAnalysisService
from app.services.monitor_recording_csv import MONITOR_JSON_LIMIT
from monitor_recording_fixture import recording_fixture, encode_recording, point_hash
from test_archives import processed
from test_factory_robot_results import result_csv


def test_real_points_metadata_units_disconnection_gaps_and_missing_are_preserved(tmp_path):
    record, rows = recording_fixture()
    raw = encode_recording(record, rows)
    storage, archive = processed(tmp_path, raw)
    manifest = archive["manifest"]
    assert storage.path(archive["execution_uuid"], "raw.csv").read_bytes() == raw
    assert manifest["monitor_recording"] == record
    assert manifest["monitor_validation"]["verified_sample_count"] == 3
    assert manifest["monitor_validation"]["high_water_sequence"] == 4
    assert manifest["monitor_validation"]["missing_sequence_count"] == 1
    assert manifest["monitor_validation"]["disconnected_rows"] == 1
    assert manifest["time_range"] == {"start": 1.25, "end": 5.25}
    assert [item["key"] for item in manifest["catalog"]] == record["metrics"]
    assert [item["unit"] for item in manifest["catalog"]] == ["°C", "N·m"]
    assert manifest["quality_status"] == "PARTIAL"
    service = CurveAnalysisService(storage)
    selection = service.select(archive, record["metrics"], [], None, None)
    samples = service.samples(selection)["rows"]
    assert samples[1][record["metrics"][0]] is None
    assert [row["time_s"] for row in samples] == [1.25, 3.25, 5.25]
    assert archive["facts"]["active_seconds"] == 2, "recorded span must not replace supplied acquisition facts"


@pytest.mark.parametrize("kind", ["simulated", "other", None])
def test_non_ros2_session_cannot_become_ready(tmp_path, kind):
    record, rows = recording_fixture(kind)
    with pytest.raises(ValueError, match="真实ros2"):
        processed(tmp_path, encode_recording(record, rows))


@pytest.mark.parametrize("damage", ["hash", "value", "missing_zero", "sample", "sequence", "offset", "sample_time", "count", "watermark", "changed_metadata", "simulated_point", "none_connected", "bool_value", "metrics_order", "missing_first_metadata", "time_basis", "connected_value", "non_monotonic"])
def test_tampered_evidence_is_rejected(tmp_path, damage):
    record, rows = recording_fixture()
    if damage == "hash": rows[0][3]["payload_hash"] = "0"*64
    elif damage == "value": rows[0][4] = "999"
    elif damage == "missing_zero": rows[1][4] = "0"
    elif damage == "sample": rows[0][1] = "2"
    elif damage == "sequence":
        rows[1][1] = "1"; rows[1][3]["sequence"] = 1
    elif damage == "offset": rows[0][0] = "0"
    elif damage == "sample_time": rows[0][3]["payload"]["observed_at"] = record["started_at"]
    elif damage == "count": record["sample_count"] = 4
    elif damage == "watermark": record["high_water_sequence"] = 5
    elif damage == "changed_metadata": rows[1][2] = {**record, "robot_id": "another"}
    elif damage == "simulated_point": rows[0][3]["payload"]["source_kind"] = "simulated"
    elif damage == "none_connected": rows[0][3]["payload"]["source_kind"] = None
    elif damage == "bool_value": rows[0][3]["payload"]["values"][record["metrics"][0]] = True; rows[0][4] = "1"
    elif damage == "metrics_order": record["metrics"] = list(reversed(record["metrics"]))
    elif damage == "missing_first_metadata": rows[0][2] = None
    elif damage == "time_basis": rows[0][3]["payload"]["time_basis"] = "device_time"
    elif damage == "connected_value": rows[0][3]["payload"]["connected"] = False
    elif damage == "non_monotonic":
        rows[1][0] = rows[0][0]; rows[1][3]["observed_at"] = rows[0][3]["observed_at"]
        rows[1][3]["payload"]["observed_at"] = rows[0][3]["observed_at"]
        rows[1][3]["payload_hash"] = point_hash(rows[1][3]["payload"])
    if damage in {"sample_time", "simulated_point", "none_connected", "bool_value", "time_basis", "connected_value"}:
        rows[0][3]["payload_hash"] = point_hash(rows[0][3]["payload"])
    with pytest.raises(ValueError):
        processed(tmp_path, encode_recording(record, rows, fields=["time_s", "sample", "rp1_recording_json", "rp1_sample_json", "joint.0.motor_temperature", "joint.0.torque"]))


def test_large_bounded_metadata_is_accepted_but_ordinary_limits_remain(tmp_path):
    record, rows = recording_fixture()
    record["source_context"]["large_note"] = "x"*6000
    _, archive = processed(tmp_path/"accepted", encode_recording(record, rows))
    assert len(archive["manifest"]["monitor_recording"]["source_context"]["large_note"]) == 6000
    record["source_context"]["large_note"] = "x"*MONITOR_JSON_LIMIT
    with pytest.raises((ValueError, csv.Error)):
        processed(tmp_path/"too-large", encode_recording(record, rows))
    with pytest.raises(ValueError, match="普通字段"):
        processed(tmp_path/"ordinary", b"time_s,joint_pos_rad\n0,"+b"1"*4097+b"\n")


def test_missing_units_are_marked_without_suffix_guessing_and_17_columns_rejected(tmp_path):
    record, rows = recording_fixture()
    record["source_context"]["catalog"] = []
    _, archive = processed(tmp_path/"units", encode_recording(record, rows))
    assert all(item["unit"] == "" and item["unit_source"] == "not_provided" for item in archive["manifest"]["catalog"])
    with pytest.raises(ValueError, match="秒时间列"):
        processed(tmp_path/"factory", result_csv())


def test_gap_count_counts_missing_sequences_without_inserting_points(tmp_path):
    record, rows = recording_fixture()
    rows[1][1] = "5"; rows[1][3]["sequence"] = 5
    rows[2][1] = "6"; rows[2][3]["sequence"] = 6
    record["high_water_sequence"] = 6
    _, archive = processed(tmp_path, encode_recording(record, rows))
    validation = archive["manifest"]["monitor_validation"]
    assert validation["verified_sample_count"] == 3
    assert validation["sequence_gap_count"] == validation["missing_sequence_count"] == 3
    assert validation["sequence_gap_events"] == 1


def test_physical_line_budget_counts_utf8_bytes_and_repeated_metadata_is_exact(tmp_path):
    from app.services.curve_query import bounded_lines
    with pytest.raises(ValueError, match="单行过长"):
        list(bounded_lines(io.StringIO("中"*5), limit=12))
    record, rows = recording_fixture()
    rows[1][2] = {**record, "sample_count": 3.0}
    with pytest.raises(ValueError, match="元数据与首行不一致"):
        processed(tmp_path, encode_recording(record, rows))


def test_sequence_gaps_are_partial_even_when_all_numeric_columns_are_present(tmp_path):
    record, rows = recording_fixture()
    disconnected = rows[1][3]["payload"]
    disconnected["connected"] = True
    disconnected["source_kind"] = "ros2"
    disconnected["missing_reasons"] = {}
    disconnected["values"] = dict(zip(record["metrics"], (22.0, -0.5)))
    rows[1][3]["payload_hash"] = point_hash(disconnected)
    rows[1][4:] = ["22.0", "-0.5"]
    _, archive = processed(tmp_path, encode_recording(record, rows))
    manifest = archive["manifest"]
    assert all(metric["missing"] == 0 for metric in manifest["analysis"]["metrics"])
    assert manifest["analysis"]["quality"]["monitor_missing_sequence_count"] == 1
    assert manifest["quality_status"] == "PARTIAL"
    assert manifest["row_count"] == 3 and archive["facts"]["active_seconds"] == 2
