"""Synthetic source-oracle checks for bounded, gap-safe viewport sampling."""
from contextlib import contextmanager
import hashlib
import random

import duckdb
import pytest

from app.core.errors import ApiError
from app.services.curve_query import CurveQueryService
import app.services.curve_query as curve
from test_archives import processed


def csv_bytes(headers, rows):
    return (",".join(headers) + "\n" + "".join(
        ",".join("" if value is None else str(value) for value in row) + "\n"
        for row in rows)).encode()


def file_hashes(storage, archive):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in storage.directory(archive["execution_uuid"]).iterdir() if path.is_file()}


def assert_source_safe(points, source_rows, gap_threshold):
    """Independent Python oracle; source_rows are (time, value, original row).

    Test generators give every finite (time, value) an unambiguous source row.
    Inspect every drawn pair, including source rows omitted by downsampling.
    """
    ordered = sorted(source_rows, key=lambda row: (row[0], row[2]))
    positions = {}
    for index, (time, value, _) in enumerate(ordered):
        if value is not None:
            assert (time, value) not in positions, "ambiguous test fixture"
            positions[(time, value)] = index
    assert [point[0] for point in points] == sorted(point[0] for point in points)
    for time, value in points:
        if value is not None:
            assert (time, value) in positions, "invented finite sample"
    for left, right in zip(points, points[1:]):
        if left[1] is None or right[1] is None:
            continue
        begin, end = positions[tuple(left)], positions[tuple(right)]
        assert begin < end, "source row order changed"
        between = ordered[begin:end+1]
        assert all(row[1] is not None for row in between), (left, right, "crossed null")
        assert all(b[0]-a[0] <= gap_threshold for a, b in zip(between, between[1:])), (left, right, "crossed gap")


@contextmanager
def capture_queries(monkeypatch, explain=False):
    captured = []
    original = curve.connection

    class Capture:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, sql, params=None):
            item = {"sql": sql, "params": params}
            if explain and sql.lstrip().startswith(("SELECT", "WITH")):
                item["plan"] = "\n".join(row[1] for row in self.conn.execute("EXPLAIN " + sql, params or []).fetchall())
            captured.append(item)
            return self.conn.execute(sql, params or [])

    @contextmanager
    def connection(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield Capture(conn)

    with monkeypatch.context() as local:
        local.setattr(curve, "connection", connection)
        yield captured


def test_all_time_gaps_in_one_bucket_break_without_losing_extrema(tmp_path):
    rows = [(i, 999 if i == 39 else -999 if i == 50 else 0)
            for i in range(2000) if i not in set(range(10, 20)) | set(range(40, 50))]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows), {"max_gap_seconds": 1.1})
    before = file_hashes(storage, archive)
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 100)
    points = result["series"][0]["points"]
    assert [39, 999] in points and [50, -999] in points
    assert [14.5, None] in points and [44.5, None] in points
    assert_source_safe(points, [(t, y, row) for row, (t, y) in enumerate(rows)], 1.1)
    assert len(points) <= 100
    assert file_hashes(storage, archive) == before


@pytest.mark.parametrize("budget,nulls,peaks,legacy", [
    (100, {10, 30, 50}, {29: 999, 31: -999}, False),
    (1000, {5, 15, 25}, {14: 999, 16: -999}, True),
])
def test_middle_null_restored_for_dynamic_and_unchanged_old_pyramid(tmp_path, monkeypatch, budget, nulls, peaks, legacy):
    rows = [(i, None if i in nulls else peaks.get(i, 0)) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows))
    before = file_hashes(storage, archive)
    if legacy:
        pyramid = storage.path(archive["execution_uuid"], "pyramid-3.parquet")
        with curve.connection(pyramid.parent) as conn:
            old_nulls = conn.execute("SELECT t FROM read_parquet(?) WHERE y IS NULL", [str(pyramid)]).fetchall()
        assert (15.0,) not in old_nulls  # Verify that this fixture exercises the old loss.
    with capture_queries(monkeypatch) as captured:
        result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, budget)
    assert any("SELECT t,y,row_id FROM read_parquet" in q["sql"] for q in captured) is legacy
    points = result["series"][0]["points"]
    for time, value in peaks.items():
        assert [time, value] in points
    assert [15 if legacy else 30, None] in points
    assert_source_safe(points, [(t, y, row) for row, (t, y) in enumerate(rows)], archive["manifest"]["gap_threshold_seconds"])
    assert len(points) <= budget
    assert file_hashes(storage, archive) == before


def test_manifest_truncation_cannot_hide_later_time_gaps(tmp_path):
    rows = [(i*2, 999 if i == 1500 else -999 if i == 1501 else 0) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows), {"max_gap_seconds": 1.1})
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 100)
    assert result["gaps_truncated"] and len(result["gaps"]) == 500
    points = result["series"][0]["points"]
    assert [3000, 999] in points and [3002, -999] in points and [3001, None] in points
    assert_source_safe(points, [(t, y, row) for row, (t, y) in enumerate(rows)], 1.1)
    assert len(points) <= 100


