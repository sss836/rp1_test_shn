"""Synthetic script-profile engine and cleanup; no CAN sockets or publication."""
from copy import deepcopy
from pathlib import Path

import pytest

from factory_hmi.core.motor_maintenance import SERIAL_LOCK
from factory_hmi.core.motor_parameter_transport import SessionQuarantinedError
from factory_hmi.motor_parameter_contract import PN_SCRIPT_PROFILE,SCHEMA_V2,sha
from test_module_parameters import make,finish,preview
from test_module_firmware_maintenance import observation,wait


def scripted(tmp_path,n=1,*,ordinary=False):
    engine,selections,writes,state,actor,record,targets=make(tmp_path,n)
    entries=[dict(key='pn_0401',expected_raw=[200],tolerance_raw=0),
             dict(key='pn_0402',expected_raw=[20],tolerance_raw=0)]
    if ordinary:entries.insert(0,dict(key='kt',expected_raw=[174],tolerance_raw=0))
    record['template'].update(schema=SCHEMA_V2,protocol_profile=PN_SCRIPT_PROFILE,entries=entries)
    record['sha256']=sha(record['template'])
    for values in state.values():values.update(pn_0401=[100],pn_0402=[10])
    sessions=[];calls=[]
    class Session:
        fail_close=False
        def __init__(self,*,quarantine_root):
            self.root=Path(quarantine_root);self.closed=False;self.close_attempts=0;sessions.append(self)
        def read(self,row,item,stop):
            assert not self.closed and item['_protocol_profile']==PN_SCRIPT_PROFILE
            calls.append(('read',row['index'],item['key']))
            return dict(status='read',raw=deepcopy(state[row['index']][item['key']]),
                        observed_at=observation()['observed_at'],protocol_profile=PN_SCRIPT_PROFILE,
                        association_method='single_flight_time_window',parameter_number_echoed=False)
        def write(self,row,item,stop):
            assert not self.closed and item['_protocol_profile']==PN_SCRIPT_PROFILE
            calls.append(('write',row['index'],item['key']))
            state[row['index']][item['key']]=deepcopy(item['expected_raw'])
            return dict(sent=True,protocol_profile=PN_SCRIPT_PROFILE)
        def check_idle(self,stop):
            calls.append(('idle',))
        def ensure_available(self,row,item,stop):
            calls.append(('preflight',row['index'],item['key']))
        def close(self):
            self.close_attempts+=1;calls.append(('close',))
            if self.fail_close:raise OSError('synthetic session close failure')
            self.closed=True
    engine.script_session_factory=Session
    return engine,selections,writes,state,actor,record,targets,sessions,calls,Session


def write_after_read(engine,selections):
    p=preview(engine,selections)
    engine.start(p['preview_id'],True)
    return p


def complete_human_steps(engine,n=1):
    for index in range(n):
        job=wait(engine,'awaiting_power')
        row=job['targets'][index]
        assert not row['post_power_verified']
        with pytest.raises(ValueError):engine.readback(job['job_id'])
        engine.acknowledge(job['job_id'])
        engine.readback(job['job_id'])
        if index<n-1:
            import time
            end=time.monotonic()+2
            while time.monotonic()<end and engine.snapshot()['targets'][index]['status']!='verified':time.sleep(.001)
    return finish(engine)


def test_one_session_per_task_and_all_pn_phases_are_serial(tmp_path):
    engine,selections,writes,_,_,_,_,sessions,calls,_=scripted(tmp_path,2)
    try:
        p=write_after_read(engine,selections)
        job=complete_human_steps(engine,2)
        assert job['status']=='verified' and job['summary']['written_verified']==4
        assert len(sessions)==2 and all(s.closed for s in sessions)
        assert all(s.root==engine.root/'pn-quarantine' for s in sessions)
        assert not writes  # The legacy writer never receives a Pn item.
        assert [c for c in calls if c[0]=='write']==[
            ('write',0,'pn_0401'),('write',0,'pn_0402'),('write',1,'pn_0401'),('write',1,'pn_0402')]
        for row in p['targets']:
            for item in row['items']:
                assert item['_protocol_profile']==PN_SCRIPT_PROFILE
        for row in job['targets']:
            assert row['human_acknowledged_at'] and row['post_power_verified']
            for item in row['items']:
                assert item['actual']['parameter_number_echoed'] is False
                assert item['immediate']['association_method']=='single_flight_time_window'
                assert item['post_power']['protocol_profile']==PN_SCRIPT_PROFILE
    finally:engine.cancel()


