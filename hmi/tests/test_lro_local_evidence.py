"""Local protocol evidence checks use synthetic responses and no hardware."""
from dataclasses import replace
from datetime import datetime, timezone
import json
import time
from types import SimpleNamespace

import pytest

from factory_hmi.core.backend import FakeMotorBackend, MotorSafetyError, MotorsPyBackend
from factory_hmi.core.lro_local_evidence import read_local_legacy_evidence
from factory_hmi.core.motion_completion import configuration_sha256
from factory_hmi.models import StationConfig
from scripts.rp1_limb_common import LroProtocolExpectation

RAW_1E = "a01e00010800010000000111"


def entries(n=1):
    return tuple(replace(e, interface="can0", interface_type="canfd", motor_type="LRO",
                         motor_model=0, motor_id=i + 1)
                 for i, e in enumerate(FakeMotorBackend(n).entries))


def config(es, limb="left_arm", raw=None):
    return StationConfig(limb=limb, entries=es, raw={} if raw is None else raw)


def payload(cfg, *, nodes=None):
    return {"schema": "rp1-lro-local-protocol-evidence-v1",
            "configuration_sha256": configuration_sha256(cfg), "limb": cfg.limb,
            "confirmed_profile": "legacy", "confirmed_firmware_generation": 108,
            "evidence": "2026-11-12 当前左臂节点现场确认兼容旧协议",
            "nodes": nodes if nodes is not None else [
                {"interface": e.interface, "motor_id": e.motor_id, "motor_model": e.motor_model,
                 "numeric_software": "0.1.8", "hardware": "0.1.0", "protocol": "0.0.1.17"}
                for e in cfg.entries]}


def policy_file(tmp_path, payload_data, mode=0o600):
    directory = tmp_path / "config"
    directory.mkdir(exist_ok=True)
    path = directory / "lro-local-protocol-evidence.json"
    path.write_text(json.dumps(payload_data, ensure_ascii=False))
    path.chmod(mode)
    return path


def observation(**updates):
    data = {"status": "known", "raw_firmware": None, "numeric_software": "0.1.8",
            "hardware": "0.1.0", "protocol": "0.0.1.17", "_monotonic": time.monotonic(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "raw_responses": {"0x1e": RAW_1E}, "errors": {"0x31": "版本查询超时"}}
    data.update(updates)
    return data


def backend(cfg, rule, observed=None):
    result = MotorsPyBackend(cfg.entries, station_config=cfg, local_evidence=rule)
    result._connected = True
    observations = observed if observed is not None else [observation() for _ in cfg.entries]
    result._versions = SimpleNamespace(stale=180, timeout=.5,
                                       snapshot=lambda **kwargs: [dict(row, index=i) for i, row in enumerate(observations)])
    result._motors = [SimpleNamespace(set_lro_protocol_profile=lambda p: True) for _ in cfg.entries]
    for i, row in enumerate(observations):
        result._observe_motor_protocol(i, row)
    return result


def test_requires_protected_file_owned_by_user_and_strict_schema(tmp_path):
    cfg = config(entries())
    assert read_local_legacy_evidence(str(tmp_path)) is None
    path = policy_file(tmp_path, payload(cfg), mode=0o644)
    assert read_local_legacy_evidence(str(tmp_path)) is None
    path.chmod(0o600)
    assert read_local_legacy_evidence(str(tmp_path)) is not None
    data = payload(cfg)
    data['nodes'].append(dict(data['nodes'][0]))
    path.write_text(json.dumps(data))
    assert read_local_legacy_evidence(str(tmp_path)) is None
    path.write_text('{"schema":"first", "schema":"second"}')
    assert read_local_legacy_evidence(str(tmp_path)) is None
    path.unlink()
    target = tmp_path / 'other'
    target.write_text(json.dumps(payload(cfg)))
    target.chmod(0o600)
    path.symlink_to(target)
    assert read_local_legacy_evidence(str(tmp_path)) is None


def test_exact_node_uses_confirmed_legacy_without_inventing_firmware(tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    rule = read_local_legacy_evidence(str(tmp_path))
    b = backend(cfg, rule)
    before_enable = b.motor_versions()[0]
    assert before_enable['active_profile'] is None
    assert before_enable['confirmed_firmware_generation'] == 108
    assert before_enable['firmware_confirmation_source'] == 'local_numeric_evidence'
    assert before_enable['raw_firmware'] is None
    b.prepare_motor_protocols()
    assert b._selected_protocols == ["legacy"]
    rows = b.motor_versions()
    assert rows[0]['raw_firmware'] is None
    assert rows[0]['numeric_software'] == '0.1.8'
    assert rows[0]['protocol_confirmation_source'] == 'local_numeric_evidence'
    assert rows[0]['protocol_confirmation_evidence'] == rule.evidence


def test_new_snapshot_cannot_inherit_previous_version_confirmation(tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    snapshots = [observation()]
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)), snapshots)
    assert b.motor_versions()[0]['confirmed_firmware_generation'] == 108
    previous = snapshots[0]
    snapshots[0] = {**previous, 'observed_at': '2026-11-12T00:00:00+00:00'}
    assert 'confirmed_firmware_generation' not in b.motor_versions()[0]
    snapshots[0] = {**previous, 'status': 'stale'}
    assert 'confirmed_firmware_generation' not in b.motor_versions()[0]
    snapshots[0] = previous
    b._connected = False
    assert 'confirmed_firmware_generation' not in b.motor_versions()[0]


