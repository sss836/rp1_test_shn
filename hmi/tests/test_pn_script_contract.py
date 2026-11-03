"""Data-only v2 profile boundary; all version applicability here is synthetic."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from factory_hmi import motor_parameter_contract as contract


ROOT=Path(__file__).resolve().parents[2]
CANDIDATES=ROOT/'hmi/factory_hmi/resources/motor-parameters/candidates-v1.json'


def load_tool():
    path=ROOT/'hmi/tools/prepare_motor_parameters.py'
    spec=importlib.util.spec_from_file_location('pn_script_preparation_for_test',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def candidate_rows():
    return json.loads(CANDIDATES.read_text())['templates']


def versions():
    return dict(raw_firmware='synthetic-full-1.0.9',software='1.0.9',hardware='2.0.0',protocol='1.0.0.0')


def evidence():
    return dict(evidence_sha256='a'*64,notes='仅合成确认，不作为真实设备适用性依据')


def v2():
    row=deepcopy(candidate_rows()[0])
    row.update(schema=contract.SCHEMA_V2,protocol_profile=contract.PN_SCRIPT_PROFILE,
               applicability=[versions()],confirmation=evidence(),version='synthetic-v2')
    return row


def test_v1_preserved_and_does_not_authorize_script_profile():
    for row in candidate_rows():
        assert contract.validate(row)==row
        assert contract.pn_profile(row) is None
    old=v2();old['schema']=contract.SCHEMA_V1;old.pop('protocol_profile')
    assert contract.validate(old,published=True)==old
    assert contract.pn_profile(old) is None


def test_confirmed_profile_requires_exact_complete_model_and_versions():
    row=v2()
    assert contract.validate(row,published=True)==row
    assert contract.pn_profile(row)==contract.PN_SCRIPT_PROFILE
    actual=dict(status='known',model=row['model'],**versions())
    assert contract.applicable(row,actual)
    for key in ('model','raw_firmware','software','hardware','protocol'):
        mismatch=dict(actual);mismatch[key]='unconfirmed'
        assert not contract.applicable(row,mismatch)
    assert not contract.applicable(row,dict(actual,status='unknown'))


@pytest.mark.parametrize('mutation',[
    lambda t:t.update(protocol_profile='serial-membus'),
    lambda t:t.update(source_sha256='c'*64),
    lambda t:t.pop('protocol_profile'),
    lambda t:t.update(unknown_field=True),
    lambda t:t.update(schema=contract.SCHEMA_V1),
    lambda t:t.update(confirmation=None),
    lambda t:t.update(applicability=[]),
    lambda t:t['applicability'][0].update(hardware='*'),
    lambda t:t['applicability'][0].pop('protocol'),
])
def test_invalid_profile_never_silently_falls_back(mutation):
    row=v2();mutation(row)
    with pytest.raises(ValueError):contract.pn_profile(row)


def test_pn_uint32_raw_not_serial_uint16_or_scaled_twice():
    row=v2()
    row['entries']=[dict(key='pn_0401',expected_raw=[200],tolerance_raw=0),
                    dict(key='pn_0f09',expected_raw=[0xffffffff],tolerance_raw=0)]
    accepted=contract.validate(row,published=True)
    for entry in accepted['entries']:
        assert contract.spec(entry['key'])['scale']==1
        assert contract.display(entry,entry['expected_raw'])['value']==entry['expected_raw']
    assert accepted['entries'][0]['expected_raw']==[200]
    for invalid in (-1,0x100000000,True,200.0):
        changed=deepcopy(row);changed['entries'][0]['expected_raw']=[invalid]
        with pytest.raises(ValueError):contract.validate(changed,published=True)


def test_preparation_preserves_all_59_values_and_is_not_publication(monkeypatch):
    tool=load_tool();original=candidate_rows()
    monkeypatch.setattr(tool,'extract',lambda path:deepcopy(original))
    result=tool.prepare_script_compatible('synthetic-reference.py',
        applicability={model:[versions()] for model in contract.MODELS},confirmation=evidence())
    assert len(result)==4 and sum(len(t['entries']) for t in result)==59
    for old,new in zip(original,result):
        assert old['entries']==new['entries'] and old['source_sha256']==new['source_sha256']
        assert new['template_id']!=old['template_id']
        assert contract.pn_profile(new)==contract.PN_SCRIPT_PROFILE
    assert not any('published_at' in t or 'published_by' in t for t in result)


def test_preparation_only_includes_explicitly_confirmed_models(monkeypatch):
    tool=load_tool();monkeypatch.setattr(tool,'extract',lambda path:deepcopy(candidate_rows()))
    result=tool.prepare_script_compatible('synthetic.py',applicability={'LRO_6562':[versions()]},confirmation=evidence())
    assert [t['model'] for t in result]==['LRO_6562']
    assert result[0]['applicability']==[versions()]
    for apps in ({},{'unknown':[versions()]},{'LRO_6562':[]}):
        with pytest.raises(ValueError):tool.prepare_script_compatible('synthetic.py',applicability=apps,confirmation=evidence())


def test_cli_legacy_default_and_review_draft_require_actual_evidence(tmp_path,monkeypatch):
    tool=load_tool();original=candidate_rows()
    monkeypatch.setattr(tool,'extract',lambda path:deepcopy(original))
    old=tmp_path/'old.json';new=tmp_path/'draft.json'
    tool.main(['synthetic.py',str(old)])
    assert json.loads(old.read_text())['status']=='candidate_unconfirmed'
    assert json.loads(old.read_text())['templates']==original
    with pytest.raises(SystemExit):tool.main(['synthetic.py',str(new),'--script-compatible'])
    assert not new.exists()
    apps=tmp_path/'applicability.json';apps.write_text(json.dumps({'LRO_5550':[versions()]}))
    proof=tmp_path/'evidence.txt';proof.write_bytes(b'synthetic confirmation only')
    tool.main(['synthetic.py',str(new),'--script-compatible','--applicability',str(apps),
               '--confirmation-evidence',str(proof),'--confirmation-notes','synthetic only'])
    draft=json.loads(new.read_text())
    assert draft['status']=='draft_for_admin_publication'
    assert draft['templates'][0]['confirmation']['evidence_sha256']==hashlib.sha256(proof.read_bytes()).hexdigest()
    assert contract.pn_profile(draft['templates'][0])==contract.PN_SCRIPT_PROFILE
    assert original==candidate_rows()


def test_script_extraction_is_ast_only_and_source_hash_pinned(tmp_path,monkeypatch):
    tool=load_tool();source=tmp_path/'reference.py'
    source.write_text("raise AssertionError('must never execute')\n"
                      "MODEL_PARAMS={0:{'kt':1.74,'kp_max':500,'kd_max':5,'pos_max':12.5,'spd_max':20,'tau_max':10,'cur_max':10}}\n"
                      "PN_SETTINGS=((0x0401,200),)\nMODEL_PN_SETTINGS={}\n"
                      "SCALE={'kt':100,'kp':1,'kd':1,'pos':100,'spd':100,'tor':10,'cur':10,'can_timeout':1}\n")
    with pytest.raises(ValueError,match='哈希'):tool.extract(source)
    # A test-only pin permits this synthetic fixture while keeping the parser
    # exercise independent of a user's Downloads directory or real hardware.
    monkeypatch.setattr(tool,'SOURCE_SHA256',hashlib.sha256(source.read_bytes()).hexdigest())
    rows=tool.extract(source)
    assert len(rows)==1
    assert next(r for r in rows[0]['entries'] if r['key']=='pn_0401')['expected_raw']==[200]


def test_backend_contract_copy_matches_and_rejects_unconfirmed_v2():
    path=ROOT/'backend/app/schemas/motor_parameters.py'
    assert path.read_bytes()==(ROOT/'hmi/factory_hmi/motor_parameter_contract.py').read_bytes()
    spec=importlib.util.spec_from_file_location('backend_pn_contract_for_test',path)
    backend=importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
    assert backend.pn_profile(v2())==contract.PN_SCRIPT_PROFILE
    invalid=v2();invalid['confirmation']=None
    with pytest.raises(ValueError):backend.validate(invalid,published=True)
