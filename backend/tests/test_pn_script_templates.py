"""Existing HTTP guards and durable temporary template store for v2 data.

All compatibility and confirmation below is synthetic, never a real publication.
"""
from copy import deepcopy

import pytest
from test_hmi_updates import materials,actor,call
from test_motor_parameters import settings,app,template,BASE,QUERY
from app.schemas.motor_parameters import PN_SCRIPT_PROFILE,SCHEMA_V2,sha,pn_profile


def script_template():
    row=template()
    row.update(schema=SCHEMA_V2,protocol_profile=PN_SCRIPT_PROFILE,
               template_id='c'*32,version='synthetic-script-1',
               entries=[dict(key='pn_0401',expected_raw=[200],tolerance_raw=0),
                        dict(key='pn_0f09',expected_raw=[0xffffffff],tolerance_raw=0)])
    return row


def test_v2_admin_publish_and_executor_query_exact_applicability(settings,monkeypatch):
    admin=app(settings,monkeypatch,actor('SYSTEM_ADMIN'))
    value=script_template()
    published=call(admin,'POST',BASE,json=value)
    assert published.status_code==201,published.text
    row=published.json()['data']['record']
    assert row['template']==value and row['sha256']==sha(value)
    assert row['published_by'] and row['published_at']
    executor=app(settings,monkeypatch,actor('TEST_EXECUTOR'))
    response=call(executor,'GET',BASE+QUERY)
    assert response.status_code==200,response.text
    assert response.headers['cache-control']=='no-store'
    received=response.json()['data']['templates']
    assert len(received)==1 and received[0]==row
    assert pn_profile(received[0]['template'])==PN_SCRIPT_PROFILE
    for old,new in [('model=LRO_5550','model=LRO_6562'),('hardware=1.0.2','hardware=2.0.0'),
                    ('protocol=1.0.0.0','protocol=1.0.0.1'),('software=1.0.8','software=1.0.9'),
                    ('raw_firmware=1.0.8','raw_firmware=1.0.9')]:
        assert call(executor,'GET',BASE+QUERY.replace(old,new)).json()['data']['templates']==[]
    denied=call(executor,'POST',BASE,json=value)
    assert denied.status_code==403,denied.text


@pytest.mark.parametrize('role',['TEST_EXECUTOR','VIEWER'])
def test_v2_non_admin_publication_rejected(settings,monkeypatch,role):
    a=app(settings,monkeypatch,actor(role))
    assert call(a,'POST',BASE,json=script_template()).status_code==403


@pytest.mark.parametrize('change',[
    lambda t:t.pop('source_sha256'),
    lambda t:t.update(source_sha256='c'*64),
    lambda t:t.update(source=''),
    lambda t:t.update(protocol_profile='serial-membus'),
    lambda t:t.pop('protocol_profile'),
    lambda t:t.update(version=''),
    lambda t:t.update(applicability=[]),
    lambda t:t['applicability'][0].update(hardware='*'),
    lambda t:t['applicability'][0].pop('raw_firmware'),
    lambda t:t.update(confirmation=None),
    lambda t:t['confirmation'].update(evidence_sha256='invalid'),
    lambda t:t['confirmation'].update(notes=''),
    lambda t:t['entries'][0].update(expected_raw=[0x100000000]),
    lambda t:t['entries'][0].update(expected_raw=[-1]),
])
def test_v2_invalid_source_profile_version_or_confirmation_rejected(settings,monkeypatch,change):
    a=app(settings,monkeypatch,actor('SYSTEM_ADMIN'))
    value=script_template();change(value)
    response=call(a,'POST',BASE,json=value)
    assert response.status_code==422,response.text
    assert call(a,'GET',BASE+QUERY).json()['data']['templates']==[]


def test_v2_http_idempotency_and_immutable_published_values(settings,monkeypatch):
    a=app(settings,monkeypatch,actor('SYSTEM_ADMIN'));value=script_template()
    first=call(a,'POST',BASE,json=value)
    again=call(a,'POST',BASE,json=deepcopy(value))
    assert first.status_code==201 and again.status_code==200
    assert first.json()['data']['record']==again.json()['data']['record']
    changed=deepcopy(value);changed['entries'][0]['expected_raw']=[201]
    refused=call(a,'POST',BASE,json=changed)
    assert refused.status_code==409,refused.text
    changed['template_id']='d'*32
    refused=call(a,'POST',BASE,json=changed)
    assert refused.status_code==409,refused.text
    stored=call(a,'GET',BASE+QUERY).json()['data']['templates']
    assert len(stored)==1 and stored[0]['template']==value


def test_v1_remains_valid_and_distinct_from_v2_profile(settings,monkeypatch):
    a=app(settings,monkeypatch,actor('SYSTEM_ADMIN'));legacy=template()
    assert call(a,'POST',BASE,json=legacy).status_code==201
    assert call(a,'POST',BASE,json=script_template()).status_code==201
    rows=call(a,'GET',BASE+QUERY).json()['data']['templates']
    assert len(rows)==2
    v1=next(r['template'] for r in rows if r['template']['schema']!=SCHEMA_V2)
    assert v1==legacy and pn_profile(v1) is None
