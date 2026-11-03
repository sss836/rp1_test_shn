"""Isolated parameter lifecycle; values and compatibility below are SYNTHETIC."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import pytest
from factory_hmi.motor_parameter_contract import validate,sha,spec,SOURCE_SHA256,compare
from factory_hmi.core.motor_parameters import ModuleParameterMaintenance
from factory_hmi.core.motor_parameter_transport import decode,query_payload,write_payload,exchange,FRAME
from test_module_firmware_maintenance import observation,target,wait


def template():
    return dict(schema='rp1-motor-parameters-v1',template_id='a'*32,version='synthetic-1',model='LRO_5550',source_sha256=SOURCE_SHA256,source='https://synthetic.invalid/parameters',
        applicability=[{k:observation()[k] for k in ('raw_firmware','software','hardware','protocol')}],
        entries=[dict(key='kt',expected_raw=[174],tolerance_raw=1),dict(key='kp',expected_raw=[0,500],tolerance_raw=0)],
        confirmation=dict(evidence_sha256='b'*64,notes='仅合成检查，非真实适用性'))

def record(t=None):
    t=t or template();return dict(template=validate(t,published=True),sha256=sha(t),published_by='synthetic-admin',published_at='2026-10-30T01:00:00Z',request_id='synthetic')


def make(tmp_path,n=1):
    rec=record();state={};writes=[];targets=[target(i) for i in range(n)];actor={'username':'executor','client_session':'synthetic','role':'TEST_EXECUTOR'}
    for i in range(n):state[i]={'kt':[100],'kp':[0,400]}
    def reader(row,item,stop):return dict(status='read',raw=deepcopy(state[row['index']][item['key']]),observed_at=observation()['observed_at'])
    def writer(row,item,stop):
        writes.append((row['index'],item['key']));state[row['index']][item['key']]=deepcopy(item['expected_raw']);return dict(synthetic=True)
    task=ModuleParameterMaintenance(tmp_path,lambda:targets,lambda:[observation(i) for i in range(n)],lambda:deepcopy(actor),lambda i,o:deepcopy(rec),lambda:nullcontext(),writer,reader,lambda r,s:observation(r['index']))
    selections=[dict(index=i,template_id='a'*32) for i in range(n)]
    return task,selections,writes,state,actor,rec,targets


def finish(task):
    task._thread.join(3);assert not task._thread.is_alive();assert not task.active
    return task.snapshot()


def preview(task,selections):
    task.read(selections);r=finish(task);return task.preview(r['job_id'])


def test_independent_read_preview_write_power_and_recheck(tmp_path):
    task,selections,writes,state,*_=make(tmp_path,2)
    try:
        task.read(selections);job=finish(task)
        assert writes==[] and job['status']=='checked_with_differences'
        assert all(i['status']=='mismatch' for r in job['targets'] for i in r['items'])
        p=task.preview(job['job_id']);assert p['targets'][0]['items'][0]['old_raw']==[100]
        with pytest.raises(ValueError):task.start(p['preview_id'],False)
        task.start(p['preview_id'],True)
        for i in range(2):
            j=wait(task,'awaiting_power');assert j['targets'][i]['post_power_verified'] is False
            with pytest.raises(ValueError):task.readback(j['job_id'])
            ack=task.acknowledge(j['job_id']);assert ack['targets'][i]['post_power_verified'] is False
            task.readback(j['job_id'])
            if i==0:
                end=time.monotonic()+2
                while time.monotonic()<end and task.snapshot()['targets'][0]['status']!='verified':time.sleep(.002)
        j=finish(task);assert j['status']=='verified'
        assert writes==[(0,'kt'),(0,'kp'),(1,'kt'),(1,'kp')]
        assert all(r['post_power_verified'] for r in j['targets'])
        assert j['summary']['written_verified']==4 and not j['atomic_rollback']
        stored=json.loads((task.root/(j['job_id']+'.json')).read_text());assert stored['summary']==j['summary']
    finally:task.cancel()


@pytest.mark.parametrize('status',['timeout','unknown','skipped','association_unknown'])
def test_unknown_never_passes_or_authorizes_write(tmp_path,status):
    task,selections,writes,*_=make(tmp_path)
    task.reader=lambda *args:dict(status=status,raw=None)
    task.read(selections);j=finish(task)
    assert j['status']=='checked_with_differences' and all(i['status']==status for i in j['targets'][0]['items'])
    with pytest.raises(ValueError):task.preview(j['job_id'])
    assert writes==[]


@pytest.mark.parametrize('change',['actor','config','template','old_value','version'])
def test_preview_binding_and_last_moment_checks(tmp_path,change):
    task,selections,writes,state,actor,rec,targets=make(tmp_path)
    p=preview(task,selections)
    if change=='actor':actor['client_session']='different'
    if change=='config':targets[0]['motor_id']=77
    if change=='template':rec['template']['version']='changed'
    if change=='old_value':state[0]['kt']=[111]
    if change=='version':task.version_reader=lambda r,s:dict(observation(),hardware='wrong')
    if change in {'actor','config','template'}:
        with pytest.raises(ValueError):task.start(p['preview_id'],True)
    else:
        task.start(p['preview_id'],True);assert finish(task)['status']=='failed'
    assert writes==[]


def test_partial_write_failure_preserves_each_state_and_stops_next(tmp_path):
    task,selections,writes,state,*_=make(tmp_path,2);p=preview(task,selections);normal=task.writer
    def writer(row,item,stop):
        if item['key']=='kp':raise OSError('synthetic send failure, delivery unknown')
        return normal(row,item,stop)
    task.writer=writer;task.start(p['preview_id'],True);j=finish(task)
    assert writes==[(0,'kt')] and j['status']=='failed'
    assert j['targets'][0]['items'][0]['write_status']=='written_verified'
    assert j['targets'][0]['items'][1]['write_status']=='send_uncertain_unverified'
    assert j['targets'][1]['status']=='not_run' and j['summary']['not_written']==2
    assert j['targets'][0]['power_cycle_required'] and not j['targets'][0]['post_power_verified']


def test_immediate_mismatch_no_later_write(tmp_path):
    task,selections,writes,state,*_=make(tmp_path);p=preview(task,selections)
    task.writer=lambda row,item,stop:writes.append(item['key']) or {'synthetic':True}
    task.start(p['preview_id'],True);j=finish(task)
    assert writes==['kt'] and j['summary']['written_unverified']==1
    assert j['targets'][0]['items'][0]['immediate']['raw']==[100]


def test_cancel_awaiting_human_and_restart_never_replays(tmp_path):
    task,selections,writes,*_=make(tmp_path);p=preview(task,selections)
    task.start(p['preview_id'],True);j=wait(task,'awaiting_power');task.cancel()
    assert task.snapshot()['status']=='interrupted' and len(writes)==2
    # Simulate persisted process death; a fresh engine marks it interrupted.
    path=tmp_path/(j['job_id']+'.json');j['active']=True;path.write_text(json.dumps(j))
    again,*_=make(tmp_path)
    assert not again.active and again.snapshot()['status']=='interrupted'
    assert len(writes)==2


def test_post_power_mismatch_distinct_from_human_statement(tmp_path):
    task,selections,writes,state,*_=make(tmp_path);p=preview(task,selections)
    task.start(p['preview_id'],True);j=wait(task,'awaiting_power');task.acknowledge(j['job_id']);state[0]['kp']=[0,123]
    task.readback(j['job_id']);j=finish(task)
    assert j['status']=='failed' and j['targets'][0]['human_acknowledged_at'] and not j['targets'][0]['post_power_verified']


@pytest.mark.parametrize('selection', [[],[{'index':0,'template_id':'a'*32}]*2,[{'index':99,'template_id':'a'*32}],[{'index':0,'template_id':'a'*32,'expected_raw':[999]}]])
def test_target_and_request_rejection(tmp_path,selection):
    task,*_=make(tmp_path)
    with pytest.raises(ValueError):task.read(selection)


def test_terminal_save_failure_keeps_bus_until_explicit_cleanup(tmp_path):
    task,selections,*_=make(tmp_path);save=task._save
    def fail():
        if not task._job['active']:raise OSError('synthetic disk-full')
        save()
    task._save=fail;task.read(selections);task._thread.join(2)
    assert task.active and task.snapshot()['cleanup_quarantined']
    other,sel,*_=make(tmp_path/'other');other.read(sel);assert finish(other)['status']=='failed'
    task._save=save;task.cancel();assert not task.active


def test_close_failure_keeps_reservation_and_no_replay(tmp_path):
    task,selections,writes,*_=make(tmp_path)
    class Reservation:
        fail=True
        def __enter__(self):return self
        def close(self):
            if self.fail:raise OSError('synthetic close failure')
    reservation=Reservation();task.reserve=lambda:reservation
    task.read(selections);task._thread.join(2)
    assert task.active and task.snapshot()['cleanup_quarantined']
    reservation.fail=False;task.cancel();assert not task.active and not writes


@pytest.mark.parametrize('key,payload,expected',[('kt',b'\xa0\x16\x00\xae',[174]),('pos',b'\xa0\x19\xfb\x1e\x04\xe2',[-1250,1250]),('kp',b'\xa0\x17\x00\x00\x01\xf4',[0,500])])
def test_wire_response_association_and_signed_units(key,payload,expected):
    t=target();assert decode(key,1,payload,t)['raw']==expected
    assert decode(key,2,payload,t) is None
    assert decode(key,0x80000001,payload,t) is None
    assert decode(key,1,bytes([0x80])+payload[1:],t) is None
    assert decode(key,1,payload[:2],t) is None
    assert decode(key,1,bytes([0xa0,0x1f])+payload[2:],t) is None


def test_pn_no_echo_never_passes_or_writes():
    result=decode('pn_0401',1,b'\xff\xfe\x11\x00\x00\x00\xc8',target())
    entry=dict(key='pn_0401',expected_raw=[200],tolerance_raw=0)
    assert compare(entry,result)=='association_unknown' and result['raw']==[200]
    assert query_payload('pn_0401')==b'\xc0\x11\x04\x01'
    with pytest.raises(ValueError):write_payload(entry)
    assert write_payload(dict(key='pos',expected_raw=[-1250,1250]))==b'\xc0\x07\xfb\x1e\x04\xe2'


def test_candidates_unconfirmed_and_distinct_from_running_gains():
    source=Path(__file__).resolve().parents[1]/'factory_hmi/resources/motor-parameters/candidates-v1.json'
    rows=json.loads(source.read_text())['templates'];assert len(rows)==4
    for t in rows:
        validate(t)
        with pytest.raises(ValueError):validate(t,published=True)
    assert '非运行增益' in spec('kp')['meaning']
    assert next(i for i in rows[0]['entries'] if i['key']=='can_timeout')['expected_raw']==[0]


@pytest.mark.parametrize('change',[lambda t:t.update(entries=[]),lambda t:t['entries'][0].update(expected_raw=[True]),lambda t:t['entries'][0].update(expected_raw=[65536]),lambda t:t['entries'][0].update(tolerance_raw=-1),lambda t:t['entries'].append(t['entries'][0]),lambda t:t.update(confirmation=None),lambda t:t['applicability'][0].update(hardware='*')])
def test_template_invalid_cases(change):
    t=template();change(t)
    with pytest.raises(ValueError):validate(t,published=True)

@pytest.mark.parametrize('timeout,cancel',[ (False,False),(True,False),(False,True)])
def test_socket_boundary_wrong_id_code_and_bounded_exit(timeout,cancel):
    stop=threading.Event();sent=[]
    class Socket:
        replies=[]
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def setsockopt(self,*args):pass
        def bind(self,*args):pass
        def setblocking(self,*args):pass
        def send(self,wire):
            sent.append(wire)
            if cancel:stop.set()
            if not timeout:
                self.replies=[FRAME.pack(2,4,1,b'\xa0\x16\x00\xae'),FRAME.pack(1,4,1,b'\xa0\x1f\x00\xae'),FRAME.pack(1,4,1,b'\xa0\x16\x00\xae')]
            return len(wire)
        def recv(self,n):
            if not self.replies:raise BlockingIOError()
            return self.replies.pop(0)
    bus=Socket();ready=lambda *args:([bus],[],[]) if bus.replies else ([],[],[])
    if cancel:
        with pytest.raises(InterruptedError):exchange(target(),b'\xe0\x16',stop,'kt',socket_factory=lambda *a:bus,ready=ready,timeout=.01)
    else:
        r=exchange(target(),b'\xe0\x16',stop,'kt',socket_factory=lambda *a:bus,ready=ready,timeout=.01)
        assert r['status']==('timeout' if timeout else 'read')
        if not timeout:assert r['raw']==[174] and r['response_hex']=='a01600ae'
    assert len(sent)==1 and FRAME.unpack(sent[0])[3][:2]==b'\xe0\x16'


def test_global_read_lock_blocks_firmware_without_device_write(tmp_path):
    from test_module_firmware_maintenance import make_task
    task,selections,*_=make(tmp_path/'params');entered=threading.Event();release=threading.Event();reader=task.reader
    def blocked(*args):entered.set();assert release.wait(2);return reader(*args)
    task.reader=blocked;task.read(selections);assert entered.wait(1)
    ota,sel,writes,_=make_task(tmp_path/'ota')
    try:
        p=ota.preview(sel);ota.start(p['preview_id'],True);ota._thread.join(2)
        assert ota.snapshot()['status']=='failed' and not writes
    finally:
        release.set();finish(task);ota.cancel()


def test_read_reserves_no_enable_and_cancel_waits_for_worker(tmp_path):
    task,selections,*_=make(tmp_path);entered=threading.Event();release=threading.Event();reader=task.reader
    class Reservation:
        opened=False;closed=False
        def __enter__(self):self.opened=True;return self
        def close(self):self.closed=True
    resource=Reservation();task.reserve=lambda:resource
    def delayed(*args):entered.set();assert release.wait(2);return reader(*args)
    task.reader=delayed;task.read(selections);assert entered.wait(1)
    with pytest.raises(TimeoutError):task.cancel(.01)
    assert task.active and resource.opened and not resource.closed
    release.set();task.cancel();assert not task.active and resource.closed

def test_partial_power_ack_is_audit_only_no_replay(tmp_path):
    task,selections,writes,*_=make(tmp_path);p=preview(task,selections)
    def broken(row,item,stop):writes.append(item['key']);raise OSError('uncertain synthetic send')
    task.writer=broken;task.start(p['preview_id'],True);j=finish(task)
    ack=task.acknowledge(j['job_id'])
    assert ack['status']=='failed' and not ack['targets'][0]['post_power_verified']
    assert ack['targets'][0]['human_acknowledged_at'] and writes==['kt']
    with pytest.raises(ValueError):task.readback(j['job_id'])

def test_restart_preserves_unexecuted_targets_and_uncertain_sends(tmp_path):
    task,selections,writes,*_=make(tmp_path,2);p=preview(task,selections)
    # Construct the durable pre-send boundary of a crashed first item. No
    # device operation is performed; the engine must never replay this record.
    rows=deepcopy(p['targets']);rows[0]['status']='writing';rows[0]['power_cycle_required']=True
    rows[0]['items'][0].update(status='writing',write_status='sending')
    job=dict(job_id='c'*32,actor={'username':'executor','client_session':'synthetic','role':'TEST_EXECUTOR'},mode='write',active=True,status='writing',targets=rows)
    (tmp_path/('c'*32+'.json')).write_text(json.dumps(job))
    recovered,*_=make(tmp_path)
    j=recovered.snapshot()
    assert j['status']=='interrupted' and not j['active']
    assert j['targets'][1]['status']=='not_run'
    assert j['targets'][0]['items'][0]['write_status']=='send_uncertain_unverified'
    assert j['summary']['send_uncertain_unverified']==1 and j['summary']['not_written']==3
    assert not writes