@pytest.mark.parametrize('changes', [
    {'numeric_software': '0.1.9'}, {'hardware': '0.1.1'}, {'protocol': '0.0.1.18'},
    {'protocol': None}, {'raw_firmware': '109'}, {'raw_firmware': 'unknown'},
    {'errors': {'0x31': '版本查询超时', '0x1e': '版本查询超时'}},
    {'raw_responses': {}}, {'raw_responses': {'0x1e': 'a01e00010800010000000112'}},
    {'_monotonic': time.monotonic() - 1000}, {'status': 'stale'}])
def test_missing_conflicting_untrusted_or_stale_identity_never_selects(tmp_path, changes):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)), [observation(**changes)])
    assert 'confirmed_firmware_generation' not in b.motor_versions()[0]
    with pytest.raises(MotorSafetyError):
        b.prepare_motor_protocols()
    assert b._selected_protocols == [None]


def test_existing_evidence_scope_remains_exact_but_known_numeric_versions_are_automatic(tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    rule = read_local_legacy_evidence(str(tmp_path))
    assert MotorsPyBackend(cfg.entries, local_evidence=rule)._local_evidence is None
    b = backend(cfg, None)
    assert 'confirmed_firmware_generation' not in b.motor_versions()[0]
    b.prepare_motor_protocols()
    assert b._selected_protocol_sources == ['automatic_numeric_version']
    for changed in (config(cfg.entries, raw={'different': True}),
                    config((replace(cfg.entries[0], motor_model=1),)),
                    config((replace(cfg.entries[0], interface='can1'),)),
                    config((replace(cfg.entries[0], motor_id=2),)),
                    config(cfg.entries, limb='right_arm')):
        b = backend(changed, rule)
        assert 'confirmed_firmware_generation' not in b.motor_versions()[0]
        b.prepare_motor_protocols()
        assert b._selected_protocol_sources == ['automatic_numeric_version']


def test_late_conflict_latches_and_fences_commands(tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)))
    b.prepare_motor_protocols()
    b._observe_motor_protocol(0, observation(raw_firmware='109'))
    with pytest.raises(MotorSafetyError):
        b._check_protocol_conflicts()
    b._observe_motor_protocol(0, observation())
    with pytest.raises(MotorSafetyError):
        b._check_protocol_conflicts()


def test_loss_of_numeric_confirmation_after_selection_fences_commands(tmp_path):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)))
    b.prepare_motor_protocols()
    b._observe_motor_protocol(0, observation(protocol='0.0.1.18'))
    with pytest.raises(MotorSafetyError):
        b._check_protocol_conflicts()


