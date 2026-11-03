"""Bounded asynchronous CSV segments with durable checkpoints and no device access."""
from __future__ import annotations
import csv, hashlib, io, json, os, queue, sqlite3, threading, time
from pathlib import Path

class SegmentedWriter:
 def __init__(self, output, fields, *, max_bytes=128*1024**2,max_seconds=900.,queue_bytes=2*1024**2,durability_s=1.,sync_interval=.25):
  if min(max_bytes,max_seconds,queue_bytes,durability_s,sync_interval)<=0 or sync_interval>=durability_s: raise ValueError("invalid recording budgets")
  self.output=Path(output);self.fields=fields;self.max_bytes=max_bytes;self.max_seconds=max_seconds;self.queue_bytes=queue_bytes;self.durability_s=durability_s;self.sync_interval=sync_interval
  self.root=self.output.with_suffix(".segments"); self.root.mkdir(parents=True,exist_ok=True)
  self.checkpoint=self.output.with_suffix(".checkpoint.json");self.manifest=self.output.with_suffix(".segments.json")
  self._q=queue.Queue(maxsize=4096);self._bytes=0;self._lock=threading.Lock();self._pending=[];self._oldest=None;self.error=None;self._closed=False;self._rows=0;self.durable_rows=0;self.max_lag_s=0.;self._stop=threading.Event()
  self._thread=threading.Thread(target=self._run,name="rp1-segment-writer",daemon=True);self._thread.start()
 def writerow(self,row):
  buffer=io.StringIO(newline="");csv.DictWriter(buffer,fieldnames=self.fields).writerow(row);data=buffer.getvalue().encode()
  if self.health_error(): raise OSError(self.health_error())
  now=time.monotonic()
  with self._lock:
   if self._closed: raise OSError("recorder closed")
   if self._bytes+len(data)>self.queue_bytes: raise OSError("evidence queue byte budget exceeded")
   self._bytes+=len(data); self._oldest=self._oldest or now
  try:self._q.put_nowait((data,now,row.get("time_s"),row.get("sample")))
  except queue.Full:
   with self._lock:self._bytes-=len(data)
   raise OSError("evidence queue item budget exceeded")
 def health_error(self):
  with self._lock:
   lag=time.monotonic()-self._oldest if self._oldest else 0.;self.max_lag_s=max(self.max_lag_s,lag)
   if lag>self.durability_s and not self.error:self.error=f"evidence durability lag {lag:.3f}s exceeds {self.durability_s}s; tail uncertain"
   return self.error
 def _sync(self,f,index,path,last_time,last_seq,digest,size,first_time,first_seq,segment_rows):
  f.flush();os.fsync(f.fileno());now=time.monotonic()
  with self._lock:
   if self._oldest and now-self._oldest>self.durability_s:self.error=f"evidence durability lag {now-self._oldest:.3f}s exceeds budget; tail uncertain"
   self.durable_rows=self._rows
   self._oldest=self._pending[0][1] if self._pending else None
  checkpoint=dict(schema_version="segment-checkpoint-v1",segment=index,path=str(path.name),offset=f.tell(),durable_rows=self.durable_rows,last_time_s=last_time,last_sequence=last_seq,uncertain=bool(self.error),confirmed_at_utc_s=time.time())
  temp=self.checkpoint.with_suffix(".tmp")
  with temp.open("w") as out:json.dump(checkpoint,out);out.flush();os.fsync(out.fileno())
  os.replace(temp,self.checkpoint)
 def _run(self):
  db=None;f=None;reducer=None
  try:
   db=sqlite3.connect(self.root/"index.sqlite3");db.execute("CREATE TABLE segments(n INTEGER PRIMARY KEY,payload TEXT)")
   from .evidence import EvidenceReducer
   reducer=EvidenceReducer(self.root,self.fields)
   index=0; previous="";complete=True
   while True:
    if index>=4096:raise OSError("segment count budget exceeded; evidence incomplete")
    target=self.output if index==0 else self.root/f"{index:06d}.csv";path=target.with_suffix(".csv.partial")
    with path.open("xb") as f:
     buffer=io.StringIO(newline="");csv.writer(buffer).writerow(self.fields);header=buffer.getvalue().encode();f.write(header)
     digest=hashlib.sha256(header);size=len(header);start=time.monotonic();last_sync=start;first_time=last_time=first_seq=last_seq=None;segment_rows=0
     while True:
      try:item=self._q.get(timeout=self.sync_interval)
      except queue.Empty:item=None
      if item:
       data,queued,last_time,last_seq=item
       with self._lock:self._pending.append((len(data),queued))
       reducer.add(next(csv.DictReader(io.StringIO(data.decode()),fieldnames=self.fields)))
       f.write(data);digest.update(data);size+=len(data);segment_rows+=1;self._rows+=1
       if first_time is None:first_time=last_time;first_seq=last_seq
      now=time.monotonic();closing=self._stop.is_set() and self._q.empty()
      rotate=size>=self.max_bytes or now-start>=self.max_seconds
      if now-last_sync>=self.sync_interval or closing or rotate:
       self._sync(f,index,path,last_time,last_seq,digest,size,first_time,first_seq,segment_rows)
       with self._lock:
        self._bytes-=sum(n for n,_ in self._pending);self._pending.clear()
        # Queued samples, not yet written, are still unconfirmed.
        with self._q.mutex:self._oldest=self._q.queue[0][1] if self._q.queue else None
       last_sync=now
      if self.error: complete=False;raise OSError(self.error)
      if closing or rotate:break
    os.replace(path,target)
    directory_fd=os.open(target.parent,os.O_RDONLY)
    try:os.fsync(directory_fd)
    finally:os.close(directory_fd)
    payload=dict(sequence=index,path=str(target.relative_to(self.output.parent)),sha256=digest.hexdigest(),previous_sha256=previous,size_bytes=size,sample_count=segment_rows,first_time_s=first_time,last_time_s=last_time,first_sequence=first_seq,last_sequence=last_seq,sealed=True)
    db.execute("INSERT INTO segments VALUES (?,?)",(index,json.dumps(payload)));db.commit();previous=digest.hexdigest();index+=1
    if closing:break
   temp=self.manifest.with_suffix(".tmp")
   with temp.open("w") as out:
    out.write('{"schema_version":"run-segments-v1","complete":true,"segments":[')
    for n,payload in db.execute("SELECT n,payload FROM segments ORDER BY n"):
     if n:out.write(',')
     out.write(payload)
    out.write(']}');out.flush();os.fsync(out.fileno())
   os.replace(temp,self.manifest)
  except Exception as exc:
   with self._lock:self.error=self.error or str(exc)
  finally:
   if reducer is not None:
    try:reducer.close()
    except Exception as exc:self.error=self.error or str(exc)
   if db is not None:db.close()
 def close(self):
  with self._lock:self._closed=True
  self._stop.set();self._thread.join(timeout=5.)
  if self._thread.is_alive():raise OSError("segment writer close timeout; evidence tail uncertain")
  if self.error:raise OSError(self.error)
 def status(self):
  return dict(queued_bytes=self._bytes,durable_rows=self.durable_rows,max_durability_lag_s=self.max_lag_s,error=self.health_error(),manifest=str(self.manifest))

