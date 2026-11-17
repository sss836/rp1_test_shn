"""Normal stop then exit, with the deployed V1.2 feedback gap (mock only)."""
from dataclasses import replace

import pytest

from factory_hmi.plc.config import PlcCabinetConfig, PlcTimingConfig
from factory_hmi.plc.models import CabinetPhase
from factory_hmi.plc.service import PlcCabinetService


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    service = PlcCabinetService(PlcCabinetConfig(timings=PlcTimingConfig(poll_ms=10)), data_root=tmp_path)
    client, controller = service.client, service.controller
    read = client.read_status

    def cabinet_read():
        status = read()
        # Actual mapping has no MainContactorFB. PS1 loses status when KM0 opens.
        return replace(status, main_contactor_fb=None,
                       ps1_feedback_flags=status.ps1_feedback_flags if status.main_ready else 0,
                       ps1_status_age_ms=0 if status.main_ready else 65535,
                       ps1_comm_ok=bool(status.main_ready))

    monkeypatch.setattr(client, "read_status", cabinet_read)
    controller.connect(user="mock-only")
    service.start_sequence([1], user="mock-only")
    for _ in range(100):
        controller.tick()
        if controller.phase == CabinetPhase.RUNNING:
            break
    assert controller.phase == CabinetPhase.RUNNING
    assert controller.request_stop(user="mock-only")
    for _ in range(100):
        controller.tick()
        if controller.phase == CabinetPhase.OFF:
            break
    assert controller.phase == CabinetPhase.OFF
    assert controller.normal_stop_confirmed()
    yield service
    service._stop.set()
    client.disconnect()
    service._log_writer.close()


def test_close_uses_completed_stop_after_ps1_loses_power(stopped, monkeypatch):
    controller = stopped.controller
    assert controller.status.main_contactor_fb is None
    assert controller.snapshot()["status"]["PS1ActualOutput"] is None
    # Stay OFF across polling and heartbeat; no second stop command is needed.
    for _ in range(10):
        controller.heartbeat()
        controller.tick()
    assert controller.normal_stop_confirmed()
    def unexpected_stop(**kwargs):
        pytest.fail("exit repeated stop against a depowered PS1")
    monkeypatch.setattr(controller, "request_stop", unexpected_stop)
    stopped.close()
    assert stopped.shutdown_status["state"] == "confirmed"
    assert not stopped.client.connected


def test_repeated_stop_is_idempotent(stopped):
    before = len(stopped.client.command_history)
    assert stopped.controller.request_stop(user="mock-only")
    assert len(stopped.client.command_history) == before
    assert stopped.controller.normal_stop_confirmed()


@pytest.mark.parametrize("change", [
    {"main_contactor_fb": True}, {"main_ready": True},
    {"channel_permit": (True, False, False, False)},
    {"ps1_request": True}, {"ps1_actual_output": True},
    {"host_connected": False}, {"plc_ready": False},
    {"fault_latched": True}, {"protocol_version": 0x0101},
    {"received_monotonic": 0}, {"ps1_power_cycle": 200},
    {"acknowledged_sequence": 200},
])
def test_contradictory_or_stale_feedback_cannot_close(stopped, change):
    stopped.controller.status = replace(stopped.controller.status, **change)
    assert not stopped.controller.normal_stop_confirmed()
    assert not stopped._shutdown_feedback_confirmed()


def test_start_invalidates_previous_stop(stopped):
    assert stopped.controller.request_start(user="mock-only")
    assert not stopped.controller.normal_stop_confirmed()


def test_link_loss_revokes_receipt(stopped):
    stopped.controller._communication_lost("mock link lost")
    assert stopped.controller._normal_stop_receipt is None
    assert not stopped.controller.normal_stop_confirmed()


def test_cpu_restart_revokes_receipt(stopped):
    stopped.controller.notify_cpu_restart()
    assert stopped.controller._normal_stop_receipt is None
    assert not stopped.controller.normal_stop_confirmed()


def test_missing_contact_feedback_without_stop_proof_is_not_off(stopped):
    stopped.controller._normal_stop_receipt = None
    assert not stopped._shutdown_feedback_confirmed()


def test_new_observed_ack_permanently_revokes_receipt(stopped):
    stopped.client.set_status(acknowledged_sequence=200)
    stopped.controller.tick()
    assert stopped.controller._normal_stop_receipt is None
    stopped.client.set_status(acknowledged_sequence=stopped.controller.command.command_sequence)
    stopped.controller.tick()
    assert not stopped.controller.normal_stop_confirmed()