def test_v2_ordinary_items_keep_existing_reader_writer(tmp_path):
    engine,selections,writes,_,_,_,_,sessions,calls,_=scripted(tmp_path,ordinary=True)
    try:
        p=write_after_read(engine,selections)
        job=complete_human_steps(engine)
        assert job['status']=='verified' and writes==[(0,'kt')]
        assert '_protocol_profile' not in p['targets'][0]['items'][0]
        assert not any(c[0] in {'read','write'} and c[2]=='kt' for c in calls)
        assert len(sessions)==2
    finally:engine.cancel()


def test_v1_pn_does_not_create_script_session_and_cannot_preview(tmp_path):
    engine,selections,writes,_,_,record,_=make(tmp_path)
    record['template']['entries']=[dict(key='pn_0401',expected_raw=[200],tolerance_raw=0)]
    record['sha256']=sha(record['template'])
    engine.reader=lambda *args:dict(status='association_unknown',raw=[200])
    engine.script_session_factory=lambda **kwargs:pytest.fail('v1 must not create script session')
    engine.read(selections);job=finish(engine)
    assert job['status']=='checked_with_differences' and not writes
    assert '_protocol_profile' not in job['targets'][0]['items'][0]
    with pytest.raises(ValueError):engine.preview(job['job_id'])


def test_request_cannot_inject_profile_and_changed_old_value_stops_before_write(tmp_path):
    engine,selections,_,state,_,_,_,_,calls,_=scripted(tmp_path)
    with pytest.raises(ValueError):engine.read([dict(selections[0],_protocol_profile=PN_SCRIPT_PROFILE)])
    p=preview(engine,selections);state[0]['pn_0401']=[101]
    engine.start(p['preview_id'],True);job=finish(engine)
    assert job['status']=='failed' and not any(c[0]=='write' for c in calls)
    assert job['targets'][0]['items'][0]['actual_before_operation']['raw']==[101]


@pytest.mark.parametrize('missing',['association_method','parameter_number_echoed','protocol_profile'])
def test_incomplete_success_evidence_cannot_authorize_preview(tmp_path,missing):
    engine,selections,*rest=scripted(tmp_path,2)
    Session=rest[-1];normal=Session.read
    def incomplete(self,*args):
        value=normal(self,*args);value.pop(missing);return value
    Session.read=incomplete
    engine.read(selections);job=finish(engine)
    item=job['targets'][0]['items'][0]
    assert job['status']=='failed' and item['actual']['status']=='association_unknown'
    assert item['transport_error']['phase']=='actual'
    assert job['targets'][0]['items'][1]['status']=='not_run' and job['targets'][1]['status']=='not_run'
    with pytest.raises(ValueError):engine.preview(job['job_id'])


@pytest.mark.parametrize('mutation',[
    dict(association_method='parameter_number_echo'),
    dict(parameter_number_echoed=True),
    dict(parameter_number_echoed=0),
    dict(protocol_profile='serial-membus'),
    dict(raw=[True]),dict(raw=[-1]),dict(raw=[0x100000000]),dict(raw=[]),
])
def test_wrong_success_metadata_or_non_uint32_never_authorizes_preview(tmp_path,mutation):
    engine,selections,*rest=scripted(tmp_path)
    Session=rest[-1];normal=Session.read
    def invalid(self,*args):
        value=normal(self,*args);value.update(mutation);return value
    Session.read=invalid
    engine.read(selections);job=finish(engine)
    assert job['status']=='failed'
    assert job['targets'][0]['items'][0]['actual']['status']=='association_unknown'
    with pytest.raises(ValueError):engine.preview(job['job_id'])


