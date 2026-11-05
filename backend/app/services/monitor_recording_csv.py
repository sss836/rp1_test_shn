"""Validate exported monitor points without turning observations into exposure."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
from uuid import UUID

MONITOR_PREFIX = ["time_s", "sample", "rp1_recording_json", "rp1_sample_json"]
MONITOR_JSON_FIELDS = frozenset(MONITOR_PREFIX[2:])
MONITOR_JSON_LIMIT = 256 * 1024
ORDINARY_FIELD_LIMIT = 4096
MAX_SEQUENCE = 9223372036854775807


def check_field_sizes(row, fields):
    for key, value in zip(fields, row):
        limit = MONITOR_JSON_LIMIT if key in MONITOR_JSON_FIELDS else ORDINARY_FIELD_LIMIT
        if len(value) > limit or (key in MONITOR_JSON_FIELDS and len(value.encode("utf-8")) > limit):
            raise ValueError("CSV 字段过长；监控证据JSON上限256KiB，普通字段上限4096字符")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("监控证据JSON含重复字段")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("监控证据JSON不得包含NaN或Infinity")


def _json(raw):
    try:
        result = json.loads(raw, object_pairs_hook=_object, parse_constant=_nonfinite)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("监控证据JSON无效") from exc
    if not isinstance(result, dict):
        raise ValueError("监控证据JSON必须是对象")
    return result


def _stamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValueError("监控记录必须保留带时区的真实观测时间")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone required")
        return result
    except (ValueError, OverflowError) as exc:
        raise ValueError("监控记录必须保留带时区的真实观测时间") from exc


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_SEQUENCE:
        raise ValueError("监控采样数量和序号必须为正整数")
    return value


def _numeric(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("监控曲线值必须为数值或缺失，不能用布尔值代替")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("监控曲线值超出数值范围") from exc
    if not math.isfinite(result) or abs(result) > 1e100:
        raise ValueError("监控曲线值必须有限且绝对值不超过1e100")
    return result


class MonitorRecordingCsv:
    def __init__(self, fields):
        if fields[:4] != MONITOR_PREFIX or not 1 <= len(fields[4:]) <= 256:
            raise ValueError("监控原点CSV必须使用固定前四列及1至256个指标列")
        self.fields = fields
        self.metrics = fields[4:]
        if set(self.metrics) & {"stage", "stage_code", "cycle", "cycle_no", "cycle_index", *MONITOR_PREFIX}:
            raise ValueError("监控指标键与保留字段冲突")
        self.recording = None
        self.previous_sequence = 0
        self.rows = self.sequence_gap_events = self.missing_sequence_count = self.disconnected_rows = 0
        self.previous_offset = None
        self.first_observed_at = self.last_observed_at = None

    def _recording(self, raw):
        if not raw:
            if self.recording is None:
                raise ValueError("监控CSV第一行必须包含完整记录元数据")
            return
        record = _json(raw)
        if self.recording is not None:
            if json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False) != self.recording_canonical:
                raise ValueError("监控CSV后续记录元数据与首行不一致")
            return
        required = {"schema_version", "recording_id", "robot_id", "source_kind", "started_at", "ended_at", "metrics", "source_context", "sample_count", "high_water_sequence"}
        if not required <= record.keys() or record.get("schema_version") != "rp1-monitor-recording-v1":
            raise ValueError("监控记录元数据缺失或版本不支持")
        if record["source_kind"] != "ros2":
            raise ValueError("仅真实ros2监控原点可用于研发归档；模拟或其他来源不能送审")
        try:
            UUID(record["recording_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("监控recording_id必须是原始记录UUID") from exc
        if not isinstance(record["robot_id"], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", record["robot_id"]) is None:
            raise ValueError("监控robot_id无效")
        if record["metrics"] != self.metrics or not isinstance(record["source_context"], dict):
            raise ValueError("监控指标目录与CSV列顺序或来源上下文不一致")
        self.started_at, self.ended_at = _stamp(record["started_at"]), _stamp(record["ended_at"])
        if self.ended_at < self.started_at or self.ended_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("监控记录必须已真实结束，且结束时间不得在未来")
        _positive_int(record["sample_count"])
        if _positive_int(record["high_water_sequence"]) < record["sample_count"]:
            raise ValueError("监控记录高水位序号小于原点数量")
        source_catalog = record["source_context"].get("catalog", [])
        if not isinstance(source_catalog, list):
            raise ValueError("监控来源单位目录必须为列表")
        self.source_catalog = {}
        for item in source_catalog:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str) or item["key"] in self.source_catalog:
                raise ValueError("监控来源单位目录含无效或重复指标")
            if "unit" in item and not isinstance(item["unit"], str):
                raise ValueError("监控来源单位必须明确为字符串")
            self.source_catalog[item["key"]] = item
        self.recording_canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.recording = record

    def add(self, row):
        self._recording(row[2])
        sample = _json(row[3])
        if set(sample) != {"sequence", "observed_at", "payload", "payload_hash"}:
            raise ValueError("监控原点必须保留sequence、observed_at、payload和payload_hash")
        sequence = _positive_int(sample["sequence"])
        if re.fullmatch(r"[1-9][0-9]*", row[1]) is None or int(row[1]) != sequence or sequence <= self.previous_sequence:
            raise ValueError("监控sample与原始sequence不一致或序号重复倒序")
        gap = sequence - self.previous_sequence - 1
        self.sequence_gap_events += int(gap > 0)
        self.missing_sequence_count += gap
        observed = _stamp(sample["observed_at"])
        if not self.started_at <= observed <= self.ended_at:
            raise ValueError("监控原点时间不在原记录起止范围内")
        try:
            offset = float(row[0])
        except ValueError as exc:
            raise ValueError("监控time_s必须保留真实时间偏移") from exc
        if not math.isfinite(offset) or not 0 <= offset <= 315360000 or not math.isclose(offset, (observed-self.started_at).total_seconds(), rel_tol=0, abs_tol=1e-6):
            raise ValueError("监控time_s与原始observed_at时间偏移不一致")
        if self.previous_offset is not None and offset <= self.previous_offset:
            raise ValueError("监控原点观测时间重复或倒序；不能修复或重排来源事实")
        payload = sample["payload"]
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), dict):
            raise ValueError("监控原点必须保留完整payload及values")
        try:
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("监控原点payload不能规范化校验") from exc
        if sample["payload_hash"] != digest:
            raise ValueError("监控原点payload_hash不符，拒绝生成查询副本")
        if _stamp(payload.get("observed_at")) != observed:
            raise ValueError("监控原点内外层observed_at不一致")
        if payload.get("time_basis") != "pc_sampled":
            raise ValueError("监控原点必须保留pc_sampled时间依据，不能冒充设备采样时间")
        if not isinstance(payload.get("connected"), bool):
            raise ValueError("监控原点必须保留真实connected状态")
        if payload["connected"] is False and any(value is not None for value in payload["values"].values()):
            raise ValueError("断线监控原点必须保留缺失值，不能携带伪造数值")
        if payload.get("source_kind") is None:
            if payload["connected"] is not False or any(value is not None for value in payload["values"].values()):
                raise ValueError("缺失来源只允许断线且全部值缺失的原点")
            self.disconnected_rows += 1
        elif payload["source_kind"] != "ros2":
            raise ValueError("研发监控原点含模拟或不一致的数据来源")
        for index, key in enumerate(self.metrics, 4):
            expected = _numeric(payload["values"].get(key))
            if expected is None:
                if row[index] != "":
                    raise ValueError("监控缺失值必须保留为空，不能补零或填充")
            else:
                try:
                    actual = float(row[index])
                except ValueError as exc:
                    raise ValueError("监控数值列不是数值") from exc
                if not math.isfinite(actual) or actual != expected:
                    raise ValueError("监控数值列与原点payload.values不一致")
        self.rows += 1
        self.previous_sequence = sequence
        self.previous_offset = offset
        self.first_observed_at = sample["observed_at"] if self.first_observed_at is None else self.first_observed_at
        self.last_observed_at = sample["observed_at"]

    def finish(self):
        if self.recording is None or self.rows != self.recording["sample_count"] or self.previous_sequence != self.recording["high_water_sequence"]:
            raise ValueError("监控原点数量或高水位序号与封存元数据不一致")
        return {"verified_sample_count": self.rows, "high_water_sequence": self.previous_sequence,
                "sequence_gap_count": self.missing_sequence_count, "sequence_gap_events": self.sequence_gap_events,
                "missing_sequence_count": self.missing_sequence_count,
                "disconnected_rows": self.disconnected_rows, "first_observed_at": self.first_observed_at,
                "last_observed_at": self.last_observed_at, "payload_hashes_verified": True,
                "time_basis": "pc_sampled", "scope": "OBSERVATION_ONLY_NOT_QUALIFIED_EXPOSURE"}

    def catalog(self):
        result = []
        for key in self.metrics:
            original = self.source_catalog.get(key, {})
            result.append({**original, "key": key, "metric": key, "subject": key.rsplit(".", 1)[0] if "." in key else "recording",
                           "unit": original.get("unit", ""), "unit_source": "source_catalog" if "unit" in original else "not_provided"})
        return result
