"""Site-isolated shutdown tests using only an in-memory PLC and local IPC."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from factory_hmi.plc.config import PlcCabinetConfig, PlcTimingConfig
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.plc.models import CabinetPhase
from factory_hmi.plc.process import PlcCabinetClient, PlcCabinetProcess, _CabinetServer
from factory_hmi.plc.service import PlcCabinetService


class SiteIsolatedShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = PlcCabinetConfig(driver="mock", timings=PlcTimingConfig(poll_ms=1000))

    def test_close_joins_worker_and_disconnects_without_plc_io(self):
        service = PlcCabinetService(self.config, data_root=self.root)
        service.connect(user="offline")
        service.controller.phase = CabinetPhase.FAULT
        service._power_off_unverified = True
        with patch.object(service.client, "read_status", side_effect=AssertionError("PLC read")), \
             patch.object(service.client, "write_command", side_effect=AssertionError("PLC write")):
            service.close(site_isolated=True)
            service.close()

        self.assertFalse(service._thread.is_alive())
        self.assertFalse(service.client.connected)
        self.assertFalse(service.communication_enabled)
        self.assertTrue(service._power_off_unverified)
        self.assertEqual(service.controller.phase, CabinetPhase.DISCONNECTED)
        outcome = service.shutdown_status
        self.assertEqual(outcome["state"], "site_isolated")
        self.assertTrue(outcome["operator_asserted"])
        self.assertFalse(outcome["plc_feedback_verified"])
        self.assertIsNotNone(datetime.fromisoformat(outcome["asserted_at"]).tzinfo)
        records = (self.root / "plc" / "shutdown.jsonl").read_text().splitlines()
        self.assertEqual(len(records), 1)
        record = json.loads(records[0])
        self.assertEqual(record["event"], "site_isolation_asserted")
        self.assertEqual(record["asserted_at"], outcome["asserted_at"])
        self.assertFalse(record["plc_feedback_verified"])

    def test_pending_command_and_unjoined_worker_keep_shutdown_blocked(self):
        service = PlcCabinetService(self.config, data_root=self.root)
        service.controller._pending = Mock()
        with self.assertRaisesRegex(RuntimeError, "awaiting acknowledgement"):
            service.close(site_isolated=True)
        self.assertEqual(service.shutdown_status["state"], "unconfirmed")

        service.controller._pending = None
        service._thread = Mock()
        service._thread.is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, "worker did not stop"):
            service.close(site_isolated=True)
        self.assertEqual(service.shutdown_status["state"], "unconfirmed")
        self.assertFalse((self.root / "plc" / "shutdown.jsonl").exists())

    def test_audit_failure_blocks_before_disconnect(self):
        service = PlcCabinetService(self.config, data_root=self.root)
        service.controller.connect(user="offline")
        with patch.object(service, "_record_site_isolation", side_effect=OSError("disk unavailable")), \
             patch.object(service.client, "disconnect", wraps=service.client.disconnect) as disconnect:
            with self.assertRaisesRegex(RuntimeError, "disk unavailable"):
                service.close(site_isolated=True)
            disconnect.assert_not_called()
        self.assertTrue(service.client.connected)
        self.assertEqual(service.shutdown_status["state"], "unconfirmed")
        service.close(site_isolated=True)

    def test_cabinet_drains_accepted_safety_before_site_isolation(self):
        service = PlcCabinetService(self.config, data_root=self.root)
        entered = threading.Event()
        release = threading.Event()

        def accepted_safety(**_params):
            entered.set()
            if not release.wait(timeout=3):
                raise AssertionError("test did not release safety request")
            return {"accepted": True}

        params = {"operation": "stop", "client_id": "offline", "request_id": "offline"}
        with _CabinetServer(str(self.root / "cabinet.sock"), service, "attached", "owner") as server, \
             ThreadPoolExecutor(max_workers=2) as threads, \
             patch.object(service.coordination, "execute", side_effect=accepted_safety):
            pending = threads.submit(server._dispatch, "execute", params)
            self.assertTrue(entered.wait(timeout=1))
            closing = threads.submit(server.close_cabinet, site_isolated=True)
            try:
                with server._gate:
                    self.assertTrue(server._gate.wait_for(lambda: server._site_isolating, timeout=1))
                self.assertFalse(closing.done())
                with self.assertRaises(PlcRequestConflict) as conflict:
                    server._dispatch("execute", params)
                self.assertEqual(conflict.exception.code, "cabinet_closing")
            finally:
                release.set()
            self.assertEqual(pending.result(timeout=1), {"accepted": True})
            self.assertEqual(closing.result(timeout=1)["state"], "site_isolated")

    def test_only_owner_can_request_site_isolation_over_ipc(self):
        process = PlcCabinetProcess(self.config, data_root=self.root)
        process.start()
        try:
            attached = PlcCabinetClient.attach(process.connection_info())
            with self.assertRaises(PlcRequestConflict) as conflict:
                attached._request("close", {"site_isolated": True}, timeout=1)
            self.assertEqual(conflict.exception.code, "cabinet_forbidden")
            with self.assertRaises(PlcRequestConflict):
                process._request("close", {"site_isolated": "false"}, timeout=1, owner=True)
            self.assertEqual(process.shutdown_status["state"], "not_requested")
            process.close(site_isolated=True)
            self.assertEqual(process.shutdown_status["state"], "site_isolated")
            self.assertTrue(process.shutdown_status["ready"])
            self.assertFalse(process.shutdown_status["plc_feedback_verified"])
        finally:
            process.close()
