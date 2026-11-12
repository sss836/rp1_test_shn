"""Bounded scans over actual sealed evidence, never a device/snapshot query."""
import csv,json,math,time,sqlite3,threading
from functools import lru_cache
from pathlib import Path
from fastapi import HTTPException
from factory_hmi.core.telemetry_hub import METRICS

CSV_METRIC_COLUMNS = {
 'position_rad':'pos_rad','command_position_rad':'cmd_pos_rad','speed_rad_s':'spd_rad_s',
 'estimated_torque_nm':'estimated_torque_nm','torque_kt_nm_a':'torque_kt_nm_a',
 'torque_nm':'torque_nm','temperature_c':'temp_c','bus_voltage_v':'bus_voltage_v',
 'bus_current_a':'bus_current_a','iq_current_a':'iq_current_a',
 'error_id':'error_id','feedback_age_s':'feedback_age_s',
}

def sealed_raw_curves(service,run_id,metrics,start,end,view):
 manager=getattr(service,'report_manager',None)
 if manager is None:raise HTTPException(404,'尚无本地记录')
 with manager.history_index.connect() as db:
  row=db.execute("SELECT payload FROM runs WHERE json_extract(payload,'$.execution_uuid')=? LIMIT 1",(run_id,)).fetchone()
 if row is None:raise HTTPException(409,'执行尚未封存；实时画面之外的证据需等待封存')
 item=json.loads(row[0]);root=manager.execution_dir(item['test_id']);selected=[m for m in metrics.split(',') if m]
 if len(selected)>12:raise HTTPException(422,'最多12条曲线')
 try:start=float(start or 0);end=float(end or start+60)
 except ValueError:raise HTTPException(422,'时间范围须为执行内秒')
 if not math.isfinite(start+end) or not 0<=start<end:raise HTTPException(422,'请输入有效的执行内时间范围')
 columns={};unit={}
 for key in selected:
  try:name,metric=key.rsplit('/',1);columns[key]=name+'_'+CSV_METRIC_COLUMNS[metric];unit[key]=METRICS[metric][0]
  except (ValueError,KeyError):raise HTTPException(422,'未知测量通道')
 bins={k:{} for k in selected};points=600;width=(end-start)/points;deadline=time.monotonic()+2.;read_bytes=0;previous=None;gaps=[];count=0
 manifest_path=root/'telemetry/segments/manifest.json'
 if manifest_path.is_file():
  manifest=json.loads(manifest_path.read_text());paths=[manifest_path.parent/f"{p['sequence']:06d}.csv" for p in manifest['segments'] if p.get('last_time_s') is not None and p['last_time_s']>=start and (p.get('first_time_s') or 0)<=end]
 else:paths=[root/'telemetry/raw.csv']
 for path in paths:
  read_bytes+=path.stat().st_size
  if read_bytes>256*1024**2:raise HTTPException(422,'查询超过扫描预算，请缩小时间范围')
  with path.open(encoding='utf-8-sig',newline='') as handle:
   for row in csv.DictReader(handle):
    count+=1
    if count%1024==0 and time.monotonic()>deadline:raise HTTPException(422,'查询超过2秒预算，请缩小范围')
    try:t=float(row['time_s'])
    except (ValueError,KeyError):continue
    if not math.isfinite(t) or not start<=t<=end:continue
    if previous is not None and (t<=previous or t-previous>.2) and len(gaps)<600:gaps.append([previous,t])
    previous=t;bucket=min(points-1,int((t-start)/width))
    for key,col in columns.items():
     try:v=float(row[col]);v=v if math.isfinite(v) else None
     except (KeyError,ValueError):v=None
     if v is None:continue
     s=bins[key].setdefault(bucket,[0.,0,None,None]);s[0]+=v;s[1]+=1;s[2]=v if s[2] is None else min(s[2],v);s[3]=v if s[3] is None else max(s[3],v)
 series=[]
 for key,values in bins.items():
  series.append(dict(id=key,unit=unit[key],points=[[(start+(i+.5)*width)*1000,(values[i][0]/values[i][1] if i in values else None)] for i in range(points)],extrema=[[(start+(i+.5)*width)*1000,values[i][2],values[i][3]] for i in sorted(values)]))
 return dict(series=series,resolution_s=width,method='point-mean-v1',gaps=gaps,source='sealed_local_evidence',view=view,start_s=start,end_s=end,scanned_bytes=read_bytes)


@lru_cache(maxsize=16)
def _query_lock(path):
 return threading.Lock()


