"""WP1 behavior checks: real service + fake motors; never PLC/CAN hardware."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
import time
import numpy as np
import pytest

from factory_hmi.client import GatewayClient, GatewayError, GatewayHTTPError
from factory_hmi.core.service import FactoryService
from factory_hmi.core.command_receipts import StartReceipts
from factory_hmi.core.fault_response import request_fault_disable
from factory_hmi.core.backend import MotorBackend
from factory_hmi.models import StationState

HMI = Path(__file__).resolve().parents[1]

@pytest.fixture
def armed(tmp_path):
    service = FactoryService(data_root=tmp_path / 'data')
    service.configure(config_path=str(HMI / 'scripts/config/arm_motors.yaml'), limb='left_arm', backend='fake')
    service.connect()
    samples = service.controller.backend.read_samples()
    path = tmp_path / 'trajectory.npz'
    np.savez(path, motor_pos=np.tile([s.pos_rad for s in samples], (3,1)), time=[0., .5, 1.], fps=[2.], limb=['left_arm'])
    service.import_trajectory(str(path))
    service.arm()
    yield service
    service.close()


def payload(service, command='start-command-001', **changes):
    return dict(command_id=command, expected_state='armed', expected_config_revision=service._config_revision,
                test_case_id='OTHER', test_name='WP1 synthetic evidence', robot_id='mock-object', bench_id='B-WP1',
                loop=True, record=True, **changes)


def test_same_command_concurrent_reuses_receipt_without_identity_or_download_change(armed):
    p = payload(armed)
    with patch.object(armed.controller, 'start', wraps=armed.controller.start) as start:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: armed.start(**p), range(2)))
        assert start.call_count == 1
    assert results[0] == results[1]
    identity = dict(armed._record_identity)
    pointer = armed.controller.record_output
    session = armed._recording_session
    for changed in ({**p, 'speed': .5}, {**p, 'command_id': 'second-command-001'}):
        with pytest.raises((ValueError, RuntimeError)):
            armed.start(**changed)
        assert armed._record_identity == identity
        assert armed.controller.record_output == pointer
        assert armed._recording_session is session
    assert armed.start_receipt('second-command-001')['status'] == 'rejected'
    assert len(list(armed.record_root.glob('*.context.json'))) == 1


def test_distinct_concurrent_commands_have_one_winner(armed):
    def start(key):
        try:
            return armed.start(**payload(armed, key))['start_command']['command_id']
        except (RuntimeError, ValueError):
            return None
    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(start, ['concurrent-one', 'concurrent-two']))
    assert sum(r is not None for r in result) == 1
    assert armed.start_receipt(next(r for r in result if r))['status'] == 'accepted'
    assert len(list(armed.record_root.glob('*.context.json'))) == 1


def test_stale_configuration_rejection_and_failure_preserve_previous_evidence(armed):
    before = (dict(armed._record_identity), armed.controller.record_output, armed.controller.playback)
    p = payload(armed)
    p['expected_config_revision'] = 'stale-version'
    with pytest.raises(ValueError, match='版本'):
        armed.start(**p)
    with patch.object(armed.controller, 'start', side_effect=ValueError('synthetic preflight rejected')):
        with pytest.raises(ValueError, match='preflight'):
            armed.start(**payload(armed, 'preflight-reject'))
    assert (armed._record_identity, armed.controller.record_output, armed.controller.playback) == before
    assert not list(armed.record_root.glob('*.context.json'))
    assert armed.start_receipt('preflight-reject')['status'] == 'rejected'
    # Rejected receipts survive a newly opened journal.
    assert StartReceipts(armed.data_root / 'start-receipts.sqlite3').get('preflight-reject')['status'] == 'rejected'


def test_completed_record_pointer_survives_rejected_next_draft(armed):
    armed.start(**payload(armed))
    armed.stop(reason='synthetic immediate stop')
    assert armed.controller.wait(3)
    armed.wait_for_reports(5)
    pointer = armed.record_file()
    original = pointer.read_bytes()
    identity = dict(armed._record_identity)
    with pytest.raises((RuntimeError, ValueError)):
        armed.start(**payload(armed, 'rejected-next-draft', data_domain='FACTORY'))
    assert armed.record_file() == pointer and pointer.read_bytes() == original
    assert armed._record_identity == identity


@pytest.mark.parametrize('domain', ['RESEARCH', 'FACTORY'])
def test_formal_task_without_measurement_record_is_rejected_in_both_domains(armed, domain):
    p = payload(armed, 'formal-no-record-' + domain, data_domain=domain)
    p.update(test_case_id='REL-SARM-001', record=False)
    with pytest.raises(ValueError, match='必须启用测量记录'):
        armed.start(**p)
    assert armed.controller.state == StationState.ARMED
    assert not armed._record_identity and armed.controller.record_output is None


@pytest.mark.parametrize('feedback', ['confirmed_mock', 'unknown', 'failed'])
def test_drive_fault_stops_playback_disables_without_automatic_return_or_resume(armed, feedback):
    armed.start(**payload(armed))
    playback = armed.controller.playback
    backend = armed.controller.backend
    deadline = time.monotonic() + 2
    while playback._recorder is None and time.monotonic() < deadline:
        time.sleep(.005)
    assert playback._recorder is not None
    with patch.object(backend, 'disable_feedback', return_value=feedback), \
         patch.object(backend, 'disable', side_effect=RuntimeError('synthetic disable failed') if feedback == 'failed' else None, wraps=backend.disable) as disable, \
         patch.object(playback, '_return_and_settle') as unused_return:
        # Drive faults keep immediate hard-fault handling. Host recording
        # failures now have their own stronger passive-damping checks.
        backend.inject_error(0, 1)
        with playback._lock:
            playback._last_samples = backend.read_samples()
        playback._handle_fault(RuntimeError('confirmed synthetic drive fault'))
        assert playback.fault is not None
        assert playback.wait(3)
        response = playback.snapshot()['fault_response']
        assert response['playback_stop'] == 'stopped'
        assert response['disable_request'] == ('failed' if feedback == 'failed' else 'sent')
        assert response['disable_feedback'] == ('unknown' if feedback == 'failed' else feedback)
        assert response['safe_confirmed'] is False
        assert not armed.controller.position_hold_active
        disable.assert_called_once()
        unused_return.assert_not_called()
    with pytest.raises(RuntimeError):
        armed.resume()
    assert armed.controller.state == StationState.FAULT
    assert not playback.running


def test_hardware_default_has_no_disable_feedback_acknowledgement():
    assert MotorBackend.disable_feedback(object()) == 'unknown'


def client_args():
    return dict(speed=1., loop=True, cycles=0, duration_hours=None, record=True, test_case_id='OTHER',
                test_name='mock run', robot_id='mock', bench_id='B-WP1', expected_config_revision='v1')


def test_lost_start_response_queries_receipt_once_without_reposting():
    client = GatewayClient('http://isolated.invalid')
    accepted = {'start_command': {'command_id': 'stable-command', 'status': 'accepted'}, 'platform': {'execution_uuid':'same'}}
    with patch.object(client, 'post', side_effect=GatewayError('response lost')) as post, \
         patch.object(client, 'get', side_effect=[{'status':'accepted', 'result':accepted}, {'state':'fault'}]) as get:
        assert client.playback_start(command_id='stable-command', **client_args())['result'] == accepted
        post.assert_called_once()
        assert get.call_args_list[0].args == ('/api/v1/playback/commands/stable-command',)
        assert get.call_args_list[1].args == ('/api/v1/snapshot',)


def test_unknown_start_blocks_next_draft_and_only_queries():
    client = GatewayClient('http://isolated.invalid')
    with patch.object(client, 'post', side_effect=TimeoutError()) as post, \
         patch.object(client, 'get', return_value={'status':'pending'}) as get:
        with pytest.raises(GatewayError, match='结果待确认'):
            client.playback_start(command_id='unknown-command', **client_args())
        with pytest.raises(GatewayError, match='结果待确认'):
            client.playback_start(command_id='another-command', **client_args())
        assert post.call_count == 1 and get.call_count == 2


def test_pending_receipt_survives_restart_and_never_reexecutes(armed):
    p = payload(armed, 'interrupted-command')
    import inspect
    bound = inspect.signature(armed._start_once).bind(**{k:v for k,v in p.items() if k not in {'command_id','expected_state','expected_config_revision'}})
    bound.apply_defaults()
    digest = armed._start_receipts.digest({**bound.arguments, 'expected_state':p['expected_state'], 'expected_config_revision':p['expected_config_revision']})
    armed._start_receipts.begin(p['command_id'], digest)
    with patch.object(armed.controller, 'start') as action:
        with pytest.raises(RuntimeError, match='结果待确认'):
            armed.start(**p)
        action.assert_not_called()


def test_gateway_start_receipt_is_read_only_and_conflicting_payload_rejected(armed, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from factory_hmi.gateway.app import create_app
    with TestClient(create_app(controller=armed, data_root=tmp_path / 'gateway')) as client:
        headers = {'X-RP1-Client-ID':'wp1-owner'}
        client.app.state.runtime.session('wp1-owner').lease.claim('wp1-owner')
        p = payload(armed, 'asgi-stable-command')
        first = client.post('/api/v1/playback/start',json=p,headers=headers)
        assert first.status_code == 200, first.text
        repeat = client.post('/api/v1/playback/start',json=p,headers=headers)
        assert repeat.status_code == 200 and repeat.json()['result'] == first.json()['result']
        conflict = client.post('/api/v1/playback/start',json={**p,'speed':.5},headers=headers)
        assert conflict.status_code == 409
        lease = client.get('/api/v1/snapshot',headers=headers).json()['lease']
        receipt = client.get('/api/v1/playback/commands/asgi-stable-command',headers=headers)
        assert receipt.status_code == 200 and receipt.json()['status'] == 'accepted'
        later = client.get('/api/v1/snapshot',headers=headers).json()['lease']
        assert later['generation'] == lease['generation']
        monkeypatch.setenv('RP1_FACTORY_OPERATOR_TOKEN','synthetic-wp1-token')
        assert client.get('/api/v1/playback/commands/asgi-stable-command',headers=headers).status_code == 401


def test_normal_finish_without_completion_parameters_reports_holding(armed):
    armed.start(**payload(armed))
    result = armed.finish(reason='operator normal finish')
    assert armed.controller.wait(3)
    end = armed.controller.playback.snapshot()
    assert end['completion']['phase'] == 'holding'
    assert end['completion']['safe_disabled'] is False
    assert armed.controller.backend.motors_enabled
    assert end['fault_response'] is None


def test_inflight_disable_exposes_requested_before_feedback(armed):
    import threading
    armed.start(**payload(armed))
    playback = armed.controller.playback
    entered, release = threading.Event(), threading.Event()
    backend = armed.controller.backend
    original = backend.disable
    def slow_disable():
        entered.set()
        assert release.wait(3)
        original()
    with patch.object(backend, 'disable', side_effect=slow_disable):
        backend.inject_error(0,1)
        with playback._lock:playback._last_samples=backend.read_samples()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(playback._handle_fault, RuntimeError('confirmed synthetic drive fault'))
            try:
                assert entered.wait(1)
                response = playback.snapshot()['fault_response']
                assert response['disable_request'] == 'requested'
                assert response['disable_feedback'] == 'unknown'
                assert armed.controller.state == StationState.FAULT
                assert not armed.controller.position_hold_active
                with patch.object(armed.controller, '_resume_position_hold_from_feedback') as hold:
                    armed.controller._on_playback_complete()
                    hold.assert_not_called()
                assert response['safe_confirmed'] is False
            finally:
                release.set()
            future.result(3)
    assert playback.wait(3)
    assert playback.snapshot()['fault_response']['disable_feedback'] == 'confirmed_mock'


def test_record_finalization_failure_latches_fault_and_stops_position_hold(armed):
    armed.start(**payload(armed))
    playback = armed.controller.playback
    deadline = time.monotonic() + 2
    while playback._recorder is None and time.monotonic() < deadline:
        time.sleep(.005)
    with patch.object(playback._recorder, 'finalize', side_effect=OSError('synthetic disk finalize failure')):
        armed.finish()
        assert playback.wait(3)
        assert playback.wait_recording(3)
    assert armed.controller.state == StationState.FAULT
    assert playback.snapshot()['recording']['finished'] is False
    deadline=time.monotonic()+2
    while (armed.controller._protective_damping is None or not armed.controller._protective_damping.snapshot()['active']) and time.monotonic()<deadline:time.sleep(.005)
    assert armed.controller._protective_damping.snapshot()['active']
    assert armed.controller._protective_damping.snapshot()['kd']==1.0
    assert armed.controller.backend.commands_inhibited
    assert armed.controller.backend.motors_enabled
    assert not armed.controller.position_hold_active
    assert playback.snapshot()['fault_response']['disable_request']=='not_requested'
    deadline = time.monotonic() + 2
    while armed.controller.position_hold_active and time.monotonic() < deadline:
        time.sleep(.005)
    assert not armed.controller.position_hold_active
    with pytest.raises(RuntimeError):
        armed.resume()


def test_server_response_error_after_action_queries_unknown_and_blocks_repost():
    client = GatewayClient('http://isolated.invalid')
    with patch.object(client, 'post', side_effect=GatewayHTTPError('failed response',status_code=409)) as post, \
         patch.object(client, 'get', return_value={'status':'unknown'}):
        for _ in range(2):
            with pytest.raises(GatewayError,match='结果待确认'):
                client.playback_start(command_id='stable-error-command', **client_args())
        post.assert_called_once()
