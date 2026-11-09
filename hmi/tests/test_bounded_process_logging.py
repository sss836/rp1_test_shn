"""Offline pipe producers only: never opens a PLC, CAN, or motor driver."""
import importlib.util
import logging
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('hmi_launch_logging_test', Path(__file__).parents[1] / 'launch.py')
launch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launch)
from factory_hmi.plc.connection_logging import ConnectionLogFilter


class ProcessLoggingTests(unittest.TestCase):
    def test_rotation_is_bounded_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'gateway.log'
            path.write_text('original diagnostic evidence')
            sink = launch.BoundedProcessLog(path, max_bytes=8192, backups=2)
            child = subprocess.Popen([sys.executable, '-c', 'import os;os.write(1,b"x"*100000)'], stdout=subprocess.PIPE)
            sink.attach(child.stdout)
            self.assertEqual(child.wait(timeout=5), 0)
            sink.close()
            self.assertEqual(list(Path(root).glob('*.legacy-*'))[0].read_text(), 'original diagnostic evidence')
            logs = [path, path.with_name('gateway.log.1'), path.with_name('gateway.log.2')]
            self.assertLessEqual(sum(p.stat().st_size for p in logs if p.exists()), 3*8192)
            self.assertEqual(sink.dropped_bytes, 0)
            self.assertEqual(sink.failed_bytes, 0)
            self.assertTrue(path.with_suffix('.log.status.json').exists())

    def test_slow_disk_does_not_block_producer_and_loss_is_reported(self):
        gate = threading.Event()
        class SlowSink(launch.BoundedProcessLog):
            def _write_loop(self):
                gate.wait(5)
                super()._write_loop()
        with tempfile.TemporaryDirectory() as root:
            sink = SlowSink(Path(root)/'gateway.log', capacity=1)
            child = subprocess.Popen([sys.executable,'-c','import os;os.write(1,b"x"*4000000)'],stdout=subprocess.PIPE)
            sink.attach(child.stdout)
            try:
                self.assertEqual(child.wait(timeout=3),0)
                self.assertGreater(sink.dropped_bytes,0)
                self.assertLessEqual(sink.queue.qsize(),1)
            finally:
                gate.set()
                sink.close()
            self.assertIn('HMI LOG DEGRADED', sink.path.read_text())

    def test_sink_initial_failure_recovers_without_retrying_uncertain_chunk(self):
        original = launch.RotatingProcessFile
        calls = [0]
        def flaky(*args, **kwargs):
            calls[0] += 1
            if calls[0] == 1:
                raise OSError("injected disk unavailable")
            return original(*args, **kwargs)
        with tempfile.TemporaryDirectory() as root, patch.object(launch, 'RotatingProcessFile', flaky):
            sink = launch.BoundedProcessLog(Path(root)/'gateway.log')
            child = subprocess.Popen([sys.executable, '-c', 'import os,time;os.write(1,b"first\\n");time.sleep(.25);os.write(1,b"recovered\\n")'], stdout=subprocess.PIPE)
            sink.attach(child.stdout)
            self.assertEqual(child.wait(timeout=5),0)
            sink.close()
            self.assertGreater(sink.failed_bytes,0)
            self.assertIn('recovered', sink.path.read_text())
            self.assertIn('HMI LOG DEGRADED', sink.path.read_text())

    def test_mock_mode_never_constructs_modbus_transport(self):
        from factory_hmi.plc.config import PlcCabinetConfig
        from factory_hmi.plc.service import PlcCabinetService
        from factory_hmi.plc.client import ModbusTcpPlcClient
        with tempfile.TemporaryDirectory() as root, patch.object(ModbusTcpPlcClient, '_default_transport_factory', side_effect=AssertionError('no network in mock')):
            service = PlcCabinetService(PlcCabinetConfig(driver='mock'), data_root=root)
            service.start()
            service.connect(user="mock-only")
            self.assertEqual(service.snapshot()['driver'],'mock')
            self.assertEqual(service.snapshot()['communication_state'],'LIVE')
            service.close()

    def test_close_never_performs_status_disk_io(self):
        with tempfile.TemporaryDirectory() as root:
            sink = launch.BoundedProcessLog(Path(root)/'gateway.log')
            with patch.object(sink, '_save_status', side_effect=AssertionError('close must not write disk')):
                self.assertFalse(sink.close(timeout=.01)['close_timed_out'])
            sink.queue.put(b"in-flight")
            sink.queue.get()
            self.assertEqual(sink.snapshot()["pending_chunks"],1)
            gate = threading.Event()
            sink._writer = threading.Thread(target=gate.wait, daemon=True)
            sink._writer.start()
            try:
                self.assertTrue(sink.close(timeout=.01)['close_timed_out'])
            finally:
                gate.set()
                sink._writer.join()

    def test_partial_utf8_at_eof_is_visible(self):
        with tempfile.TemporaryDirectory() as root:
            sink = launch.BoundedProcessLog(Path(root)/'gateway.log')
            child = subprocess.Popen([sys.executable, '-c', 'import os;os.write(1,bytes([0xe4,0xb8]))'], stdout=subprocess.PIPE)
            sink.attach(child.stdout)
            self.assertEqual(child.wait(timeout=5),0)
            sink.close()
            self.assertEqual(sink.path.read_text(), '\ufffd')

    def test_process_exits_with_permanently_stuck_log_writer(self):
        with tempfile.TemporaryDirectory() as root:
            script = f"""
import importlib.util, logging, os, threading
spec = importlib.util.spec_from_file_location('isolated_launcher', {str(Path(__file__).parents[1] / 'launch.py')!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
entered = threading.Event()
never = threading.Event()
def stuck_write(self, text):
    entered.set()
    never.wait()
m.RotatingProcessFile.write = stuck_write
sink = m.BoundedProcessLog({str(Path(root)/'gateway.log')!r})
reader, writer = os.pipe()
sink.attach(os.fdopen(reader, 'rb', buffering=0))
os.write(writer, b'diagnostic')
os.close(writer)
assert entered.wait(2)
state = sink.close(timeout=.01)
assert state['close_timed_out'] and state['pending_chunks'] == 1, state
logging.shutdown()
print('normal interpreter exit', flush=True)
"""
            result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('normal interpreter exit',result.stdout)

    def test_failed_fstat_closes_new_descriptor(self):
        with patch.object(launch.os, 'open', return_value=123), patch.object(launch.os, 'fstat', side_effect=OSError('injected EIO')), patch.object(launch.os, 'close') as close:
            with self.assertRaisesRegex(OSError, 'injected EIO'):
                launch.RotatingProcessFile('/not-opened', max_bytes=8192, backups=2)
            close.assert_called_once_with(123)

    def test_repeat_filter_keeps_first_new_critical_and_summary(self):
        now = [0.]
        filt = ConnectionLogFilter(clock=lambda:now[0])
        def record(msg='Connection to (host, 502) failed: timed out', level=logging.ERROR):
            return logging.LogRecord('pymodbus.logging', level, '', 0, msg, (), None)
        self.assertTrue(filt.filter(record()))
        for _ in range(100):self.assertFalse(filt.filter(record()))
        self.assertTrue(filt.filter(record('other safety fault')))
        self.assertTrue(filt.filter(record(level=logging.CRITICAL)))
        self.assertTrue(filt.filter(record('Connection to (other, 502) failed: refused')))
        now[0] = 11.
        repeated = record()
        self.assertTrue(filt.filter(repeated))
        self.assertIn('suppressed 100', repeated.getMessage())
        self.assertEqual(filt.snapshot()['suppressed_repeats'],100)

if __name__ == '__main__':unittest.main()
