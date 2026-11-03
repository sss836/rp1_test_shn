"""Isolated OTA integration checks; no live platform or device connections."""
from __future__ import annotations

import http.cookiejar
import asyncio
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

from factory_hmi.gateway.app import create_app
from factory_hmi.gateway.software_updates import SoftwareUpdates, installation_readiness
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient


async def asgi_calls(app, calls):
    # This sandbox's socketless runtime needs a timer to wake cross-thread
    # callbacks. The pulse is harness-only, not a control/latency assertion.
    async def pulse():
        while True:
            await asyncio.sleep(.01)
    timer = asyncio.create_task(pulse())
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
                return [await asyncio.wait_for(client.request(method, path, **kwargs), 5)
                        for method, path, kwargs in calls]
    finally:
        timer.cancel()
        try:
            await timer
        except asyncio.CancelledError:
            pass


def test_software_transport_has_independent_cookie_jar_and_refuses_redirects():
    client = ReliabilityPlatformClient("https://platform.invalid")
    cookie = http.cookiejar.Cookie(0, "rp1_session", "test-secret", None, False,
                                  "platform.invalid", False, False, "/", True, False,
                                  None, True, None, None, {}, False)
    client.cookies.set_cookie(cookie)
    opener = client._software_opener()
    handler = next(h for h in opener.handlers if isinstance(h, __import__("urllib.request", fromlist=["HTTPCookieProcessor"]).HTTPCookieProcessor))
    assert handler.cookiejar is not client.cookies
    client.cookies.clear()
    assert len(list(handler.cookiejar)) == 1
    redirect = next(h for h in opener.handlers if hasattr(h, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid") is None


def test_short_version_query_uses_dedicated_channel_and_envelope():
    client = ReliabilityPlatformClient("https://platform.invalid")
    response = io.BytesIO(json.dumps({"data": {"available": False}}).encode())
    opener = Mock()
    opener.open.return_value = response
    with patch.object(client, "_software_opener", return_value=opener):
        assert client.software_latest({"os_id": "ubuntu", "os_version": "24.04",
                                       "architecture": "amd64", "current_version": "2.0.13"}) == {"available": False}
    request = opener.open.call_args.args[0]
    assert "/api/v1/hmi/updates/latest?" in request.full_url
    assert opener.open.call_args.kwargs["timeout"] == 3
    assert "ingestion" not in request.full_url


def test_optional_check_never_turns_login_or_control_into_update_failure():
    class Controller:
        def snapshot(self):
            return {"state": "connected", "motors": []}
        def close(self):
            pass
    with tempfile.TemporaryDirectory() as directory, patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        plc.return_value.snapshot.return_value = {}
        app = create_app(Controller(), data_root=Path(directory))
        response, health = asyncio.run(asgi_calls(app, [("GET", "/api/v1/software-updates/check", {}),
                                                       ("GET", "/api/v1/health", {})]))
        assert response.status_code == 200
        assert response.json()["ok"] is False
        assert "登录" in response.json()["result"]["reason"]
        assert health.json()["ok"] is True


def test_viewer_and_changed_release_cannot_start_a_download():
    platform = SimpleNamespace(_lock=threading.RLock(), _identity="viewer",
                               _mode="reliability_v1", _uploader=SimpleNamespace(client=Mock()))
    updates = SoftwareUpdates(platform, Path("/unused"))
    with patch.object(updates, "check", return_value={"available": True, "can_update": False}):
        try:
            updates.download("a" * 32)
        except ValueError as exc:
            assert "无权" in str(exc)
        else:
            raise AssertionError("viewer download accepted")
    with patch.object(updates, "check", return_value={"available": True, "can_update": True,
                                                        "release": {"manifest": {"release_id": "b" * 32}}}):
        try:
            updates.download("a" * 32)
        except ValueError as exc:
            assert "变化" in str(exc)
        else:
            raise AssertionError("stale release accepted")
    platform._uploader.client.software_package.assert_not_called()


def test_software_disabled_and_stopped_plc_do_not_fabricate_motor_acknowledgement():
    runtime = SimpleNamespace(_invoke=lambda _names: {"station": {"state": "completed", "motors_enabled": False}})
    plc = SimpleNamespace(snapshot=lambda: {"driver": "modbus_tcp", "communication_state": "LIVE", "stale": False,
                                           "status": {"MainReady": False, "ChannelPermit": [False] * 4,
                                                      "MainContactorFB": False, "PS1FeedbackValid": True,
                                                      "PS1StopOffConfirmed": True},
                                           "feedback": {"AllStop": {"state": "confirmed"}}})
    hub = SimpleNamespace(_lock=threading.RLock(), _sessions={"window": runtime}, plc=plc)
    result = installation_readiness(hub)
    assert result["ready"] is False
    assert any("实际失能确认" in text for text in result["reasons"])
    assert not any("PLC 正常总停止" in text for text in result["reasons"])
    plc.snapshot = lambda: {"driver": "modbus_tcp", "communication_state": "LIVE", "stale": True}
    assert any("PLC 正常总停止" in text for text in installation_readiness(hub)["reasons"])


def test_update_monitor_rejects_motion_and_plc_start_but_keeps_protective_stop():
    class Controller:
        def __init__(self):
            self.disable_motors = Mock()
        def snapshot(self):
            return {"state": "connected", "motors": []}
        def close(self):
            pass
    controller = Controller()
    with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"RP1_HMI_UPDATE_MONITOR_ONLY": "1"}), patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        plc.return_value.snapshot.return_value = {}
        plc.return_value.coordination.execute.return_value = {"ok": True}
        app = create_app(controller, data_root=Path(directory))
        responses = asyncio.run(asgi_calls(app, [
            ("POST", "/api/v1/connect", {"headers": {"X-RP1-Client-ID": "window"}}),
            ("POST", "/api/v1/arm", {"headers": {"X-RP1-Client-ID": "window"}}),
            ("POST", "/api/v1/plc/start", {"json": {"user": "operator", "channels": [1], "voltage": 1, "current": 1}}),
            ("POST", "/api/v1/plc/all-stop", {"json": {"user": "operator"}}),
        ]))
        assert [response.status_code for response in responses] == [403, 403, 403, 200]
        assert plc.return_value.coordination.execute.call_args.args[0] == "all-stop"


