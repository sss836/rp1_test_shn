"""Fake motors only: loss of UI keeps passive damping, never enables/reclaims."""
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import numpy as np
import pytest
from factory_hmi import FactoryController,StationState
from factory_hmi.core.backend import FakeMotorBackend,MotorSafetyError,MotorsPyBackend
from factory_hmi.core.protective_damping import ProtectiveDamping
from factory_hmi.gateway.app import GatewayRuntime
from factory_hmi.gateway.lease import LeaseConflict
from test_factory_hmi_core import fake_station

def until(check,timeout=2):
    end=time.monotonic()+timeout
    while not check():
        assert time.monotonic()<end,'condition timeout'
        time.sleep(.005)

@pytest.fixture
def connected():
    config,backend=fake_station();backend.connect()
    yield config,backend
    backend.disconnect()

def test_absolute_kd_one_without_position_servo_or_feedforward(connected):
    _,backend=connected;backend.positions[:]=[1.2,-.7];backend.speeds[:]=[.4,-.2]
    session=ProtectiveDamping(backend,kd=1)
    try:
        session.start();until(lambda:session.snapshot()['command_count']>=3)
        last=backend.command_history[-1]
        assert last['mode']=='protective_damping'
        assert np.array_equal(last['kp'],[0,0]) and np.array_equal(last['kd'],[1,1])
        assert np.array_equal(last['velocity'],[0,0]) and np.array_equal(last['feedforward_torque'],[0,0])
        assert np.allclose(backend.positions,[1.2,-.7])
        assert np.allclose(backend.torques,[-.4,.2])
        assert backend.motors_enabled and backend.connected
        assert session.snapshot()['safe_confirmed'] is False
        with pytest.raises(MotorSafetyError):backend.command_mit([0,0])
    finally:session.stop()

def test_damping_cannot_enable_or_ignore_explicit_disable(connected):
    _,backend=connected
    with pytest.raises(MotorSafetyError):backend.command_protective_damping(None,1)
    owner=backend.begin_protective_damping();backend.command_protective_damping(owner,1)
    backend.disable_motors()
    with pytest.raises(MotorSafetyError):backend.command_protective_damping(owner,1)
    with pytest.raises(MotorSafetyError):backend.begin_protective_damping()
    assert not backend.motors_enabled

def test_emergency_inhibit_and_old_owner_remain_fenced(connected):
    _,backend=connected;owner=backend.begin_protective_damping()
    backend.end_protective_damping(owner)
    with pytest.raises(MotorSafetyError):backend.command_mit([0,0])
    with pytest.raises(MotorSafetyError):backend.begin_protective_damping()
    backend.disable_motors();backend.enable_motors()
    with pytest.raises(MotorSafetyError):backend.command_protective_damping(owner,1)
    backend.command_mit([0,0])

def test_native_batch_adapter_emits_absolute_kd_one_using_fake_sdk(connected):
    _,fake=connected
    entries=[replace(e,interface_type="canfd",interface="test-fake-can",motor_type="LRO",kp=60,kd=3 if i==0 else 2) for i,e in enumerate(fake.entries)]
    backend=MotorsPyBackend(entries);backend._connected=True;backend._enabled=True
    motor=Mock();backend._motors=[motor,motor]
    owner=backend.begin_protective_damping();backend.command_protective_damping(owner,1)
    arrays=motor.motors_mit_cmd.call_args.args
    assert all(v==0 for v in arrays[0]+arrays[1]+arrays[2]+arrays[4])
    slots={e.motor_id-1 for e in entries}
    assert [v for i,v in enumerate(arrays[3]) if i in slots]==[1.,1.]
    assert all(v==0 for i,v in enumerate(arrays[3]) if i not in slots)

def test_drive_fault_not_reported_as_successful_damping(connected):
    _,backend=connected;session=ProtectiveDamping(backend)
    try:
        session.start();backend.inject_error(0,1)
        until(lambda:session.snapshot()['phase']=='failed')
        until(lambda:not session.snapshot()['active'])
        assert not backend.motors_enabled and session.snapshot()['safe_confirmed'] is False
    finally:session.stop()