def test_each_channel_uses_its_own_null_boundaries(tmp_path):
    rows = [(i, None if i in {10, 30, 50} else 999 if i == 29 else -999 if i == 31 else 0,
             None if i in {12, 32, 52} else 999 if i == 31 else -999 if i == 33 else 0)
            for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "a_pos_rad", "b_pos_rad"], rows))
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 200)
    first, second = [series["points"] for series in result["series"]]
    assert [30, None] in first and [32, None] not in first
    assert [32, None] in second and [30, None] not in second
    assert [29, 999] in first and [31, -999] in first
    assert [31, 999] in second and [33, -999] in second
    for index, series in enumerate(result["series"]):
        assert_source_safe(series["points"], [(row[0], row[index+1], i) for i, row in enumerate(rows)], archive["manifest"]["gap_threshold_seconds"])


@pytest.mark.parametrize("channels,budget", [(1, 50), (6, 50), (6, 54), (6, 100), (32, 256), (32, 288), (32, 20000)])
def test_total_budget_including_breaks_and_all_channel_extrema(tmp_path, channels, budget):
    rows = [(i, *(None if i % 17 == 8 else 999 if i == 21 else -999 if i == 23 else i+j/100
                  for j in range(channels))) for i in range(600)]
    headers = ["time_s", *(f"j{j}_pos_rad" for j in range(channels))]
    storage, archive = processed(tmp_path, csv_bytes(headers, rows))
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, budget)
    assert len(result["series"]) == channels
    assert sum(len(series["points"]) for series in result["series"]) <= budget
    for index, series in enumerate(result["series"]):
        points = series["points"]
        assert [21, 999] in points and [23, -999] in points
        assert len(points) == series["returned_points"] <= budget//channels
        assert_source_safe(points, [(row[0], row[index+1], i) for i, row in enumerate(rows)], archive["manifest"]["gap_threshold_seconds"])


@pytest.mark.parametrize("budget", [100, 1000, 20000])
def test_duplicate_and_unsorted_times_keep_null_between_same_x_extrema(tmp_path, budget):
    rows = [(i, i/10000) for i in range(2, 2000)]
    rows[::2] = reversed(rows[::2])
    rows[100:100] = [(1, 999), (1, None), (1, -999), (0, 0)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows))
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, budget)
    points = result["series"][0]["points"]
    assert [point for point in points if point[0] == 1] == [[1, 999], [1, None], [1, -999]]
    assert archive["manifest"]["quality_status"] == "INVALID"
    assert_source_safe(points, [(t, y, row) for row, (t, y) in enumerate(rows)], archive["manifest"]["gap_threshold_seconds"])


@pytest.mark.parametrize("rows,expected", [
    ([(0, 7)], [[0, 7]]),
    ([(0, None)], [[0, None]]),
    ([(0, 1), (2, 2), (5, None), (8, 3), (9, 4)], [[0, 1], [1, None], [2, 2], [5, None], [8, 3], [9, 4]]),
])
def test_raw_path_keeps_samples_and_only_inserts_needed_time_breaks(tmp_path, rows, expected):
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows), {"max_gap_seconds": 1.1})
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 50)
    assert result["resolution"] == "raw"
    assert result["series"][0]["points"] == expected
    empty = CurveQueryService(storage).query(archive, ["pos_rad"], [], 100, 101, 50)
    assert empty["series"][0]["points"] == []


@pytest.mark.parametrize("budget", [50, 1000])
def test_all_null_series_remain_missing_instead_of_zero(tmp_path, budget):
    rows = [(i, *(None for _ in range(6))) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", *(f"j{i}_pos_rad" for i in range(6))], rows))
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, budget)
    assert sum(series["returned_points"] for series in result["series"]) <= budget
    assert all(series["points"] and all(p[1] is None for p in series["points"]) for series in result["series"])


@pytest.mark.parametrize("selected_null", [False, True])
def test_stage_cycle_filter_excludes_other_categories_nulls(tmp_path, selected_null):
    rows = [(i, "A" if i % 2 == 0 else "B", i % 2,
             None if i % 2 or selected_null and i == 400 else i) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "stage", "cycle", "j_pos_rad"], rows), {"max_gap_seconds": 3})
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], 200, 800, 50, stage="A", cycle="0")
    points = result["series"][0]["points"]
    assert any(value is None for _, value in points) is selected_null
    source = [(t, value, index) for index, (t, stage, cycle, value) in enumerate(rows) if stage == "A" and cycle == 0 and 200 <= t <= 800]
    assert_source_safe(points, source, 3)


