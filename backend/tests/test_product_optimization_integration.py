import json,os
from uuid import uuid4
import pytest
from app.core.config import get_settings
from test_archives import archive_payload
from test_archives_integration import RAW,create,upload,verify,classify,submit,headers
from test_hmi_presence_integration import environment,signed_in
from test_factory_partition_integration import factory_review,ctx_with_code
pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='disposable PostgreSQL required')]

def test_production_handoff_and_presenter_projection_do_not_expose_engineering(environment,monkeypatch):
 engine,users=environment
 with signed_in(users['admin']) as admin:
  ctx=ctx_with_code(admin,engine);payload={**archive_payload(raw=RAW),'data_domain':'FACTORY','serial_number':'SECRET-SN-'+uuid4().hex,'bench_id':ctx['station_code']}
  archive=upload(admin,create(admin,payload));archive=verify(admin,archive,ctx['station_id']).json()['data'];archive=classify(admin,archive,ctx).json()['data'];archive=submit(admin,archive).json()['data'];response=factory_review(admin,archive,'APPROVE');assert response.status_code==200,response.text
  reference=archive['id'];url=f'/api/v1/archives/{reference}/handoffs';body={'kind':'HANDOFF','note':'synthetic next shift','destination':'next shift','expected_revision':0};key=uuid4().hex
  response=admin.post(url,json=body,headers=headers(key));assert response.status_code==200,response.text
  assert admin.post(url,json=body,headers=headers(key)).json()['data']['revision']==1
  assert admin.post(url,json={**body,'note':'conflict'},headers=headers(key)).status_code==409
  assert admin.post(url,json=body,headers=headers()).status_code==409
  notes=admin.get(url).json()['data']['items'];assert len(notes)==1 and notes[0]['kind']=='HANDOFF'
  topic={'id':'factory-demo','domain':'FACTORY','alias':'合资格成果','part_alias':'单臂','case_alias':'老化检验','archive_ids':[reference],'approved':True,'media_rights_confirmed':False,'selected_curves':[{'archive_id':reference,'metric':'pos_rad','subject':'joint','alias':'已批历史轨迹'}]}
  monkeypatch.setattr(get_settings(),'showcase_topics_json',json.dumps([topic]))
  opened=admin.post('/api/v1/showcase/session',json={'topic_id':topic['id']});assert opened.status_code==200,opened.text
  denied=admin.get(f'/api/v1/archives/{reference}');assert denied.status_code in {401,403}
  response=admin.get('/api/v1/showcase/view');assert response.status_code==200,response.text
  data=response.json()['data'];serialized=json.dumps(data)
  assert data['completed_count']==1 and data['passed_count']==1 and data['denominator']==1
  assert len(data['selected_curves'])==1 and data['selected_curves'][0]['id']=='已批历史轨迹'
  assert data['export_enabled'] is False and data['media_allowed'] is False
  assert payload['serial_number'] not in serialized and ctx['station_code'] not in serialized and reference not in serialized and 'synthetic next shift' not in serialized
  monkeypatch.setattr(get_settings(),'showcase_topics_json','[]')
  assert admin.get('/api/v1/showcase/view').status_code==403


def test_streamed_ledger_requires_account_and_emits_csv(environment):
 _,users=environment
 with signed_in(users['admin']) as admin:
  response=admin.get('/api/v1/archive-ledger.csv?data_domain=FACTORY')
  assert response.status_code==200,response.text
  assert 'attachment' in response.headers['content-disposition']
  assert response.content.startswith(b'\xef\xbb\xbf')
