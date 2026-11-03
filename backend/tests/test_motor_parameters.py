"""Real backend ASGI and durable temporary store; synthetic admins/templates."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_hmi_updates import materials,actor,app_for,call
from app.api.routes import motor_parameters as routes
from app.services.motor_parameters import ParameterTemplates
from app.core.errors import ApiError
from factory_hmi.motor_parameter_contract import SOURCE_SHA256,sha

BASE='/api/v1/hmi/motor-parameters/templates'
QUERY='?model=LRO_5550&hardware=1.0.2&protocol=1.0.0.0&software=1.0.8&raw_firmware=1.0.8'

def template():
    return dict(schema='rp1-motor-parameters-v1',template_id='a'*32,version='synthetic-1',model='LRO_5550',source_sha256=SOURCE_SHA256,source='https://synthetic.invalid/params',applicability=[dict(raw_firmware='1.0.8',software='1.0.8',hardware='1.0.2',protocol='1.0.0.0')],entries=[dict(key='kt',expected_raw=[174],tolerance_raw=1)],confirmation=dict(evidence_sha256='b'*64,notes='synthetic only'))

@pytest.fixture
def settings(materials):
    root,s,*_=materials
    return s.model_copy(update=dict(motor_parameters_enabled=True,motor_parameters_root=str(root/'parameters')))

def app(settings,monkeypatch,identity):
    a=app_for(settings,monkeypatch,identity);a.include_router(routes.router);monkeypatch.setattr(routes,'get_settings',lambda:settings);return a

@pytest.mark.parametrize('role,expected',[('SYSTEM_ADMIN',201),('TEST_EXECUTOR',403),('VIEWER',403)])
def test_admin_only_publish(settings,monkeypatch,role,expected):
    a=app(settings,monkeypatch,actor(role));r=call(a,'POST',BASE,json=template());assert r.status_code==expected,r.text

@pytest.mark.parametrize('role,expected',[('SYSTEM_ADMIN',200),('TEST_EXECUTOR',200),('VIEWER',403)])
def test_template_use_roles_and_exact_compatibility(settings,monkeypatch,role,expected):
    ParameterTemplates(settings).publish(template(),'synthetic-admin','fixture')
    a=app(settings,monkeypatch,actor(role));r=call(a,'GET',BASE+QUERY);assert r.status_code==expected,r.text
    if expected==200:
        row=r.json()['data']['templates'][0];assert row['template']==template() and row['sha256']==sha(template())
        r=call(a,'GET',BASE+QUERY.replace('hardware=1.0.2','hardware=wrong'));assert r.json()['data']['templates']==[]
        assert r.headers['cache-control']=='no-store'

@pytest.mark.parametrize('kind,session',[('SERVICE',True),('HUMAN',False)])
def test_non_human_browser_rejected(settings,monkeypatch,kind,session):
    a=app(settings,monkeypatch,actor(kind=kind,session=session));assert call(a,'POST',BASE,json=template()).status_code==403

def test_unconfirmed_candidate_rejected(settings,monkeypatch):
    t=template();t.update(confirmation=None,applicability=[])
    a=app(settings,monkeypatch,actor());assert call(a,'POST',BASE,json=t).status_code==422

def test_immutable_idempotency_concurrency_and_corruption(settings):
    store=ParameterTemplates(settings);t=template()
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:store.publish(t,'synthetic-admin','fixture'),range(8)))
    assert sum(r['created'] for r in results)==1
    changed=deepcopy(t);changed['entries'][0]['expected_raw']=[123]
    with pytest.raises(ApiError):store.publish(changed,'synthetic-admin','fixture')
    changed['template_id']='b'*32
    with pytest.raises(ApiError):store.publish(changed,'synthetic-admin','fixture')
    path=store.root/('a'*32+'.json');r=json.loads(path.read_text());r['template']['entries'][0]['expected_raw']=[1];path.write_text(json.dumps(r))
    with pytest.raises(ApiError):store.read(path)

def test_contract_copies_identical():
    root=Path(__file__).resolve().parents[2]
    assert (root/'backend/app/schemas/motor_parameters.py').read_bytes()==(root/'hmi/factory_hmi/motor_parameter_contract.py').read_bytes()