def test_lease_expiry_retains_revoked_owner_transport_and_damping(tmp_path):
    config,backend=fake_station();controller=FactoryController(config,backend,enable_recording=False)
    controller.connect()
    service=SimpleNamespace(_controller=controller,
        enter_protective_damping=controller.enter_protective_damping,
        protective_damping_snapshot=lambda:controller._protective_damping.snapshot() if controller._protective_damping else None,
        disable_motors=Mock(side_effect=controller.disable_motors),disconnect=Mock(),can_disconnect=Mock())
    released=Mock();runtime=GatewayRuntime(service,lease_timeout_s=.1,status_rate_hz=10,data_root=tmp_path,outbox=Mock(),platform=Mock(),expiry_callback=released)
    runtime.lease.claim('owner',now=time.monotonic()-1);runtime.lease.revoke_expired()
    try:
        assert runtime._safe_release() is False
        status=runtime.lease_snapshot();assert status['owner']=='owner' and status['revoked'] and status['quarantined']
        assert status['protective_damping']['kd']==1 and status['protective_damping']['active']
        assert backend.connected and backend.motors_enabled and controller.state is StationState.FAULT
        assert controller.position_hold_active is False
        service.disable_motors.assert_not_called();service.disconnect.assert_not_called();service.can_disconnect.assert_not_called();released.assert_not_called()
        session=controller._protective_damping;assert runtime._safe_release() is False
        assert controller._protective_damping is session
        with pytest.raises(LeaseConflict):runtime.lease.refresh('owner',generation=status['generation'],require_active=True)
        with pytest.raises(LeaseConflict):runtime.lease.claim('other')
    finally:controller.disconnect()

def test_release_requires_owner_generation_and_support_confirmation(tmp_path):
    import asyncio,httpx
    from unittest.mock import patch
    from factory_hmi.gateway.app import create_app
    status={'mode':'protective_damping','active':True,'kd':1.,'safe_confirmed':False}
    def disable():status['active']=False
    device=SimpleNamespace(state='completed',protective_damping_snapshot=lambda:dict(status),
        enter_protective_damping=lambda reason:dict(status) if status['active'] else None,
        disable_motors=Mock(side_effect=disable),disconnect=Mock(),can_disconnect=Mock())
    with patch('factory_hmi.gateway.app.PlcCabinetService') as plc:
        plc.return_value.snapshot.return_value={}
        app=create_app(device,data_root=tmp_path)
    runtime=app.state.runtime.session('owner');runtime.lease.claim('owner',now=time.monotonic()-60);runtime.lease.revoke_expired()
    assert runtime._safe_release() is False
    generation=runtime.lease.snapshot().generation
    async def run():
        # A pulse keeps the socketless harness awake for sync ASGI handlers.
        async def pulse():
            while True:await asyncio.sleep(.005)
        timer=asyncio.create_task(pulse())
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://isolated',headers={'X-RP1-Client-ID':'owner'}) as client:
                path='/api/v1/lease/protective-stop/disable'
                assert (await client.post(path,json={'generation':generation})).status_code==422
                assert (await client.post(path,json={'generation':generation+1,'mechanical_support_confirmed':True})).status_code==409
                assert (await client.post(path,json={'generation':True,'mechanical_support_confirmed':True})).status_code==409
                assert (await client.post('/api/v1/lease/recover')).status_code==409
                device.disable_motors.assert_not_called()
                assert status['active']
                assert (await client.post(path,json={'generation':generation,'mechanical_support_confirmed':True})).status_code==200
                assert not status['active'] and runtime.lease.snapshot().owner is None
                assert (await client.post(path,json={'generation':generation,'mechanical_support_confirmed':True})).status_code==409
        finally:
            timer.cancel()
            try:await timer
            except asyncio.CancelledError:pass
    asyncio.run(run())

