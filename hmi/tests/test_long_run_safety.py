"""Bounded failure injection: fake motors, synthetic CSV, no device/server I/O."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from factory_hmi import FactoryController, StationState
from factory_hmi.client import GatewayClient
from factory_hmi.core.backend import FakeMotorBackend, MotorSafetyError
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.app import GatewayHub, GatewayRuntime, create_app
from factory_hmi.gateway.lease import ControlLease, LeaseConflict, validate_lease_timeout
from scripts.log_motor_telemetry import MotorTelemetryRecorder
from test_factory_hmi_core import fake_station, write_trajectory

HMI = Path(__file__).resolve().parents[1]


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('condition did not arrive before deadline')
        time.sleep(.005)


def runtime_for(controller, callback=None, timeout=.15):
    runtime = GatewayRuntime.__new__(GatewayRuntime)
    runtime.controller = controller
    runtime.lease = ControlLease(timeout)
    runtime._stop = threading.Event()
    runtime._lease_guard = threading.RLock()
    runtime._cleanup_error = None
    runtime._expiry_callback = callback
    return runtime


class LeaseSafetyTests(unittest.TestCase):
    def test_timeout_validation_and_default(self):
        self.assertEqual(ControlLease().timeout_s, 30)
        for invalid in (0, -1, float('nan'), float('inf'), 301):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_lease_timeout(invalid)
        self.assertEqual(validate_lease_timeout(15), 15)

    def test_late_refresh_cannot_undo_expiry_or_reclaim_after_release(self):
        lease = ControlLease(1)
        lease.claim('a', now=10)
        generation = lease.snapshot(now=10).generation
        with self.assertRaises(LeaseConflict):
            lease.refresh('a', now=12, generation=generation)
        self.assertIsNotNone(lease.revoke_expired(now=12))
        with self.assertRaises(LeaseConflict):
            lease.refresh('a', now=12, generation=generation)
        lease.release()
        with self.assertRaises(LeaseConflict):
            lease.refresh('a', now=13)
        lease.claim('a', now=13)
        with self.assertRaises(LeaseConflict):
            lease.refresh('a', now=13, generation=generation, require_active=True)

    def test_heartbeat_wins_before_atomic_deadline_decision(self):
        lease = ControlLease(1)
        lease.claim('a', now=10)
        lease.refresh('a', now=10.9)
        self.assertIsNone(lease.revoke_expired(now=11.1))
        self.assertFalse(lease.snapshot(now=11.1).expired)

    def test_failed_cleanup_quarantines_each_step_and_retains_owner(self):
        for failed in ('disable_motors', 'disconnect', 'can_disconnect'):
            with self.subTest(failed=failed):
                release = Mock()
                device = SimpleNamespace(disable_motors=Mock(), disconnect=Mock(), can_disconnect=Mock())
                getattr(device, failed).side_effect = OSError('synthetic cleanup failure')
                runtime = runtime_for(device, release)
                runtime.lease.claim('a', now=10)
                runtime.lease.revoke_expired(now=11)
                self.assertFalse(runtime._safe_release())
                self.assertEqual(runtime.lease.snapshot(now=11).owner, 'a')
                self.assertTrue(runtime.lease.snapshot(now=11).revoked)
                self.assertIn(failed, runtime._cleanup_error)
                release.assert_not_called()
                getattr(device, failed).side_effect = None
                self.assertTrue(runtime._safe_release())
                release.assert_called_once()
                self.assertIsNone(runtime._cleanup_error)

    def test_expiry_does_not_wait_for_full_snapshot(self):
        released = threading.Event()
        device = SimpleNamespace(state='running', snapshot=Mock(side_effect=RuntimeError('slow snapshot')),
                 stop=Mock(), disable_motors=Mock(), disconnect=Mock(), can_disconnect=Mock())
        runtime = runtime_for(device, released.set)
        runtime.lease.claim('a', now=time.monotonic()-1)
        thread = threading.Thread(target=runtime._monitor_lease, daemon=True)
        thread.start()
        try:
            self.assertTrue(released.wait(1))
            device.stop.assert_called_once_with(reason='operator lease expired')
            device.snapshot.assert_not_called()
        finally:
            runtime._stop.set()
            thread.join(1)

    def test_cleanup_timeout_retains_owner_and_never_starts_parallel_cleanup(self):
        release=threading.Event()
        device=SimpleNamespace(state='completed', disable_motors=Mock(side_effect=lambda:release.wait(2)),
                               disconnect=Mock(),can_disconnect=Mock())
        runtime=runtime_for(device)
        runtime._cleanup_timeout_s=.03
        runtime.lease.claim('a',now=10)
        runtime.lease.revoke_expired(now=11)
        try:
            started=time.monotonic()
            self.assertFalse(runtime._safe_release())
            self.assertLess(time.monotonic()-started,.3)
            self.assertFalse(runtime._safe_release())
            self.assertEqual(device.disable_motors.call_count,1)
            self.assertEqual(runtime.lease.snapshot(now=11).owner,'a')
            release.set()
            until(lambda:runtime._cleanup_job['done'].is_set())
            self.assertTrue(runtime._safe_release())
        finally:release.set()

    def test_release_preserves_other_window_and_failed_backend_keeps_resources(self):
        hub = GatewayHub.__new__(GatewayHub)
        hub._lock = threading.RLock()
        hub._interface_owners = {'can0':'a', 'can4':'b', 'can5':'b'}
        hub.release_interfaces('a')
        self.assertEqual(hub.interface_owners(), {'can4':'b','can5':'b'})
        backend = FakeMotorBackend(1)
        backend.connect()
        try:
            with patch.object(backend, '_disable_impl', side_effect=OSError('device did not disable')):
                with self.assertRaises(OSError): backend.disable_motors()
                self.assertTrue(backend.motors_enabled)
                with self.assertRaises(OSError): backend.close_transport()
                self.assertTrue(backend.connected)
                self.assertTrue(backend.commands_inhibited)
        finally:
            backend.disconnect()

    def test_old_control_watchdog_callback_cannot_disable_a_reenabled_backend(self):
        backend=FakeMotorBackend(1)
        on_fault=Mock()
        backend.set_control_watchdog(.08,on_fault)
        arrived,release=threading.Event(),threading.Event()
        original=backend._command_watchdog._on_timeout
        def delayed(message,generation):
            arrived.set()
            release.wait(1)
            original(message,generation)
        backend._command_watchdog._on_timeout=delayed
        try:
            backend.connect()
            self.assertTrue(arrived.wait(1))
            backend.disable_motors()
            backend.enable_motors()
            release.set()
            for _ in range(3):
                backend.command_mit([0])
                time.sleep(.015)
            self.assertFalse(backend.commands_inhibited)
            self.assertTrue(backend.motors_enabled)
            on_fault.assert_not_called()
        finally:
            release.set()
            backend.disconnect()

    def test_http_client_heartbeat_has_short_timeout_and_fenced_generation(self):
        client = GatewayClient('http://invalid.test', timeout=10)
        client.observe_lease({'lease':{'generation':7}})
        response = Mock()
        response.read.return_value = b'{"ok":true,"lease":{"generation":7}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with patch('factory_hmi.client.request.urlopen', return_value=response) as opening:
            client.lease_heartbeat()
        self.assertEqual(opening.call_args.kwargs['timeout'], .5)
        self.assertEqual(opening.call_args.args[0].get_header('X-rp1-lease-generation'), '7')
        self.assertFalse(client.observe_lease({'lease':{'generation':6}}))
        self.assertEqual(client.lease_generation, 7)

    def test_gateway_cli_reads_env_and_rejects_nonfinite(self):
        from factory_hmi.gateway.__main__ import parse_args
        with patch.dict(os.environ, {'RP1_FACTORY_LEASE_TIMEOUT_S':'15'}), patch('sys.argv',['gateway']):
            self.assertEqual(parse_args().lease_timeout, 15)
        with patch.dict(os.environ, {'RP1_FACTORY_LEASE_TIMEOUT_S':'30'}), patch('sys.argv',['gateway','--lease-timeout','20']):
            self.assertEqual(parse_args().lease_timeout, 20)

    def test_launcher_preserves_only_authorized_record_and_lease_settings(self):
        launcher_path = HMI/'launch.py'
        if not launcher_path.is_file():
            launcher_path = HMI.parent/'launch.py'  # Installed DEB layout.
        spec=importlib.util.spec_from_file_location('isolated_hmi_launcher',launcher_path)
        launcher=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
            'RP1_FACTORY_LEASE_TIMEOUT_S':'15','RP1_FACTORY_RECORD_RESERVE_BYTES':'123456',
            'RP1_FACTORY_PLC_CONFIG':'/must/not/be/inherited.yaml'}):
            env=launcher.gateway_environment(Path(temp),None,offline=True,driver='mock',mode='control')
            self.assertEqual(env['RP1_FACTORY_LEASE_TIMEOUT_S'],'15')
            self.assertEqual(env['RP1_FACTORY_RECORD_RESERVE_BYTES'],'123456')
            self.assertEqual(env['RP1_FACTORY_PLC_CONFIG'],'config/plc_cabinet.yaml')
            self.assertEqual(env['RP1_FACTORY_CAN_DISABLED'],'1')
            self.assertEqual(env['RP1_FACTORY_PLATFORM_URL'],'')

    def test_heartbeat_endpoint_rejects_missing_or_old_generation(self):
        from starlette.requests import Request
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as temp, patch('factory_hmi.gateway.app.PlcCabinetService') as plc:
            plc.return_value.snapshot.return_value = {}
            app = create_app(data_root=Path(temp))
            hub = app.state.runtime
            runtime = hub.session('a')
            runtime.lease.claim('a')
            heartbeat = next(r.endpoint for r in app.routes if getattr(r,'path','')=='/api/v1/lease/heartbeat')
            request = Request({'type':'http','method':'POST','path':'/','headers':[],'client':('127.0.0.1',1)})
            kwargs = dict(request=request,x_rp1_client_id='a',x_rp1_operator_token=None)
            with patch.dict(os.environ, {'RP1_FACTORY_OPERATOR_TOKEN':''}):
                for generation in (None, runtime.lease.snapshot().generation-1):
                    with self.assertRaises(HTTPException) as error:
                        heartbeat(**kwargs,x_rp1_lease_generation=generation)
                    self.assertEqual(error.exception.status_code,409)
                reply=heartbeat(**kwargs,x_rp1_lease_generation=runtime.lease.snapshot().generation)
                self.assertTrue(reply['ok'])
            for worker in list(runtime.controller._report_threads): worker.join(1)


class RecordingSafetyTests(unittest.TestCase):
    def test_csv_write_and_close_errors_are_latched(self):
        config,backend=fake_station()
        backend.connect()
        try:
            for phase in ('write','close'):
                with self.subTest(phase=phase):
                    recorder=MotorTelemetryRecorder(backend.motors,list(config.entries),'/tmp/not-created.csv',
                        quiet=True, local_only=True, sample_provider=backend.read_samples)
                    recorder._writer=Mock()
                    recorder._file=Mock()
                    if phase=='write': recorder._writer.writerow.side_effect=OSError(28,'synthetic ENOSPC')
                    else:
                        recorder._stop.set()
                        recorder._file.close.side_effect=OSError('synthetic close error')
                    with patch('shutil.disk_usage',return_value=SimpleNamespace(free=10**12)):
                        recorder._run()
                    self.assertIsNotNone(recorder.blocked_reason)
                    self.assertTrue(recorder.wait_closed(.1))
                    with self.assertRaises(RuntimeError): recorder.finalize()
        finally: backend.disconnect()

    def test_local_only_ignores_legacy_network_configuration(self):
        config,backend=fake_station()
        backend.connect()
        try:
            with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{
                'TEST_PLATFORM_API_URL':'http://invalid.test','INFLUXDB_TOKEN':'synthetic-unused'}), \
                    patch('urllib.request.urlopen',side_effect=AssertionError('network forbidden')) as network:
                recorder=MotorTelemetryRecorder(backend.motors,list(config.entries),Path(temp)/'raw.csv',
                     quiet=True,local_only=True,sample_provider=backend.read_samples,rate_hz=20)
                recorder.start()
                until(lambda: recorder.row_count>0)
                recorder.request_stop_sampling()
                recorder.finalize()
                self.assertTrue(recorder.wait_closed(.1))
                self.assertIsNone(recorder._influx)
                network.assert_not_called()
        finally: backend.disconnect()

    def test_stalled_recorder_is_detectable_without_waiting_for_file_lock(self):
        config,backend=fake_station()
        recorder=MotorTelemetryRecorder(backend.motors,list(config.entries),'/tmp/not-created.csv',quiet=True,local_only=True)
        recorder._last_write=time.monotonic()-3
        self.assertIn('telemetry_stalled',recorder.health_error())

    def test_local_recorder_does_not_overwrite_existing_measurements(self):
        config,backend=fake_station()
        with tempfile.TemporaryDirectory() as temp:
            output=Path(temp)/'raw.csv'
            original=b'original measurement evidence\n'
            output.write_bytes(original)
            recorder=MotorTelemetryRecorder(backend.motors,list(config.entries),output,quiet=True,local_only=True)
            recorder.start()
            self.assertTrue(recorder.wait_closed(1))
            with self.assertRaises(RuntimeError):recorder.finalize()
            self.assertEqual(output.read_bytes(),original)

    def test_motion_and_other_window_continue_during_slow_csv_close(self):
        entered,release=threading.Event(),threading.Event()
        config,a=fake_station()
        _,b=fake_station()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            ca=FactoryController(config,a,enable_recording=True,record_output=root/'raw.csv')
            cb=FactoryController(config,b)
            original=MotorTelemetryRecorder._open_csv
            def slow_open(recorder):
                original(recorder)
                real_file=recorder._file
                def close():
                    entered.set()
                    if not release.wait(3): raise TimeoutError('test close release missing')
                    real_file.close()
                recorder._file=SimpleNamespace(close=close)
            hub=GatewayHub.__new__(GatewayHub)
            hub._lock=threading.RLock()
            hub._interface_owners={'can0':'a','can1':'a','can2':'a','can3':'a','can4':'b','can5':'b'}
            runtimes=[runtime_for(ca,lambda:hub.release_interfaces('a')),
                      runtime_for(cb,lambda:hub.release_interfaces('b'))]
            monitors=[]
            heartbeat_stop=threading.Event()
            heartbeat_errors=[]
            def keep_healthy_leases():
                # Recording isolation assumes a live operator. The current
                # completion path finishes the 200 ms cycle; renew leases
                # during that wait too, before the slow-close event arrives.
                while not heartbeat_stop.wait(.04):
                    try:
                        for owner,runtime in zip(('a','b'),runtimes):
                            runtime.lease.refresh(owner,require_active=True)
                    except Exception as exc:
                        heartbeat_errors.append(exc)
                        return
            heartbeat_thread=threading.Thread(target=keep_healthy_leases,daemon=True)
            try:
                for c in (ca,cb):
                    c.connect()
                    c.load_trajectory(write_trajectory(root,np.zeros((2,2)),np.array([0.,.2])))
                    c.arm()
                for owner,runtime in zip(('a','b'),runtimes):
                    runtime.lease.claim(owner)
                    thread=threading.Thread(target=runtime._monitor_lease,daemon=True)
                    thread.start()
                    monitors.append(thread)
                heartbeat_thread.start()
                with patch.object(MotorTelemetryRecorder,'_open_csv',slow_open):
                    ca.start(loop=True,duration_s=.04)
                    cb.start(loop=True,duration_s=1.0)
                    self.assertTrue(entered.wait(1))
                    self.assertTrue(ca.playback.wait(.15))
                    self.assertEqual(ca.state,StationState.COMPLETED)
                    self.assertTrue(ca.position_hold_active)
                    self.assertFalse(ca.playback.snapshot()['recording']['finished'])
                    count_a,count_b=len(a.command_history),len(b.command_history)
                    for _ in range(14):
                        for owner,runtime in zip(('a','b'),runtimes): runtime.lease.refresh(owner,require_active=True)
                        time.sleep(.04)
                    self.assertGreater(len(a.command_history),count_a+20)
                    self.assertGreater(len(b.command_history),count_b+20)
                    self.assertFalse(a.commands_inhibited)
                    self.assertFalse(b.commands_inhibited)
                    self.assertEqual(hub._interface_owners['can4'],'b')
                    self.assertEqual(hub._interface_owners['can5'],'b')
                    self.assertEqual(cb.state,StationState.RUNNING)
                    self.assertFalse(heartbeat_errors)
            finally:
                for r in runtimes:r._stop.set()
                for t in monitors:t.join(1)
                heartbeat_stop.set()
                if heartbeat_thread.ident is not None:heartbeat_thread.join(1)
                release.set()
                for c in (ca,cb):
                    c.wait(2)
                    c.disconnect()
            self.assertTrue(ca.playback is None)

    def test_control_watchdog_fences_a_blocked_native_dispatch(self):
        config,backend=fake_station()
        entered,release=threading.Event(),threading.Event()
        config=type(config)(**{**config.__dict__,'raw':{'factory_hmi_faults':{'heartbeat_stale_seconds':.08}}})
        controller=FactoryController(config,backend)
        original=backend._command_mit_impl
        def blocked(*args,**kwargs):
            entered.set()
            release.wait(2)
            return original(*args,**kwargs)
        try:
            with patch.object(backend,'_command_mit_impl',side_effect=blocked):
                controller.connect()
                self.assertTrue(entered.wait(1))
                until(lambda:backend.commands_inhibited,1)
                self.assertEqual(controller.state,StationState.FAULT)
                self.assertIn('controller_timeout',controller._fault.message)
                # The blocked native call cannot be forcefully interrupted by Python.
                self.assertTrue(backend._enabled)
                release.set()
                until(lambda:not backend._enabled)
            with self.assertRaises(MotorSafetyError): backend.command_mit([0,0])
        finally:
            release.set()
            controller.disconnect()

    def test_recording_low_disk_gate_prevents_motion(self):
        with tempfile.TemporaryDirectory() as temp:
            service=FactoryService(data_root=Path(temp)/'data')
            try:
                service.configure(config_path=str(HMI/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake')
                service.connect()
                positions=[sample.pos_rad for sample in service.controller.backend.read_samples()]
                trajectory=write_trajectory(Path(temp),np.tile(positions,(3,1)),np.array([0.,.5,1.]))
                service.import_trajectory(str(trajectory))
                service.arm()
                with patch('shutil.disk_usage',return_value=SimpleNamespace(free=0)), self.assertRaisesRegex(RuntimeError,'reserve'):
                    service.start(test_case_id='synthetic',robot_id='fake',record=True,recording_scope='local')
                self.assertEqual(service.controller.state,StationState.ARMED)
                self.assertIsNone(service.controller.playback)
            finally:
                service.close()

    def test_writer_failure_stops_only_its_running_fake_station(self):
        config,backend=fake_station()
        original=MotorTelemetryRecorder._open_csv
        def fail_writer(recorder):
            original(recorder)
            recorder._writer=Mock()
            recorder._writer.writerow.side_effect=OSError(28,'synthetic ENOSPC')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            controller=FactoryController(config,backend,enable_recording=True,record_output=root/'raw.csv')
            try:
                controller.connect()
                controller.load_trajectory(write_trajectory(root,np.zeros((2,2)),np.array([0.,.2])))
                controller.arm()
                with patch.object(MotorTelemetryRecorder,'_open_csv',fail_writer):
                    controller.start(loop=True,duration_s=1)
                    self.assertTrue(controller.wait(2))
                self.assertEqual(controller.state,StationState.FAULT)
                until(lambda:controller._protective_damping is not None and controller._protective_damping.snapshot()['active'])
                self.assertTrue(backend.motors_enabled)
                self.assertTrue(backend.connected)
                self.assertTrue(backend.commands_inhibited)
                self.assertEqual(controller._protective_damping.snapshot()['kd'],1.)
                self.assertFalse(controller._protective_damping.snapshot()['safe_confirmed'])
                self.assertIn('telemetry_io_error',controller.playback.snapshot()['record_error'])
            finally:controller.disconnect()

    def test_service_report_waits_for_closed_record_and_preserves_motion_end(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            service=FactoryService(data_root=root/'data')
            service.configure(config_path=str(HMI/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake')
            try:
                service.connect()
                values=service.controller.backend.positions.copy()
                trajectory=root/'synthetic.npz'
                np.savez(trajectory,motor_pos=np.vstack([values,values]),time=np.array([0.,.15]),fps=np.array([20.]),limb=np.array(['left_arm']))
                service.import_trajectory(str(trajectory))
                service.arm()
                service.start(test_case_id='synthetic',robot_id='fake-sample',record=True,recording_scope='local',loop=False)
                self.assertTrue(service.controller.wait(2))
                for thread in list(service._report_threads):thread.join(3)
                self.assertEqual(service._report_errors,{})
                self.assertEqual(len(service.execution_history()),1)
                summary=service.report_manager.summary(service.execution_history()[0]['test_id'])
                self.assertEqual(summary['timing']['ended_at'],service.controller.playback.snapshot()['ended_at'])
                self.assertTrue(service.record_file().is_file())
            finally:service.disconnect()


if __name__=='__main__':unittest.main()
