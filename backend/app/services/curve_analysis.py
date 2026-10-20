"""Read-only range inspection of full-resolution telemetry, never min/max pyramids.

This is exploratory inspection, not an approval or a new formal analysis version.
Every caller must first resolve the archive through ArchiveRepository.get (RLS).
"""
from contextlib import ExitStack, contextmanager
import csv
from dataclasses import dataclass, field
import io
import math
from pathlib import Path
from uuid import UUID

import duckdb

from app.core.errors import ApiError
from app.services.archive_storage import ArchiveStorage
from app.services.curve_query import MAX_SERIES, connection, quoted


MAX_TIME_SECONDS = 315360000
MAX_EXPORT_ROWS = 250000
MAX_SAMPLE_ROWS = 500
FETCH_BATCH_ROWS = 2048


def numeric(value):
    """Keep missing/nonfinite measurements missing, including in JSON and CSV."""
    return value if value is not None and math.isfinite(value) else None


def csv_text(value):
    """Neutralize spreadsheet formulas in text only; numeric negatives stay numeric."""
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@dataclass
class AccurateSum:
    # Nonoverlapping partial sums retain cancellation residuals (unlike naive
    # running averages). Parser bounds measurements to 1e100, derived differences
    # to 2e100; even the maximum archive size cannot overflow their sum.
    partials: list[float] = field(default_factory=list)

    def add(self, value):
        index = 0
        for other in self.partials:
            if abs(value) < abs(other):
                value, other = other, value
            high = value + other
            low = other - (high - value)
            if low:
                self.partials[index] = low
                index += 1
            value = high
        self.partials[index:] = [value]

    def total(self):
        return math.fsum(self.partials)


@dataclass
class RawStatistic:
    count: int = 0
    missing: int = 0
    minimum: float | None = None
    maximum: float | None = None
    first: float | None = None
    last: float | None = None
    total: AccurateSum = field(default_factory=AccurateSum)
    squares: AccurateSum = field(default_factory=AccurateSum)
    scale: float = 0.0

    def add(self, value):
        value = numeric(value)
        if value is None:
            self.missing += 1
            return
        self.count += 1
        self.total.add(value)
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if self.first is None:
            self.first = value
        self.last = value
        magnitude = abs(value)
        if magnitude > self.scale:
            factor = (self.scale / magnitude) ** 2
            self.squares = AccurateSum([part * factor for part in self.squares.partials])
            self.scale = magnitude
        if self.scale:
            self.squares.add((value / self.scale) ** 2)

    def result(self):
        return {"count": self.count, "missing": self.missing,
                "min": self.minimum, "max": self.maximum,
                "mean": self.total.total() / self.count if self.count else None,
                "rms": self.scale * math.sqrt(max(0.0, self.squares.total() / self.count)) if self.count else None,
                "first": self.first, "last": self.last}


@dataclass(frozen=True)
class RawSelection:
    archive: dict
    catalog: list[dict]
    directory: Path
    path: Path
    start: float
    end: float
    stage: str | None
    cycle: str | None

    @property
    def columns(self):
        return ["time_s", "_row", "_stage", "_cycle", *[item["key"] for item in self.catalog]]

    @property
    def where(self):
        clauses, parameters = ["time_s BETWEEN ? AND ?"], [self.start, self.end]
        for column, value in (("_stage", self.stage), ("_cycle", self.cycle)):
            if value is not None:
                clauses.append(f"{column}=?")
                parameters.append(value)
        return " AND ".join(clauses), parameters

    def select_sql(self):
        # Identifiers originate only in the validated manifest catalog. Quoting
        # still matters: a legitimate imported CSV header can contain punctuation.
        return f"SELECT {','.join(quoted(key) for key in self.columns)} FROM samples WHERE {self.where[0]} ORDER BY time_s,_row"

    def metadata(self, count):
        return {"archive_id": self.archive["id"], "execution_uuid": self.archive["execution_uuid"],
                "archive_code": self.archive["archive_code"], "range": {"start": self.start, "end": self.end},
                "time_range": self.archive["manifest"]["time_range"], "catalog": self.catalog,
                "filters": {"stage": self.stage, "cycle": self.cycle},
                "resolution": "raw", "scope": "RAW_RANGE_INSPECTION", "total_rows": count}


class CsvExport:
    """Own the preflighted cursor until completion, disconnect, or response failure."""
    def __init__(self, selection, conn, stack, count):
        self.selection, self.conn, self.stack, self.count = selection, conn, stack, count
        self.closed = False
        # UUID filenames cannot inject Content-Disposition headers.
        self.filename = f"archive-{UUID(str(selection.archive['id']))}-raw.csv"

    def close(self):
        if not self.closed:
            self.closed = True
            self.stack.close()

    def chunks(self):
        selection = self.selection
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        headers = ["archive_id", "execution_uuid", "range_start_s", "range_end_s",
                   "filter_stage", "filter_cycle", "resolution", "time_s [s]", "_row", "_stage", "_cycle"]
        headers += [f"{item['key']} [{item['unit']}]" if item["unit"] else item["key"] for item in selection.catalog]
        prefix = [str(selection.archive["id"]), str(selection.archive["execution_uuid"]), selection.start, selection.end,
                  csv_text(selection.stage), csv_text(selection.cycle), "raw"]
        try:
            writer.writerow([csv_text(value) for value in headers])
            yield "\ufeff" + buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            while rows := self.conn.fetchmany(FETCH_BATCH_ROWS):
                for time_s, row_id, stage, cycle, *values in rows:
                    writer.writerow([*prefix, time_s, row_id, csv_text(stage), csv_text(cycle), *map(numeric, values)])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        finally:
            buffer.close()
            self.close()


