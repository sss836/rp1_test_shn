"""Read-only fake motors and private sockets; never accesses CAN/PLC hardware."""
import multiprocessing
import socket
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from factory_hmi.core.backend import FakeMotorBackend, MotorProtocolPending, MotorSafetyError, MotorsPyBackend
from factory_hmi.core import backend as backend_module
from factory_hmi.core.controller import FactoryController
from factory_hmi.core.service import FactoryService
from factory_hmi.models import StationConfig, StationState
from factory_hmi.gateway.quick_run import QuickRun
from factory_hmi.gateway.station_process import _Channel, _ERROR_TYPES, _receive, _send, StationOutcomeUnknown


class FakeRuntime:
    def __init__(self):
        self._motion_epoch_guard = threading.Lock()
        self._motion_epoch = 4
        self._motion_interrupts_pending = 0
        self.expired = False
        self.lease = SimpleNamespace(snapshot=lambda: SimpleNamespace(expired=self.expired))
        self.backend = SimpleNamespace(motor_protocols_pending=lambda: False)
        self.controller = SimpleNamespace(controller=SimpleNamespace(backend=self.backend))
        self.calls = []
        self.failure = None

    def _invoke(self, names, **kwargs):
        self.calls.append((names, kwargs))
        if self.failure:
            raise self.failure
        return {"accepted": True}


def runner():
    runtime = FakeRuntime()
    quick = QuickRun(runtime, None, 'offline')
    quick._epoch = runtime._motion_epoch
    quick._power_source = 'external'
    return quick, runtime


def test_waits_only_on_read_and_sends_enable_once():
    quick, runtime = runner()
    reads = []
    def pending():
        reads.append(1)
        return len(reads) < 3
    runtime.backend.motor_protocols_pending = pending
    assert quick._enable_when_protocol_ready(timeout_s=1) == {"accepted": True}
    assert len(reads) == 3
    assert runtime.calls == [(('enable_motors',), {'physical_estop_confirmed': True})]


def test_unknown_enable_outcome_is_never_retried():
    quick, runtime = runner()
    runtime.failure = StationOutcomeUnknown('结果未确认')
    with pytest.raises(StationOutcomeUnknown):
        quick._enable_when_protocol_ready(timeout_s=.5)
    assert len(runtime.calls) == 1


@pytest.mark.parametrize('error', [StationOutcomeUnknown('read 结果未确认'), RuntimeError('read 通道繁忙')])
def test_read_channel_error_never_triggers_enable(error):
    quick, runtime = runner()
    def pending():
        raise error
    runtime.backend.motor_protocols_pending = pending
    with pytest.raises(type(error)):
        quick._enable_when_protocol_ready(timeout_s=.5)
    assert runtime.calls == []


@pytest.mark.parametrize('kind', ['stop_on_ready', 'stop_after_power', 'disconnect'])
def test_stop_and_disconnect_at_wait_boundary_never_retry(kind):
    quick, runtime = runner()
    if kind == 'stop_on_ready':
        def pending():
            quick._stop.set()
            return False
        runtime.backend.motor_protocols_pending = pending
    elif kind == 'stop_after_power':
        checks = []
        def check_power():
            checks.append(1)
            if len(checks) == 2:
                quick._stop.set()
        quick._check_power = check_power
    else:
        # A disconnect concurrent with readiness rejects the single command;
        # it must not trigger another attempt.
        runtime.failure = RuntimeError('工位已断开或重新配置')
    with pytest.raises(RuntimeError):
        quick._enable_when_protocol_ready(timeout_s=.5)
    assert len(runtime.calls) == (1 if kind == 'disconnect' else 0)


@pytest.mark.parametrize('condition', ['stop', 'epoch', 'lease', 'power'])
def test_safety_change_during_read_wait_never_enables(condition):
    quick, runtime = runner()
    power_ok = [True]
    if condition == 'power':
        quick._check_power = lambda: None if power_ok[0] else (_ for _ in ()).throw(RuntimeError('PLC 供电许可无效'))
    def pending():
        if condition == 'stop':
            quick._stop.set()
        elif condition == 'epoch':
            runtime._motion_epoch += 1
        elif condition == 'lease':
            runtime.expired = True
        else:
            power_ok[0] = False
        return True
    runtime.backend.motor_protocols_pending = pending
    with pytest.raises(RuntimeError):
        quick._enable_when_protocol_ready(timeout_s=.5)
    assert runtime.calls == []


