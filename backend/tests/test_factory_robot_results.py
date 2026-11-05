import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
from uuid import uuid4

import pytest

from app.core.auth import factory_route_allowed
from app.core.errors import ApiError
from app.schemas.factory_robots import FactoryRobotHeartbeat, FactoryRobotUpload
from app.services.factory_robot_results import FactoryRobotResultStorage, parse_result_csv
from app.test_record_csv import FIELDS


def result_row(**changes):
    values = dict(zip(FIELDS, ("" for _ in FIELDS)))
    values.update({"记录编号": str(uuid4()), "上位机编号": "PC-QA", "样品编号": "00001234",
                   "机型": "24轴", "测试体系": "SYS", "测试分类": "主控", "记录层级": "项目",
                   "测试项目编号": "SYS-012", "原始检查ID": "remote_estop_pairing",
                   "测试内容": "远程急停开关对频测试", "实际结果": "已人工确认，本次未触发任何设备命令。",
                   "通过标准": "合成测试夹具标准", "是否通过": "通过"})
    values.update(changes)
    return [values[field] for field in FIELDS]


def result_csv(*rows, fields=FIELDS):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(fields); writer.writerows(rows or [result_row()])
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def test_17_cells_parent_child_and_unknown_times_are_preserved():
    parent = result_row(**{"测试项目编号": "SYS-001", "原始检查ID": "BASIC_CHECKS", "是否通过": "未测试"})
    child = result_row(**{"测试项目编号": "SYS-001", "记录层级": "子项", "原始检查ID": "RP001", "测试内容": "主控温区"})
    parsed = parse_result_csv(result_csv(parent, child), "PC-QA", "real")
    assert parsed["serial_number"] == "00001234"
    assert parsed["columns"] == list(FIELDS)
    assert parsed["rows"][1]["values"] == child
    assert parsed["rows"][0]["values"][10:12] == ["", ""]


@pytest.mark.parametrize("changes", [
    {"上位机编号": "OTHER-PC"}, {"样品编号": " 00001234"}, {"机型": "未知"},
    {"测试项目编号": "SYS-REL-001"}, {"测试体系": "UNIT-AGING", "测试项目编号": "UNIT-AGING-001"},
    {"是否通过": "完成"}, {"记录层级": "子项"}, {"记录编号": ""},
    {"开始时间": "not-a-time"},
    {"开始时间": "2026-01-03T00:00:00+08:00", "结束时间": "2026-01-02T00:00:00+08:00"},
    {"结束时间": (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()},
    {"机型": "28轴", "测试项目编号": "SYS-011", "原始检查ID": "stunt"},
])
def test_invalid_identities_domains_states_and_times_are_rejected(changes):
    with pytest.raises(ApiError):
        parse_result_csv(result_csv(result_row(**changes)), "PC-QA", "real")


def test_mixed_sn_duplicate_ids_wrong_header_bad_utf8_and_simulation_fail_closed():
    row = result_row()
    cases = [result_csv(row, result_row(**{"样品编号": "ANOTHER"})), result_csv(row, row),
             result_csv(row, fields=list(reversed(FIELDS))), b"\xff\xfe", b"time_s,value\n0,1\n",
             result_csv(result_row(**{"备注": "数据来源：simulated（模拟数据，不代表实机验收）"}))]
    for body in cases:
        with pytest.raises(ApiError):
            parse_result_csv(body, "PC-QA", "real")
    assert parse_result_csv(cases[-1], "PC-QA", "simulated")["serial_number"] == "00001234"


def test_verification_completion_and_presence_source_contract():
    body = result_csv()
    metadata = dict(source_id="PC-QA", robot_id="qa-robot", source_kind="real", filename="results.csv",
                    sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), verified=True, completed=True, reason="人工核实后提交")
    assert FactoryRobotUpload(**metadata).verified
    for key in ("verified", "completed"):
        with pytest.raises(ValueError): FactoryRobotUpload(**{**metadata, key: False})
    with pytest.raises(ValueError): FactoryRobotUpload(**{**metadata, "filename": "../results.csv"})
    with pytest.raises(ValueError): FactoryRobotHeartbeat(protocol="reliability_v1",source_id="PC-QA",source_kind="real",robots=[
        dict(robot_id="robot-a",serial_number="SN",model="24轴",station_name="QA",online=True),
        dict(robot_id="robot-b",serial_number="SN",model="24轴",station_name="QA",online=True)])


def test_immutable_file_retries_and_hash_review(tmp_path):
    store = FactoryRobotResultStorage(tmp_path)
    body, reference = result_csv(), uuid4()
    digest = hashlib.sha256(body).hexdigest()
    store.put(reference, body, digest); store.put(reference, body, digest)
    assert store.read(reference, len(body), digest) == body
    with pytest.raises(ApiError): store.read(reference, len(body), "0"*64)
    with pytest.raises(ApiError): store.put(reference, body+b"x", digest)


def test_reviewer_only_gets_new_review_write_route():
    path = f"/api/v1/factory/robot-result-archives/{uuid4()}/review"
    assert factory_route_allowed("POST", path)
    assert not factory_route_allowed("POST", "/api/v1/factory/robots/presence")
    assert not factory_route_allowed("POST", "/api/v1/factory/robot-result-archives")