@pytest.mark.parametrize('phase',['actual','actual_before_operation','immediate','post_power','write'])
def test_quarantine_evidence_preserved_and_whole_batch_stops(tmp_path,phase):
    engine,selections,writes,_,_,_,_,sessions,calls,Session=scripted(tmp_path,2)
    failure=dict(status='association_unknown',raw=[999],response_hex='fffe11000003e7',
                 request_hex='c0110401',error='synthetic ambiguous or late response')
    failure_message='synthetic uncertain send' if phase=='write' else 'synthetic quarantine'
    expected_evidence=SessionQuarantinedError(failure_message,deepcopy(failure)).evidence
    if phase!='actual':p=preview(engine,selections)
    normal=Session.read
    reads={}
    def uncertain(self,row,item,stop):
        reads[item['key']]=reads.get(item['key'],0)+1
        trigger=(phase in {'actual','actual_before_operation'} and item['key']=='pn_0401'
                 or phase=='immediate' and item['key']=='pn_0401' and reads[item['key']]==2
                 or phase=='post_power' and item['key']=='pn_0401' and reads[item['key']]==3)
        if trigger:raise SessionQuarantinedError('synthetic quarantine',deepcopy(failure))
        return normal(self,row,item,stop)
    Session.read=uncertain
    if phase=='write':
        Session.write=lambda *args:(_ for _ in ()).throw(SessionQuarantinedError('synthetic uncertain send',deepcopy(failure)))
    try:
        if phase=='actual':engine.read(selections)
        else:
            engine.start(p['preview_id'],True)
            if phase=='post_power':
                job=wait(engine,'awaiting_power');engine.acknowledge(job['job_id']);engine.readback(job['job_id'])
        job=finish(engine);row=job['targets'][0];item=row['items'][0]
        assert job['status']=='failed' and job['targets'][1]['status']=='not_run'
        assert item['transport_error']['phase']==phase
        assert item['transport_error']['evidence']==expected_evidence
        if phase!='write':assert item[phase]==expected_evidence
        if phase in {'actual','actual_before_operation'}:assert not any(c[0]=='write' for c in calls)
        if phase=='immediate':assert [c for c in calls if c[0]=='write']==[('write',0,'pn_0401')]
        if phase=='write':assert item['write_status']=='send_uncertain_unverified'
        if phase=='post_power':assert row['human_acknowledged_at'] and not row['post_power_verified']
        assert all(s.closed for s in sessions) and not writes
    finally:engine.cancel()


def test_timeout_result_without_exception_stops_all_items(tmp_path):
    engine,selections,_,_,_,_,_,_,calls,Session=scripted(tmp_path,2)
    Session.read=lambda *args:dict(status='timeout',raw=None,error='synthetic timeout')
    engine.read(selections);job=finish(engine)
    assert job['status']=='failed' and job['targets'][0]['items'][0]['actual']['status']=='timeout'
    assert job['targets'][0]['items'][1]['status']=='not_run' and job['targets'][1]['status']=='not_run'
    assert not any(c[0]=='write' for c in calls)


@pytest.mark.parametrize('failing',['session','base'])
def test_close_failure_keeps_serial_claim_and_retries_in_order_without_io(tmp_path,failing):
    engine,selections,_,_,_,_,_,sessions,calls,Session=scripted(tmp_path)
    events=[]
    class Reservation:
        fail=failing=='base'
        def __enter__(self):events.append('enter');return self
        def close(self):
            events.append('base-close')
            if self.fail:raise OSError('synthetic base close failure')
    base=Reservation();engine.reserve=lambda:base
    if failing=='session':Session.fail_close=True
    engine.read(selections);engine._thread.join(2)
    assert not engine._thread.is_alive()
    try:
        assert engine.active and engine.snapshot()['cleanup_quarantined']
        assert not SERIAL_LOCK.acquire(blocking=False)
        if failing=='session':assert events==['enter'] and sessions[0].close_attempts==1
        else:assert sessions[0].closed and sessions[0].close_attempts==1
        io=[c for c in calls if c[0] in {'read','write'}]
        Session.fail_close=False;base.fail=False
        engine.cancel()
        assert not engine.active and io==[c for c in calls if c[0] in {'read','write'}]
        assert sessions[0].close_attempts==(2 if failing=='session' else 1)
        assert events[-1]=='base-close'
        assert SERIAL_LOCK.acquire(blocking=False)
        SERIAL_LOCK.release()
    finally:
        Session.fail_close=False;base.fail=False;engine.cancel()


