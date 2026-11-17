"""Trusted shutdown-context forwarding; mock services and fake motors only."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from factory_hmi import FactoryController
from factory_hmi.core.backend import MotorSafetyError
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.app import GatewayHub, GatewayRuntime
from factory_hmi.gateway.station_process import StationProcess
from factory_hmi.sync.outbox import Outbox
from test_factory_hmi_core import fake_station
from test_disable_confirmation import Drive, backend as synthetic_backend
from factory_hmi.models import SafetyLimits, StationConfig


@pytest.mark.parametrize("isolated", [False, True])
def test_controller_only_trusted_shutdown_uses_isolated_backend_release(isolated):
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    ordinary = Mock(side_effect=RuntimeError("synthetic missing disable reply"))
    released = Mock()
    backend.disconnect = ordinary
    backend.disconnect_site_isolated = released
    backend.disable = Mock(side_effect=RuntimeError("synthetic missing disable reply"))
    if isolated:
        controller.disconnect_for_shutdown(site_isolated=True)
        released.assert_called_once_with()
        ordinary.assert_not_called()
    else:
        with pytest.raises(RuntimeError, match="missing disable reply"):
            controller.disconnect_for_shutdown()
        ordinary.assert_called_once_with()
        released.assert_not_called()
    assert controller._shutdown_committed is False


def synthetic_controller(backend):
    config = StationConfig(limb="left_arm", entries=backend.entries, raw={},
                           control_rate_hz=200., safety_limits=SafetyLimits())
    return FactoryController(config, backend, enable_recording=False)


def test_controller_read_only_poweroff_closes_without_turning_missing_reply_into_enabled_state():
    motor = Drive(cached_false=True, replies=False)
    backend = synthetic_backend(motor, enabled=False)
    controller = synthetic_controller(backend)
    controller.disconnect_for_shutdown()
    status = backend.disable_status()
    assert not backend.connected and not backend.motors_enabled and not backend.motors
    assert status["state"] == "unknown" and status["read_only_release"] is True
    assert motor.enables == motor.refreshes == 0
    assert controller._shutdown_committed is False


def test_controller_previously_enabled_missing_stop_reply_remains_strict_and_keeps_handles():
    motor = Drive(never=True, replies=False)
    backend = synthetic_backend(motor)
    backend._host_enable_attempt_epoch = backend._enable_open_epoch
    controller = synthetic_controller(backend)
    with pytest.raises(MotorSafetyError, match="失能反馈未确认"):
        controller.disconnect_for_shutdown()
    assert backend.connected and backend.motors == [motor]
    assert backend.motors_enabled and backend.commands_inhibited
    assert backend.disable_status()["state"] == "unconfirmed"
    assert backend.disable_status()["motors"][0]["attempts"] == 3
    assert motor.disables >= 3 and motor.enables == 0
    assert controller._shutdown_committed is False


def test_controller_normal_disconnect_still_disables_fake_motors_through_backend_transaction():
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    if not backend.motors_enabled:
        backend.enable_motors()
    assert backend.motors_enabled and backend.enabled.all()
    controller.disconnect()
    assert not backend.connected and not backend.motors_enabled and not backend.enabled.any()


def test_isolated_shutdown_keeps_existing_active_protection_boundary():
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    controller._damping_transition = True
    backend.disconnect_site_isolated = Mock()
    try:
        with pytest.raises(RuntimeError, match="保护阻尼"):
            controller.disconnect_for_shutdown(site_isolated=True)
        backend.disconnect_site_isolated.assert_not_called()
        assert backend.connected
    finally:
        controller._damping_transition = False
        controller.disconnect()


@pytest.mark.parametrize("isolated", [False, True])
def test_service_forwards_context_after_stopping_tasks_and_draining_records(tmp_path, isolated):
    service = FactoryService(data_root=tmp_path)
    calls = []
    controller = SimpleNamespace(
        protective_damping_snapshot=lambda: None,
        disconnect_for_shutdown=lambda **kw: calls.append(("disconnect", kw)),
        disconnect=lambda: pytest.fail("atomic shutdown entry must be used"))
    service._controller = controller
    service._wait_for_report_staging = lambda timeout: calls.append(("drain", timeout))
    service._require_recording_closed = lambda: calls.append(("recording_closed", None))
    assert service.close(site_isolated=isolated)["closed"]
    assert calls[0] == ("disconnect", {"site_isolated": True} if isolated else {})
    assert [name for name, _ in calls] == ["disconnect", "drain", "recording_closed"]


@pytest.mark.parametrize("isolated", [False, True])
def test_runtime_forwards_context_and_can_release_to_service(tmp_path, isolated):
    service = SimpleNamespace(close=Mock(), protective_damping_snapshot=lambda: None)
    runtime = GatewayRuntime(service, outbox=Outbox(tmp_path/"outbox.sqlite"),
                             platform=Mock(), data_root=tmp_path)
    runtime.close(disconnect_can=True, site_isolated=isolated)
    expected = {"disconnect_can": True}
    if isolated:
        expected["site_isolated"] = True
    service.close.assert_called_once_with(**expected)


def test_hub_forwards_isolation_to_stations_before_closing_plc(tmp_path):
    with patch("factory_hmi.gateway.app.PlcCabinetService"):
        hub = GatewayHub(None, data_root=tmp_path, lease_timeout_s=2., status_rate_hz=10.)
    events = []
    runtime = SimpleNamespace(close=lambda **kw: events.append(("station", kw)))
    hub._sessions = {"synthetic": runtime}
    hub.plc.close = lambda **kw: events.append(("plc", kw))
    hub._platform.close = lambda: events.append(("platform", {}))
    hub.close(site_isolated=True)
    assert events == [("station", {"site_isolated": True}),
                      ("plc", {"site_isolated": True}), ("platform", {})]


@pytest.mark.parametrize("isolated", [False, True])
def test_station_process_private_close_rpc_carries_context_and_joins(tmp_path, isolated):
    station = StationProcess(tmp_path/"session", tmp_path/"records", Mock(), 30.)
    alive, events = [True], []
    station._process = SimpleNamespace(is_alive=lambda: alive[0], join=lambda timeout: events.append("join"))
    def rpc(name, args, kwargs, **options):
        events.append((name, args, kwargs))
        alive[0] = False
    station._channels = {"safety": SimpleNamespace(call=rpc, pending=None,
        connection=SimpleNamespace(close=lambda: events.append("channel_closed")))}
    station.close(site_isolated=isolated)
    assert events == [("close", (), {"site_isolated": True} if isolated else {}),
                      "join", "channel_closed"]
    assert station._close_confirmed and station._closing


@pytest.mark.parametrize("value", ["false", "true", 1, None])
def test_private_station_close_rejects_nonboolean_context_before_io(tmp_path, value):
    station = StationProcess(tmp_path/"session", tmp_path/"records", Mock(), 30.)
    with pytest.raises(ValueError, match="布尔值"):
        station.close(site_isolated=value)
    assert station._process is None


@pytest.mark.parametrize("isolated", [False, True])
def test_real_private_station_close_channel_accepts_context_with_empty_mock_station(tmp_path, monkeypatch, isolated):
    # No motor configuration or real device handles are created in this child.
    monkeypatch.setenv("RP1_FACTORY_CAN_DISABLED", "1")
    monkeypatch.setenv("RP1_FACTORY_PLATFORM_URL", "")
    station = StationProcess(tmp_path/"session", tmp_path/"records",
                             Outbox(tmp_path/"outbox.sqlite"), 30.)
    station._start()
    station.close(site_isolated=isolated)
    assert station._close_confirmed and not station._process.is_alive()