def test_health_keeps_loaded_version_when_installed_metadata_changes():
    class Controller:
        def snapshot(self):
            return {"state": "connected", "motors": []}
        def close(self):
            pass
    with tempfile.TemporaryDirectory() as directory, patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        plc.return_value.snapshot.return_value = {}
        with patch("factory_hmi.version.code_version", return_value="2.0.13"):
            app = create_app(Controller(), data_root=Path(directory))
        with patch("factory_hmi.version.code_version", return_value="2.0.14"):
            response, = asyncio.run(asgi_calls(app, [("GET", "/api/v1/health", {})]))
        assert response.json()["software"]["running_version"] == "2.0.13"
        assert response.json()["software"]["start_ticks"]


def test_normal_stop_route_calls_finish_without_protective_cancellation():
    class Controller:
        def __init__(self):
            self.finish = Mock(return_value=None)
            self.stop = Mock()
            self.request_motion_stop = Mock()
        def snapshot(self):
            return {"state": "running", "motors": []}
        def close(self):
            pass
    controller = Controller()
    with tempfile.TemporaryDirectory() as directory, patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        plc.return_value.snapshot.return_value = {}
        app = create_app(controller, data_root=Path(directory))
        response, = asyncio.run(asgi_calls(app, [("POST", "/api/v1/playback/stop",
                                                 {"headers": {"X-RP1-Client-ID": "window"}})]))
        assert response.status_code == 200, response.text
        controller.finish.assert_called_once()
        # The ASGI lifespan closes its mock controller after the response.
        # Only that explicit shutdown may call the protective stop path.
        assert all(call.kwargs == {"reason": "gateway shutdown"}
                   for call in controller.stop.call_args_list)
        controller.request_motion_stop.assert_not_called()
