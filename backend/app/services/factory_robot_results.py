"""Bounded 17-column factory summary intake. Never invokes waveform parsers."""
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import io
import os
from pathlib import Path
import re
import tempfile
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.errors import ApiError
from app.test_record_csv import FIELDS


MAX_CSV_BYTES = 4 * 1024**2
MAX_ROWS = 1000
MAX_FIELD_CHARS = 65536
VERDICTS = {"未测试", "通过", "不通过", "待复核", "中断", "不适用"}
SYS_NAMES = {
    "SYS-001": "基础自检（17项）", "SYS-002": "灯板点亮确认",
    "SYS-003": "电机固件版本核查", "SYS-004": "电机参数核查",
    "SYS-005": "整机标定", "SYS-006": "标定检查", "SYS-007": "远程急停",
    "SYS-008": "热换电", "SYS-009": "站立", "SYS-010": "行走／跑步",
    "SYS-011": "UFO特技", "SYS-012": "远程急停开关对频测试",
}
BASIC_IDS = {"RP001", "RP002", "RP003", "RP005", "RP007", "RP008", "RP011", "RP012",
             "RP013", "RP020", "RP021", "RP022", "RP029", "RP034", "IMU_CONTINUOUS",
             "IMU_FIRMWARE", "BMS_RECOVERY"}
SIMULATED = re.compile(r"simulated|simulation|数据来源[：:]\s*模拟", re.IGNORECASE)


def invalid(message, code="invalid_factory_result_csv"):
    raise ApiError(422, code, message)


def _time(value, line):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        invalid(f"CSV第{line}行含非法起止时间；未知时间请保留空白，不得编造。")
    now = datetime.now(timezone.utc) if result.tzinfo else datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    if result > now + timedelta(minutes=5):
        invalid(f"CSV第{line}行测试时间在未来，不能声明测试已结束。")
    return result


def parse_result_csv(body: bytes, source_id: str, source_kind: str) -> dict:
    """Preserve all cells as strings, including leading zeroes and missing times."""
    if not body or len(body) > MAX_CSV_BYTES:
        raise ApiError(413, "factory_result_too_large", "工厂结果CSV大小必须介于1字节与4MiB。")
    try:
        decoded = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        invalid("工厂结果CSV必须使用UTF-8编码。")
    if "\x00" in decoded:
        invalid("CSV不能包含NUL字节。")
    # The original exporter keeps simulated provenance even after editing notes.
    if source_kind == "real" and SIMULATED.search(decoded):
        invalid("CSV含模拟来源标识，不能按实机来源提交。", "simulated_source_mismatch")
    csv.field_size_limit(MAX_FIELD_CHARS)
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    try:
        if next(reader, None) != list(FIELDS):
            invalid("仅接受原顺序的17列工厂结果汇总CSV；不能上传采样曲线或旧摘要。")
        rows, identities, samples, models = [], set(), set(), set()
        for values in reader:
            line = reader.line_num
            if len(rows) >= MAX_ROWS:
                invalid("工厂结果CSV最多1000行记录。")
            if len(values) != len(FIELDS):
                invalid(f"CSV第{line}行不是17列。")
            if any(len(v) > MAX_FIELD_CHARS for v in values):
                invalid(f"CSV第{line}行字段过长。")
            record_id, station, sn, model, suite, _, level, code, original, name, start, end, _, _, _, verdict, notes = values
            if not record_id or len(record_id) > 200 or record_id != record_id.strip() or any(ord(c) < 32 for c in record_id):
                invalid(f"CSV第{line}行必须填写稳定记录编号；空模板不作为已测记录上传。")
            if record_id in identities:
                invalid("同一文件出现重复记录编号；父子项与复测须使用各自记录编号。")
            identities.add(record_id)
            if station != source_id:
                invalid(f"CSV第{line}行上位机编号与登录来源不一致。")
            if not sn or len(sn) > 200 or sn != sn.strip() or any(ord(c) < 32 for c in sn):
                invalid(f"CSV第{line}行样品SN无效。")
            if model not in {"24轴", "28轴"}:
                invalid(f"CSV第{line}行必须保留实际24轴或28轴机型。")
            samples.add(sn); models.add(model)
            if len(samples) > 1 or len(models) > 1:
                invalid("每次归档只接受一个明确SN和机型，不能合并不同样品。")
            if level not in {"项目", "子项"} or verdict not in VERDICTS or not name.strip() or len(name) > 500:
                invalid(f"CSV第{line}行层级、结论或测试内容无效。")
            if suite == "SYS":
                if code not in SYS_NAMES:
                    invalid(f"CSV第{line}行不是当前工厂SYS-001至SYS-012项目；SYS-REL目录不能混用。")
                if level == "子项" and not ((code == "SYS-001" and original in BASIC_IDS) or (code == "SYS-010" and original in {"walking", "running"})):
                    invalid(f"CSV第{line}行子项来源ID无效。")
                if model == "28轴" and code == "SYS-011" and verdict not in {"未测试", "不适用"}:
                    invalid("28轴UFO特技应按实物填不适用，不能填写通过等结论。")
            else:
                invalid(f"CSV第{line}行不是SYS整机工厂汇总；独立体系须沿用各自正式归档流程。")
            starts, ends = _time(start, line), _time(end, line)
            if starts and ends:
                try:
                    if ends < starts:
                        invalid(f"CSV第{line}行结束时间早于开始时间。")
                except TypeError:
                    invalid(f"CSV第{line}行起止时间时区表达不一致。")
            rows.append({"record_id": record_id, "values": values})
    except csv.Error:
        invalid("CSV语法错误或字段超出长度预算。")
    if not rows:
        invalid("CSV没有记录行。")
    return {"columns": list(FIELDS), "rows": rows, "serial_number": next(iter(samples)), "model": next(iter(models))}


class FactoryRobotResultStorage:
    """A distinct immutable file namespace below the external archive root."""
    def __init__(self, root):
        root = Path(root)
        if not root.is_absolute() or root.is_symlink():
            raise ValueError("ARCHIVE_ROOT must be an absolute non-symlink directory")
        self.root = root / "factory-robot-results"
        if self.root.is_symlink():
            raise ValueError("factory result root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)

    def path(self, public_id):
        path = self.root / (str(UUID(str(public_id))) + ".csv")
        if path.is_symlink():
            raise ApiError(409, "factory_result_storage_conflict", "工厂结果原件路径无效。")
        return path

    def put(self, public_id, body, digest):
        target = self.path(public_id)
        if target.exists():
            if self.read(public_id, len(body), digest) != body:
                raise ApiError(409, "factory_result_content_conflict", "已有工厂结果原件不可覆盖。")
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".result-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(body); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o440)
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(public_id, len(body), digest)
            descriptor = os.open(self.root, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read(self, public_id, size, digest):
        try:
            with self.path(public_id).open("rb") as handle:
                data = handle.read(MAX_CSV_BYTES + 1)
        except FileNotFoundError as exc:
            raise ApiError(409, "factory_result_file_missing", "工厂结果原件缺失，不能下载或批准；请恢复一致备份。") from exc
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ApiError(409, "factory_result_file_corrupt", "工厂结果原件大小或哈希不符，不能下载或批准。")
        return data
