"""Small, Qt-independent client for the factory HMI gateway."""

from __future__ import annotations

import json
import math
import os
import re
import threading
from copy import deepcopy
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib import error, parse, request

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class GatewayError(RuntimeError):
    """Base error raised when the gateway cannot satisfy a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        # ``status`` is kept as a convenient alias for UI and callers.
        self.status = status_code
        self.detail = detail
        self.service_detail = detail
        self.method = method
        self.url = url

        parts = [message]
        if status_code is not None:
            parts.append(f"HTTP {status_code}")
        if detail not in (None, ""):
            if isinstance(detail, (dict, list)):
                rendered = str(detail["message"]) if isinstance(detail, Mapping) and detail.get("message") else json.dumps(detail, ensure_ascii=False, sort_keys=True)
            else:
                rendered = str(detail)
            parts.append(f"gateway detail: {rendered}")
        super().__init__("; ".join(parts))


class GatewayHTTPError(GatewayError):
    """An HTTP response with a non-success status."""


class GatewayClient:
    """Synchronous REST client.

    The desktop application runs these calls in ``QThreadPool`` workers, while
    tests and non-Qt tools can use the same class directly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        client_id: str | None = None,
        operator_token: str | None = None,
        trajectory_upload_timeout: float | None = None,
        lease_heartbeat_timeout: float = 0.5,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        parsed = parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("gateway URL must be an absolute http(s) URL")
        self.base_url = base_url
        self.timeout = timeout
        if not math.isfinite(lease_heartbeat_timeout) or not 0 < lease_heartbeat_timeout <= 2:
            raise ValueError("lease heartbeat timeout must be finite and in (0, 2] seconds")
        self.lease_heartbeat_timeout = float(lease_heartbeat_timeout)
        self._lease_lock = threading.Lock()
        self._lease_generation: int | None = None

        upload_timeout = (
            trajectory_upload_timeout if trajectory_upload_timeout is not None
            else float(os.environ.get("RP1_FACTORY_TRAJECTORY_UPLOAD_TIMEOUT_S", "120"))
        )
        if not math.isfinite(upload_timeout) or upload_timeout <= 0:
            raise ValueError("trajectory upload timeout must be a positive finite number")
        self.trajectory_upload_timeout = max(timeout, upload_timeout)
        self.client_id = (client_id or f"hmi-{uuid.uuid4()}").strip()
        self.operator_token = (operator_token or "").strip()
        self._start_lock = threading.Lock()
        self._pending_start: str | None = None
        self._plc_lock = threading.RLock()
        self._plc_snapshot: dict[str, Any] = {}
        self._plc_retired_epochs: set[str] = set()

    @property
    def status_ws_url(self) -> str:
        """Return the WebSocket endpoint corresponding to this gateway."""

        parsed = parse.urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        parameters = {"client_id": self.client_id}
        if self.operator_token:
            parameters["token"] = self.operator_token
        query = parse.urlencode(parameters)
        return parse.urlunsplit((scheme, parsed.netloc, "/ws/status", query, ""))

    @property
    def ws_url(self) -> str:
        """Backward-friendly alias for the status WebSocket URL."""

        return self.status_ws_url

    def build_ws_url(self) -> str:
        return self.status_ws_url

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _decode_response(raw: bytes, *, method: str, url: str) -> JsonValue:
        if not raw or not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(
                "gateway returned invalid JSON",
                detail=str(exc),
                method=method,
                url=url,
            ) from exc

    @staticmethod
    def _decode_error_detail(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace").strip()
        if isinstance(payload, Mapping):
            for key in ("detail", "message", "error"):
                if key in payload:
                    return payload[key]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *, timeout: float | None = None,
    ) -> JsonValue:
        url = self._url(path)
        headers = {
            "Accept": "application/json",
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        data = None
        if method != "GET":
            data = json.dumps(
                dict(payload or {}), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=data, headers=headers, method=method)
        if path == "/api/v1/lease/heartbeat" and self.lease_generation is not None:
            req.add_header("X-RP1-Lease-Generation", str(self.lease_generation))
        try:
            with request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
                result = self._decode_response(
                    response.read(),
                    method=method,
                    url=url,
                )
                if isinstance(result, Mapping):
                    self.observe_lease(result)
                return result
        except error.HTTPError as exc:
            try:
                raw = exc.read()
            except (AttributeError, OSError):
                raw = b""
            detail = self._decode_error_detail(raw)
            raise GatewayHTTPError(
                "gateway request failed",
                status_code=exc.code,
                detail=detail,
                method=method,
                url=url,
            ) from exc
        except error.URLError as exc:
            detail = getattr(exc, "reason", exc)
            raise GatewayError(
                "cannot reach gateway",
                detail=detail,
                method=method,
                url=url,
            ) from exc

    def get(self, path: str) -> JsonValue:
        return self._request("GET", path)

    def post(self, path: str, payload: Mapping[str, Any] | None = None) -> JsonValue:
        return self._request("POST", path, payload)

    def download(self, path: str, destination: Path | str) -> Path:
        url = self._url(path)
        headers = {"X-RP1-Client-ID": self.client_id}
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        req = request.Request(url, headers=headers, method="GET")
        target = Path(destination).expanduser()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = response.read()
        except error.HTTPError as exc:
            detail = self._decode_error_detail(exc.read())
            raise GatewayHTTPError(
                "gateway download failed",
                status_code=exc.code,
                detail=detail,
                method="GET",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="GET",
                url=url,
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def health(self) -> JsonValue:
        return self.get("/api/v1/health")

    def snapshot(self) -> JsonValue:
        return self.get("/api/v1/snapshot")

    def quick_profiles(self) -> JsonValue:
        return self.get("/api/v1/quick/profiles")

    def quick_status(self) -> JsonValue:
        return self.get("/api/v1/quick/status")

    def quick_prepare(self, profile: str, interfaces: list[str], backend: str = "motors_py") -> JsonValue:
        return self.post("/api/v1/quick/prepare", dict(profile=profile, interfaces=interfaces, backend=backend))

    def quick_start(self, hours: float, sample_id: str, *, physical_estop_confirmed: bool,
                    power_source: str = "plc", completion_mode: str = "configured",
                    parking_pose_confirmed: bool = False, data_domain: str | None = None,
                    bench_id: str | None = None, operator_id: str | None = None) -> JsonValue:
        payload = dict(duration_hours=hours,
            sample_id=sample_id, physical_estop_confirmed=physical_estop_confirmed,
            power_source=power_source)
        payload.update({key: value for key, value in dict(data_domain=data_domain, bench_id=bench_id, operator_id=operator_id).items() if value is not None})
        if completion_mode != "configured":
            payload.update(completion_mode=completion_mode, parking_pose_confirmed=parking_pose_confirmed)
        return self._request("POST", "/api/v1/quick/start", payload, timeout=120.0)

    def quick_end(self) -> JsonValue:
        return self._request("POST", "/api/v1/quick/end", timeout=15.0)

    def quick_abort(self) -> JsonValue:
        return self._request("POST", "/api/v1/quick/abort", timeout=15.0)

    def configs(self) -> JsonValue:
        return self.get("/api/v1/configs")

    def configure(
        self,
        config_path: str,
        limb: str,
        backend: str,
        *,
        bus_bindings: list[Mapping[str, Any]] | None = None,
        motor_overrides: list[Mapping[str, Any]] | None = None,
    ) -> JsonValue:
        payload: dict[str, Any] = {
            "config_path": config_path,
            "limb": limb,
            "backend": backend,
        }
        if bus_bindings is not None:
            payload["bus_bindings"] = bus_bindings
        if motor_overrides is not None:
            payload["motor_overrides"] = motor_overrides
        return self.post(
            "/api/v1/configure",
            payload,
        )

    def clear_configuration(self) -> JsonValue:
        return self.post("/api/v1/configuration/clear")

    def config_preview(self, config_path: str, limb: str) -> JsonValue:
        return self.post(
            "/api/v1/config/preview",
            {"config_path": config_path, "limb": limb},
        )

    def config_upload(self, path: str | Path, limb: str) -> JsonValue:
        source = Path(path)
        body = source.read_bytes()
        url = self._url("/api/v1/config/upload")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-yaml",
            "X-Filename": source.name,
            "X-RP1-Limb": limb,
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_response(response.read(), method="POST", url=url)
        except error.HTTPError as exc:
            raise GatewayHTTPError(
                "gateway configuration upload failed",
                status_code=exc.code,
                detail=self._decode_error_detail(exc.read()),
                method="POST",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="POST",
                url=url,
            ) from exc

    def connect(self) -> JsonValue:
        return self.post("/api/v1/connect")

    def can_status(self) -> JsonValue:
        return self.get("/api/v1/can/status")

    def can_interfaces(self) -> JsonValue:
        return self.get("/api/v1/can/interfaces")

    def can_connect(
        self,
        bus_bindings: list[Mapping[str, Any]] | None = None,
    ) -> JsonValue:
        payload = None if bus_bindings is None else {"bus_bindings": bus_bindings}
        return self.post("/api/v1/can/connect", payload)

    def can_disconnect(self) -> JsonValue:
        return self.post("/api/v1/can/disconnect")

    def can_recover(self) -> JsonValue:
        return self.post("/api/v1/can/recover")

    def discover_motors(self) -> JsonValue:
        # Discovery is capped at 10 s in the gateway; allow response/cleanup
        # margin without changing command or lease-heartbeat timeouts.
        return self._request("POST", "/api/v1/motors/discover", timeout=max(self.timeout, 15.0))

    def enable_motors(self, *, physical_estop_confirmed: bool) -> JsonValue:
        return self.post(
            "/api/v1/motors/enable",
            {"physical_estop_confirmed": physical_estop_confirmed},
        )

    def disable_motors(self) -> JsonValue:
        return self.post("/api/v1/motors/disable")

    def disable_protective_stop(self, *, mechanical_support_confirmed: bool) -> JsonValue:
        return self.post("/api/v1/lease/protective-stop/disable", {
            "mechanical_support_confirmed": mechanical_support_confirmed,
            "generation": self.lease_generation})

    def disconnect(self) -> JsonValue:
        return self.post("/api/v1/disconnect")

    def arm(self) -> JsonValue:
        return self.post("/api/v1/arm")

    def trajectory_import(self, path: str) -> JsonValue:
        return self.post("/api/v1/trajectory/import", {"path": path})

    def import_trajectory(self, path: str) -> JsonValue:
        return self.trajectory_import(path)

    def trajectory_upload(self, path: str | Path) -> JsonValue:
        source = Path(path)
        url = self._url("/api/v1/trajectory/upload")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "X-Filename": source.name,
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        try:
            with source.open("rb") as stream:
                headers["Content-Length"] = str(os.fstat(stream.fileno()).st_size)
                req = request.Request(url, data=stream, headers=headers, method="POST")
                with request.urlopen(req, timeout=self.trajectory_upload_timeout) as response:
                    return self._decode_response(response.read(), method="POST", url=url)
        except error.HTTPError as exc:
            detail = self._decode_error_detail(exc.read())
            raise GatewayHTTPError(
                "gateway trajectory upload failed",
                status_code=exc.code,
                detail=detail,
                method="POST",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="POST",
                url=url,
            ) from exc

    def playback_start(
        self,
        *,
        command_id: str | None = None,
        expected_config_revision: str | None = None,
        speed: float,
        loop: bool,
        cycles: int,
        duration_hours: float | None,
        record: bool,
        recording_scope: str | None = None,
        data_domain: str | None = None,
        record_rate_hz: float = 20.0,
        test_id: str | None = None,
        robot_id: str | None = None,
        execution_uuid: str | None = None,
        campaign_id: str | None = None,
        cycle_id: str | None = None,
        segment_id: str | None = None,
        asset_id: str | None = None,
        configuration_id: str | None = None,
        test_case_version_id: str | None = None,
        test_case_id: str | None = None,
        test_case_version: str | None = None,
        test_name: str | None = None,
        bench_id: str | None = None,
        station_id: str | None = None,
        operator_id: str | None = None,
        stage_code: str | None = None,
        stage_name: str | None = None,
        subject_map: Mapping[str, str] | None = None,
        completion_mode: str = "configured",
        parking_pose_confirmed: bool = False,
    ) -> JsonValue:
        payload: dict[str, Any] = {
            "speed": speed,
            "loop": loop,
            "cycles": cycles,
            "duration_hours": duration_hours,
            "record": record,
            "record_rate_hz": record_rate_hz,
            "test_id": test_id,
            "robot_id": robot_id,
        }
        optional = {
            "recording_scope": recording_scope,
            "data_domain": data_domain,
            "execution_uuid": execution_uuid,
            "campaign_id": campaign_id,
            "cycle_id": cycle_id,
            "segment_id": segment_id,
            "asset_id": asset_id,
            "configuration_id": configuration_id,
            "test_case_version_id": test_case_version_id,
            "test_case_id": test_case_id,
            "test_case_version": test_case_version,
            "test_name": test_name,
            "bench_id": bench_id,
            "station_id": station_id,
            "operator_id": operator_id,
            "stage_code": stage_code,
            "stage_name": stage_name,
            "subject_map": dict(subject_map) if subject_map else None,
        }
        payload.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if completion_mode != "configured":
            payload.update(completion_mode=completion_mode, parking_pose_confirmed=parking_pose_confirmed)
        with self._start_lock:
            if self._pending_start:
                receipt = self.playback_start_receipt(self._pending_start)
                if not isinstance(receipt, Mapping) or receipt.get("status") in {"pending", "unknown", "not_found"}:
                    raise GatewayError("上次启动结果待确认；请核对回执和当前执行，禁止再次启动", detail=receipt)
                self._pending_start = None
                if receipt.get("status") == "accepted":
                    return self._recovered_start(receipt)
            key = command_id or uuid.uuid4().hex
            # The server validates both just before accepting device actions.
            if expected_config_revision is None:
                view = self.snapshot()
                view = view.get("snapshot", view) if isinstance(view, Mapping) else {}
                expected_config_revision = view.get("config_revision")
                if not expected_config_revision:
                    raise GatewayError("网关未提供配置版本，不能确认启动身份；请刷新或升级配套网关")
            payload.update(command_id=key, expected_state="armed", expected_config_revision=expected_config_revision)
            self._pending_start = key
            try:
                result = self.post("/api/v1/playback/start", payload)
            except (GatewayError, OSError, TimeoutError) as exc:
                try:
                    receipt = self.playback_start_receipt(key)
                except (GatewayError, OSError, TimeoutError) as lookup_error:
                    raise GatewayError("启动结果待确认；回执查询失败，未重发设备动作", detail=str(lookup_error)) from exc
                if isinstance(receipt, Mapping) and receipt.get("status") == "accepted":
                    self._pending_start = None
                    return self._recovered_start(receipt)
                if isinstance(receipt, Mapping) and receipt.get("status") == "rejected":
                    self._pending_start = None
                    raise GatewayError("启动被拒绝；已核对回执", detail=receipt.get("error")) from exc
                if isinstance(exc, GatewayHTTPError) and exc.status_code is not None and 400 <= exc.status_code < 500 and isinstance(receipt, Mapping) and receipt.get("status") == "not_found":
                    self._pending_start = None  # rejected by auth/lease/schema, before command acceptance
                    raise
                raise GatewayError("启动响应丢失，结果待确认；已查询回执，未重发设备动作", detail=receipt) from exc
            self._pending_start = None
            return result

    def _recovered_start(self, receipt: Mapping[str, Any]) -> JsonValue:
        # Receipts establish execution identity. Fetch current state separately;
        # an accepted historical snapshot must not overwrite a later fault.
        try:
            current = self.snapshot()
        except (GatewayError, OSError, TimeoutError):
            current = None
        return {"ok": True, "result": receipt["result"], "snapshot": current,
                "start_receipt_recovered": True, "current_state_unknown": current is None}

    def playback_start_receipt(self, command_id: str) -> JsonValue:
        return self.get("/api/v1/playback/commands/" + parse.quote(command_id, safe=""))


    def playback_pause(self) -> JsonValue:
        return self.post("/api/v1/playback/pause")

    def playback_resume(self) -> JsonValue:
        return self.post("/api/v1/playback/resume")

    def playback_next(self) -> JsonValue:
        return self.post("/api/v1/playback/next")

    def playback_stop(self) -> JsonValue:
        return self.post("/api/v1/playback/stop")

    def playback_reset(self) -> JsonValue:
        return self.post("/api/v1/playback/reset")

    def reset_motor_fault(self) -> JsonValue:
        return self.post("/api/v1/motors/reset-fault")

    def playback_disable(self) -> JsonValue:
        return self.post("/api/v1/playback/disable")

    def lease_heartbeat(self) -> JsonValue:
        return self._request("POST", "/api/v1/lease/heartbeat", timeout=self.lease_heartbeat_timeout)

    @property
    def lease_generation(self) -> int | None:
        with self._lease_lock:
            return self._lease_generation

    def observe_lease(self, payload: Mapping[str, Any]) -> bool:
        snapshot = payload.get("snapshot", payload)
        lease = snapshot.get("lease") if isinstance(snapshot, Mapping) else None
        if not isinstance(lease, Mapping) or not isinstance(lease.get("generation"), int):
            return True
        generation = int(lease["generation"])
        with self._lease_lock:
            if self._lease_generation is not None and generation < self._lease_generation:
                return False
            self._lease_generation = generation
        return True

    def manual_move(
        self,
        targets_deg: list[float],
        speed_deg_s: float,
    ) -> JsonValue:
        return self.post(
            "/api/v1/manual/move",
            {
                "targets_deg": targets_deg,
                "speed_deg_s": speed_deg_s,
            },
        )

    def manual_stop(self) -> JsonValue:
        return self.post("/api/v1/manual/stop")

    def preview_launch(self) -> JsonValue:
        return self.post("/api/v1/preview/launch")

    def download_current_record(self, destination: Path | str) -> Path:
        return self.download("/api/v1/records/current.csv", destination)

    def platform_login(
        self,
        username: str,
        password: str,
        *,
        station_id: str | None = None,
    ) -> JsonValue:
        return self.post(
            "/api/v1/platform/login",
            {
                "username": username,
                "password": password,
                "station_id": station_id,
            },
        )

    def diagnostic(self, action: str, payload: Mapping[str, Any] | None = None) -> JsonValue:
        if action not in {"status", "submit", "job", "cancel", "threads", "history", "local-approve", "local-renew", "local-resume", "events", "steer", "thread-rename", "thread-delete", "references", "feedback", "faq", "faq-publish", "repairs", "repair-confirm", "repair-pending", "repair-apply", "report", "attachment-get", "draft-load", "draft-save", "upload-start", "upload-progress", "upload-cancel"}:
            raise ValueError("unsupported diagnostic action")
        return self._request("POST", f"/api/v1/diagnostics/{action}", payload,
                             timeout=2 if action in {'upload-progress','upload-cancel','upload-start'} else 30 if action in {'repair-apply', 'status', 'job', 'local-approve', 'local-resume', 'local-renew'} else 20 if action in { 'job', 'local-approve', 'report', 'attachment-get'} else 7)

    def diagnostic_screenshot(self, raw: bytes, mime: str, generation: str, *, filename: str | None = None) -> JsonValue:
        if mime not in {"image/png", "image/jpeg"} or not 0 < len(raw) <= 4 * 1024**2:
            raise ValueError("请选择4MB以内的PNG或JPEG截图。")
        filename = filename or ("diagnostic-screenshot.png" if mime == "image/png" else "diagnostic-screenshot.jpg")
        return self._diagnostic_upload(raw, filename, generation, mime)

    def diagnostic_attachment(self, raw: bytes, filename: str, generation: str) -> JsonValue:
        """Upload inert original bytes; extension never grants execution permission."""
        return self._diagnostic_upload(raw, filename, generation, "application/octet-stream")

    def diagnostic_attachment_progress(self, raw: bytes, filename: str, generation: str, upload_id: str) -> JsonValue:
        # Register a fresh transport attempt; business request_key remains stable.
        self.diagnostic('upload-start', dict(generation=generation, upload_id=upload_id, total_bytes=len(raw)))
        return self._diagnostic_upload(raw, filename, generation, 'application/octet-stream', upload_id=upload_id)

    def _diagnostic_upload(self, raw: bytes, filename: str, generation: str, mime: str, *, upload_id: str | None = None) -> JsonValue:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= 32 * 1024**2:
            raise ValueError("附件须为非空文件，单个最多32MiB。")
        if (not isinstance(filename, str) or not filename.strip() or filename in {".", ".."}
                or len(filename.encode("utf-8")) > 1024 or any(c in filename for c in "/\\")
                or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
            raise ValueError("附件文件名无效。")
        if not generation:
            raise ValueError("缺少当前诊断关联。")
        headers = {"Content-Type": mime, "X-RP1-Client-ID": self.client_id,
                   "X-RP1-Diagnostic-Generation": generation,
                   "X-Filename-UTF8": parse.quote(filename, safe="")}
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        if upload_id: headers["X-RP1-Diagnostic-Upload-ID"] = str(uuid.UUID(upload_id))
        url = self._url("/api/v1/diagnostics/upload")
        req = request.Request(url, data=raw, headers=headers, method="POST")
        try:
            # Only attachment I/O gets this budget. Control/lease requests are unchanged.
            with request.urlopen(req, timeout=120) as response:
                return self._decode_response(response.read(), method="POST", url=url)
        except error.HTTPError as exc:
            raise GatewayHTTPError("诊断附件上传被拒绝", status_code=exc.code, detail=self._decode_error_detail(exc.read())) from exc
        except OSError as exc:
            raise GatewayError("诊断附件上传暂不可用") from exc

    def platform_logout(self) -> JsonValue:
        return self.post("/api/v1/platform/logout")

    def software_update_check(self) -> JsonValue:
        return self._request("GET", "/api/v1/software-updates/check", timeout=5)

    def motor_parameter_status(self) -> JsonValue:
        return self._request("GET", "/api/v1/motor-parameters")

    def motor_parameter_templates(self, index: int) -> JsonValue:
        return self._request("GET", f"/api/v1/motor-parameters/{int(index)}/templates", timeout=10)

    def motor_parameter_action(self, action: str, payload: dict) -> JsonValue:
        if action not in {"read", "preview", "start", "acknowledge", "readback", "cancel"}:
            raise ValueError("unknown parameter action")
        return self._request("POST", f"/api/v1/motor-parameters/{action}", payload=payload)

    def motor_maintenance_status(self) -> JsonValue:
        return self._request("GET", "/api/v1/motor-maintenance")

    def motor_maintenance_action(self, action: str, payload: dict) -> JsonValue:
        if action not in {"preview", "start", "acknowledge", "readback", "cancel"}:
            raise ValueError("unknown motor maintenance action")
        return self._request("POST", f"/api/v1/motor-maintenance/{action}", payload=payload)

    def motor_firmware_releases(self, index: int) -> JsonValue:
        return self._request("GET", f"/api/v1/motor-firmware/{int(index)}/releases", timeout=30)

    def motor_firmware_download(self, index: int, release_id: str) -> JsonValue:
        if not re.fullmatch(r"[0-9a-f]{32}", release_id):
            raise ValueError("invalid motor firmware release id")
        return self._request("POST", f"/api/v1/motor-firmware/{int(index)}/{release_id}/download", timeout=360)

    def software_update_download(self, release_id: str) -> JsonValue:
        if not re.fullmatch(r"[0-9a-f]{32}", release_id):
            raise ValueError("invalid software release id")
        return self._request("POST", f"/api/v1/software-updates/{release_id}/download", timeout=1800)

    def motor_firmware_resource_releases(self) -> JsonValue:
        return self._request("GET", "/api/v1/motor-firmware-resources/releases", timeout=30)

    def motor_firmware_resource_download(self, release_id: str) -> JsonValue:
        if not re.fullmatch(r"[0-9a-f]{32}", release_id):
            raise ValueError("invalid motor resource release id")
        return self._request("POST", f"/api/v1/motor-firmware-resources/{release_id}/download", timeout=360)

    def software_update_readiness(self) -> JsonValue:
        return self._request("GET", "/api/v1/software-updates/installation-readiness", timeout=5)

    def platform_bench(self) -> JsonValue:
        return self.get("/api/v1/platform/bench")

    def set_platform_bench(self, bench_code: str) -> JsonValue:
        return self.post("/api/v1/platform/bench", {"bench_code": bench_code})

    def submit_execution(
        self,
        test_id: str,
        reason: str = "",
        *,
        verified_status: str = "",
        operator_summary: str = "",
        review: Mapping[str, Any] | None = None,
    ) -> JsonValue:
        payload = {
            "test_id": test_id,
            "reason": reason,
            "verified_status": verified_status,
            "operator_summary": operator_summary,
            "tester": "",
            "bench": "",
            "outcome": "",
            "notes": "",
            "analysis": "",
        }
        if review:
            payload["operator_summary"] = str(review.get("operator_summary") or operator_summary)
            payload["verified_status"] = str(review.get("verified_status") or verified_status)
            for name in ("tester", "bench", "outcome", "notes", "analysis", "station_id", "discrepancy_reason", "test_name", "actual_object"):
                payload[name] = str(review.get(name) or "")
        return self.post("/api/v1/executions/submit", payload)

    def import_execution_history(self, paths: list[str], metadata: Mapping[str, str]) -> JsonValue:
        return self.post("/api/v1/executions/import-history", {"paths": paths, **dict(metadata)})

    def execution_history(self, *, limit: int = 100, offset: int = 0,
                          start_at: str = "", end_at: str = "", sn: str = "", data_domain: str = "", bench: str = "") -> JsonValue:
        query = parse.urlencode({"limit": int(limit), "offset": int(offset),
                                 "start_at": start_at, "end_at": end_at, "sn": sn, "data_domain": data_domain, "bench": bench})
        return self.get(f"/api/v1/executions?{query}")

    def execution_location(self, test_id: str = "") -> JsonValue:
        return self.get("/api/v1/records/location?" + parse.urlencode({"test_id": test_id}))

    def export_execution_history(self, destination: Path | str, *, start_at: str = "", end_at: str = "", sn: str = "", data_domain: str = "", bench: str = "") -> Path:
        query = parse.urlencode({"start_at": start_at, "end_at": end_at, "sn": sn, "data_domain": data_domain, "bench": bench})
        return self.download("/api/v1/executions/export.csv?" + query, destination)

    def execution_summary(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/summary")

    def execution_local_corrections(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/local-corrections")

    def save_local_correction(self, test_id: str, payload: Mapping[str, Any]) -> JsonValue:
        return self.post(f"/api/v1/executions/{parse.quote(test_id, safe='')}/local-corrections", dict(payload))

    def download_execution_report(
        self,
        test_id: str,
        destination: Path | str,
    ) -> Path:
        return self.download(
            f"/api/v1/executions/{parse.quote(test_id, safe='')}/report",
            destination,
        )

    def download_execution_bundle(
        self,
        test_id: str,
        destination: Path | str,
    ) -> Path:
        return self.download(
            f"/api/v1/executions/{parse.quote(test_id, safe='')}/bundle",
            destination,
        )

    def execution_sync_status(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/sync")

    def platform_samples(self) -> JsonValue:
        return self.get("/api/v1/platform/samples")

    def platform_options(self) -> JsonValue:
        return self.get("/api/v1/platform/options")

    def rebuild_execution(self, test_id: str, reason: str) -> JsonValue:
        return self.post("/api/v1/executions/rebuild", {"test_id": test_id, "reason": reason})

    def execution_analysis_versions(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/analysis-revisions")

    def retry_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/retry", {"test_id": test_id})

    def _remember_plc_snapshot(self, payload: JsonValue) -> None:
        value = payload
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping) or not isinstance(value.get("coordination"), Mapping):
            return
        with self._plc_lock:
            old = self._plc_snapshot.get("coordination", {})
            new = value["coordination"]
            epoch = str(new.get("epoch", ""))
            if epoch in self._plc_retired_epochs:
                return
            if old.get("epoch") == epoch:
                if new.get("snapshot_sequence", 0) < old.get("snapshot_sequence", 0):
                    return
            elif old.get("epoch"):
                self._plc_retired_epochs.add(old["epoch"])
            self._plc_snapshot = deepcopy(dict(value))

    def plc_snapshot(self) -> JsonValue:
        result = self.get("/api/v1/plc/snapshot")
        self._remember_plc_snapshot(result)
        return result

    def _plc_post(self, operation: str, payload: Mapping[str, Any],
                  context: Mapping[str, Any] | None = None) -> JsonValue:
        if context is None:
            with self._plc_lock:
                context = dict(self._plc_snapshot.get("coordination", {}))
            if not context and operation != "/api/v1/plc/all-stop":
                self.plc_snapshot()
                with self._plc_lock:
                    context = dict(self._plc_snapshot.get("coordination", {}))
        body = dict(payload)
        body.update(
            request_id=context.get("request_id") or str(uuid.uuid4()),
            expected_epoch=context.get("epoch"),
            expected_revision=context.get("revision"),
        )
        try:
            result = self.post(operation, body)
        except GatewayError as exc:
            if isinstance(exc.detail, Mapping):
                self._remember_plc_snapshot(exc.detail.get("snapshot"))
            # Never automatically refresh and re-send a conflicting command.
            raise
        self._remember_plc_snapshot(result)
        return result

    def plc_connect(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/connect", {"user": user}, context)

    def plc_disconnect(self, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/disconnect", {}, context)

    def plc_start(self, channels: list[int], user: str, voltage: float, current: float,
                  *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/start", {
            "channels": list(channels), "voltage": float(voltage),
            "current": float(current), "user": user,
        }, context)

    def plc_stop(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/stop", {"user": user}, context)

    def plc_all_stop(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/all-stop", {"user": user}, context)

    def plc_reset_fault(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/reset-fault", {"user": user}, context)

    def plc_set_channel(self, channel: int, enabled: bool, user: str,
                        *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post(f"/api/v1/plc/channels/{int(channel)}", {"enabled": bool(enabled), "user": user}, context)

    def plc_set_ps1_setpoints(self, voltage: float, current: float, user: str,
                              *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/setpoints", {"voltage": float(voltage), "current": float(current), "user": user}, context)

    def zero_prepare(self, motor_index: int) -> JsonValue:
        return self.post("/api/v1/zero/prepare", {"motor_index": motor_index})

    def zero_motor(self, motor_index: int) -> JsonValue:
        return self.post("/api/v1/zero/motor", {"motor_index": motor_index})

    def zero_all_motors(self) -> JsonValue:
        return self.post("/api/v1/zero/all")

    def zero_read(self) -> JsonValue:
        return self.post("/api/v1/zero/read")

    def zero_confirm(self, *, allow_on_fault: bool = False) -> JsonValue:
        return self.post(
            "/api/v1/zero/confirm",
            {"allow_on_fault": allow_on_fault},
        )

    def zero_skip(self) -> JsonValue:
        return self.post("/api/v1/zero/skip")

    def zero_abort(self) -> JsonValue:
        return self.post("/api/v1/zero/abort")

    # Concise aliases are convenient for wiring command buttons.
    def pause(self) -> JsonValue:
        return self.playback_pause()

    def resume(self) -> JsonValue:
        return self.playback_resume()

    def stop(self) -> JsonValue:
        return self.playback_stop()

    def reset(self) -> JsonValue:
        return self.playback_reset()

    def disable(self) -> JsonValue:
        return self.playback_disable()