def _source(service,run_id):
 """Read file/identity metadata only. Never call snapshot or device adapters."""
 manager=getattr(service,'report_manager',None)
 if manager is None:raise HTTPException(404,'本次没有本地记录；只能查看已有实时缓存')
 with manager.history_index.connect() as db:
  row=db.execute("SELECT payload FROM runs WHERE json_extract(payload,'$.execution_uuid')=? LIMIT 1",(run_id,)).fetchone()
 if row:
  item=json.loads(row[0]);root=manager.execution_dir(item['test_id']);raw=root/'telemetry/raw.csv'
  with raw.open('rb') as f:
   f.seek(max(0,raw.stat().st_size-65536));tail=f.read(65536).splitlines()
  duration=0.
  for line in reversed(tail):
   try:duration=float(line.split(b',',1)[0]);break
   except ValueError:continue
  return dict(windows=root/'telemetry/segments/windows.jsonl',duration=duration,sealed=True)
 read_identity=getattr(service,'preview_record_identity',None)
 if callable(read_identity):
  identity=read_identity(run_id) or {}
 else:
  identity=dict(getattr(service,'_record_identity',{}) or {})
 if identity.get('execution_uuid')!=run_id:raise HTTPException(409,'本次没有可读取的测量记录；只能查看已有实时缓存')
 record_root=getattr(service,'record_root',None)
 if record_root is None and callable(read_identity):
  record_root=Path(service.data_root)/'records'
 if record_root is None:raise HTTPException(409,'本次记录缺少可用的工位目录')
 test_id=str(identity.get('test_id') or '')
 if not test_id or test_id in {'.','..'} or Path(test_id).name!=test_id:raise HTTPException(422,'无效记录身份')
 output=Path(record_root)/(test_id+'.csv');checkpoint=output.with_suffix('.checkpoint.json')
 if not checkpoint.is_file():raise HTTPException(409,'记录正在准备，尚无已持久化时段')
 if checkpoint.stat().st_size>8192:raise HTTPException(422,'记录检查点超出预算')
 value=json.loads(checkpoint.read_text());duration=float(value.get('last_time_s') or 0.)
 if not math.isfinite(duration) or duration<0:raise HTTPException(422,'记录检查点时间无效')
 return dict(windows=output.with_suffix('.segments')/'windows.jsonl',duration=duration,sealed=False)


def curves(service,run_id,metrics,start,end,view,window=''):
 selected=[key for key in metrics.split(',') if key]
 if not 1<=len(selected)<=12 or len(set(selected))!=len(selected):raise HTTPException(422,'请选择1至12条不同曲线')
 columns={};units={}
 for key in selected:
  try:name,metric=key.rsplit('/',1);columns[key]=name+'_'+CSV_METRIC_COLUMNS[metric];units[key]=METRICS[metric][0]
  except (ValueError,KeyError):raise HTTPException(422,'未知测量通道')
 source=_source(service,run_id);duration=source['duration']
 try:
  if window=='all':begin,finish=0.,max(duration,.001)
  elif window:
   span=float(window)
   if not math.isfinite(span) or span<=0:raise ValueError()
   finish=max(duration,.001);begin=max(0.,finish-span)
  else:begin=float(start or 0);finish=float(end or max(duration,60))
  if not math.isfinite(begin+finish) or not 0<=begin<finish<=10**12:raise ValueError()
 except ValueError:raise HTTPException(422,'开始须早于结束；请输入有效的执行内时间范围')
 if source['sealed'] and finish-begin<=600:
  result=sealed_raw_curves(service,run_id,metrics,str(begin),str(finish),view)
  return {**result,'duration_s':duration,'saved_until_s':duration,'indexing':False}
 if not source['windows'].is_file():
  if source['sealed']:
   result=sealed_raw_curves(service,run_id,metrics,str(begin),str(finish),view)
   return {**result,'duration_s':duration,'indexing':False}
  raise HTTPException(409,'尚无已保存的时间窗口，请稍后重试；测试和记录继续')
 from factory_hmi.core.curve_cache import CurveCache
 base=getattr(service,'data_root',None) or getattr(service.report_manager,'data_root',None)
 if base is None:raise HTTPException(409,'本次记录缺少可用的本地查询缓存目录')
 path=Path(base)/'preview-curve-index.sqlite3';lock=_query_lock(str(path.resolve()))
 if not lock.acquire(blocking=False):raise HTTPException(429,'另一条曲线查询正在处理，请稍后重试')
 cache=None
 try:
  cache=CurveCache(path)
  progress=cache.ingest(source['windows'],run_id,duration,source['sealed'])
  result=dict(series=[],resolution_s=1.,method='saved-window-weighted-mean-v1') if progress['indexing'] else cache.query(columns,units,begin,finish)
  return {**result,**progress,'duration_s':duration,'saved_until_s':min(duration,cache.meta().get('last_s',0.)),'source':'sealed_saved_windows' if source['sealed'] else 'durable_recording_windows','view':view,'start_s':begin,'end_s':finish,'gaps':[]}
 except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:raise HTTPException(422,'已保存曲线索引无法读取：'+str(exc))
 except (OSError,sqlite3.Error) as exc:raise HTTPException(503,'曲线查询缓存暂不可用；不会中断记录：'+str(exc))
 finally:
  if cache is not None:cache.close()
  lock.release()


def events(service,run_id,offset=0):
 manager=getattr(service,'report_manager',None)
 if manager is None:raise HTTPException(404,'尚无本地记录')
 with manager.history_index.connect() as db:
  row=db.execute("SELECT payload FROM runs WHERE json_extract(payload,'$.execution_uuid')=? LIMIT 1",(run_id,)).fetchone()
 if row is None:raise HTTPException(409,'执行尚未封存')
 root=manager.execution_dir(json.loads(row[0])['test_id']);path=root/'telemetry/segments/events.jsonl'
 if not path.exists():return {'items':[],'next_offset':None,'scope':'此记录没有已保存的事件索引'}
 if offset<0 or offset>path.stat().st_size:raise HTTPException(422,'事件游标越界')
 items=[]
 with path.open('rb') as source:
  source.seek(offset)
  for _ in range(100):
   line=source.readline(4097)
   if not line:break
   if len(line)>4096:raise HTTPException(422,'事件记录超出预算')
   items.append(json.loads(line))
  next_offset=source.tell() if source.tell()<path.stat().st_size else None
 return {'items':items,'next_offset':next_offset,'scope':'已封存事件；没有索引不证明没有故障'}