def test_pending_timeout_does_not_enable_and_is_station_scoped():
    first, first_runtime = runner()
    second, second_runtime = runner()
    first_runtime.backend.motor_protocols_pending = lambda: True
    with pytest.raises(RuntimeError, match='未下发使能'):
        first._enable_when_protocol_ready(timeout_s=.03)
    assert first_runtime.calls == []
    assert second._enable_when_protocol_ready(timeout_s=.5) == {"accepted": True}
    assert len(second_runtime.calls) == 1


def test_actual_controller_disconnect_at_readiness_cannot_enable(tmp_path):
    quick, runtime = runner()
    backend = FakeMotorBackend(1)
    backend.open_transport()
    controller = FactoryController(StationConfig(limb='left_arm', entries=backend.entries, raw={}),
                                   backend, enable_watchdog=False)
    controller.state_machine.transition(StationState.CONNECTED)
    service = FactoryService(data_root=tmp_path)
    service._controller = controller
    runtime.controller.controller = controller
    def disconnected_before_enable():
        backend.close_transport()
        controller.state_machine.transition(StationState.DISCONNECTED)
        return False
    backend.motor_protocols_pending = disconnected_before_enable
    runtime._invoke = lambda names, **kwargs: service.enable_motors(**kwargs)
    with pytest.raises(RuntimeError):
        quick._enable_when_protocol_ready(timeout_s=.5)
    assert not backend.connected and not backend.motors_enabled


def test_seven_motor_protocol_prepare_keeps_previous_overall_deadline(monkeypatch):
    entries = [replace(e, motor_type='LRO', interface_type='canfd', motor_model=0)
               for e in FakeMotorBackend(7).entries]
    backend = MotorsPyBackend(entries)
    backend._connected = True
    backend._versions = SimpleNamespace(timeout=.5)
    now = [0.]
    clock = SimpleNamespace(monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(backend_module, 'time', clock)
    with pytest.raises(MotorSafetyError, match='电机协议未确认'):
        backend.prepare_motor_protocols()
    assert 5.15 <= now[0] < 5.17
    assert all(value is None for value in backend._selected_protocols)


def test_fault_reset_with_unconfirmed_protocol_never_clears_errors(tmp_path, monkeypatch):
    backend = FakeMotorBackend(1)
    controller = FactoryController(StationConfig(limb='left_arm', entries=backend.entries, raw={}),
                                   backend, enable_watchdog=False)
    called = []
    def reject(*args, **kwargs):
        raise MotorSafetyError('电机协议未确认')
    monkeypatch.setattr(backend, 'prepare_motor_protocols', reject)
    monkeypatch.setattr(backend, 'clear_errors', lambda *args: called.append(True))
    with pytest.raises(MotorSafetyError, match='电机协议未确认'):
        controller.reset_motor_fault()
    assert called == [] and not backend.motors_enabled


def child_pending_response(conn):
    _receive(conn, bytearray())
    exc = MotorProtocolPending('电机版本读取中，本次未使能')
    # Same mapping as station_process.serve(), across an actual process.
    kind = next((name for name, error in _ERROR_TYPES.items() if isinstance(exc, error)), 'RuntimeError')
    _send(conn, (kind, str(exc)))
    conn.close()


def test_pending_exception_survives_private_process_channel():
    parent, child = socket.socketpair()
    process = multiprocessing.get_context('fork').Process(target=child_pending_response, args=(child,))
    process.start()
    child.close()
    try:
        with pytest.raises(MotorProtocolPending, match='本次未使能'):
            _Channel(parent).call('enable_motors', (), {}, timeout=2)
    finally:
        parent.close()
        process.join(2)
    assert process.exitcode == 0
