from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib import error
from unittest import mock

from factory_hmi.client import GatewayClient, GatewayHTTPError


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class GatewayClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GatewayClient("http://127.0.0.1:8765/", timeout=3.5)

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_get_uses_expected_url_and_timeout(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b'{"status":"ok"}')

        result = self.client.health()

        self.assertEqual(result, {"status": "ok"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8765/api/v1/health")
        self.assertEqual(request.method, "GET")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 3.5)

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_post_serializes_json_and_headers(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b'{"accepted":true}')

        result = self.client.configure("/tmp/left.yaml", "left_leg", "hardware")

        self.assertEqual(result, {"accepted": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://127.0.0.1:8765/api/v1/configure"
        )
        self.assertEqual(request.method, "POST")
        self.assertEqual(
            json.loads(request.data),
            {
                "config_path": "/tmp/left.yaml",
                "limb": "left_leg",
                "backend": "hardware",
            },
        )
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_websocket_url_matches_http_security(self) -> None:
        self.assertEqual(
            self.client.status_ws_url,
            f"ws://127.0.0.1:8765/ws/status?client_id={self.client.client_id}",
        )
        secure = GatewayClient("https://gateway.example.test/prefix")
        self.assertEqual(
            secure.build_ws_url(),
            f"wss://gateway.example.test/ws/status?client_id={secure.client_id}",
        )

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_local_maintenance_routes_keep_mode_in_query(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')

        self.client.motor_maintenance_status(local=True)
        self.client.motor_maintenance_action("start", {"confirmed": True}, local=True)
        self.client.motor_parameter_status(local=True)
        self.client.motor_parameter_templates(2, local=True)
        self.client.motor_parameter_action("read-reference", {"indices": [2]}, local=True)
        self.client.motor_firmware_cached_releases(2)
        self.client.motor_firmware_import(2, "/tmp/release.json")
        self.client.motor_parameter_import("/tmp/template.json")
        self.client.motor_maintenance_status()

        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual([(r.method, r.selector) for r in requests], [
            ("GET", "/api/v1/motor-maintenance?local=true"),
            ("POST", "/api/v1/motor-maintenance/start?local=true"),
            ("GET", "/api/v1/motor-parameters?local=true"),
            ("GET", "/api/v1/motor-parameters/2/templates?local=true"),
            ("POST", "/api/v1/motor-parameters/read-reference?local=true"),
            ("GET", "/api/v1/motor-firmware/2/cached-releases"),
            ("POST", "/api/v1/motor-firmware/2/import"),
            ("POST", "/api/v1/motor-parameters/import"),
            ("GET", "/api/v1/motor-maintenance"),
        ])
        self.assertEqual(json.loads(requests[1].data), {"confirmed": True})
        self.assertEqual(json.loads(requests[4].data), {"indices": [2]})
        self.assertEqual(json.loads(requests[6].data), {"manifest_path": "/tmp/release.json"})
        self.assertEqual(json.loads(requests[7].data), {"manifest_path": "/tmp/template.json"})

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_local_maintenance_credential_stays_on_direct_loopback_requests(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')
        with mock.patch.dict("os.environ", {"RP1_HMI_LOCAL_MAINTENANCE_TOKEN": "test-local-token"}):
            local = GatewayClient("http://127.0.0.1:8766")
            remote = GatewayClient("https://gateway.example.test")
        local.motor_maintenance_status(local=True)
        local.health()
        remote.motor_maintenance_status(local=True)

        maintenance, health, remote_maintenance = [call.args[0] for call in urlopen.call_args_list]
        header = "X-rp1-local-maintenance"
        self.assertEqual(maintenance.unredirected_hdrs[header], "test-local-token")
        self.assertNotIn(header, maintenance.headers)
        self.assertFalse(health.has_header(header))
        self.assertFalse(remote_maintenance.has_header(header))

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_motor_version_refresh_is_separate_from_status(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')
        client = GatewayClient("http://127.0.0.1:8766", operator_token="operator-token")
        client.refresh_motor_versions()
        client.motor_version_refresh_status()
        trigger, status = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual((trigger.method, trigger.selector), ("POST", "/api/v1/motor-versions/refresh"))
        self.assertEqual(json.loads(trigger.data), {})
        self.assertEqual((status.method, status.selector), ("GET", "/api/v1/motor-versions/refresh"))
        for req in (trigger, status):
            self.assertEqual(req.get_header("X-rp1-operator-token"), "operator-token")
            self.assertEqual(req.get_header("X-rp1-client-id"), client.client_id)

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_standalone_can_and_enable_endpoints(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')

        self.client.can_connect()
        can_request = urlopen.call_args.args[0]
        self.assertEqual(can_request.full_url, self.client.base_url + "/api/v1/can/connect")

        self.client.discover_motors()
        discover_request = urlopen.call_args.args[0]
        self.assertEqual(
            discover_request.full_url,
            self.client.base_url + "/api/v1/motors/discover",
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 15.0)

        self.client.enable_motors(physical_estop_confirmed=True)
        enable_request = urlopen.call_args.args[0]
        self.assertEqual(
            enable_request.full_url,
            self.client.base_url + "/api/v1/motors/enable",
        )
        self.assertEqual(
            json.loads(enable_request.data),
            {"physical_estop_confirmed": True},
        )
        self.client.reset_motor_fault()
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            self.client.base_url + "/api/v1/motors/reset-fault",
        )

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_configuration_upload_and_editable_overrides(self, urlopen: mock.Mock) -> None:
        urlopen.return_value = FakeResponse(b'{"ok":true}')
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "arm.yaml"
            path.write_text("kp: [70.0]\n", encoding="utf-8")
            self.client.config_upload(path, "left_arm")
        upload = urlopen.call_args.args[0]
        self.assertEqual(upload.data, b"kp: [70.0]\n")
        self.assertEqual(upload.get_header("X-rp1-limb"), "left_arm")
        self.assertEqual(upload.get_header("X-filename"), "arm.yaml")

        bindings = [
            {
                "index": 0,
                "interface": "can2",
                "mode": "canfd",
                "bitrate": 1_000_000,
                "dbitrate": 5_000_000,
            }
        ]
        motors = [
            {
                "index": 0,
                "motor_id": 1,
                "motor_type": "LRO",
                "motor_model": 2,
                "zero_offset": 0.0,
                "kp": 80.0,
                "kd": 2.0,
                "sign": 1.0,
            }
        ]
        self.client.configure(
            "/var/lib/rp1-factory-hmi/configs/arm.yaml",
            "left_arm",
            "motors_py",
            bus_bindings=bindings,
            motor_overrides=motors,
        )
        configured = urlopen.call_args.args[0]
        payload = json.loads(configured.data)
        self.assertEqual(payload["bus_bindings"], bindings)
        self.assertEqual(payload["motor_overrides"], motors)

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_http_error_exposes_status_and_service_detail(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.side_effect = error.HTTPError(
            "http://127.0.0.1:8765/api/v1/arm",
            409,
            "Conflict",
            {},
            io.BytesIO(b'{"detail":{"code":"unsafe","reason":"preflight"}}'),
        )

        with self.assertRaises(GatewayHTTPError) as caught:
            self.client.arm()

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.service_detail,
            {"code": "unsafe", "reason": "preflight"},
        )
        self.assertIn("HTTP 409", str(caught.exception))
        self.assertIn("preflight", str(caught.exception))

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_empty_success_response_returns_empty_object(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b" \n")

        result = self.client.playback_stop()

        self.assertEqual(result, {})
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8765/api/v1/playback/stop",
        )
        self.assertEqual(json.loads(request.data), {})

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_playback_start_uses_complete_gateway_contract(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.playback_start(
            command_id="contract-start-1",
            expected_config_revision="config-v1",
            speed=0.25,
            loop=True,
            cycles=12,
            duration_hours=8.0,
            record=True,
            record_rate_hz=10.0,
            test_id="AGE-42",
            robot_id="RP1-007",
        )

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8765/api/v1/playback/start",
        )
        self.assertEqual(
            json.loads(request.data),
            {
                "command_id": "contract-start-1",
                "expected_state": "armed",
                "expected_config_revision": "config-v1",
                "speed": 0.25,
                "loop": True,
                "cycles": 12,
                "duration_hours": 8.0,
                "record": True,
                "record_rate_hz": 10.0,
                "test_id": "AGE-42",
                "robot_id": "RP1-007",
            },
        )

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_zero_confirm_never_implies_fault_override(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.zero_confirm()

        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"allow_on_fault": False})

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_direct_zero_requests_use_explicit_routes(
        self, urlopen: mock.Mock
    ) -> None:
        urlopen.return_value = FakeResponse(b"{}")

        self.client.zero_motor(3)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/zero/motor"))
        self.assertEqual(json.loads(request.data), {"motor_index": 3})

        self.client.zero_all_motors()
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/api/v1/zero/all"))

    @mock.patch("factory_hmi.client.GatewayClient._open")
    def test_client_identity_token_and_binary_upload(
        self, urlopen: mock.Mock
    ) -> None:
        received = []

        def consume_upload(req, **_kwargs):
            received.append(req.data.read(8192))
            return FakeResponse(b'{"ok":true}')

        urlopen.side_effect = consume_upload
        client = GatewayClient(
            "http://127.0.0.1:8765",
            client_id="station-operator",
            operator_token="secret",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "aging.npz"
            path.write_bytes(b"npz")
            result = client.trajectory_upload(path)

        self.assertEqual(result, {"ok": True})
        sent = urlopen.call_args.args[0]
        self.assertEqual(received, [b"npz"])
        self.assertTrue(sent.data.closed)
        self.assertEqual(sent.get_header("Content-length"), "3")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 120.0)
        self.assertEqual(sent.get_header("X-rp1-client-id"), "station-operator")
        self.assertEqual(sent.get_header("X-rp1-operator-token"), "secret")
        self.assertEqual(sent.get_header("X-filename"), "aging.npz")
        self.assertEqual(
            client.status_ws_url,
            "ws://127.0.0.1:8765/ws/status?"
            "client_id=station-operator&token=secret",
        )


if __name__ == "__main__":
    unittest.main()