def test_v103_mixed_node_uses_existing_full_raw_identity(tmp_path):
    exp = LroProtocolExpectation('v1.03', '5.0.0.109', '5.0.1', '1.2.3', '1.0.3.0', 'fixture')
    es = entries(2)
    es = (es[0], replace(es[1], lro_protocol=exp))
    cfg = config(es)
    p = payload(cfg, nodes=[payload(cfg)['nodes'][0]])
    policy_file(tmp_path, p)
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)), [observation(), observation(
        raw_firmware=exp.raw_firmware, numeric_software=exp.numeric_software,
        hardware=exp.hardware, protocol=exp.protocol, errors={})])
    b._motors[1] = SimpleNamespace(set_lro_protocol_profile=lambda p: p == 'v1.03',
                                    get_feedback_count=lambda: 0, get_response_count=lambda: 0)
    b.prepare_motor_protocols()
    assert b._selected_protocols == ['legacy', 'v1.03']
    assert b._selected_protocol_sources == ['local_numeric_evidence', 'configured_identity']


def test_connected_worker_reads_seven_nodes_and_rechecks_each_after_reconnect(tmp_path, monkeypatch):
    """Seven simulated exact nodes: 0x31 timeout + actual 0x1e response, then reconnect."""
    import sys

    models = (2, 1, 1, 1, 1, 0, 0)
    cfg = config(tuple(replace(e, motor_model=models[i]) for i, e in enumerate(entries(7))))
    policy_file(tmp_path, payload(cfg))
    rule = read_local_legacy_evidence(str(tmp_path))
    motors = []

    class SyntheticMotor:
        def __init__(self):
            self.enables = 0
            self.requests = []
        def set_motor_control_mode(self, mode):
            pass
        def request_version(self, code):
            self.requests.append(code)
            return True
        def version_payload(self, code):
            return bytes.fromhex(RAW_1E) if code == 0x1e else b''
        def init_motor(self):
            self.enables += 1
            return 0
        def deinit_motor(self):
            pass

    def driver(_entry, _sdk):
        motor = SyntheticMotor()
        motors.append(motor)
        return motor

    monkeypatch.setitem(sys.modules, 'motors_py', SimpleNamespace(MotorControlMode=SimpleNamespace(MIT='MIT')))
    monkeypatch.setattr('factory_hmi.core.backend.create_motor_driver', driver)
    b = MotorsPyBackend(cfg.entries, station_config=cfg, local_evidence=rule)
    try:
        b.open_transport()
        deadline = time.monotonic() + 6
        while b.motor_protocols_pending() and time.monotonic() < deadline:
            time.sleep(.01)
        assert not b.motor_protocols_pending()
        b.prepare_motor_protocols()
        assert b._selected_protocols == ['legacy'] * 7
        assert b._selected_protocol_sources == ['local_numeric_evidence'] * 7
        assert all(m.enables == 0 for m in motors)
        assert all(m.requests[:2] == [0x31, 0x1e] for m in motors)
        # A synthetic explicit enable can proceed only after all seven
        # independently matched the station-bound evidence.
        b.enable_motors()
        assert all(m.enables == 1 for m in motors)
        b.close_transport()
        b.open_transport()
        # Old identities cannot be reused across connection lifetimes.
        assert b._selected_protocols == [None] * 7
        assert b._selected_protocol_sources == [None] * 7
        assert b.motor_protocols_pending()
        assert len(motors) == 14 and all(m.enables == 0 for m in motors[7:])
    finally:
        b.close_transport()