def test_software_fault_transitions_to_damping_without_disabling():
    from factory_hmi.core.controller import FactoryControllerError
    config,backend=fake_station();controller=FactoryController(config,backend)
    controller.connect()
    try:
        samples=backend.read_samples()
        controller._set_fault(FactoryControllerError('synthetic UI-side failure'),samples=samples)
        until(lambda:controller._protective_damping is not None and controller._protective_damping.snapshot()['active'])
        assert backend.motors_enabled and backend.connected
        assert controller.state is StationState.FAULT
        with pytest.raises(MotorSafetyError):backend.command_mit([1,1])
        controller.disable_motors()
        assert not controller._protective_damping.snapshot()['active']
        assert not backend.motors_enabled
    finally:controller.disconnect()

def test_playback_software_fault_hands_off_without_first_disabling(tmp_path):
    from test_factory_hmi_core import write_trajectory
    from factory_hmi.core.playback import PlaybackError
    config,backend=fake_station();controller=FactoryController(config,backend)
    controller.connect()
    path=write_trajectory(tmp_path,np.zeros((3,2)),np.array([0.,1.,2.]))
    try:
        controller.load_trajectory(path);controller.arm();controller.start()
        until(lambda:bool(controller.playback.last_samples()))
        controller.playback._handle_fault(PlaybackError('synthetic recording failure'))
        until(lambda:controller._protective_damping is not None and controller._protective_damping.snapshot()['active'])
        assert backend.motors_enabled and backend.connected
        assert controller.state is StationState.FAULT
        assert controller.playback.snapshot()['fault_response']['disable_request']=='not_requested'
    finally:controller.disconnect()

def test_confirmed_motor_fault_keeps_hard_fault_handling():
    from factory_hmi.core.controller import FactoryControllerError
    config,backend=fake_station();controller=FactoryController(config,backend)
    controller.connect()
    try:
        backend.inject_error(0,1)
        controller._set_fault(FactoryControllerError('confirmed drive fault'),samples=backend.read_samples())
        assert not backend.motors_enabled
        assert controller._protective_damping is None
    finally:controller.disconnect()

def test_late_damping_start_and_failure_cannot_affect_a_fresh_enable(connected):
    _,backend=connected
    old_generation=backend.control_generation
    old_owner=backend.begin_protective_damping(old_generation)
    backend.disable_motors();backend.enable_motors()
    with pytest.raises(MotorSafetyError):backend.begin_protective_damping(old_generation)
    assert backend.disable_protective_owner(old_owner) is False
    assert backend.motors_enabled
    backend.command_mit([.1,.2])

def test_failed_policy_callback_keeps_hard_fault_fallback(tmp_path):
    from test_factory_hmi_core import write_trajectory
    from factory_hmi.core.playback import PlaybackError
    config,backend=fake_station();controller=FactoryController(config,backend);controller.connect()
    path=write_trajectory(tmp_path,np.zeros((3,2)),np.array([0.,1.,2.]))
    try:
        controller.load_trajectory(path);controller.arm();controller.start()
        until(lambda:bool(controller.playback.last_samples()))
        controller.playback.on_protective_fault=Mock(side_effect=RuntimeError('synthetic policy callback failure'))
        controller.playback._handle_fault(PlaybackError('synthetic failure'))
        assert not backend.motors_enabled
        assert controller.playback.snapshot()['fault_response']['disable_request']=='sent'
        assert 'policy callback failure' in controller.playback.snapshot()['fault_response']['protective_selection_error']
        assert controller._protective_damping is None
    finally:controller.disconnect()

def test_overlapping_software_fault_during_handoff_does_not_request_disable():
    from factory_hmi.models import FaultStatus
    config,backend=fake_station();controller=FactoryController(config,backend);controller.connect()
    try:
        controller._damping_transition=True
        fault=FaultStatus(message='second software failure',kind='PlaybackError',timestamp_s=time.monotonic())
        assert controller._request_protective_fault(fault,backend.read_samples()) is True
        assert backend.motors_enabled
        backend.inject_error(0,1)
        assert controller._request_protective_fault(fault,backend.read_samples()) is False
    finally:
        controller._damping_transition=False;controller.disconnect()
