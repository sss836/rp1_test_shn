"""Numeric 108 maintenance uses device data independently of motion profiles."""
from datetime import datetime, timedelta, timezone

import pytest

from factory_hmi.core.motor_maintenance import confirmed_legacy_108
from factory_hmi.motor_parameter_contract import MODELS
from test_module_parameters import local_numeric_observation, numeric_legacy_task, finish


def numeric_observation(model):
    row = local_numeric_observation(model=model)
    row.pop("confirmed_firmware_generation")
    row.pop("firmware_confirmation_source")
    return row


@pytest.mark.parametrize("model", MODELS)
def test_all_models_accept_device_numeric_identity_without_station_declaration(model):
    row = numeric_observation(model)
    assert confirmed_legacy_108(row, row)


@pytest.mark.parametrize("updates", [
    {"raw_responses": {}},
    {"raw_responses": {"0x1e": "a01e00010900010000000111"}},
    {"raw_responses": {"0x1e": "a01e000108000100"}},
    {"hardware": "0.2.0"},
    {"protocol": "0.0.1.18"},
    {"software": "0.1.9", "numeric_software": "0.1.9"},
    {"status": "timeout"},
    {"raw_firmware": "V5.0.0.109"},
    {"model": "UNKNOWN"},
    {"observed_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
])
def test_missing_changed_and_expired_numeric_data_remains_unavailable(updates):
    row = {**numeric_observation("LRO_6562"), **updates}
    assert not confirmed_legacy_108(row, row)


def test_numeric_identity_must_belong_to_selected_node():
    row = numeric_observation("LRO_6562")
    assert not confirmed_legacy_108({**row, "motor_id": row["motor_id"] + 1}, row)


def test_parameter_check_can_use_signed_template_without_motion_declaration(tmp_path):
    task, selections, writes = numeric_legacy_task(tmp_path)
    task.observations = lambda: [dict(numeric_observation("LRO_5550"), index=i, motor_id=i+1) for i in range(2)]
    try:
        task.read(selections, local=True)
        assert finish(task)["status"] == "checked_with_differences"
        assert writes == []
    finally:
        task.cancel()