def test_quarantine_root_is_shared_when_explicit_and_never_cleared_by_task(tmp_path):
    engine,selections,_,_,_,_,_,sessions,_,Session=scripted(tmp_path/'tasks')
    shared=tmp_path/'shared-pn-quarantine';shared.mkdir();marker=shared/'synthetic.json';marker.write_text('preserved')
    engine.pn_quarantine_root=shared
    def refuse(self,*args):raise SessionQuarantinedError('synthetic persistent quarantine',{'status':'association_unknown','raw':None})
    Session.read=refuse
    for _ in range(2):
        engine.read(selections);job=finish(engine);assert job['status']=='failed'
    assert len(sessions)==2 and all(s.root==shared for s in sessions)
    assert marker.read_text()=='preserved'


def ordinary_between_pn(record):
    rows=record['template']['entries']
    rows[:]=[rows[1],rows[0],rows[2]]
    record['sha256']=sha(record['template'])


@pytest.mark.parametrize('when',['before_read','after_read'])
def test_late_pn_observed_around_ordinary_read_stops_whole_batch(tmp_path,when):
    engine,selections,_,_,_,record,_,_,calls,Session=scripted(tmp_path,ordinary=True)
    ordinary_between_pn(record)
    normal_pn=Session.read;normal_ordinary=engine.reader
    pending={'late':False,'ordinary_reads':0}
    def pn(self,*args):
        result=normal_pn(self,*args)
        if when=='before_read':pending['late']=True
        return result
    def ordinary(*args):
        pending['ordinary_reads']+=1
        if when=='after_read':pending['late']=True
        return normal_ordinary(*args)
    def idle(self,stop):
        if pending['late']:raise SessionQuarantinedError('late Pn during ordinary read',{'raw':[999]})
    Session.read=pn;Session.check_idle=idle;engine.reader=ordinary
    engine.read(selections);job=finish(engine)
    assert job['status']=='failed'
    item=job['targets'][0]['items'][1]
    assert item['transport_error']['phase']=='actual' and item['actual']['status']=='association_unknown'
    assert job['targets'][0]['items'][2]['status']=='not_run'
    assert pending['ordinary_reads']==(0 if when=='before_read' else 1)
    assert not any(c[0]=='write' for c in calls)
    with pytest.raises(ValueError):engine.preview(job['job_id'])


@pytest.mark.parametrize('when',['before_write','after_write'])
def test_late_pn_observed_around_ordinary_write_preserves_partial_state(tmp_path,when):
    engine,selections,writes,_,_,record,_,_,calls,Session=scripted(tmp_path,ordinary=True)
    ordinary_between_pn(record);p=preview(engine,selections)
    normal_reader=engine.reader;normal_writer=engine.writer
    pending={'idle_until_late':None,'late':False}
    def reader(*args):
        value=normal_reader(*args)
        if when=='before_write':pending['idle_until_late']=2
        return value
    def writer(*args):
        value=normal_writer(*args)
        if when=='after_write':pending['late']=True
        return value
    def idle(self,stop):
        if pending['idle_until_late'] is not None:
            pending['idle_until_late']-=1
            if pending['idle_until_late']==0:pending['late']=True
        if pending['late']:raise SessionQuarantinedError('late Pn during ordinary write',{'raw':[999]})
    engine.reader=reader;engine.writer=writer;Session.check_idle=idle
    engine.start(p['preview_id'],True);job=finish(engine)
    items=job['targets'][0]['items']
    assert job['status']=='failed' and items[0]['write_status']=='written_verified'
    assert items[1]['write_status']=='send_uncertain_unverified'
    assert items[1]['transport_error']['phase']=='write' and items[2]['status']=='not_run'
    assert writes==([] if when=='before_write' else [(0,'kt')])
    assert [c for c in calls if c[0]=='write']==[('write',0,'pn_0401')]