def test_discontinuous_filtered_times_use_selected_viewport_threshold(tmp_path):
    rows = [(i, "A" if i < 10 or i > 100 else "B", 0, i) for i in range(300)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "stage", "cycle", "j_pos_rad"], rows), {"max_gap_seconds": 1.1})
    result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 50, stage="A", cycle="0")
    assert_source_safe(result["series"][0]["points"], [(t, y, index) for index, (t, stage, _, y) in enumerate(rows) if stage == "A"], 1.1)


def test_oversized_pyramid_falls_back_before_unbounded_fetch_without_trimming_extrema(tmp_path, monkeypatch):
    rows = [(i, 999 if i == 1500 else -999 if i == 1501 else i/10000) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows))
    # Under-reported metadata exercises the actual row-cap check independently
    # from the conservative 12*bins qualification; no file content is modified.
    archive["manifest"]["pyramids"][0]["bins"] = 1
    before = file_hashes(storage, archive)
    with capture_queries(monkeypatch) as captured:
        result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 100)
    pyramid_reads = [item for item in captured if "SELECT t,y,row_id FROM read_parquet" in item["sql"]]
    assert len(pyramid_reads) == 1 and "LIMIT ?" in pyramid_reads[0]["sql"] and pyramid_reads[0]["params"][-1] == 51
    assert any("first(_row ORDER BY time_s,_row)" in item["sql"] for item in captured)
    assert [1500, 999] in result["series"][0]["points"] and [1501, -999] in result["series"][0]["points"]
    assert result["series"][0]["returned_points"] <= 100
    assert file_hashes(storage, archive) == before


def test_continuity_sql_projects_only_selected_column_and_pushes_filters(tmp_path, monkeypatch):
    rows = [(i, "A" if i < 1800 else "B", 0 if i < 1800 else 1, i, i+100, i+200) for i in range(2000)]
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "stage", "cycle", "j_pos_rad", "k_pos_rad", "j_temp_c"], rows))
    with capture_queries(monkeypatch, explain=True) as captured:
        CurveQueryService(storage).query(archive, ["j_pos_rad"], [], 200, 1500, 100, stage="A", cycle="0")
    check = next(item for item in captured if "count(null_time) OVER history" in item["sql"])
    plan = check["plan"]
    assert "j_pos_rad" in plan and "j_temp_c" not in plan and "k_pos_rad" not in plan
    compact_plan = "".join(plan.replace("│", "").split())
    assert "time_s>=200.0" in compact_plan and "time_s<=1500.0" in compact_plan and "_stage='A'" in compact_plan and "_cycle='0'" in compact_plan
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in check["sql"]


def test_continuity_failure_cleans_temporary_files_without_mutating_archive(tmp_path, monkeypatch):
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], [(i, i) for i in range(2000)]))
    before = file_hashes(storage, archive)
    original = curve.connection
    unregistered = []

    class FailedRead:
        def __init__(self, conn): self.conn = conn
        def __getattr__(self, name): return getattr(self.conn, name)
        def execute(self, sql, params=None):
            if "count(null_time) OVER history" in sql:
                raise duckdb.IOException("Synthetic read failure after candidate registration")
            return self.conn.execute(sql, params or [])
        def unregister(self, name):
            unregistered.append(name)
            return self.conn.unregister(name)

    @contextmanager
    def connection(*args, **kwargs):
        with original(*args, **kwargs) as conn:
            yield FailedRead(conn)

    monkeypatch.setattr(curve, "connection", connection)
    with pytest.raises(ApiError) as error:
        CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, 100)
    assert error.value.status_code == 409 and error.value.code == "curve_query_failed"
    assert unregistered == ["curve_candidates"]
    assert file_hashes(storage, archive) == before
    assert not list(storage.directory(archive["execution_uuid"]).glob(".query-*"))


@pytest.mark.parametrize("seed", range(6))
def test_independent_source_oracle_on_many_interleaved_gaps_and_nulls(tmp_path, seed):
    rng = random.Random(seed)
    time = 0
    rows = []
    for index in range(2000):
        time += rng.choice([0, 1, 1, 1, 7])
        rows.append((time, None if rng.random() < .15 else index+.125))
    rows[510] = (rows[510][0], 9999)
    rows[512] = (rows[512][0], -9999)
    storage, archive = processed(tmp_path, csv_bytes(["time_s", "j_pos_rad"], rows), {"max_gap_seconds": 1.1})
    for budget in (50, 100, 1000, 20000):
        result = CurveQueryService(storage).query(archive, ["pos_rad"], [], None, None, budget)
        points = result["series"][0]["points"]
        assert [rows[510][0], 9999] in points and [rows[512][0], -9999] in points
        assert_source_safe(points, [(t, y, row) for row, (t, y) in enumerate(rows)], 1.1)
        assert len(points) <= budget
