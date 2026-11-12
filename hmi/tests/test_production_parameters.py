"""Actual production functions, synthetic candump/commands only; no hardware."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
import threading
import time
import pytest

from factory_hmi.core.production_parameters import (load_script, entries_for, production_record,
    ProductionParameterSession, SOURCE_KIND, compare)
from factory_hmi.core.motor_parameters import ModuleParameterMaintenance
from factory_hmi.core.motor_ota_transport import selfcheck_accepted
from factory_hmi.motor_parameter_contract import MODELS, SOURCE_SHA256, spec
from factory_hmi.gateway.motor_parameters import LocalTemplates
from test_module_parameters import local_numeric_observation
from test_module_firmware_maintenance import observation, target, make_task, begin


def wait(task,status):
    end=time.monotonic()+20
    while time.monotonic()<end:
        job=task.snapshot()
        if job['status']==status:return job
        if not job['active']:break
        time.sleep(.01)
    raise AssertionError((job['status'],job.get('error')))


def finish(task):
    task._thread.join(20)
    assert not task.active
    return task.snapshot()


def obs(index=0, generation=109, model=None):
    result = local_numeric_observation(index, model=model or MODELS[index % 4])
    if generation==109:
        result.update(raw_firmware='V5.0.0.109', software='0.1.9', numeric_software='0.1.9',
                      raw_responses={'0x1e':'a01e00010900010000000111'},errors={},
                      confirmed_firmware_generation=109,firmware_confirmation_source='fixture')
    return result


@pytest.mark.parametrize('model,count,kt,cur',[(MODELS[0],12,174,180),(MODELS[1],17,162,320),(MODELS[2],15,231,350),(MODELS[3],15,238,900)])
def test_exact_production_model_tables(model,count,kt,cur):
    record=production_record(obs(model=model));t=record['template']
    entries={e['key']:e['expected_raw'] for e in t['entries']}
    assert len(entries)==count and entries['kt']==[kt] and entries['cur']==[-cur,cur]
    assert entries['can_timeout']==[0] and entries['pn_0e71']==[75]
    assert t['source_sha256']==SOURCE_SHA256 and record['execution_source']==SOURCE_KIND
    assert len(production_record(obs(generation=108,model=model))['template']['entries'])==8


def test_offline_catalog_and_unknown_model_do_not_call_network(tmp_path):
    catalog=LocalTemplates(SimpleNamespace(key=lambda:pytest.fail('missing catalog needs no key')),root=tmp_path)
    for model in MODELS:
        result=catalog.query(obs(model=model))
        assert result[0]['template']['model']==model
        assert catalog.get(result[0]['template']['template_id'],obs(model=model))==result[0]
    assert production_record(obs(model='UNKNOWN')) is None
    assert production_record(dict(obs(),hardware='9.9.9')) is None


@pytest.mark.parametrize('value,accepted', [('ff020200bcf53ec2',True),('ff0202',True),('ff020100abcd',True),('ff0201',True),('ff0203',False),('fe0201',False),('ff0200',False),('ff02',False),('',False),(None,False),('nothex',False)])
def test_same_production_selfcheck_parser(value,accepted):
    assert selfcheck_accepted(value) is accepted


def test_extended_receipt_reaches_power_then_real_version_verification(tmp_path):
    task,selections,written,_=make_task(tmp_path,n=1)
    old=task.writer
    def writer(*args):
        value=old(*args);value['selfcheck_response']='ff020200bcf53ec2';return value
    task.writer=writer
    try:
        job=begin(task,selections);pending=wait(task,'awaiting_power')
        assert not pending['targets'][0]['actual_version_verified']
        task.acknowledge(job['job_id']);task.readback(job['job_id'])
        assert wait(task,'verified')['targets'][0]['actual_version_verified']
    finally:task.cancel()


def fake_session(state, events, *, fail_key=None):
    """Mock child-process I/O; execute original write/query/build/parse functions."""
    s=ProductionParameterSession();p=s.script
    s.sleep=lambda _:None
    def send(iface,iface_type,motor_id,payload,verbose):
        data=bytes.fromhex(payload);events.append(('send',motor_id,payload))
        keys={p.CONFIG_CODE[k]:k for k,_ in p.PARAM_DEFS}
        if data[:2]==bytes([p.LRO_CONFIG_HEADER,p.PN_WRITE_CODE]):
            key=f'pn_{int.from_bytes(data[2:4],"big"):04x}'
            state[motor_id][key]=[int.from_bytes(data[4:8],'big')]
        elif len(data)>=2 and data[1] in keys:
            key=keys[data[1]]
            if key==fail_key:raise OSError('synthetic write failure')
            signed=p.IS_SIGNED[key]
            state[motor_id][key]=([int.from_bytes(data[2:4],'big')]
                if key in p.SINGLE_VALUE_KEYS else [int.from_bytes(data[2:4],'big',signed=signed),int.from_bytes(data[4:6],'big',signed=signed)])
    def capture(iface,motor_id,request):
        request();raw=bytes.fromhex(events[-1][2]);events.append(('read',motor_id,raw.hex()))
        if raw[:2]==bytes([p.LRO_CONFIG_HEADER,p.PN_READ_CODE]):
            key=f'pn_{int.from_bytes(raw[2:4],"big"):04x}'
            response=b'\xff\xfe\x11'+state[motor_id][key][0].to_bytes(4,'big')
        else:
            key=next(k for k,_ in p.PARAM_DEFS if p.QUERY_CODE[k]==raw[1])
            response=bytes([0xa0,raw[1]])+b''.join(v.to_bytes(2,'big',signed=p.IS_SIGNED[key]) for v in state[motor_id][key])
        text=f'{iface}  {motor_id:03X}  [{len(response)}] '+ ' '.join(f'{b:02X}' for b in response)
        s.evidence['candump']=text
        return text
    p.send_frame=send;p.capture_response=capture
    return s


def make_production_task(tmp_path,n=7,fail_key=None):
    models=[MODELS[2],MODELS[1],MODELS[1],MODELS[1],MODELS[1],MODELS[0],MODELS[3]][:n]
    targets=[dict(target(i),model=models[i]) for i in range(n)]
    observations=lambda:[obs(i,model=models[i]) for i in range(n)]
    records={r['index']:production_record(r) for r in observations()}
    state={i+1:{e['key']:deepcopy(e['expected_raw']) for e in records[i]['template']['entries']} for i in range(n)}
    for values in state.values():values['kt']=[100]
    events=[];sessions=[]
    def factory():
        s=fake_session(state,events,fail_key=fail_key);sessions.append(s);return s
    def lookup(tid,o):
        record=production_record(o)
        if not record or record['template']['template_id']!=tid:raise ValueError('model mismatch')
        return record
    task=ModuleParameterMaintenance(tmp_path,lambda:targets,observations,
        lambda:pytest.fail('platform auth'),lambda *_:pytest.fail('platform template'),lambda:nullcontext(),
        lambda *_:pytest.fail('legacy writer'),lambda *_:pytest.fail('legacy reader'),
        lambda r,s:obs(r['index'],model=r['model']),authorize_local=lambda:{'username':'local'},local_template=lookup,
        production_session_factory=factory)
    selections=[dict(index=i,template_id=records[i]['template']['template_id']) for i in range(n)]
    return task,selections,state,events,sessions


@pytest.mark.parametrize('indices',[[1],[0,1,2,3,4,5,6]])
def test_offline_single_and_all_write_production_order_and_one_power_ack(tmp_path,indices):
    task,selections,state,events,sessions=make_production_task(tmp_path)
    chosen=[selections[i] for i in indices]
    try:
        task.read(chosen,local=True);job=finish(task)
        assert job['status']=='checked_with_differences', job.get('error')
        plan=task.preview(job['job_id'],selections=chosen,local=True)
        events.clear();task.start(plan['preview_id'],True,local=True)
        pending=wait(task,'awaiting_power')
        assert pending.get('production_batch_power') and len(pending['targets'])==len(indices)
        assert {e[1] for e in events}=={i+1 for i in indices}
        p=load_script()
        for i in indices:
            # No parameter reads interleaved between first and last write.
            per_motor=[e for e in events if e[1]==i+1]
            def is_write(e):
                data=bytes.fromhex(e[2])
                return e[0]=='send' and ((data[:2]==bytes([p.LRO_CONFIG_HEADER,p.PN_WRITE_CODE])) or (data[0]==p.LRO_CONFIG_HEADER and data[1] in p.CONFIG_CODE.values()))
            places=[j for j,e in enumerate(per_motor) if is_write(e)]
            assert len(places)==len(pending['targets'][indices.index(i)]['items'])
            assert all(e[0]!='read' for e in per_motor[min(places):max(places)+1])
        with pytest.raises(ValueError):task.readback(pending['job_id'],local=True)
        task.acknowledge(pending['job_id'],local=True)
        assert all(r['human_acknowledged_at'] for r in task.snapshot()['targets'])
        task.readback(pending['job_id'],local=True)
        done=finish(task)
        assert done['status']=='verified' and all(r['post_power_verified'] for r in done['targets'])
        assert all(i['post_power_status']=='passed' and i['immediate']['candump'] for r in done['targets'] for i in r['items'])
    finally:task.cancel()


def test_production_partial_write_failure_stops_later_motors(tmp_path):
    task,selections,state,events,sessions=make_production_task(tmp_path,fail_key='kp')
    try:
        task.read(selections,local=True);job=finish(task)
        plan=task.preview(job['job_id'],local=True);events.clear()
        task.start(plan['preview_id'],True,local=True);done=finish(task)
        assert done['status']=='failed' and all(r['status']=='not_run' for r in done['targets'][1:])
        assert {e[1] for e in events}=={1}
        assert done['targets'][0]['items'][0]['write_status']=='written_unverified'
        assert done['targets'][0]['items'][1]['write_status']=='send_uncertain_unverified'
        assert not any(r['post_power_verified'] for r in done['targets'])
    finally:task.cancel()


def test_model_mismatch_prevents_operation(tmp_path):
    task,selections,*_=make_production_task(tmp_path)
    with pytest.raises(ValueError):task.read([dict(index=0,template_id=selections[1]['template_id'])],local=True)
    assert not task.active


def test_comparison_uses_script_tolerance_not_raw_fixed_tolerance():
    item=dict(key='kt',expected_raw=[174],tolerance_raw=0,_production_script=True)
    assert compare(item,dict(status='read',raw=[175]))=='passed'
    assert compare(item,dict(status='read',raw=[176]))=='mismatch'
    assert compare(item,dict(status='timeout',raw=None))=='timeout'


@pytest.mark.parametrize('cancel', [False,True])
def test_original_capture_reaps_child_and_preserves_canfd_command(monkeypatch,cancel):
    session=ProductionParameterSession();p=session.script;event=threading.Event()
    child=SimpleNamespace(returncode=None)
    child.poll=lambda:child.returncode
    def terminate():child.returncode=-15
    child.terminate=terminate
    child.communicate=lambda timeout=None:('fixture-can  001  [4] A0 16 00 AE','')
    calls=[]
    def spawn(args,**kwargs):
        calls.append(args)
        if cancel:event.set()
        return child
    monkeypatch.setattr(p.subprocess,'Popen',spawn)
    monkeypatch.setattr(p.subprocess,'run',lambda args,**kwargs:calls.append(args))
    row=dict(target(),template_record=production_record(obs(model='LRO_5550')))
    item=row['template_record']['template']['entries'][0]
    session.begin_motor(event)
    if cancel:
        with pytest.raises(InterruptedError):session.read(row,item,event)
        assert len(calls)==1
    else:
        result=session.read(row,item,event)
        assert result['raw']==[174] and result['candump']
        assert calls==[['candump','-n','10','can-fixture,001:7FF'],['cansend','can-fixture','001##1E016']]
    assert child.returncode==-15
    session.close()


def test_wrong_motor_reply_never_compares_as_actual():
    p=load_script()
    assert p.parse_query_response('can0  002  [4] A0 16 00 AE',1,p.QUERY_CODE['kt'],'kt') is None
    assert p.parse_pn_read_response('can0  002  [7] FF FE 11 00 00 00 C8',1,0x401) is None