def _service_child(state_root, cfg, queue):
    import os
    from pathlib import Path
    from factory_hmi.core.service import FactoryService

    os.environ['RP1_HMI_STATE_ROOT'] = state_root
    service = FactoryService(data_root=Path(state_root) / 'service-data',
                             can_manager=SimpleNamespace(status=lambda names: []))
    try:
        service.configure(config_path=str(Path(__file__).resolve().parents[1] / 'scripts/config/arm_motors.yaml'),
                          limb='left_arm', backend='motors_py', station_config=cfg)
        b = service.controller.backend
        enabled = b._local_evidence is not None
        b._connected = True
        b._versions = SimpleNamespace(stale=180, timeout=.5)
        b._motors = [SimpleNamespace() for _ in cfg.entries]
        for i in range(len(cfg.entries)):
            b._observe_motor_protocol(i, observation())
        b.prepare_motor_protocols()
        queue.put((enabled, b._selected_protocols, b._selected_protocol_sources))
    except BaseException as exc:
        queue.put(('error', str(exc)))


def test_service_configure_loads_evidence_inside_isolated_station_process(tmp_path):
    import multiprocessing

    models = (2, 1, 1, 1, 1, 0, 0)
    cfg = config(tuple(replace(e, motor_model=models[i]) for i, e in enumerate(entries(7))))
    policy_file(tmp_path, payload(cfg))
    context = multiprocessing.get_context('spawn')
    queue = context.Queue()
    process = context.Process(target=_service_child, args=(str(tmp_path), cfg, queue))
    process.start()
    process.join(15)
    assert not process.is_alive()
    assert process.exitcode == 0
    result = queue.get(timeout=2)
    assert result == (True, ['legacy'] * 7, ['local_numeric_evidence'] * 7)


def test_local_evidence_long_running_refresh_then_dead_worker_expires(tmp_path, monkeypatch):
    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)))
    b.prepare_motor_protocols()
    import factory_hmi.core.backend as backend_module
    real_now = time.monotonic()
    clock = [real_now]
    monkeypatch.setattr(backend_module.time, 'monotonic', lambda: clock[0])
    for _ in range(240):
        clock[0] += 5
        if _ % 12 == 0:  # One synthetic good refresh each simulated minute.
            b._observe_motor_protocol(0, observation())
        b._check_protocol_conflicts()
    # The reader is now stalled: eventually evidence goes stale and stays
    # latched even if a later callback happens, until explicit recovery.
    clock[0] += 181
    with pytest.raises(MotorSafetyError, match='已过期'):
        b._check_protocol_conflicts()
    b._observe_motor_protocol(0, observation())
    with pytest.raises(MotorSafetyError, match='已过期'):
        b._check_protocol_conflicts()


def test_expired_identity_uses_existing_fault_response_without_claiming_disable_feedback(tmp_path):
    from factory_hmi.core.controller import FactoryController
    from factory_hmi.models import StationState

    cfg = config(entries())
    policy_file(tmp_path, payload(cfg))
    b = backend(cfg, read_local_legacy_evidence(str(tmp_path)))
    b.prepare_motor_protocols()
    operations = []
    b._motors = [SimpleNamespace(unlock_motor=lambda: operations.append('disable'))]
    b._enabled = True
    b._protocol_observations[0]['_monotonic'] -= 181
    controller = FactoryController(cfg, b, enable_watchdog=False)
    controller.state_machine.transition(StationState.CONNECTED)
    with pytest.raises(MotorSafetyError, match='已过期') as fault:
        b.command_mit([0.0])
    controller._set_fault(fault.value, samples=[])
    assert controller.state == StationState.FAULT
    assert operations == ['disable']
    assert controller._fault_response['disable_request'] == 'sent'
    assert controller._fault_response['disable_feedback'] == 'unknown'
    assert controller._fault_response['safe_confirmed'] is False


@pytest.mark.parametrize('error',['SDK或驱动不支持版本查询','版本响应长度不支持','版本查询超时'])
def test_valid_numeric_108_does_not_require_optional_raw_string(tmp_path,error):
    b=backend(config(entries()),None,[observation(errors={'0x31':error})])
    b.prepare_motor_protocols()
    assert b._selected_protocols==['legacy'] and b._selected_protocol_sources==['automatic_numeric_version']
