"""RDfull companion summaries and bounded fault evidence; never deletes raw rows."""
from collections import deque
import csv,json,math,os
from pathlib import Path
class EvidenceReducer:
 def __init__(self,root,fields,*,pre_seconds=2.,post_seconds=2.,ring_bytes=512*1024,window_s=1.):
  self.root=Path(root);self.fields=fields;self.pre=pre_seconds;self.post=post_seconds;self.limit=ring_bytes;self.window_s=window_s
  self.ring=deque();self.bytes=0;self.window=None;self.stats={};self.active={};self.events=[];self.event_file=None;self.writer=None;self.post_until=None;self.last_saved=-1
  self.trend=(self.root/'windows.jsonl').open('x');self.event_index=(self.root/'events.jsonl').open('x')
 def _number(self,v):
  try:n=float(v);return n if math.isfinite(n) else None
  except (ValueError,TypeError):return None
 def add(self,row):
  t=self._number(row.get('time_s'));seq=int(row['sample'])
  if t is None:return
  bucket=int(t/self.window_s)
  if self.window is not None and bucket!=self.window:self.flush_window()
  self.window=bucket
  size=sum(len(str(k))+len(str(v)) for k,v in row.items())
  self.ring.append((t,seq,dict(row),size));self.bytes+=size
  while self.ring and (self.bytes>self.limit or t-self.ring[0][0]>self.pre):self.bytes-=self.ring.popleft()[3]
  fault=False;episodes=[]
  for key,value in row.items():
   if key in {'time_s','sample'}:continue
   n=self._number(value);s=self.stats.setdefault(key,dict(count=0,invalid=0,min=None,max=None,sum=0.,sum_sq=0.,min_time_s=None,max_time_s=None))
   if n is None:s['invalid']+=1;continue
   s['count']+=1;s['sum']+=n;s['sum_sq']+=n*n
   if s['min'] is None or n<s['min']:s['min']=n;s['min_time_s']=t
   if s['max'] is None or n>s['max']:s['max']=n;s['max_time_s']=t
   if key.endswith('_error_id'):
    current=int(n)!=0;fault|=current
    if current and not self.active.get(key):episodes.append(dict(channel=key,code=int(n),started_s=t,sample=seq))
    if not current and self.active.get(key):self.event_index.write(json.dumps(dict(type='fault_recovered',channel=key,time_s=t,sample=seq))+'\n')
    self.active[key]=current
  for event in episodes:self.event_index.write(json.dumps(dict(type='fault_started',**event))+'\n')
  if fault:
   self.post_until=t+self.post
   if self.event_file is None:
    self.event_file=(self.root/'fault-evidence.csv').open('x',newline='');self.writer=csv.DictWriter(self.event_file,fieldnames=self.fields);self.writer.writeheader()
   for before,number,raw,_ in self.ring:
    if number>self.last_saved:self.writer.writerow(raw);self.last_saved=number
   if episodes:self.event_index.write(json.dumps(dict(type='evidence_window',trigger_s=t,pre_complete=bool(self.ring and t-self.ring[0][0]>=self.pre-.1),pre_available_s=t-self.ring[0][0],ring_bytes=self.bytes))+'\n')
  elif self.post_until is not None and t<=self.post_until and seq>self.last_saved:self.writer.writerow(row);self.last_saved=seq
 def flush_window(self):
  if self.window is None:return
  metrics={}
  for key,s in self.stats.items():
   metrics[key]={k:v for k,v in s.items() if k not in {'sum','sum_sq'}}
   metrics[key]['mean']=s['sum']/s['count'] if s['count'] else None
   if key.endswith(('_spd_rad_s','_torque_nm')):metrics[key]['rms']=math.sqrt(s['sum_sq']/s['count']) if s['count'] else None
  self.trend.write(json.dumps(dict(schema_version='window-stat-v1',method='point-mean-exact-v1',start_s=self.window*self.window_s,end_s=(self.window+1)*self.window_s,metrics=metrics),allow_nan=False)+'\n');self.stats={}
 def close(self):
  self.flush_window()
  for f in [self.trend,self.event_index,self.event_file]:
   if f is not None:f.flush();os.fsync(f.fileno());f.close()