def sources(path):
 path=Path(path);manifest=path.with_suffix(".segments.json")
 if not manifest.exists():return [path]
 if manifest.stat().st_size>8*1024**2:raise ValueError("segment manifest exceeds budget")
 value=json.loads(manifest.read_text())
 if value.get("schema_version")!="run-segments-v1" or not value.get("complete"):raise ValueError("incomplete segment manifest")
 result=[];previous=""
 for n,part in enumerate(value["segments"]):
  p=path.parent/part["path"]
  if p.is_symlink() or not p.resolve().is_relative_to(path.parent.resolve()) or part["sequence"]!=n or part["previous_sha256"]!=previous:raise ValueError("invalid segment identity or chain")
  h=hashlib.sha256()
  with p.open("rb") as handle:
   while chunk:=handle.read(1024**2):h.update(chunk)
  if p.stat().st_size!=part["size_bytes"] or h.hexdigest()!=part["sha256"]:raise ValueError("segment hash mismatch")
  previous=h.hexdigest();result.append(p)
 return result

def copy_logical_csv(path,target):
 fields=None
 with Path(target).open("x",encoding="utf-8",newline="") as out:
  writer=None
  for part in sources(path):
   with part.open(encoding="utf-8-sig",newline="") as handle:
    reader=csv.DictReader(handle)
    if fields is None:fields=reader.fieldnames;writer=csv.DictWriter(out,fieldnames=fields);writer.writeheader()
    if reader.fieldnames!=fields:raise ValueError("segment channel schema changed")
    for row in reader:writer.writerow(row)
  out.flush();os.fsync(out.fileno())

def recover_checkpoint(output):
 """Copy only the fsynced prefix; preserve every original including uncertain tail."""
 import uuid
 output=Path(output);checkpoint=output.with_suffix('.checkpoint.json')
 value=json.loads(checkpoint.read_text());index=int(value['segment'])
 partial=(output if index==0 else output.with_suffix('.segments')/f'{index:06d}.csv').with_suffix('.csv.partial')
 if not partial.exists():partial=partial.with_suffix('')
 if not partial.is_file() or partial.is_symlink():raise ValueError('recoverable segment missing')
 offset=int(value['offset'])
 if not 0<offset<=partial.stat().st_size:raise ValueError('invalid durable offset')
 recovery=output.with_suffix('.recovery')
 if recovery.is_symlink():raise ValueError('invalid recovery directory')
 recovery.mkdir(exist_ok=True)
 target=recovery/('segment-'+str(index)+'-prefix-'+uuid.uuid4().hex+'.csv')
 remaining=offset
 with partial.open('rb') as source,target.open('xb') as dest:
  while remaining:
   chunk=source.read(min(1024**2,remaining))
   if not chunk:raise ValueError('durable prefix truncated')
   dest.write(chunk);remaining-=len(chunk)
  dest.flush();os.fsync(dest.fileno())
 return dict(path=str(target),durable_offset=offset,original_bytes=partial.stat().st_size,uncertain_tail_bytes=partial.stat().st_size-offset,requires_verification=True,mechanical_restart=False,checkpoint=value)
