"""Exact raw range inspection, bounded export, and response resource ownership."""
from copy import deepcopy
import csv
import io
import math

import anyio
import pytest
from starlette.requests import ClientDisconnect

from app.api.routes.archives import ArchiveCsvResponse
from app.core.errors import ApiError
from app.services.curve_analysis import CurveAnalysisService, RawStatistic
import app.services.curve_analysis as analysis
from test_archives import processed


RAW = (b"time_s,stage,cycle,a_pos_rad,b_pos_rad\n"
       b"2,b,2,20,\n1,a,1,,\n1,a,1,10,\n3,b,2,-4,\n")


def service_selection(tmp_path, raw=RAW, **kwargs):
    storage, archive = processed(tmp_path, raw)
    service = CurveAnalysisService(storage)
    return service, service.select(archive, ["pos_rad"], [], **kwargs)


def test_exact_counts_values_nulls_and_raw_order_do_not_use_pyramids(tmp_path):
    service, selection = service_selection(tmp_path, start=1, end=2)
    before = deepcopy(selection.archive)
    selection.archive["manifest"]["pyramids"] = [{"level": 999, "bins": 1}]
    result = service.statistics(selection)
    assert result["resolution"] == "raw" and result["total_rows"] == 3
    assert result["series"][0] == {**selection.catalog[0], "count": 2, "missing": 1,
        "min": 10, "max": 20, "mean": 15, "rms": pytest.approx(math.sqrt(250)), "first": 10, "last": 20}
    missing = result["series"][1]
    assert missing["count"] == 0 and missing["missing"] == 3
    assert all(missing[key] is None for key in ("min", "max", "mean", "rms", "first", "last"))
    page = service.samples(selection, offset=1, limit=1)
    assert page["rows"] == [{"time_s": 1.0, "_row": 3, "_stage": "a", "_cycle": "1", "a_pos_rad": 10.0, "b_pos_rad": None}]
    assert page["total_rows"] == 3 and page["has_more"] is True
    assert service.samples(selection, offset=3)["rows"] == []
    assert service.samples(selection, offset=3)["has_more"] is False
    assert service.storage.path(selection.archive["execution_uuid"], "raw.csv").read_bytes() == RAW
    selection.archive["manifest"]["pyramids"] = before["manifest"]["pyramids"]
    assert selection.archive == before
    assert not list(selection.directory.glob(".query-*"))


def test_inclusive_stage_cycle_selection_and_empty_range(tmp_path):
    service, selection = service_selection(tmp_path, start=1, end=1, stage="a", cycle="1")
    result = service.statistics(selection)
    assert result["total_rows"] == 2
    assert result["series"][0]["mean"] == 10
    assert result["series"][0]["missing"] == 1
    empty = service.select(selection.archive, ["pos_rad"], [], 100, 101)
    assert service.samples(empty)["total_rows"] == 0
    stats = service.statistics(empty)["series"][0]
    assert stats["count"] == stats["missing"] == 0
    assert stats["mean"] is stats["rms"] is stats["min"] is stats["max"] is None
    document = "".join(service.export_csv(empty).chunks())
    assert len(list(csv.reader(io.StringIO(document.lstrip("\ufeff"))))) == 1


def test_numerics_preserve_cancellation_residuals_and_tiny_rms(tmp_path):
    raw = b"time_s,a_pos_rad,b_pos_rad\n0,1e100,1e-300\n1,1,-1e-300\n2,-1e100,1e-300\n3,3,-1e-300\n"
    service, selection = service_selection(tmp_path, raw)
    first, second = service.statistics(selection)["series"]
    assert first["mean"] == 1.0
    assert first["rms"] == pytest.approx(1e100 / math.sqrt(2))
    assert second["rms"] == pytest.approx(1e-300, rel=1e-12, abs=0)
    assert second["mean"] == 0
    direct = RawStatistic()
    for value in (None, float("nan"), float("inf"), -float("inf"), 0):
        direct.add(value)
    assert direct.result() == {"count": 1, "missing": 4, "min": 0, "max": 0, "mean": 0, "rms": 0, "first": 0, "last": 0}


@pytest.mark.parametrize("start,end", [(float("nan"), 2), (0, float("inf")), (-1, 2), (0, 315360001), (2, 1)])
def test_invalid_ranges_rejected_before_parquet_access(tmp_path, start, end):
    service, selection = service_selection(tmp_path)
    selection.path.unlink()
    with pytest.raises(ApiError) as error:
        service.select(selection.archive, ["pos_rad"], [], start, end)
    assert error.value.status_code == 422 and error.value.code == "invalid_time_range"


@pytest.mark.parametrize("change,code", [({"metrics": ["x); DROP TABLE samples;--"]}, "unknown_curve"),
    ({"subjects": ["missing"]}, "unknown_curve"), ({"metrics": ["pos_rad"] * 33}, "too_many_series"),
    ({"stage": "missing"}, "unknown_stage_cycle"), ({"cycle": "missing"}, "unknown_stage_cycle")])
def test_catalog_and_categories_are_closed_allowlists(tmp_path, change, code):
    service, selection = service_selection(tmp_path)
    parameters = {"metrics": ["pos_rad"], "subjects": [], **change}
    with pytest.raises(ApiError) as error:
        service.select(selection.archive, **parameters)
    assert error.value.code == code


