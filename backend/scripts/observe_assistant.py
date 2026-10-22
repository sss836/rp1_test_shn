"""Continuous synthetic observation. Refuses site databases and live models."""
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from datetime import datetime,timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

import httpx
from argon2 import PasswordHasher
from sqlalchemy import create_engine,text

out=Path('/reports')
url=os.environ['ASSISTANT_QA_ADMIN_URL']
assert url.endswith('/rp1_presence_assistant') and os.environ.get('APP_ENV')=='test'
assert not os.getenv('ASSISTANT_QA_LIVE'), 'observation must use the synthetic model'
seconds=int(os.environ['ASSISTANT_OBSERVE_SECONDS']);assert 60<=seconds<=86400
engine=create_engine(url)
pilot=os.environ['ASSISTANT_QA_PILOT_IDS'].split(',')[0]
password='Synthetic-Soak-Only-2026!'
username='assistant-soak'
interrupted=False
def stop(*_):
    global interrupted
    interrupted=True
signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)


def stamp():return datetime.now(timezone.utc).isoformat()
def record(value):
    temp=out/'observation-status.tmp'
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    temp.replace(out/'observation-status.json')
def app_hash():
    return hashlib.sha256(''.join(str(p)+hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path('/app/app').rglob('*.py'))).encode()).hexdigest()
def data(response,status=200):
    assert response.status_code==status, ('http',response.status_code)
    return response.json()['data']


with engine.begin() as conn:
    user=conn.execute(text("INSERT INTO iam.app_user(public_id,username,display_name,role,principal_kind,must_change_password) VALUES(:id,:name,'隔离观察合成账号','SYSTEM_ADMIN','HUMAN',false) RETURNING id"),{'id':pilot,'name':username}).scalar_one()
    conn.execute(text('INSERT INTO iam.user_credential(user_id,password_hash) VALUES(:id,:password)'),{'id':user,'password':PasswordHasher().hash(password)})

started=stamp();clock=time.monotonic();initial_hash=app_hash();samples=0;thread=None;authenticated=0;installation=str(uuid4());days=set()
status={'status':'running','started_at':started,'duration_seconds':seconds,'model':'synthetic SSE; no official API calls','app_hash':initial_hash,'samples':0,'no_site_database':True,'no_real_device':True}
record(status)
try:
    with httpx.Client(base_url='http://web:8080',trust_env=False,timeout=130) as client:
        while not interrupted and (samples==0 or time.monotonic()-clock<seconds):
            cycle=time.monotonic()
            assert app_hash()==initial_hash, 'candidate source changed during observation'
            if time.monotonic()-authenticated>3600 or not authenticated:
                data(client.post('/api/v1/auth/login',json={'username':username,'password':password}))
                client.headers['X-CSRF-Token']=client.cookies['rp1_csrf'];authenticated=time.monotonic()
            presence={'installation_id':installation,'station_name':'SYNTHETIC-24H','sequence':samples+1,'snapshot_age_seconds':0,'plc':{'driver':'mock','phase':'IDLE','stale':False},'runs':[]}
            data(client.put('/api/v1/hmi/presence/'+installation,json=presence))
            jobs=[]
            for _ in range(3):
                payload={'request_key':str(uuid4()),'message':'查询当前工位状态','context':{'view':'stations','kind':'MODULE'}}
                if thread:payload['thread_id']=thread
                queued=data(client.post('/api/v1/assistant/jobs',json=payload),202);jobs.append(queued['id']);thread=queued['thread_id']
            deadline=time.monotonic()+130
            pending=set(jobs)
            while pending:
                assert time.monotonic()<deadline, 'task failed to reach a terminal state'
                with engine.connect() as conn:
                    assert conn.execute(text("SELECT count(*) FROM integration.assistant_job WHERE state='RUNNING'")).scalar_one()<=2
                for identifier in list(pending):
                    job=data(client.get('/api/v1/assistant/jobs/'+identifier))
                    if job['state'] not in {'QUEUED','RUNNING'}:
                        assert job['state']=='COMPLETED' and 0<job['rounds']<=6, 'query task failed'
                        assert any(card['source']=='/api/v1/hmi/stations' for card in job['result']['cards']), 'missing source'
                        pending.remove(identifier)
                if pending:time.sleep(.1)
            budget=data(client.get('/api/v1/assistant/budget'))
            assert not budget['blocked'] and float(budget['reserved_cny'])==0 and float(budget['spent_cny'])<=100
            local_day=str(datetime.now(ZoneInfo('Asia/Shanghai')).date())
            assert budget['day']==local_day;days.add(local_day)
            assert client.get('/readyz').status_code==200 and client.get('/api/v1/hmi/stations').status_code==200
            with engine.connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM pg_stat_activity WHERE usename='rp1_app' AND state='idle in transaction'")).scalar_one()==0
                calls=conn.execute(text("SELECT count(*) FROM integration.assistant_model_call WHERE state<>'SETTLED'")).scalar_one()
                assert calls==0
            samples+=1
            sample={'at':stamp(),'sample':samples,'elapsed_seconds':round(time.monotonic()-clock,3),'jobs':jobs,'budget':budget,'day':local_day}
            with (out/'observation-samples.jsonl').open('a') as handle:
                handle.write(json.dumps(sample,ensure_ascii=False)+'\n');handle.flush();os.fsync(handle.fileno())
            status.update(last_sample_at=stamp(),samples=samples,elapsed_seconds=time.monotonic()-clock,observed_beijing_days=sorted(days));record(status)
            while not interrupted and time.monotonic()-cycle<60 and time.monotonic()-clock<seconds:time.sleep(1)
    status.update(status='interrupted' if interrupted else 'passed',finished_at=stamp(),elapsed_seconds=time.monotonic()-clock,full_24h=(not interrupted and time.monotonic()-clock>=86400))
    record(status)
    if interrupted:raise SystemExit(2)
except Exception as exc:
    status.update(status='failed',finished_at=stamp(),error_kind=type(exc).__name__,elapsed_seconds=time.monotonic()-clock);record(status)
    raise
finally:engine.dispose()
