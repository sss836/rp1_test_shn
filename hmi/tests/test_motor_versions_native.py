"""Requires explicitly supplied offline native test module; never uses SocketCAN."""
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
import os
import pytest

MODULE = os.environ.get("RP1_OFFLINE_MOTOR_SDK")
pytestmark = pytest.mark.skipif(not MODULE, reason="set RP1_OFFLINE_MOTOR_SDK to isolated compiled test SDK")


def test_compiled_lro_response_correlation_and_telemetry_separation():
    sys.path.insert(0, MODULE)
    import motors_py_testing as sdk
    assert sdk._test_lro_version_checks()
    motor = sdk._test_create_version_motor()
    assert motor.request_version(0x31)
    assert bytes(motor.version_payload(0x31))[2:7] == b"1.0.8"
    assert motor.get_response_count() == 0
    assert motor.get_feedback_count() == 0


def test_native_to_backend_service_gateway_chain(tmp_path, monkeypatch):
    sys.path.insert(0, MODULE)
    import motors_py_testing as sdk
    from factory_hmi.core.backend import MotorsPyBackend
    from factory_hmi.core.service import FactoryService
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.models import StationConfig
    from scripts.rp1_limb_common import MotorEntry
    from factory_hmi.gateway.app import create_app
    from test_software_update_integration import asgi_calls
    entry = MotorEntry(index=0, motor_id=3, interface_type="canfd", interface="can22", motor_type="LRO", motor_model=0,
        master_id_offset=0, zero_offset=0., kp=1., kd=1., sign=1., joint_name="测试关节")
    monkeypatch.setitem(sys.modules, "motors_py", sdk)
    monkeypatch.setattr("factory_hmi.core.backend.create_motor_driver", lambda e, sdk: sdk._test_create_version_motor())
    # Native fake transport is injected. Opening real CAN or enabling a motor
    # anywhere in this path is forbidden by the test.
    backend = MotorsPyBackend([entry])
    with patch("socket.socket", side_effect=AssertionError("unexpected network/device socket")):
        backend.open_transport()
        deadline = time.monotonic() + 2
        while backend.motor_versions()[0]["status"] == "unknown" and time.monotonic() < deadline:
            time.sleep(.01)
        assert backend.motor_versions()[0]["raw_firmware"] == "1.0.8"
        assert not backend.motors_enabled
        config = StationConfig(limb="left_arm", entries=(entry,), raw={})
        service = FactoryService(data_root=tmp_path / "service")
        service._controller = FactoryController(config, backend, enable_watchdog=False, enable_recording=False)
        service._backend_name = "motors_py"
        service._can_interfaces = ()
        assert service.snapshot()["motor_versions"][0]["hardware"] == "1.0.2"
    with patch("factory_hmi.gateway.app.PlcCabinetService") as plc:
        plc.return_value.snapshot.return_value = {}
        app = create_app(service, data_root=tmp_path / "gateway")
        response = asyncio.run(asgi_calls(app, [("GET", "/api/v1/snapshot", {})]))[0]
    assert response.status_code == 200
    assert response.json()["motor_versions"][0]["protocol"] == "1.0.0.0"
    assert service._closed
    assert not backend.connected
    assert backend._versions.motors == ()
    backend.close_transport()
    assert backend.motor_versions()[0]["status"] == "stale"