def test_expanded_metric_selection_cannot_exceed_32_series(tmp_path):
    raw = ("time_s," + ",".join(f"j{i}_pos_rad" for i in range(33)) + "\n0," + ",".join("0" for _ in range(33)) + "\n").encode()
    storage, archive = processed(tmp_path, raw)
    with pytest.raises(ApiError) as error:
        CurveAnalysisService(storage).select(archive, ["pos_rad"], [])
    assert error.value.code == "invalid_series_count"


@pytest.mark.parametrize("offset,limit", [(-1, 100), (0, 0), (0, 501), (2**63, 100)])
def test_invalid_pagination(tmp_path, offset, limit):
    service, selection = service_selection(tmp_path)
    with pytest.raises(ApiError) as error:
        service.samples(selection, offset, limit)
    assert error.value.code == "invalid_pagination"


def test_ready_state_missing_file_and_symlink_rejected(tmp_path):
    service, selection = service_selection(tmp_path)
    selection.archive["processing_state"] = "PROCESSING"
    with pytest.raises(ApiError, match="正在校验"):
        service.select(selection.archive, ["pos_rad"], [])
    selection.archive["processing_state"] = "READY"
    selection.path.unlink()
    with pytest.raises(ApiError) as missing:
        service.select(selection.archive, ["pos_rad"], [])
    assert missing.value.code == "derived_file_missing"
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"private")
    selection.path.symlink_to(outside)
    with pytest.raises(ApiError) as symlink:
        service.select(selection.archive, ["pos_rad"], [])
    assert symlink.value.code == "archive_storage_conflict"


def test_csv_is_complete_batched_parseable_with_units_and_metadata(tmp_path, monkeypatch):
    service, selection = service_selection(tmp_path, start=1, end=2)
    monkeypatch.setattr(analysis, "FETCH_BATCH_ROWS", 2)
    export = service.export_csv(selection)
    assert export.count == 3 and export.closed is False
    chunks = list(export.chunks())
    assert len(chunks) == 3  # header, two rows, one row (no truncation)
    rows = list(csv.DictReader(io.StringIO("".join(chunks).lstrip("\ufeff"))))
    assert [int(row["_row"]) for row in rows] == [2, 3, 1]
    assert [row["a_pos_rad [rad]"] for row in rows] == ["", "10.0", "20.0"]
    assert all(row["b_pos_rad [rad]"] == "" for row in rows)
    assert all(row["archive_id"] == str(selection.archive["id"]) and row["resolution"] == "raw" for row in rows)
    assert all(float(row["range_start_s"]) == 1 and float(row["range_end_s"]) == 2 for row in rows)
    assert export.closed and not list(selection.directory.glob(".query-*"))


def test_export_cap_rejects_before_stream_and_cleans_up(tmp_path, monkeypatch):
    service, selection = service_selection(tmp_path)
    monkeypatch.setattr(analysis, "MAX_EXPORT_ROWS", 3)
    with pytest.raises(ApiError) as error:
        service.export_csv(selection)
    assert error.value.status_code == 413 and error.value.code == "export_too_large"
    assert error.value.details == [{"total_rows": 4, "max_rows": 3}]
    assert not list(selection.directory.glob(".query-*"))


def test_csv_text_formula_escaping_does_not_change_negative_measurements(tmp_path):
    raw = b'time_s,stage,cycle,=2+2,"odd""key"\n0,=2+2,@danger,-12,3\n'
    storage, archive = processed(tmp_path, raw)
    service = CurveAnalysisService(storage)
    selection = service.select(archive, ["=2+2", 'odd"key'], [], stage="=2+2", cycle="@danger")
    assert service.statistics(selection)["series"][1]["mean"] == 3
    document = "".join(service.export_csv(selection).chunks())
    rows = list(csv.DictReader(io.StringIO(document.lstrip("\ufeff"))))
    assert rows[0]["'=2+2"] == "-12.0"
    assert rows[0]["_stage"] == rows[0]["filter_stage"] == "'=2+2"
    assert rows[0]["_cycle"] == rows[0]["filter_cycle"] == "'@danger"
    assert service.samples(selection)["rows"][0]["_stage"] == "=2+2"


@pytest.mark.parametrize("fail_at", ["http.response.start", "http.response.body"])
def test_stream_response_releases_resources_on_send_disconnect(tmp_path, fail_at):
    service, selection = service_selection(tmp_path)
    export = service.export_csv(selection)
    response = ArchiveCsvResponse(export)

    async def disconnected_send(message):
        if message["type"] == fail_at:
            raise OSError("client disconnected")

    async def receive():
        return {"type": "http.disconnect"}

    async def run():
        with pytest.raises(ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, disconnected_send)

    anyio.run(run)
    assert export.closed and not list(selection.directory.glob(".query-*"))


def test_cancelled_response_and_early_generator_close_release_resources(tmp_path):
    service, selection = service_selection(tmp_path)
    export = service.export_csv(selection)
    chunks = export.chunks()
    next(chunks)
    chunks.close()
    assert export.closed and not list(selection.directory.glob(".query-*"))

    export = service.export_csv(selection)
    response = ArchiveCsvResponse(export)

    async def receive():
        return {"type": "http.disconnect"}

    async def run():
        with anyio.CancelScope() as scope:
            async def send(_):
                scope.cancel()
                await anyio.sleep(0)
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    anyio.run(run)
    assert export.closed and not list(selection.directory.glob(".query-*"))
