"""Recoverable single-process report jobs; never import or operate a controller."""
import fcntl,json,os,sqlite3,subprocess,sys,time,uuid
from pathlib import Path

def run_report_job(root,source,context,*,timeout_s=900):
 root=Path(root);root.mkdir(parents=True,exist_ok=True);dbpath=root/"report-jobs.sqlite3";key=str(context["test_id"])
 with sqlite3.connect(dbpath) as db:
  db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,state TEXT,source TEXT,context TEXT,error TEXT,updated REAL)")
  existing=db.execute("SELECT state,source,context FROM jobs WHERE id=?",(key,)).fetchone()
  payload=json.dumps(context,sort_keys=True,ensure_ascii=False)
  if existing and existing[1:]!=(str(source),payload):raise ValueError("report identity payload conflict")
  if existing and existing[0]=="completed":
   from .report import ReportManager
   ReportManager(root).validate(key)
   return
  if existing and existing[0]=="cancelled":raise RuntimeError("report job cancelled")
  if db.execute("SELECT count(*) FROM jobs WHERE state IN ('pending','running') AND id!=?",(key,)).fetchone()[0]>=8:raise RuntimeError("report queue full")
  db.execute("INSERT OR REPLACE INTO jobs VALUES (?,'pending',?,?,NULL,?)",(key,str(source),payload,time.time()))
 with (root/".report-process.lock").open("a") as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  with sqlite3.connect(dbpath) as db:db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?",(time.time(),key))
  env=os.environ.copy();env.update(PYTHONPATH=os.pathsep.join(p for p in sys.path if p),OPENBLAS_NUM_THREADS="1",OMP_NUM_THREADS="1")
  log=root/("report-"+uuid.uuid4().hex+".log")
  try:
   with log.open("w") as output:
    process=subprocess.Popen([sys.executable,"-m","factory_hmi.core.report_jobs",str(dbpath),key],env=env,stdout=output,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+timeout_s
    while True:
     try:code=process.wait(timeout=.2);break
     except subprocess.TimeoutExpired:
      with sqlite3.connect(dbpath) as db:state=db.execute("SELECT state FROM jobs WHERE id=?",(key,)).fetchone()[0]
      if state=="cancelled" or time.monotonic()>deadline:
       process.terminate()
       try:process.wait(timeout=2)
       except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
       raise RuntimeError("report job cancelled" if state=="cancelled" else "report job timed out; source evidence retained")
   if code:raise RuntimeError(f"report process failed ({code}); details: {log.name}")
   with sqlite3.connect(dbpath) as db:db.execute("UPDATE jobs SET state='completed',error=NULL,updated=? WHERE id=?",(time.time(),key))
  except Exception as exc:
   with sqlite3.connect(dbpath) as db:db.execute("UPDATE jobs SET state=CASE WHEN state='cancelled' THEN state ELSE 'failed' END,error=?,updated=? WHERE id=?",(str(exc),time.time(),key))
   raise
  finally:fcntl.flock(lock,fcntl.LOCK_UN)

def cancel_report_job(root,key):
 path=Path(root)/"report-jobs.sqlite3"
 if not path.exists():raise ValueError("report job not found")
 with sqlite3.connect(path) as db:
  row=db.execute("SELECT state FROM jobs WHERE id=?",(key,)).fetchone()
  if row is None:raise ValueError("report job not found")
  if row[0]=="completed":return {"cancelled":False,"state":"completed"}
  db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE id=?",(time.time(),key))
 return {"cancelled":True,"source_evidence_retained":True}

def recover_report_jobs(root):
 path=Path(root)/"report-jobs.sqlite3"
 if not path.exists():return []
 with sqlite3.connect(path) as db:rows=db.execute("SELECT id,source,context FROM jobs WHERE state IN ('pending','running','failed') ORDER BY updated LIMIT 8").fetchall()
 outcomes=[]
 for key,source,payload in rows:
  try:run_report_job(root,source,json.loads(payload));outcomes.append({"id":key,"state":"completed"})
  except Exception as exc:outcomes.append({"id":key,"state":"failed","reason":str(exc)})
 return outcomes

if __name__=="__main__":
 import resource
 resource.setrlimit(resource.RLIMIT_AS,(1024**3,1024**3));resource.setrlimit(resource.RLIMIT_CPU,(900,900));resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
 dbpath=Path(sys.argv[1]);key=sys.argv[2]
 with sqlite3.connect(dbpath) as db:source,payload=db.execute("SELECT source,context FROM jobs WHERE id=?",(key,)).fetchone()
 from .report import ReportManager
 manager=ReportManager(dbpath.parent)
 # A crash after publication but before the job receipt is repaired by validating the seal.
 with manager.evidence_lock():
  if (manager.executions_root/key/"manifest.json").exists():manager.validate(key);manager.refresh_history_index(key)
  else:manager._finalize(source,json.loads(payload))
