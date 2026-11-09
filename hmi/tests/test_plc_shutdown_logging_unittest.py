"""Offline-only P0 regression; runnable with stdlib unittest and HMI runtime deps."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from factory_hmi.plc.async_log import AsyncPlcLog
from factory_hmi.plc.config import PlcCabinetConfig, PlcTimingConfig
from factory_hmi.plc.service import PlcCabinetService
from factory_hmi.plc.models import CabinetPhase
from factory_hmi.gateway.quick_run import QuickRun


class ShutdownLoggingTests(unittest.TestCase):
    def service(self, root):
        return PlcCabinetService(PlcCabinetConfig(timings=PlcTimingConfig(poll_ms=10)), data_root=root)

    def test_external_power_never_reads_plc(self):
        class NoPlc:
            @property
            def plc(self):
                raise AssertionError('external power must not depend on PLC')
        quick = QuickRun(None, NoPlc(), 'offline')
        quick._power_source = 'external'
        quick._power()
        quick._check_power()
        self.assertEqual(quick.snapshot()['phase'], 'starting')

    def test_offline_close_is_unverified_not_power_off(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.close()
            self.assertEqual(service.shutdown_status['state'], 'unverified')

    def test_close_from_running_confirms_feedback(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.controller.connect(user='mock-only')
            service.start_sequence([1], user='mock-only')
            for _ in range(100):
                service.controller.tick()
                if service.controller.phase == CabinetPhase.RUNNING:
                    break
            self.assertEqual(service.controller.phase, CabinetPhase.RUNNING)
            service.close()
            self.assertEqual(service.shutdown_status['state'], 'confirmed')
            self.assertFalse(service.client.connected)
            service.close()
            self.assertEqual(service.shutdown_status["state"], "confirmed")

    def test_stale_feedback_never_claims_shutdown(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.controller.connect(user='mock-only')
            with patch.object(type(service.controller), 'stale', property(lambda _: True)):
                with self.assertRaisesRegex(RuntimeError, 'unconfirmed'):
                    service.close()
            self.assertEqual(service.shutdown_status['state'], 'unconfirmed')
            self.assertTrue(service.client.connected)
            service.close()

    def test_failed_shutdown_cannot_be_cleared_by_disconnection(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.shutdown_status = {"state": "unconfirmed", "detail": "lost feedback"}
            service._shutdown_error_kind = "electrical"
            with self.assertRaisesRegex(RuntimeError, 'remains unconfirmed'):
                service.close()
            self.assertEqual(service.shutdown_status['state'], 'unconfirmed')

    def test_used_plc_disconnect_before_first_exit_is_unconfirmed(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.controller.connect(user='mock-only')
            service.start_sequence([1], user='mock-only')
            service.client.disconnect()
            service.controller._communication_lost('injected link loss')
            with self.assertRaisesRegex(RuntimeError, 'remains unconfirmed'):
                service.close()
            self.assertEqual(service.shutdown_status['state'], 'unconfirmed')

    def test_monitor_worker_timeout_does_not_require_electrical_off(self):
        with tempfile.TemporaryDirectory() as root:
            service = PlcCabinetService(PlcCabinetConfig(access_mode='monitor'), data_root=root)
            from unittest.mock import Mock
            thread = Mock()
            thread.is_alive.return_value = True
            service._thread = thread
            with self.assertRaisesRegex(RuntimeError, 'worker did not stop'):
                service.close()
            self.assertEqual(service._shutdown_error_kind, 'worker')
            self.assertFalse(service._shutdown_closing.is_set())
            thread.is_alive.return_value = False
            service.close()
            self.assertEqual(service.shutdown_status['state'], 'unverified')
            self.assertFalse(service._power_off_unverified)

    def test_safety_interrupt_releases_close_lock(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.controller.connect(user='mock-only')
            entered = threading.Event()
            errors = []
            def tick(*args):
                entered.set()
            def close():
                try:
                    service.close()
                except RuntimeError as exc:
                    errors.append(str(exc))
            with patch.object(service.controller, 'tick', tick), \
                 patch.object(service.controller, 'request_stop', return_value=True), \
                 patch.object(service, '_shutdown_feedback_confirmed', return_value=False):
                thread = threading.Thread(target=close)
                thread.start()
                self.assertTrue(entered.wait(1))
                service.request_shutdown_interrupt()
                thread.join(timeout=1)
                self.assertFalse(thread.is_alive())
                self.assertIn('interrupted by safety command', errors[0])
                self.assertFalse(service._shutdown_closing.is_set())
                self.assertTrue(service._lock.acquire(timeout=.1))
                service._lock.release()
            service.close()
            service.request_shutdown_interrupt()
            self.assertFalse(service._shutdown_closing.is_set())

    def test_registered_safety_request_prevents_close_before_it_starts(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            with service.shutdown_safety_request():
                with patch.object(service, '_close_impl') as close_impl:
                    with self.assertRaisesRegex(RuntimeError, 'safety request pending'):
                        service.close()
                    close_impl.assert_not_called()
                    self.assertFalse(service._shutdown_closing.is_set())
            self.assertEqual(service._shutdown_safety_pending, 0)
            service.close()

    def test_safety_context_releases_registration_after_failure(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            with self.assertRaisesRegex(ValueError, 'injected'):
                with service.shutdown_safety_request():
                    raise ValueError('injected')
            self.assertEqual(service._shutdown_safety_pending, 0)
            service.close()

    def test_close_rejects_late_log_submission(self):
        writer = AsyncPlcLog()
        writer.close()
        self.assertFalse(writer.submit(Path('/unused'), {'late': True}))
        self.assertEqual(writer.snapshot()['pending'], 0)
        self.assertTrue(writer.snapshot()['degraded'])

    def test_slow_log_does_not_block_stop(self):
        with tempfile.TemporaryDirectory() as root:
            service = self.service(root)
            service.controller.connect(user='mock-only')
            entered, release = threading.Event(), threading.Event()
            def slow(*args):
                entered.set()
                release.wait(3)
            with patch.object(service._log_writer, '_write', slow):
                service._log_writer.submit(Path(root)/'log', {'event': 'slow'})
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                service.all_stop(user='mock-only')
                self.assertLess(time.monotonic()-start, .5)
                release.set()
                for _ in range(10):
                    service.controller.tick()
            service.close()

    def test_full_queue_is_bounded_and_visible(self):
        writer = AsyncPlcLog(capacity=1)
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
        with patch.object(writer, '_write', slow):
            writer.submit(Path('/unused'), {'n': 1})
            self.assertTrue(entered.wait(1))
            writer.submit(Path('/unused'), {'n': 2})
            self.assertFalse(writer.submit(Path('/unused'), {'n': 3}))
            self.assertEqual(writer.snapshot()['dropped'], 1)
            self.assertTrue(writer.snapshot()['degraded'])
            release.set()
            writer.close(timeout=1)

    def test_failed_append_not_retried_and_visible(self):
        writer = AsyncPlcLog()
        with patch.object(writer, '_write', side_effect=OSError('disk full')) as write:
            writer.submit(Path('/unused'), {'n': 1})
            writer.close(timeout=1)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(writer.snapshot()['failed'], 1)
            self.assertIn('disk full', writer.snapshot()['last_error'])

if __name__ == '__main__':
    unittest.main()
