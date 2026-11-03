import asyncio,csv,json,math,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from factory_hmi.models import MotorSample
from factory_hmi.core.telemetry_hub import TelemetryHub
from factory_hmi.core.recording.segments import SegmentedWriter,sources,copy_logical_csv
from factory_hmi.core.recording.policy import capacity_budget,freeze_policy


def test_cache_bounded_quality_identity():
 hub=TelemetryHub(max_bytes=12000,max_frames=8,window_s=1)
 for n in range(1000):hub.publish([MotorSample(pos_rad=math.nan,joint_name='J1')],now=100+n*.1,utc=1000+n*.1)
 data=hub.read(now=200)
 assert len(data['frames'])<=8 and data['cache_bytes']<=12000
 assert data['frames'][-1]['values']['J1/position_rad'] is None
 assert data['frames'][-1]['quality']['J1/position_rad']=='missing'
 assert data['stale'] is False
 hub.begin('new','config')
 assert hub.read()['frames']==[] and hub.read()['run_id']=='new'


def test_segments_continuous_and_tamper(tmp_path):
 output=tmp_path/'run.csv';writer=SegmentedWriter(output,['time_s','sample','j_pos_rad'],max_bytes=90)
 for n in range(40):writer.writerow(dict(time_s=n*.1,sample=n,j_pos_rad=n))
 writer.close();parts=sources(output)
 assert len(parts)>1
 merged=tmp_path/'merged.csv';copy_logical_csv(output,merged)
 with merged.open() as f:rows=list(csv.DictReader(f))
 assert [int(r['sample']) for r in rows]==list(range(40))
 assert writer.durable_rows==40
 parts[-1].write_text('corrupt')
 with pytest.raises(ValueError,match='hash'):sources(output)


def test_capacity_and_policies_do_not_grant_qualification():
 budget=capacity_budget(row_bytes=900,rate_hz=20,duration_s=604800,free_bytes=100,reserve_bytes=10)
 assert not budget['admitted'] and budget['components']['analysis_spool']>budget['components']['raw']
 assert freeze_policy(purpose='production')['qualification']=='not_automatic'
 with pytest.raises(ValueError,match='全量对照'):freeze_policy(policy='agingcompact',recipe='R')
 with pytest.raises(ValueError,match='草稿'):freeze_policy(policy='reliability')


def test_preview_is_cache_only_and_has_no_control_side_effect(tmp_path):
 import httpx
 from factory_hmi.gateway.app import create_app
 hubcache=TelemetryHub();hubcache.begin('execution-1');hubcache.publish([MotorSample(1,joint_name='J1')])
 class Device:
  telemetry_hub=hubcache
  def snapshot(self):raise AssertionError('preview polled device snapshot')
  def refresh_feedback(self):raise AssertionError('preview read hardware')
 app=create_app(Device(),data_root=tmp_path/'gateway')
 hub=app.state.runtime;rt=hub.session('operator');lease=rt.lease_snapshot();sessions=len(hub._sessions)
 async def run():
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1',headers={'X-RP1-Client-ID':'operator'}) as client:
   denied=await client.get('/api/v1/preview/state');assert denied.status_code==401
   launch=await client.post('/api/v1/preview/launch');assert launch.status_code==200
   ticket=launch.json()['url'].split('ticket=')[1]
   assert (await client.post('/api/v1/preview/session',json={'ticket':ticket})).status_code==200
   assert (await client.post('/api/v1/preview/session',json={'ticket':ticket})).status_code==401
   for _ in range(5):
    data=await client.get('/api/v1/preview/state');assert data.status_code==200 and data.json()['run_id']=='execution-1'
   assert (await client.get('/api/v1/preview/state',headers={'Origin':'http://evil.example'})).status_code==403
   assert (await client.get('/api/v1/preview/state',headers={'Host':'evil.example'})).status_code==403
   assert (await client.get('/api/v1/preview/state?limit=601')).status_code==422
   assert (await client.get('/api/v1/preview/runs/another-execution/events')).status_code==403
 asyncio.run(run())
 assert len(hub._sessions)==sessions
 after=rt.lease_snapshot()
 for key in ['owner','generation','revoked']:assert after[key]==lease[key]

def test_disk_failure_and_recovery_preserve_uncertain_tail(tmp_path,monkeypatch):
 from factory_hmi.core.recording import segments
 output=tmp_path/'failed.csv';writer=SegmentedWriter(output,['time_s','sample','j_pos_rad'],durability_s=.05,sync_interval=.01)
 real=segments.os.fsync
 def slow(fd):time.sleep(.08);return real(fd)
 monkeypatch.setattr(segments.os,'fsync',slow)
 writer.writerow({'time_s':0,'sample':0,'j_pos_rad':1})
 time.sleep(.07)
 assert 'lag' in writer.health_error()
 with pytest.raises(OSError):writer.close()
 checkpoint=output.with_suffix('.checkpoint.json')
 assert checkpoint.exists()
 partial=output.with_suffix('.csv.partial');original=partial.read_bytes()
 with partial.open('ab') as handle:handle.write(b'UNCERTAIN')
 recovered=segments.recover_checkpoint(output)
 assert recovered['uncertain_tail_bytes']==9 and recovered['mechanical_restart'] is False
 assert Path(recovered['path']).read_bytes()==original
 assert partial.read_bytes().endswith(b'UNCERTAIN')


def test_observation_and_reliability_draft_gate():
 from factory_hmi.core.recording.policy import check_observation_requirements,protocol_qualification
 assert not check_observation_requirements({'minimum_source_hz':200},{'metrics':[]})['admitted']
 assert protocol_qualification({})['state']=='draft'
 assert protocol_qualification({})['statistically_qualified'] is False


def test_capacity_reservations_do_not_overbook_or_delete_evidence(tmp_path):
 from factory_hmi.core.recording.policy import CapacityReservations
 budget=CapacityReservations(tmp_path/'capacity.sqlite3')
 budget.reserve('run-a',80,100)
 with pytest.raises(RuntimeError,match='其他执行'):budget.reserve('run-b',30,100)
 budget.reserve('run-a',80,100)
 budget.complete('run-a');budget.reserve('run-b',30,100)


def test_startup_partial_recovery_never_creates_a_complete_run_or_controller(tmp_path):
 from factory_hmi.core.service import FactoryService
 import os
 root=tmp_path/'station';records=root/'records';records.mkdir(parents=True)
 source=records/'unfinished.csv';partial=source.with_suffix('.csv.partial');data=b'time_s,sample,j_pos_rad\r\n0,0,1\r\n'
 partial.write_bytes(data+b'UNCONFIRMED')
 checkpoint={'segment':0,'offset':len(data),'durable_rows':1,'path':partial.name}
 source.with_suffix('.checkpoint.json').write_text(json.dumps(checkpoint))
 service=FactoryService(data_root=root)
 service.wait_for_reports(5)
 assert service._controller is None
 assert 'unfinished' in service._report_errors
 assert service.report_manager.history()==[]
 assert partial.read_bytes().endswith(b'UNCONFIRMED')
 event=json.loads(source.with_suffix('.recovery.json').read_text())
 assert event['uncertain_tail_bytes']==11 and event['mechanical_restart'] is False
 service.close()
