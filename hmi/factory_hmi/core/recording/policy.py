"""Independent purpose, retention policy, and statistical qualification contracts."""
import hashlib,json,math

def freeze_policy(*,purpose="development",policy="RDfull",recipe=None,approval=None,protocol=None):
 if purpose not in {"development","production","reliability","diagnostic"}:raise ValueError("unknown purpose")
 if policy not in {"RDfull","agingcompact","diagnostic","reliability"}:raise ValueError("unknown recording policy")
 if policy=="agingcompact" and not (approval and approval.get("recipe")==recipe and approval.get("comparison_passed") is True and approval.get("approved") is True):raise ValueError("紧凑策略尚无该配方的全量对照批准，必须保留RDfull")
 if policy=="reliability" and not all((protocol or {}).get(k) for k in ("object","failure_definition","exposure_rule","quality_rule","confidence","method_version")):raise ValueError("可靠性协议仍为草稿，缺少必要定义")
 value=dict(schema_version="record-policy-v1",purpose=purpose,policy=policy,recipe=recipe,qualification="not_automatic",protocol=protocol,approval=approval)
 value["sha256"]=hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest();return value

def capacity_budget(*,row_bytes,rate_hz,duration_s,free_bytes,reserve_bytes,backlog_bytes=0,log_bytes=0):
 if row_bytes<=0 or not 1<=rate_hz<=20 or duration_s<=0:raise ValueError("invalid capacity inputs")
 raw=math.ceil(row_bytes*rate_hz*duration_s)
 # Original + sealed compatibility CSV + bundle worst case + exact SQL spool + temporary retry copy.
 components=dict(raw=raw,sealed_csv=raw,bundle=math.ceil(raw*1.01)+1024**2,analysis_spool=raw*8,retry_temporary=raw,backlog=backlog_bytes,logs=log_bytes,reserve=reserve_bytes)
 required=sum(components.values());return dict(schema_version="capacity-v1",row_bytes=row_bytes,rate_hz=rate_hz,duration_s=duration_s,components=components,required_bytes=required,free_bytes=free_bytes,admitted=free_bytes>=required,raw_only_sustainable_s=max(0,(free_bytes-reserve_bytes-backlog_bytes-log_bytes)/(row_bytes*rate_hz)),estimate="conservative numeric row width; no deletion or early upload assumed")

OBSERVATION_METHODS={'communication_stale','record_gap','temperature','tracking','load','drive_events'}
def check_observation_requirements(requirements,capabilities):
 reasons=[]
 if set(requirements.get('failure_modes',[]))-OBSERVATION_METHODS:reasons.append('配方要求的失效模式没有观测方法')
 if requirements.get('minimum_source_hz') and (capabilities.get('source_rate_hz') is None or capabilities['source_rate_hz']<requirements['minimum_source_hz']):reasons.append('未证明配方要求的设备源更新频率')
 available={m['id'] for m in capabilities.get('metrics',[]) if m.get('available')}
 for metric in requirements.get('metrics',[]):
  if metric not in available:reasons.append('测量通道不可用：'+metric)
 return {'admitted':not reasons,'reasons':reasons,'scope':'software observations; no hardware-rate guarantee'}

def protocol_qualification(protocol):
 missing=[key for key in ('object','failure_definition','exposure_rule','quality_rule','confidence','method_version') if not (protocol or {}).get(key)]
 return {'state':'draft' if missing else 'configured_pending_evidence_review','missing':missing,'statistically_qualified':False,'note':'配置不自动构成正式资格；系统暴露不按关节数相乘，零失效不表示无限MTBF'}


class CapacityReservations:
 """Reserve future growth across station sessions sharing the same disk root."""
 def __init__(self,path):
  import sqlite3
  self.path=path
  with sqlite3.connect(path) as db:db.execute("CREATE TABLE IF NOT EXISTS reservations(run TEXT PRIMARY KEY,bytes INTEGER NOT NULL,active INTEGER NOT NULL)")
 def reserve(self,run,required,free):
  import sqlite3
  with sqlite3.connect(self.path,timeout=5) as db:
   db.execute('BEGIN IMMEDIATE')
   existing=db.execute('SELECT bytes,active FROM reservations WHERE run=?',(run,)).fetchone()
   if existing and existing[1]:
    if existing[0]!=required:raise ValueError('capacity reservation identity conflict')
    return
   reserved=db.execute('SELECT coalesce(sum(bytes),0) FROM reservations WHERE active=1').fetchone()[0]
   if required+reserved>free:raise RuntimeError('磁盘容量已被其他执行预留，无法满足本次完整记录预算')
   db.execute('INSERT OR REPLACE INTO reservations VALUES (?,?,1)',(run,required))
 def complete(self,run):
  import sqlite3
  with sqlite3.connect(self.path) as db:db.execute('UPDATE reservations SET active=0 WHERE run=?',(run,))
