"""Ordinary shutdown must not remove existing protective damping; fake only."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.app import GatewayRuntime


def test_runtime_exit_keeps_active_damping_and_lease_worker(tmp_path):
    status = {"active": True, "safe_confirmed": False}
    device = SimpleNamespace(protective_damping_snapshot=lambda: dict(status),
                             disconnect=Mock(), close=Mock(), stop=Mock())
    runtime = GatewayRuntime(device, lease_timeout_s=30, status_rate_hz=10,
                             data_root=tmp_path, outbox=Mock(), platform=Mock())
    runtime.quick = Mock()
    with pytest.raises(RuntimeError, match="保护阻尼仍在运行"):
        runtime.close()
    assert not runtime._stop.is_set()
    runtime.quick.cancel.assert_not_called()
    device.disconnect.assert_not_called()
    device.close.assert_not_called()
    device.stop.assert_not_called()
    status["active"] = False  # Simulate existing explicitly authorized disable.
    runtime.snapshot = lambda: {"state": "disconnected"}
    runtime.close()
    device.disconnect.assert_not_called()
    device.close.assert_called_once()


def test_facade_exit_cannot_bypass_runtime_support_guard(tmp_path):
    service = FactoryService(data_root=tmp_path, outbox=Mock())
    damping = {"active": True, "safe_confirmed": False}
    device = Mock()
    device._protective_damping.snapshot.return_value = damping
    service._controller = device
    with pytest.raises(RuntimeError, match="保护阻尼仍在运行"):
        service.close()
    assert not service._closing.is_set()
    assert not service._closed
    device.disconnect.assert_not_called()
    service._controller = None
    service.close()


def test_damping_activated_during_stop_is_not_disconnected(tmp_path):
    status = {"active": False}
    def stop(**kwargs):
        status["active"] = True
    device = SimpleNamespace(protective_damping_snapshot=lambda: dict(status),
                             disconnect=Mock(), stop=stop)
    runtime = GatewayRuntime(device, lease_timeout_s=30, status_rate_hz=10,
                             data_root=tmp_path, outbox=Mock(), platform=Mock())
    runtime.snapshot = lambda: {"state": "running"}
    with pytest.raises(RuntimeError, match="保护阻尼仍在运行"):
        runtime.close()
    device.disconnect.assert_not_called()


def test_real_controller_protection_after_facade_snapshot_cannot_be_withdrawn(tmp_path):
    import threading
    from factory_hmi import FactoryController
    from test_factory_hmi_core import fake_station
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    service = FactoryService(data_root=tmp_path, outbox=Mock())
    service._controller = controller
    observed, release = threading.Event(), threading.Event()
    errors = []
    original = service.protective_damping_snapshot
    def snapshot():
        value = original()
        observed.set()
        assert release.wait(2)
        return value
    service.protective_damping_snapshot = snapshot
    def close():
        try:
            service.close()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=close)
    thread.start()
    try:
        assert observed.wait(1)
        controller.enter_protective_damping('synthetic fault')
        release.set()
        thread.join(2)
        assert not thread.is_alive() and errors
        assert controller._protective_damping.snapshot()['active']
        assert backend.connected and backend.motors_enabled
    finally:
        release.set()
        controller.disconnect()  # Explicit test cleanup, not ordinary exit.
        service._controller = None
        service.close()


def test_fault_registered_during_shutdown_stop_yields_without_deadlock():
    import time
    from factory_hmi import FactoryController
    from factory_hmi.models import FaultStatus
    from test_factory_hmi_core import fake_station
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    original = controller._stop_position_hold
    once = [False]
    def stop_hold(*args, **kwargs):
        original(*args, **kwargs)
        if not once[0]:
            once[0] = True
            fault = FaultStatus(message='injected during stop', kind='SoftwareFault', timestamp_s=time.monotonic())
            assert controller._request_protective_fault(fault, backend.read_samples())
    controller._stop_position_hold = stop_hold
    try:
        with pytest.raises(RuntimeError, match='保护阻尼'):
            controller.disconnect_for_shutdown()
        deadline = time.monotonic()+2
        while not (controller._protective_damping and controller._protective_damping.snapshot()['active']):
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert backend.connected and backend.motors_enabled
    finally:
        controller._stop_position_hold = original
        controller.disconnect()


def test_late_protection_after_shutdown_commit_cannot_reenable():
    import threading
    from factory_hmi import FactoryController
    from test_factory_hmi_core import fake_station
    config, backend = fake_station()
    controller = FactoryController(config, backend, enable_recording=False)
    controller.connect()
    committed, release = threading.Event(), threading.Event()
    original = backend.disable
    def disable(*args, **kwargs):
        assert controller._shutdown_committed
        committed.set()
        assert release.wait(2)
        return original(*args, **kwargs)
    backend.disable = disable
    errors, late_result = [], []
    def close():
        try:
            controller.disconnect_for_shutdown()
        except Exception as exc:
            errors.append(exc)
    closer = threading.Thread(target=close)
    later = threading.Thread(target=lambda: late_result.append(controller.enter_protective_damping('late fault')))
    closer.start()
    try:
        assert committed.wait(1)
        later.start()
        release.set()
        closer.join(2); later.join(2)
        assert not closer.is_alive() and not later.is_alive() and not errors
        assert late_result == [None]
        assert not backend.connected and not backend.motors_enabled
    finally:
        release.set()
        backend.disable = original
        controller.disconnect()
