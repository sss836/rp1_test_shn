"""Adapter for the current rp1-reliability-platform ingestion API."""

from __future__ import annotations

import csv
import hashlib
import http.cookiejar
import json
import math
import os
import re
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib import error, request

import yaml

from .outbox import Outbox, OutboxItem
from .uploader import PlatformError, read_secret


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "reliability_platform.yaml"
)
_PROFILE_REQUIRED = (
    "campaign_id",
    "cycle_id",
    "segment_id",
    "asset_id",
    "configuration_id",
    "test_case_version_id",
    "station_id",
)


def load_test_profiles(path: Path | str | None = None) -> list[dict[str, Any]]:
    profile_path = Path(path or DEFAULT_PROFILE_PATH).expanduser().resolve()
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    profiles = raw.get("profiles") if isinstance(raw, Mapping) else None
    if profiles is None:
        return []
    if not isinstance(profiles, list):
        raise ValueError("reliability platform profiles must be a list")
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw_profile in profiles:
        if not isinstance(raw_profile, Mapping):
            raise ValueError("each reliability platform profile must be a mapping")
        profile = dict(raw_profile)
        key = str(profile.get("key") or "").strip()
        label = str(profile.get("label") or "").strip()
        if not key or not label:
            raise ValueError("each platform profile requires key and label")
        if key in keys:
            raise ValueError(f"duplicate platform profile key: {key}")
        keys.add(key)
        missing = [name for name in _PROFILE_REQUIRED if not str(profile.get(name) or "").strip()]
        profile["ready"] = not missing
        profile["missing"] = missing
        subject_map = profile.get("subject_map") or {}
        if not isinstance(subject_map, Mapping):
            raise ValueError(f"profile {key} subject_map must be a mapping")
        profile["subject_map"] = {
            str(joint): str(subject)
            for joint, subject in subject_map.items()
            if str(joint).strip() and str(subject).strip()
        }
        result.append(profile)
    return result