def test_pending_pn_after_human_wait_is_checked_before_version_or_parameter_reads(tmp_path):
    engine,selections,_,_,_,_,_,_,calls,Session=scripted(tmp_path)
    p=preview(engine,selections);engine.start(p['preview_id'],True)
    job=wait(engine,'awaiting_power');previous=list(calls)
    Session.check_idle=lambda *args:(_ for _ in ()).throw(SessionQuarantinedError('late while awaiting human',{'raw':[999]}))
    engine.acknowledge(job['job_id']);engine.readback(job['job_id']);job=finish(engine)
    assert job['status']=='failed' and job['targets'][0]['human_acknowledged_at']
    assert not job['targets'][0]['post_power_verified']
    assert job['transport_error']['phase']=='before_version_check'
    assert [c for c in calls[len(previous):] if c[0] in {'read','write'}]==[]


def test_terminal_idle_conflict_never_allows_preview_even_with_all_raw_values(tmp_path):
    engine,selections,_,_,_,_,_,_,_,Session=scripted(tmp_path)
    normal=Session.read;pending={'late':False}
    def reader(self,*args):
        result=normal(self,*args)
        if args[1]['key']=='pn_0402':pending['late']=True
        return result
    def idle(self,stop):
        if pending['late']:raise SessionQuarantinedError('late at terminal boundary',{'raw':[999]})
    Session.read=reader;Session.check_idle=idle
    engine.read(selections);job=finish(engine)
    assert job['status']=='failed'
    assert all(i['actual']['status']=='read' for i in job['targets'][0]['items'])
    assert job['targets'][0]['items'][-1]['transport_error']['phase']=='before_terminal'
    with pytest.raises(ValueError):engine.preview(job['job_id'])


def test_close_quiet_conflict_fails_saved_terminal_and_retry_only_closes(tmp_path):
    engine,selections,_,_,_,_,_,sessions,calls,Session=scripted(tmp_path)
    normal=Session.close;fail={'value':True}
    def close(self):
        if fail['value']:raise SessionQuarantinedError('late at session close',{'raw':[999]})
        return normal(self)
    Session.close=close
    engine.read(selections);engine._thread.join(2)
    try:
        job=engine.snapshot()
        assert job['status']=='failed' and job['active'] and job['cleanup_quarantined']
        assert job['transport_error']['phase']=='session_close'
        import json
        stored=json.loads((engine.root/(job['job_id']+'.json')).read_text())
        assert stored['status']=='failed' and stored['transport_error']['evidence']['raw']==[999]
        previous=[c for c in calls if c[0] in {'read','write'}]
        fail['value']=False;engine.cancel()
        final=engine.snapshot()
        assert not final['active'] and final['status']=='failed'
        assert sessions[0].closed
        assert previous==[c for c in calls if c[0] in {'read','write'}]
        with pytest.raises(ValueError):engine.preview(job['job_id'])
    finally:fail['value']=False;engine.cancel()


@pytest.mark.parametrize('mode',['read','write'])
def test_persistent_pn_quarantine_preflight_stops_before_any_ordinary_io(tmp_path,mode):
    engine,selections,writes,_,_,_,_,sessions,calls,Session=scripted(tmp_path,2,ordinary=True)
    if mode=='write':p=preview(engine,selections)
    calls.clear();ordinary=[]
    normal_reader=engine.reader;normal_writer=engine.writer
    def reader(*args):ordinary.append('read');return normal_reader(*args)
    def writer(*args):ordinary.append('write');return normal_writer(*args)
    def unavailable(self,row,item,stop):
        calls.append(('preflight',row['index'],item['key']))
        if row['index']==1:
            raise SessionQuarantinedError('synthetic persisted interface quarantine',
                dict(status='association_unknown',raw=None,interface=row['interface'],quarantine_path=str(self.root/'synthetic.json')))
    engine.reader=reader;engine.writer=writer;Session.ensure_available=unavailable
    if mode=='read':engine.read(selections)
    else:engine.start(p['preview_id'],True)
    job=finish(engine)
    assert job['status']=='failed' and not ordinary and not writes
    assert not any(c[0] in {'read','write'} for c in calls)
    assert job['targets'][0]['status']=='not_run'
    item=job['targets'][1]['items'][1]
    assert item['transport_error']['phase']=='preflight'
    assert item['transport_error']['evidence']['interface']==job['targets'][1]['interface']
    assert all(s.closed for s in sessions)