class CurveAnalysisService:
    def __init__(self, storage: ArchiveStorage, memory_mb=512):
        self.storage, self.memory_mb = storage, memory_mb

    def select(self, archive, metrics, subjects, start=None, end=None, stage=None, cycle=None):
        if archive["processing_state"] != "READY":
            raise ApiError(409, "archive_not_ready", "原始文件正在校验分析或处理失败，请查看归档状态。")
        manifest = archive["manifest"]
        catalog = manifest["catalog"]
        if len(metrics) > MAX_SERIES or len(subjects) > MAX_SERIES:
            raise ApiError(422, "too_many_series", "单次参数或关节选择最多 32 项。")
        available_metrics = {item["key"] for item in catalog} | {item["metric"] for item in catalog}
        if set(metrics) - available_metrics or set(subjects) - {item["subject"] for item in catalog}:
            raise ApiError(422, "unknown_curve", "所选参数或关节不在该归档目录中。")
        chosen = [item for item in catalog if (not metrics or item["metric"] in metrics or item["key"] in metrics)
                  and (not subjects or item["subject"] in subjects)]
        if not metrics:
            chosen = chosen[:1]
        if not chosen or len(chosen) > MAX_SERIES:
            raise ApiError(422, "invalid_series_count", "请选择 1 至 32 条曲线。")
        if ((stage is not None and stage not in manifest["stages"])
                or (cycle is not None and cycle not in manifest["cycles"])):
            raise ApiError(422, "unknown_stage_cycle", "该归档没有所选阶段或循环数据。")
        start = manifest["time_range"]["start"] if start is None else start
        end = manifest["time_range"]["end"] if end is None else end
        if not all(math.isfinite(value) and 0 <= value <= MAX_TIME_SECONDS for value in (start, end)) or end < start:
            raise ApiError(422, "invalid_time_range", "时间范围须为 0 至 315360000 秒的有限值，且结束不早于开始。")
        directory = self.storage.directory(archive["execution_uuid"])
        path = self.storage.path(archive["execution_uuid"], "telemetry.parquet")
        if not path.is_file():
            raise ApiError(409, "derived_file_missing", "曲线查询副本缺失，请由管理员重新处理归档。")
        return RawSelection(archive, chosen, directory, path, start, end, stage, cycle)

    @contextmanager
    def open(self, selection):
        try:
            with connection(selection.directory, self.memory_mb) as conn:
                conn.read_parquet(str(selection.path)).create_view("samples")
                yield conn
        except (duckdb.Error, OSError) as exc:
            raise ApiError(409, "raw_query_failed", "原始采样查询副本不可读或资源不足，请缩小范围或重新处理归档。") from exc

    def statistics(self, selection):
        statistics = [RawStatistic() for _ in selection.catalog]
        count = 0
        with self.open(selection) as conn:
            conn.execute(selection.select_sql(), selection.where[1])
            while rows := conn.fetchmany(FETCH_BATCH_ROWS):
                count += len(rows)
                for row in rows:
                    for statistic, value in zip(statistics, row[4:]):
                        statistic.add(value)
        return {**selection.metadata(count), "series": [{**item, **statistic.result()}
                for item, statistic in zip(selection.catalog, statistics)]}

    def samples(self, selection, offset=0, limit=100):
        if not 0 <= offset <= 9223372036854775807 or not 1 <= limit <= MAX_SAMPLE_ROWS:
            raise ApiError(422, "invalid_pagination", "原始数据偏移须为非负整数，每页 1 至 500 行。")
        with self.open(selection) as conn:
            count = conn.execute(f"SELECT count(*) FROM samples WHERE {selection.where[0]}", selection.where[1]).fetchone()[0]
            rows = conn.execute(selection.select_sql() + " LIMIT ? OFFSET ?", [*selection.where[1], limit, offset]).fetchall()
        normalized = [dict(zip(selection.columns, [*row[:4], *map(numeric, row[4:])])) for row in rows]
        return {**selection.metadata(count), "offset": offset, "limit": limit,
                "has_more": offset + len(rows) < count, "rows": normalized}

    def export_csv(self, selection):
        # Preflight happens synchronously, after RLS authorization and before any
        # streaming response status/body. The cursor retains this exact selection.
        stack = ExitStack()
        try:
            conn = stack.enter_context(self.open(selection))
            count = conn.execute(f"SELECT count(*) FROM samples WHERE {selection.where[0]}", selection.where[1]).fetchone()[0]
            if count > MAX_EXPORT_ROWS:
                raise ApiError(413, "export_too_large", f"所选区间包含 {count} 行，超过 CSV 导出上限 {MAX_EXPORT_ROWS} 行；请缩小时间范围。",
                               [{"total_rows": count, "max_rows": MAX_EXPORT_ROWS}])
            conn.execute(selection.select_sql(), selection.where[1])
            return CsvExport(selection, conn, stack, count)
        except (duckdb.Error, OSError) as exc:
            stack.close()
            raise ApiError(409, "raw_query_failed", "原始采样查询副本不可读或资源不足，请缩小范围或重新处理归档。") from exc
        except BaseException:
            stack.close()
            raise
