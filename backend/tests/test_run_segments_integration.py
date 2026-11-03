import hashlib,os
from uuid import uuid4
import pytest
from test_archives import archive_payload
from test_archives_integration import RAW,create,headers
from test_hmi_presence_integration import environment,signed_in
from app.services.archive_worker import run_once
pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='disposable PostgreSQL required')]

def test_segments_resume_out_of_order_conflicts_and_complete_one_run(environment):
 _,users=environment
 header,*rows=RAW.splitlines(keepends=True);parts=[header+b''.join(rows[:1]),header+b''.join(rows[1:])]
 with signed_in(users['first']) as client,signed_in(users['second']) as other:
  payload=archive_payload(raw=RAW);item=create(client,payload);reference=item['id'];manifest={'schema_version':'run-segments-v1','execution_uuid':str(payload['execution_uuid']),'logical_size_bytes':len(RAW),'logical_sha256':hashlib.sha256(RAW).hexdigest(),'segments':[]};previous=''
  for i,data in enumerate(parts):
   digest=hashlib.sha256(data).hexdigest();manifest['segments'].append({'sequence':i,'sha256':digest,'previous_sha256':previous,'size_bytes':len(data),'sample_count':len(data.splitlines())-1});previous=digest
  url=f'/api/v1/archives/{reference}'
  registered=client.post(url+'/segments',json=manifest,headers=headers());assert registered.status_code==200,registered.text
  assert other.post(url+'/segments',json=manifest,headers=headers()).status_code==403
  assert client.post(url+'/segments-complete',headers=headers()).status_code==409
  assert client.put(url+'/content?offset=0',content=RAW,headers={**headers(),'Content-Type':'application/octet-stream'}).status_code==409
  changed={**manifest,'logical_sha256':'0'*64};assert client.post(url+'/segments',json=changed,headers=headers()).status_code in {409,422}
  for n in [1,0]:
   data=parts[n];mid=len(data)//2
   def put(offset,chunk):return client.put(url+f'/segments/{n}?offset={offset}',content=chunk,headers={**headers(),'Content-Type':'application/octet-stream'})
   first=put(0,data[:mid]);assert first.status_code==200,first.text
   assert put(0,data[:mid]).status_code==200
   assert put(0,b'x'*mid).status_code==409
   assert put(mid+1,data[mid:]).status_code in {409,422}
   second=put(mid,data[mid:]);assert second.status_code==200,second.text
  complete=client.post(url+'/segments-complete',headers=headers());assert complete.status_code==202,complete.text
  assert client.post(url+'/segments-complete',headers=headers()).status_code==202
  for _ in range(20):
   run_once();item=client.get(url).json()['data']
   if item['processing_state'] in {'READY','FAILED'}:break
  assert item['processing_state']=='READY',item
  assert item['sha256']==hashlib.sha256(RAW).hexdigest()
  assert item['execution_uuid']==str(payload['execution_uuid'])
  assert item['submission_status'] is None
  assert client.get(url+'/segments').json()['data']['segments'][1]['received_bytes']==len(parts[1])
