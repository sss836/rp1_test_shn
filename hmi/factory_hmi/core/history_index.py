"""Persistent local read model; acquisition and sealed facts remain unchanged."""
import json,sqlite3
class HistoryIndex:
 VIEW_VERSION='local-business-v2'
 def __init__(self,path):
  self.path=path
  with self.connect() as db:
   db.execute("CREATE TABLE IF NOT EXISTS runs(test_id TEXT PRIMARY KEY,started TEXT,sn TEXT,bench TEXT,domain TEXT,payload TEXT NOT NULL)")
   db.execute("CREATE INDEX IF NOT EXISTS run_order ON runs(started DESC,test_id DESC)")
   db.execute("CREATE TABLE IF NOT EXISTS metadata(k TEXT PRIMARY KEY,v TEXT)")
 def connect(self):
  db=sqlite3.connect(self.path,timeout=5);db.execute("PRAGMA busy_timeout=5000");db.execute("PRAGMA cache_size=-2048");return db
 def put(self,item):
  from factory_hmi.execution_history import instant
  when=instant(item.get("started_at"));started=when.isoformat() if when is not None else ""
  with self.connect() as db:db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",(item["test_id"],started,item.get("sample_id",""),item.get("bench_id",""),item.get("data_domain","UNCLASSIFIED"),json.dumps(item,ensure_ascii=False)))
 def initialized(self):
  with self.connect() as db:
   values=dict(db.execute("SELECT k,v FROM metadata WHERE k IN ('initialized','view_version')"))
  return values.get('initialized')=='1' and values.get('view_version')==self.VIEW_VERSION
 def finish_rebuild(self):
  with self.connect() as db:
   db.execute("INSERT OR REPLACE INTO metadata VALUES ('initialized','1')")
   db.execute("INSERT OR REPLACE INTO metadata VALUES ('view_version',?)",(self.VIEW_VERSION,))
 def page(self,*,limit=100,offset=0,start_at="",end_at="",sn="",data_domain="",bench="",cursor=None):
  if limit is None or not 1<=limit<=500 or offset<0:raise ValueError("history page limit must be 1..500")
  from factory_hmi.execution_history import history_bounds
  begin,finish=history_bounds(start_at,end_at)
  start_at=begin.isoformat() if begin else "";end_at=finish.isoformat() if finish else ""
  clauses=[];args=[]
  for sql,value in [("started>=?",start_at),("started<?",end_at),("instr(lower(sn),lower(?))>0",sn.strip()),("domain=?",data_domain),("instr(lower(bench),lower(?))>0",bench.strip())]:
   if value:clauses.append(sql);args.append(value)
  where=" WHERE "+" AND ".join(clauses) if clauses else ""
  with self.connect() as db:
   total=db.execute("SELECT count(*) FROM runs"+where,args).fetchone()[0]
   if cursor:
    import base64
    try:t,k=json.loads(base64.urlsafe_b64decode(cursor));assert isinstance(t,str) and isinstance(k,str)
    except Exception:raise ValueError("invalid history cursor")
    clauses.append("(started,test_id)<(?,?)");args.extend([t,k]);where=" WHERE "+" AND ".join(clauses);offset=0
   rows=db.execute("SELECT started,test_id,payload FROM runs"+where+" ORDER BY started DESC,test_id DESC LIMIT ? OFFSET ?",(*args,limit+1,offset)).fetchall()
  more=len(rows)>limit;rows=rows[:limit];next_cursor=None
  if more and rows:
   import base64
   next_cursor=base64.urlsafe_b64encode(json.dumps(rows[-1][:2]).encode()).decode()
  return dict(items=[json.loads(row[2]) for row in rows],total=total,offset=offset,limit=limit,next_cursor=next_cursor)
