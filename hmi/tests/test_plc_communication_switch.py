"""Communication opt-in tests: only in-memory PLC clients and fake failures."""
import tempfile
import time
import unittest
from unittest.mock import patch
from factory_hmi.plc.config import PlcCabinetConfig, PlcTimingConfig
from factory_hmi.plc.service import PlcCabinetService
from factory_hmi.plc.client import MockPlcClient, PlcCommunicationError
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.plc.models import CabinetPhase


class CommunicationSwitchTests(unittest.TestCase):
    def service(self, root):
        return PlcCabinetService(PlcCabinetConfig(timings=PlcTimingConfig(poll_ms=20)), data_root=root)

    def test_default_real_driver_constructs_nothing_and_never_retries(self):
        with tempfile.TemporaryDirectory() as root, patch('factory_hmi.plc.service.build_plc_client', side_effect=AssertionError('default must not build driver')) as build:
            service = PlcCabinetService(PlcCabinetConfig(driver='modbus_tcp'), data_root=root)
            service.start()
            for _ in range(3):
                state = service.snapshot()
                self.assertFalse(state['communication_enabled'])
                self.assertEqual(state['communication_state'], 'DISABLED')
                time.sleep(.03)
            self.assertIsNone(service._thread)
            service.close()
            build.assert_not_called()

    def test_enable_disable_reenable_parks_worker_without_io(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            with patch.object(service.client, 'read_status', wraps=service.client.read_status) as reads:
                service.connect(user='offline')
                worker = service._thread
                time.sleep(.07)
                self.assertTrue(service.snapshot()['communication_enabled'])
                service.disconnect()
                count = reads.call_count
                for _ in range(3):
                    service.snapshot()
                    time.sleep(.04)
                self.assertEqual(reads.call_count,count)
                self.assertFalse(service.client.connected)
                self.assertEqual(service.snapshot()['shutdown']['state'],'unverified')
                service.connect(user='offline')
                self.assertIs(worker, service._thread)
                self.assertTrue(service.client.connected)
                service.close()

    def test_failed_connect_can_be_disabled_without_background_reconnect(self):
        class FailedClient(MockPlcClient):
            def __init__(self):super().__init__();self.attempts=0
            def connect(self):self.attempts+=1;raise PlcCommunicationError('offline failure')
        fake = FailedClient()
        with tempfile.TemporaryDirectory() as root, patch('factory_hmi.plc.service.build_plc_client',return_value=fake) as build:
            service = PlcCabinetService(PlcCabinetConfig(driver='modbus_tcp'),data_root=root)
            build.assert_not_called()
            service.connect(user='offline')
            self.assertEqual(build.call_count,1)
            self.assertTrue(service.communication_enabled)
            service.disconnect()
            count=fake.attempts
            time.sleep(.25)
            self.assertEqual(fake.attempts,count)
            service.close()

    def test_running_and_uncertain_power_cannot_disable_communication(self):
        with tempfile.TemporaryDirectory() as root:
            service=self.service(root)
            service.connect(user='offline')
            service.start_sequence([1],user='offline')
            with self.assertRaises(PlcRequestConflict):service.disconnect()
            self.assertTrue(service.communication_enabled)
            # Inject communication loss after a power request, not a real device.
            with service._lock:
                service.client.disconnect()
                service.controller.phase=CabinetPhase.COMM_LOST
                service.controller.communication_state='COMM_LOST'
            with self.assertRaises(PlcRequestConflict):service.disconnect()
            self.assertTrue(service.communication_enabled)
            service.connect(user='offline')
            service.close()

    def test_first_handshake_failure_preserves_unconfirmed_power(self):
        class FailedHandshake(MockPlcClient):
            def write_command(self, command):
                raise PlcCommunicationError('first handshake failed')
        fake = FailedHandshake()
        fake.set_status(main_contactor_fb=True, main_ready=True, ps1_actual_output=True)
        with tempfile.TemporaryDirectory() as root, patch('factory_hmi.plc.service.build_plc_client',return_value=fake):
            service=self.service(root)
            service.connect(user='offline')
            self.assertTrue(service._power_off_unverified)
            self.assertFalse(service.snapshot()['communication_disable_allowed'])
            with self.assertRaises(PlcRequestConflict):service.disconnect()
            self.assertTrue(service.communication_enabled)
            with self.assertRaises(RuntimeError):service.close()
            # Isolated mock teardown only. No physical transport exists.
            service._stop.set();service._communication_gate.set()
            service._thread.join(1)
            service._log_writer.close()

    def test_stale_off_feedback_cannot_disable(self):
        with tempfile.TemporaryDirectory() as root:
            service=self.service(root)
            service.connect(user='offline')
            with service._lock:
                service.controller.communication_state='STALE'
                with self.assertRaises(PlcRequestConflict):service.disconnect()
                self.assertTrue(service.communication_enabled)
                service.controller.communication_state='LIVE'
            service.close()

    def test_shared_revision_blocks_old_window_toggle_and_restart_defaults_off(self):
        with tempfile.TemporaryDirectory() as root:
            service=self.service(root)
            context=service.snapshot()['coordination']
            def execute(operation, request, seen):
                return service.coordination.execute(operation,{},user='offline',client_id='window',request_id=request,expected_epoch=seen['epoch'],expected_revision=seen['revision'])
            execute('connect','enable',context)
            with self.assertRaises(PlcRequestConflict):execute('disconnect','stale-disable',context)
            self.assertTrue(service.communication_enabled)
            execute('disconnect','disable',service.snapshot()['coordination'])
            service.close()
            restarted=self.service(root)
            restarted.start()
            self.assertFalse(restarted.communication_enabled)
            self.assertIsNone(restarted._thread)
            restarted.close()

if __name__=='__main__':unittest.main()