class ReliabilityPlatformClient:
    """Exact client for the current backend's DataEnvelope ingestion API."""

    def __init__(self, base_url: str, api_key: str = "", *, timeout: float = 30.0) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("platform URL must be absolute http(s)")
        self.api_key = api_key.strip()
        self.timeout = float(timeout)
        self.cookies = http.cookiejar.CookieJar()
        self._operator_opener = request.build_opener(request.HTTPCookieProcessor(self.cookies))
        self._operator_lock = threading.RLock()

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        service_auth: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Change-Reason": "factory HMI execution ingestion",
        }
        if service_auth:
            if not self.api_key:
                raise PlatformError("测试数据提交未配置服务凭据；上位机在线监控仍可使用账号登录。")
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            for cookie in self.cookies:
                if cookie.name == "rp1_csrf":
                    headers["X-CSRF-Token"] = cookie.value
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers=headers,
            method=method,
        )
        try:
            if service_auth:
                with request.urlopen(req, timeout=timeout or self.timeout) as response:
                    raw = response.read()
            else:
                with self._operator_lock:
                    with self._operator_opener.open(req, timeout=timeout or self.timeout) as response:
                        raw = response.read()
        except error.HTTPError as exc:
            raw = exc.read()
            try:
                decoded = json.loads(raw.decode("utf-8"))
                detail = decoded.get("error", decoded.get("detail", decoded))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise PlatformError(
                f"{method} {path} failed: {detail or exc.reason}",
                status=exc.code,
            ) from exc
        except error.URLError as exc:
            raise PlatformError(f"cannot reach reliability platform: {exc.reason}") from exc
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise PlatformError(f"{method} {path} returned a non-object response")
        data = value.get("data", value)
        if not isinstance(data, dict):
            raise PlatformError(f"{method} {path} returned invalid DataEnvelope")
        return data

    def authenticate_operator(self, username: str, password: str) -> dict[str, Any]:
        return self._call(
            "POST",
            "/api/v1/auth/login",
            payload={"username": username, "password": password},
            service_auth=False,
        )

    def heartbeat_presence(self, connection_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/api/v1/hmi/presence/{connection_id}", payload=payload, service_auth=False, timeout=5)

    def disconnect_presence(self, connection_id: str) -> dict[str, Any]:
        return self._call("DELETE", f"/api/v1/hmi/presence/{connection_id}", service_auth=False, timeout=3)

    def logout_operator(self) -> None:
        try:
            self._call("POST", "/api/v1/auth/logout", service_auth=False, timeout=3)
        finally:
            self.cookies.clear()

    def register_execution(
        self, payload: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/api/v1/ingestion/executions",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def events(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/events",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def telemetry(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/telemetry",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def artifact(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/artifacts",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def finish(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/finish",
            payload=payload,
            idempotency_key=idempotency_key,
        )


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _extract_bundle(bundle: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    with tarfile.open(bundle, "r:gz") as archive:
        summary_member = archive.getmember("analysis/summary.json")
        summary_file = archive.extractfile(summary_member)
        telemetry_member = archive.getmember("telemetry/raw.csv")
        telemetry_file = archive.extractfile(telemetry_member)
        if summary_file is None or telemetry_file is None:
            raise ValueError("execution bundle is missing summary or telemetry")
        summary = json.loads(summary_file.read().decode("utf-8"))
        lines = telemetry_file.read().decode("utf-8").splitlines()
    rows = list(csv.DictReader(lines))
    return summary, rows


_METRIC_COLUMNS = (
    ("_cmd_pos_rad", "JOINT-TARGET-POSITION", "deg", math.degrees),
    ("_pos_rad", "JOINT-ACTUAL-POSITION", "deg", math.degrees),
    ("_torque_nm", "JOINT-TORQUE", "N·m", float),
    ("_temp_c", "JOINT-TEMPERATURE", "°C", float),
    ("_current_a", "JOINT-CURRENT", "A", float),
)


def build_telemetry_batches(
    rows: list[dict[str, str]],
    *,
    manifest: Mapping[str, Any],
    stage_id: str,
    producer_key: str,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    subject_map = manifest.get("subject_map") or {}
    if not isinstance(subject_map, Mapping) or not subject_map:
        return []
    started_at = _iso(str(manifest["started_at"]))
    indexes = list(range(len(rows)))
    if len(indexes) > 5000:
        step = math.ceil(len(indexes) / 5000)
        indexes = indexes[::step][:5000]
    times: list[float] = []
    for row_index in indexes:
        try:
            times.append(float(rows[row_index].get("time_s") or 0.0))
        except ValueError:
            times.append(0.0)
    interval_ms = 10.0
    if len(times) > 1:
        interval_ms = max(10.0, (times[-1] - times[0]) * 1000.0 / (len(times) - 1))
    series: list[dict[str, Any]] = []
    headers = rows[0].keys()
    for joint_name, subject_code in subject_map.items():
        for suffix, metric_code, unit, converter in _METRIC_COLUMNS:
            column = f"{joint_name}{suffix}"
            if column not in headers:
                continue
            points: list[dict[str, Any]] = []
            for sample_index, (row_index, elapsed) in enumerate(zip(indexes, times, strict=True)):
                raw = rows[row_index].get(column)
                if raw in {None, ""}:
                    continue
                try:
                    value = converter(float(raw))
                except (TypeError, ValueError):
                    continue
                observed = started_at + timedelta(seconds=max(0.0, elapsed))
                points.append(
                    {
                        "sample_index": len(points),
                        "observed_at": observed.isoformat(),
                        "value": value,
                        "quality_status": "VALID",
                    }
                )
            if not points:
                continue
            series.append(
                {
                    "producer_series_key": (
                        f"{producer_key}:{metric_code}:{subject_code}"[:200]
                    ),
                    "metric_code": metric_code,
                    "metric_version": "1.0.0",
                    "subject_code": str(subject_code),
                    "canonical_unit": unit,
                    "series_kind": "STEADY_STATE_WINDOW",
                    "started_at": points[0]["observed_at"],
                    "ended_at": points[-1]["observed_at"],
                    "sampling_interval_ms": interval_ms,
                    "downsample_method": "UNIFORM",
                    "quality_status": "VALID",
                    "raw_data_reference": {
                        "bundle_file": "telemetry/raw.csv",
                        "source_column": column,
                    },
                    "points": points,
                }
            )
    return [
        {
            "producer_key": f"{producer_key}:telemetry:{offset // 20:04d}"[:200],
            "stage_id": stage_id,
            "series": series[offset : offset + 20],
        }
        for offset in range(0, len(series), 20)
    ]


class ReliabilityUploaderWorker:
    """Consumes the existing durable outbox using the current ingestion API."""

    def __init__(
        self,
        outbox: Outbox,
        client: ReliabilityPlatformClient,
        *,
        poll_interval_s: float = 2.0,
        presence: Any | None = None,
    ) -> None:
        self.outbox = outbox
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.presence = presence
        self.station_id = "reliability-v1"
        self._identity = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        if self._identity:
            self.clear_credentials()
        grant = self.client.authenticate_operator(username.strip(), password)
        user = grant.get("user") if isinstance(grant.get("user"), Mapping) else {}
        if user.get("must_change_password"):
            try:
                self.client.logout_operator()
            except PlatformError:
                pass
            raise PlatformError("请先在平台网页完成首次登录改密，再登录上位机。", status=403)
        if self.presence is not None:
            try:
                self.presence.connect()
            except Exception:
                self.presence.disconnect()
                try:
                    self.client.logout_operator()
                except PlatformError:
                    pass
                raise
        self._identity = str(user.get("username") or username).strip()
        return {
            "access_token": "browser-session-managed-in-memory",
            "expires_at": grant.get("expires_at", ""),
            "user": dict(user),
        }

    def clear_credentials(self) -> None:
        self._identity = ""
        if self.presence is not None:
            self.presence.disconnect()
            try:
                self.client.logout_operator()
            except PlatformError:
                pass

    def authorize_service_identity(self, identity: str = "SERVICE_API") -> None:
        self._identity = identity.strip() or "SERVICE_API"

    def set_presence(self, _payload: Mapping[str, Any]) -> None:
        return

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rp1-reliability-uploader",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def process_once(self) -> int:
        if not self._identity:
            return 0
        count = 0
        for item in self.outbox.due(limit=10):
            try:
                self._process(item)
            except (PlatformError, OSError, ValueError, KeyError) as exc:
                self.outbox.fail(item.execution_uuid, str(exc))
            count += 1
        return count

    @staticmethod
    def _validate_manifest(manifest: Mapping[str, Any]) -> None:
        missing = [name for name in _PROFILE_REQUIRED if not str(manifest.get(name) or "").strip()]
        if missing:
            raise ValueError(
                "reliability platform context is incomplete: " + ", ".join(missing)
            )

    def _process(self, item: OutboxItem) -> None:
        manifest = item.manifest
        self._validate_manifest(manifest)
        producer = str(manifest["execution_uuid"])
        bundle = Path(item.bundle_path).expanduser().resolve(strict=True)
        summary, rows = _extract_bundle(bundle)
        registration = self.client.register_execution(
            {
                "producer_key": producer,
                "campaign_id": manifest["campaign_id"],
                "cycle_id": manifest["cycle_id"],
                "segment_id": manifest["segment_id"],
                "asset_id": manifest["asset_id"],
                "configuration_id": manifest["configuration_id"],
                "test_case_version_id": manifest["test_case_version_id"],
                "station_id": manifest["station_id"],
                "started_at": manifest["started_at"],
                "source_status": "running",
                "stage_code": manifest.get("stage_code") or "AGING",
                "stage_name": manifest.get("stage_name") or "老化运行",
                "source_metadata": {
                    "hmi_test_id": manifest.get("test_id"),
                    "operator_id": manifest.get("operator_id"),
                    "bench_id": manifest.get("bench_id"),
                },
            },
            f"{producer}:register",
        )
        stage_id = str(registration["stage_id"])
        execution_id = str(registration["execution_id"])
        self.outbox.update(
            producer,
            state="uploading",
            submission_id=execution_id,
            upload_id=stage_id,
            remote_state="RUNNING",
        )
        for index, batch in enumerate(
            build_telemetry_batches(
                rows,
                manifest=manifest,
                stage_id=stage_id,
                producer_key=producer,
            )
        ):
            self.client.telemetry(
                producer,
                batch,
                f"{producer}:telemetry:{index:04d}",
            )
        verdict = summary.get("verdict") or {}
        self.client.events(
            producer,
            {
                "producer_key": f"{producer}:events",
                "events": [
                    {
                        "producer_event_key": f"{producer}:verdict",
                        "event_type": "FACTORY_AGING_VERDICT",
                        "occurred_at": manifest["ended_at"],
                        "payload": {
                            "status": manifest["status"],
                            "criteria_version": verdict.get("criteria_version"),
                            "reasons": verdict.get("reasons") or [],
                        },
                        "clock_quality": "VALID",
                        "data_quality": "VALID",
                    }
                ],
            },
            f"{producer}:events",
        )
        # The current platform has metadata registration but no binary upload
        # endpoint. Register the local bundle honestly as unavailable remotely.
        self.client.artifact(
            producer,
            {
                "producer_key": f"{producer}:bundle",
                "artifact_kind": "FACTORY_EXECUTION_BUNDLE",
                "availability_status": "MISSING",
                "source_location": str(bundle),
                "file_name": bundle.name,
                "mime_type": "application/gzip",
                "size_bytes": bundle.stat().st_size,
                "sha256": _sha256(bundle),
                "metadata": {
                    "reason": "platform binary object upload endpoint is not available",
                    "bundle_format": manifest.get("bundle_format"),
                },
            },
            f"{producer}:artifact",
        )
        timing = summary.get("timing") or {}
        source_status = str(manifest["status"])
        outcome = {
            "passed": "PASSED",
            "failed": "FAILED",
            "blocked": "INCONCLUSIVE",
        }[source_status]
        self.client.finish(
            producer,
            {
                "producer_key": f"{producer}:finish",
                "source_status": source_status,
                "finished_at": manifest["ended_at"],
                "active_seconds": float(timing.get("active_seconds") or 0.0),
                "outcome": outcome,
                "termination_kind": (
                    "NORMAL" if source_status in {"passed", "failed"} else "DATA_TIMEOUT"
                ),
                "summary": "; ".join(str(item) for item in verdict.get("reasons") or [])[:5000],
                "issues": "",
                "exception_count": int(
                    sum(int(item.get("fault_count") or 0) for item in summary.get("joint_metrics") or [])
                ),
                "data_quality": (
                    "VALID"
                    if float((summary.get("quality") or {}).get("sample_completeness") or 0.0) >= 0.99
                    else "PARTIAL"
                ),
                "clock_quality": "VALID",
            },
            f"{producer}:finish",
        )
        self.outbox.update(
            producer,
            state="approved",
            remote_state="COMPLETED",
            review_reason="accepted by reliability ingestion API",
            next_attempt_at=0,
            last_error="",
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            self.process_once()
            self._wake.wait(self.poll_interval_s)
            self._wake.clear()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reliability_worker_from_environment(outbox: Outbox, *, snapshot_provider: Any = None) -> ReliabilityUploaderWorker:
    key_file = os.environ.get("RP1_RELIABILITY_SERVICE_KEY_FILE", "").strip()
    try:
        api_key = read_secret(key_file) if key_file else os.environ.get("RP1_RELIABILITY_SERVICE_KEY", "").strip()
    except FileNotFoundError:
        api_key = ""
    client = ReliabilityPlatformClient(
        os.environ.get("RP1_FACTORY_PLATFORM_URL", "http://127.0.0.1:8080"),
        api_key,
    )
    from .presence import HmiPresencePublisher

    presence = HmiPresencePublisher(
        client, data_root=outbox.path.parent.parent,
        snapshot_provider=snapshot_provider or (lambda: {"runs": [], "plc": {}}),
    )
    return ReliabilityUploaderWorker(outbox, client, presence=presence)


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "ReliabilityPlatformClient",
    "ReliabilityUploaderWorker",
    "build_telemetry_batches",
    "load_test_profiles",
    "reliability_worker_from_environment",
]
