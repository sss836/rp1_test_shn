"""Recording runs using fake motors and a local data root, never real PLC/CAN."""
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from factory_hmi.core.identity import build_local_execution_code
from factory_hmi.core.service import FactoryService
from factory_hmi.gateway.schemas import PlaybackStartRequest

HMI = Path(__file__).resolve().parents[1]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv('RP1_FACTORY_BENCH_ID', raising=False)
    monkeypatch.delenv('RP1_FACTORY_STATION_ID', raising=False)
    result = FactoryService(data_root=tmp_path/'data')
    result.configure(config_path=str(HMI/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake')
    trajectory = tmp_path/'offline.npz'
    np.savez(trajectory,motor_pos=np.zeros((31,7)),fps=np.asarray([100.]),time=np.arange(31)/100.,limb=np.asarray(['left_arm']))
    result.import_trajectory(str(trajectory))
    result.connect()  # FakeMotorBackend only; fake CAN state, no socket or native SDK.
    result.arm()
    yield result
    result.controller.wait(timeout=3)
    with result._lock:
        threads = list(result._report_threads)
    for thread in threads:
        thread.join(timeout=5)
    result.disconnect()


def finish_report(service):
    assert service.controller.wait(timeout=3)
    with service._lock:
        threads = list(service._report_threads)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert service._report_errors == {}
    assert service.controller.snapshot()['playback']['progress']['outcome'] == 'completed'
    history = service.execution_history()
    assert len(history) == 1
    return service.report_manager.manifest(history[0]['test_id'])


def platform_context():
    return {key:'11111111-1111-4111-8111-111111111111' for key in (
        'campaign_id','cycle_id','segment_id','asset_id','configuration_id','test_case_version_id','station_id'
    )}


@pytest.mark.parametrize('scope',[None,'local'])
def test_recording_without_bench_or_platform_login_finishes_csv_and_local_report(service, scope):
    service.start(record=True,recording_scope=scope,robot_id='rp1-arm-03',loop=False,record_rate_hz=10)
    manifest=finish_report(service)
    assert manifest['test_id'].startswith('LOCAL_left_arm_')
    assert manifest['recording_scope']=='local'
    assert manifest['sample_id']=='rp1-arm-03'
    assert manifest['bench_id']=='unknown'
    assert service.record_file().is_file()
    assert len(service.record_file().read_text().splitlines()) >= 2
    assert service.report_manager.report_path(manifest['test_id']).is_file()
    schema=json.loads((HMI/'factory_hmi/contracts/execution-bundle-v1.schema.json').read_text())
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(manifest)
    rebuilt=service.report_manager.rebuild(manifest['test_id'])
    assert rebuilt.manifest['recording_scope']=='local'
    assert service.execution_history()[0]['recording_scope']=='local'
    with pytest.raises(ValueError,match='仅本地记录'):
        service.submit_execution(manifest['test_id'])
    assert service.outbox.list()==[]


def test_local_station_login_label_is_not_fabricated_as_a_bench_id(service):
    service.start(record=True,recording_scope='local',robot_id='local sample',station_id='Fab_01',operator_id='operator',loop=False)
    manifest=finish_report(service)
    assert manifest['bench_id']=='unknown'
    assert manifest['station_id']=='Fab_01'
    assert manifest['recording_scope']=='local'


@pytest.mark.parametrize('changes,reason',[
    ({'bench_id':None},'bench_id must match'),
    ({'sample':'arm local'},'sample_id must match'),
    ({'cycle_id':None},'上下文不完整'),
    ({'station_id':'Fab_01'},'必须是真实上下文的 UUID'),
])
def test_platform_record_rejects_invalid_identity_or_partial_context_before_motion(service,changes,reason):
    kwargs={**platform_context(),'bench_id':'MP-01','robot_id':'RP1.3-LARM-01'}
    changes=dict(changes)
    if 'sample' in changes:kwargs['robot_id']=changes.pop('sample')
    kwargs.update(changes)
    with patch.object(service.controller,'start') as start:
        with pytest.raises(ValueError,match=reason):
            service.start(record=True,recording_scope='platform',**kwargs)
        start.assert_not_called()
    assert service.controller.enable_recording is False
    assert not list(service.record_root.glob('*'))


def test_partial_platform_context_does_not_silently_fall_back_to_local(service):
    with patch.object(service.controller,'start') as start:
        with pytest.raises(ValueError,match='上下文不完整'):
            service.start(record=True,robot_id='RP1.3-LARM-01',campaign_id=platform_context()['campaign_id'])
        start.assert_not_called()


def test_local_request_cannot_smuggle_platform_associations(service):
    with pytest.raises(ValueError,match='本地记录不能同时携带平台测试上下文'):
        service.start(record=True,recording_scope='local',campaign_id=platform_context()['campaign_id'])


def test_complete_platform_context_preserves_canonical_identity_and_independent_submission(service):
    service.start(record=True,recording_scope='platform',robot_id='RP1.3-LARM-01',bench_id='MP-01',loop=False,**platform_context())
    manifest=finish_report(service)
    assert not manifest['test_id'].startswith('LOCAL_')
    assert manifest['test_id'].endswith('_RP1.3-LARM-01_MP-01')
    assert manifest['recording_scope']=='platform'
    assert service.outbox.list()==[]  # Completing a run does not submit it.
    queued=service.submit_execution(manifest['test_id'])
    assert queued['state']=='local_completed'  # Local queue, no network or approval.


def test_unrecorded_run_still_needs_no_identity_or_record_files(service):
    service.start(record=False,loop=False)
    assert service.controller.wait(timeout=3)
    assert service.controller.enable_recording is False
    assert service.controller.record_output is None
    assert not list(service.record_root.glob('*'))


def test_local_ids_in_same_minute_are_unique_and_safe_for_file_paths():
    from datetime import datetime,timezone
    now=datetime(2026,10,10,8,0,tzinfo=timezone.utc)
    first=build_local_execution_code('../left arm',now,'11111111-1111-4111-8111-111111111111')
    second=build_local_execution_code('../left arm',now,'22222222-2222-4222-8222-222222222222')
    assert first!=second
    assert '/' not in first and '\\' not in first
    assert first.startswith('LOCAL_left_arm_')


def test_invalid_record_scope_is_rejected_by_api_schema():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):PlaybackStartRequest(recording_scope='guess')


def test_gateway_record_checkbox_request_starts_and_local_submit_is_refused(service,tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from factory_hmi.gateway.app import create_app
    mock_config=tmp_path/'mock-plc.yaml'
    import yaml
    mock_settings=yaml.safe_load((HMI/'factory_hmi/config/plc_cabinet.yaml').read_text())
    mock_settings['driver']='mock'
    mock_settings['access_mode']='control'
    mock_config.write_text(yaml.safe_dump(mock_settings))
    monkeypatch.setenv('RP1_FACTORY_PLC_CONFIG',str(mock_config))
    with TestClient(create_app(controller=service,data_root=tmp_path/'gateway')) as client:
        headers={'X-RP1-Client-ID':'offline-recorder'}
        response=client.post('/api/v1/playback/start',headers=headers,json={
            'record':True,'recording_scope':'local','record_rate_hz':10,
            'robot_id':'现场机械臂1','loop':False,
        })
        assert response.status_code==200,response.text
        manifest=finish_report(service)
        refused=client.post('/api/v1/executions/submit',headers=headers,json={'test_id':manifest['test_id']})
        assert refused.status_code==409,refused.text
        assert '仅本地记录' in refused.text
        assert service.outbox.list()==[]
