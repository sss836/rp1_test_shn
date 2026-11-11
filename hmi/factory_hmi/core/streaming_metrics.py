"""Exact legacy report statistics using a bounded SQLite disk spool."""
import csv, math, sqlite3, tempfile
from collections.abc import Mapping
from pathlib import Path

def analyze(path, context):
 from .report import _finite, _joint_names, _entry_map
 active=max(0.,float(context.get("active_seconds") or 0)); expected=math.floor(active*max(0.,float(context.get("record_rate_hz") or 0)))+1
 entries=_entry_map(context); ratings=(context.get("acceptance") or {}).get("rated_torque_nm") or {}
 factory_quality=context.get("data_domain")=="FACTORY" and isinstance(context.get("factory_quality_policy"), Mapping)
 with tempfile.TemporaryDirectory(prefix="rp1-analysis-") as folder:
  db=sqlite3.connect(str(Path(folder)/"spool.sqlite3"))
  try:
   db.execute("PRAGMA cache_size=-8192"); db.execute("PRAGMA temp_store=FILE"); db.execute("PRAGMA journal_mode=OFF")
   db.execute("CREATE TABLE times(t REAL)"); db.execute("CREATE TABLE metrics(j INTEGER,t REAL,temp REAL,torque REAL,tracking REAL)")
   count=0; previous=None; nonmonotonic=False; first=None; last=None
   with path.open(encoding="utf-8-sig",newline="") as handle:
    reader=csv.DictReader(handle); names=_joint_names(reader.fieldnames or [])
    if not names: raise ValueError("telemetry CSV contains no joint position columns")
    faults=[0]*len(names); fault_active=[False]*len(names); factory_valid=[0]*len(names)
    for row in reader:
     t=_finite(row.get("time_s"))
     if t is None:
      nonmonotonic |= factory_quality
      continue
     nonmonotonic |= previous is not None and t<=previous; previous=t; count+=1
     first=t if first is None else min(first,t); last=t if last is None else max(last,t)
     db.execute("INSERT INTO times VALUES (?)",(t,))
     for j,name in enumerate(names):
      temp=_finite(row.get(name+"_temp_c")); torque=_finite(row.get(name+"_torque_nm")); cmd=_finite(row.get(name+"_cmd_pos_rad")); pos=_finite(row.get(name+"_pos_rad"))
      err=_finite(row.get(name+"_error_id")); fault=err is not None and int(err)!=0
      faults[j]+=int(fault and not fault_active[j]); fault_active[j]=fault
      tracking=_finite(cmd-pos) if cmd is not None and pos is not None else None
      if factory_quality and all(v is not None for v in (temp,torque,tracking,err)) and err.is_integer():
       factory_valid[j]+=1
      db.execute("INSERT INTO metrics VALUES (?,?,?,?,?)",(j,t,temp,abs(torque) if torque is not None else None,tracking))
     if count%4096==0: db.commit()
   db.commit(); db.execute("CREATE INDEX mt ON metrics(j,t)"); db.execute("CREATE INDEX torque_order ON metrics(j,torque)")
   gap=0.; prev=None
   for (t,) in db.execute("SELECT t FROM times ORDER BY t"):
    if prev is not None: gap=max(gap,t-prev)
    prev=t
   def quantile(j,column,q,where="",args=()):
    clause=f"j=? AND {column} IS NOT NULL"+where; params=(j,*args)
    n=db.execute(f"SELECT count(*) FROM metrics WHERE {clause}",params).fetchone()[0]
    if not n: return None
    v=(n-1)*q; lo=math.floor(v); hi=math.ceil(v)
    a=db.execute(f"SELECT {column} FROM metrics WHERE {clause} ORDER BY {column} LIMIT 1 OFFSET ?",(*params,lo)).fetchone()[0]
    if lo==hi: return a
    b=db.execute(f"SELECT {column} FROM metrics WHERE {clause} ORDER BY {column} LIMIT 1 OFFSET ?",(*params,hi)).fetchone()[0]
    return a*(1-(v-lo))+b*(v-lo)
   joints=[]; window=min(60.,active*.1); first=first or 0.; last=last if last is not None else first
   for j,name in enumerate(names):
    start=quantile(j,"temp",.5," AND t<=?",(first+window,)); end=quantile(j,"temp",.5," AND t>=?",(last-window,))
    peak,torque_peak,mean,track_sq,track_max=db.execute("SELECT max(temp),max(torque),avg(torque),avg(tracking*tracking),max(abs(tracking)) FROM metrics WHERE j=?",(j,)).fetchone()
    rated=_finite(ratings.get(name)); p999=quantile(j,"torque",.999); entry=entries.get(name,{})
    joints.append(dict(module_name=str(entry.get("module_name") or context.get("module_name") or context.get("limb") or "unknown"),joint_name=name,motor_id=int(entry.get("motor_id",j)),sample_count=count,sample_completeness=min(1.,count/expected if expected else 0.),max_gap_seconds=gap,temperature_start_c=start,temperature_end_c=end,temperature_peak_c=peak,temperature_rise_c=end-start if end is not None and start is not None else None,temperature_peak_rise_c=peak-start if peak is not None and start is not None else None,torque_peak_abs_nm=torque_peak,torque_p999_abs_nm=p999,torque_mean_abs_nm=mean,rated_torque_nm=rated,torque_utilization=p999/rated if p999 is not None and rated is not None and rated>0 else None,tracking_rms_rad=math.sqrt(track_sq) if track_sq is not None else None,tracking_max_rad=track_max,fault_count=faults[j]))
    if factory_quality:
     joints[-1].update(tracking_p99_abs_rad=quantile(j,"abs(tracking)",.99),
                       factory_metric_sample_count=factory_valid[j])
   return count,nonmonotonic,gap,expected,joints
  finally: db.close()
