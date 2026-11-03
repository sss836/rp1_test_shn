"""Explicitly approved presentation datasets; only whitelisted projection leaves server."""
from datetime import datetime,timezone
from uuid import UUID,uuid5,NAMESPACE_URL
import json,secrets,threading,time
from app.core.errors import ApiError
from app.core.config import get_settings
from app.repositories.archives import ArchiveRepository

_sessions={};_lock=threading.Lock()
def topics():
 try:values=json.loads(get_settings().showcase_topics_json)
 except ValueError:raise ApiError(503,'showcase_configuration','展示主题配置无效')
 if not isinstance(values,list) or len(values)>64:raise ApiError(503,'showcase_configuration','展示主题超出预算')
 return [v for v in values if v.get('approved') is True and v.get('domain') in {'RESEARCH','FACTORY'} and isinstance(v.get('archive_ids'),list) and len(v['archive_ids'])<=100]

def project(session,topic):
 repo=ArchiveRepository(session);items=[]
 for reference in topic['archive_ids']:
  archive=repo.get(str(UUID(reference)))
  if archive.get('data_domain')!=topic['domain'] or archive.get('submission_status')!='APPROVED' or archive.get('processing_state')!='READY' or archive.get('upload_state')!='COMPLETE' or (archive.get('manifest') or {}).get('original_sha256')!=archive.get('sha256'):continue
  # Formal eligibility is rechecked by the canonical, immutable summary function.
  from sqlalchemy import text
  if not session.execute(text('SELECT integration.archive_has_formal_context(id) FROM integration.execution_archive WHERE public_id=:id'),{'id':archive['id']}).scalar_one():continue
  items.append(archive)
 now=datetime.now(timezone.utc).isoformat();started=[(a.get('facts') or {}).get('started_at') for a in items];ended=[(a.get('facts') or {}).get('ended_at') for a in items];cutoff=max((v for v in ended if v),default=None)
 seconds=sum(float((a.get('facts') or {}).get('active_seconds') or 0) for a in items)
 def verdict(a):return (a.get('current_verification') or {}).get('verdict')
 passed=sum(verdict(a)=='passed' for a in items);failed=sum(verdict(a)=='failed' for a in items);denominator=passed+failed
 selected_curves=[]
 for selected in (topic.get('selected_curves') or [])[:2]:
  archive=next((a for a in items if str(a['id'])==str(selected.get('archive_id'))),None)
  if archive is None:continue
  metric=selected.get('metric')
  if metric not in {'pos_rad','vel_rad_s','torque_nm','temperature_c','current_a'}:continue
  from app.services.curve_query import CurveQueryService
  from app.services.archive_storage import ArchiveStorage
  result=CurveQueryService(ArchiveStorage(get_settings().archive_root),get_settings().archive_query_memory_mb).query(archive,[metric],[str(selected.get('subject') or '')],selected.get('start'),selected.get('end'),200)
  for series in result['series'][:1]:
   selected_curves.append(dict(id=str(selected.get('alias') or '精选历史曲线')[:80],unit=series['unit'],points=[[p[0]*1000,p[1]] for p in series['points']]))
 alias=str(topic.get('alias') or '已批准主题')[:80];part=topic.get('part','SARM')
 if part not in {'ARM','SARM','LEG','SLEG','SYS','UPPER','LOWER','WAISTHIP','CHEST','HEAD','BAT'}:part='SARM'
 case=dict(id=str(uuid5(NAMESPACE_URL,'showcase:'+str(topic['id']))),code=str(topic.get('case_alias') or '已确认检验')[:80],name=alias,target_part_code=part,target_part_name=str(topic.get('part_alias') or '获准部位')[:80],status='COMPLETED',current_elapsed_seconds=0,cumulative_duration_seconds=seconds,effective_exposure_seconds=0,excluded_seconds=0,pending_seconds=0,execution_count=len(items),scope_asset_count=0,historical_execution_count=len(items),platform_execution_count=0,passed_execution_count=passed,failed_execution_count=failed,inconclusive_execution_count=len(items)-denominator,not_evaluated_execution_count=0,exception_count=0,first_recorded_at=min((v for v in started if v),default=None),latest_recorded_at=cutoff,station_codes=[],target_duration_seconds=None,target_progress_percent=None)
 scope=dict(id='MODULE:'+part,code=part,name=alias,asset_kind='MODULE',target_part_code=part,target_part_name=case['target_part_name'],status='HISTORICAL',part_durations=dict(total_duration_seconds=seconds,effective_exposure_seconds=0,excluded_seconds=0,pending_seconds=0,active_elapsed_seconds=0),part_asset_count=0,active_execution_count=0,abnormal_asset_count=0,test_cases=[case])
 projection=dict(freshness=dict(as_of_at=now,data_cutoff_at=cutoff,last_received_at=cutoff,lag_seconds=None,status='STALE' if cutoff else 'NO_DATA'),data_sources=dict(as_of_at=now,data_cutoff_at=cutoff,sources=[],healthy_count=0,warning_count=0),anomaly_count=0,anomalies=[],groups=[dict(asset_kind='MODULE',objects=[scope])])
 return dict(schema_version='showcase-v1',domain=topic['domain'],alias=alias,source='已核实并批准的历史归档',quality='HISTORICAL',generated_at=now,data_cutoff_at=cutoff,projection=projection,completed_count=denominator if topic["domain"]=="FACTORY" else len(items),passed_count=passed,denominator=denominator,pass_rate=passed/denominator if denominator else None,exposure_note='累计时长不等于可靠性有效暴露；本展示不计算MTBF。',pending_note='进行中和待判结果未计入已确认成果。',selected_curves=selected_curves,export_enabled=False,media_allowed=topic.get('media_rights_confirmed') is True,media_rights_reference=str(topic.get('media_rights_reference') or '')[:200])

def issue(snapshot,original_token,topic_id):
 with _lock:
  now=time.monotonic()
  for key in list(_sessions):
   if _sessions[key]['expires']<now:_sessions.pop(key)
  if len(_sessions)>=128:raise ApiError(429,'showcase_sessions','展示会话已达上限')
  token=secrets.token_urlsafe(32);_sessions[token]=dict(expires=now+3600,snapshot=snapshot,original_token=original_token,topic_id=topic_id)
 return token

def read(token):
 with _lock:grant=_sessions.get(token or '')
 if not grant or grant['expires']<time.monotonic():raise ApiError(401,'showcase_expired','展示会话已过期，请由讲解员重新登录')
 return grant
