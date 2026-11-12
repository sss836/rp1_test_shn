"""Synthetic module OTA tests; no SocketCAN socket or device is created."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from factory_hmi.core.motor_maintenance import ModuleFirmwareMaintenance, effective_targets
from factory_hmi.core.config import load_station_config, build_station_config
from factory_hmi.gateway.motor_maintenance import artifact, authorize
from factory_hmi.core.motor_ota_transport import protocol, write_firmware


def observation(index=0, **changes):
    return dict(index=index, interface="can-fixture", motor_id=index+1, model="LRO_5550", status="known",
        raw_firmware="1.0.8", software="1.0.8", hardware="1.0.2", protocol="1.0.0.0",
        observed_at=datetime.now(timezone.utc).isoformat(), **changes)


def target(index=0):
    return dict(index=index, module="fixture", joint_name=f"joint-{index}", interface="can-fixture",
                motor_id=index+1, model="LRO_5550", ota_request_id=0x1001+2*index, ota_response_id=0x1002+2*index)


@pytest.fixture
def signed(tmp_path, monkeypatch):
    def run(*args): return subprocess.run(list(map(str,args)),check=True,capture_output=True)
    private, public = tmp_path/'private.pem', tmp_path/'public.pem'
    run('openssl','genpkey','-algorithm','ED25519','-out',private)
    run('openssl','pkey','-in',private,'-pubout','-out',public)
    public.chmod(0o644)
    def sign(value):
        data = tmp_path/'to-sign.json'
        data.write_bytes(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
        return base64.b64encode(run('openssl','pkeyutl','-sign','-inkey',private,'-rawin','-in',data).stdout).decode()
    content=b'SYNTHETIC-NEVER-FLASH'*30
    manifest=dict(schema_version='rp1-motor-firmware-v1',artifact_type='motor_firmware',release_id='a'*32,
        version='1.0.10',software_version='1.0.10',vendor='LRO',model='LRO_5550',interface_type='canfd',
        hardware_versions=['1.0.2'],protocol_versions=['1.0.0.0'],from_versions=['1.0.8'],filename='fixture.lsepbin',
        size_bytes=len(content),sha256=hashlib.sha256(content).hexdigest(),source_url='https://fixture.invalid/firmware',
        scope='synthetic-only',notes='not a vendor firmware',published_at='2026-10-30T01:00:00+08:00')
    root=tmp_path/'cache'/'releases'/('a'*32);root.mkdir(parents=True)
    (root/'release.json').write_text(json.dumps(dict(manifest=manifest,signature=sign(manifest))))
    (root/manifest['filename']).write_bytes(content)
    policy=dict(schema='rp1-lro-ota-compatibility-v1',entries=[dict(model='LRO_5550',hardware='1.0.2',protocol='1.0.0.0',
        raw_version='1.0.8',software_version='1.0.8',ota_protocol='lro-canfd-xcp-v1',
        evidence_url='https://fixture.invalid/mapping',evidence_sha256='b'*64)])
    policy_path=tmp_path/'policy.json'
    def save_policy(): policy_path.write_text(json.dumps(dict(policy=policy,signature=sign(policy))))
    save_policy();monkeypatch.setenv('RP1_MOTOR_OTA_POLICY',str(policy_path))
    downloads=SimpleNamespace(cache_root=tmp_path/'cache',key=lambda: public)
    return downloads, manifest, content, policy, save_policy, root


def wait(task, status):
    end=time.monotonic()+3
    while time.monotonic()<end:
        value=task.snapshot()
        if value['status']==status: return value
        if not value['active']: break
        time.sleep(.005)
    raise AssertionError(task.snapshot())


def make_task(tmp_path, n=2, *, reader=None, writer=None, auth=None, artifacts=None):
    written=[]
    targets=[target(i) for i in range(n)]
    def default_reader(row, stop):
        value=observation(row['index'])
        if row['index'] in written: value.update(raw_firmware='1.0.10',software='1.0.10')
        return value
    def default_writer(row,data,stop,progress):
        written.append(row['index']); progress(len(data),len(data))
        return dict(selfcheck_response='ff0201',reset_sent=False)
    def default_artifact(release_id, row):
        if row['status']!='known': raise ValueError('unknown version')
        return dict(manifest=dict(version='1.0.10',software_version='1.0.10',sha256='c'*64),policy={'source':'fixture'},data=b'fixture')
    task=ModuleFirmwareMaintenance(tmp_path,lambda: targets,lambda:[observation(i) for i in range(n)],
        auth or (lambda:{'username':'executor'}),artifacts or default_artifact,lambda:nullcontext(),writer or default_writer,reader or default_reader)
    selections=[dict(index=i,release_id='a'*32) for i in range(n)]
    return task, selections, written, targets


def begin(task, selections):
    preview=task.preview(selections)
    return task.start(preview['preview_id'],True)


def test_local_firmware_update_and_readback_need_no_platform(tmp_path):
    def reject_platform():
        raise AssertionError("offline task must not call the platform")

    task, selections, written, _targets = make_task(tmp_path, auth=reject_platform)
    task.authorize_local = lambda: {"username": "local-operator", "lease_generation": 7}
    preview = task.preview(selections[:1], local=True)
    with pytest.raises(ValueError, match="来源"):
        task.start(preview["preview_id"], True)
    job = task.start(preview["preview_id"], True, local=True)
    wait(task, "awaiting_power")
    with pytest.raises(ValueError, match="不匹配"):
        task.acknowledge(job["job_id"])
    task.acknowledge(job["job_id"], local=True)
    task.readback(job["job_id"], local=True)
    verified = wait(task, "verified")
    assert written == [0]
    assert verified["local"] is True and verified["targets"][0]["actual_version_verified"]


def test_signed_local_firmware_import_installs_verified_policy(signed, tmp_path, monkeypatch):
    from factory_hmi.gateway.motor_firmware import MotorFirmwareDownloads
    from factory_hmi.gateway.motor_maintenance import cached_releases

    original, manifest, content, _policy, _save, source = signed
    (source / "motor-ota-policy.json").write_bytes((tmp_path / "policy.json").read_bytes())
    local = tmp_path / "state"
    (local / "config").mkdir(parents=True)
    monkeypatch.setenv("RP1_HMI_STATE_ROOT", str(local))
    monkeypatch.setenv("RP1_MOTOR_FIRMWARE_PUBLIC_KEY", str(original.key()))
    monkeypatch.delenv("RP1_MOTOR_OTA_POLICY")
    downloads = MotorFirmwareDownloads(None, local / "data")

    imported = downloads.import_signed_release(source / "release.json", observation())
    assert imported["policy_imported"] and not imported["installed"]
    assert cached_releases(downloads, observation(), verify_bytes=True) == [manifest]
    assert artifact(downloads, manifest["release_id"], observation())["data"] == content


def test_signed_cache_policy_and_hash(signed):
    downloads, manifest, data, policy, save, root=signed
    result=artifact(downloads,'a'*32,observation())
    assert result['data']==data and result['manifest']==manifest
    assert result['policy']['entry']['software_version']=='1.0.8'
    (root/manifest['filename']).write_bytes(b'!<arch>\n')
    with pytest.raises(ValueError,match='SHA256'): artifact(downloads,'a'*32,observation())


@pytest.mark.parametrize('field,value', [('raw_version','108'),('software_version','1.0.7'),('evidence_sha256',''),('ota_protocol','unknown')])
def test_unverified_mapping_or_108_is_not_guessed(signed,field,value):
    downloads,m,data,policy,save,root=signed
    policy['entries'][0][field]=value;save()
    with pytest.raises(ValueError): artifact(downloads,'a'*32,observation())


def test_tamper_missing_policy_wrong_target_and_stale(signed,monkeypatch):
    downloads,m,data,policy,save,root=signed
    for changes in ({'model':'LRO_6562'},{'status':'stale'},{'observed_at':'2000-01-01T00:00:00+00:00'}):
        row=observation();row.update(changes)
        with pytest.raises(ValueError):artifact(downloads,'a'*32,row)
    monkeypatch.delenv('RP1_MOTOR_OTA_POLICY')
    with pytest.raises(ValueError,match='缺少'):artifact(downloads,'a'*32,observation())
    monkeypatch.setenv('RP1_MOTOR_OTA_POLICY',str(root.parents[2]/'policy.json'))
    envelope=json.loads((root.parents[2]/'policy.json').read_text());envelope['policy']['entries'][0]['software_version']='1.0.99'
    (root.parents[2]/'policy.json').write_text(json.dumps(envelope))
    with pytest.raises(ValueError,match='签名'):artifact(downloads,'a'*32,observation())


@pytest.mark.parametrize('role,kind,session,allowed',[('TEST_EXECUTOR','HUMAN','BROWSER',True),('SYSTEM_ADMIN','HUMAN','BROWSER',True),('VIEWER','HUMAN','BROWSER',False),('SYSTEM_ADMIN','SERVICE','SERVICE_API',False)])
def test_actual_session_role_checks(role,kind,session,allowed):
    client=Mock();client._call.return_value=dict(session_type=session,user=dict(username='executor',role=role,kind=kind,enabled=True,must_change_password=False))
    downloads=SimpleNamespace(sessions=SimpleNamespace(_session=lambda:(client,'executor')))
    if allowed: assert authorize(downloads)['role']==role
    else:
        with pytest.raises(ValueError):authorize(downloads)


def test_final_config_overrides_nested_and_duplicate_rejection(tmp_path):
    config=load_station_config('left_short_arm')
    bindings=[dict(index=0,interface='can-fixture',mode='canfd')]
    overrides=[dict(index=0,motor_id=77,motor_type='LRO',motor_model=2)]
    flat=build_station_config(config.raw,config.limb,bus_bindings=bindings,motor_overrides=overrides)
    nested=build_station_config({'motors':config.raw},config.limb,bus_bindings=bindings,motor_overrides=overrides)
    rows=effective_targets(flat)
    assert rows==effective_targets(nested)
    assert rows[0]['motor_id']==77 and rows[0]['model']=='LRO_8462' and rows[0]['interface']=='can-fixture'
    assert rows[0]['ota_request_id']==0x1000+2*77-1
    duplicate=replace(flat,entries=(flat.entries[0],replace(flat.entries[0],index=1)))
    with pytest.raises(ValueError,match='重复'):effective_targets(duplicate)


def test_serial_batch_write_one_human_ack_and_per_target_receipts(tmp_path):
    task,selections,written,targets=make_task(tmp_path)
    try:
        job=begin(task,selections);pending=wait(task,'awaiting_power')
        assert written==[0,1] and all(r['status']=='awaiting_power' for r in pending['targets'])
        with pytest.raises(ValueError):task.readback(job['job_id'])
        task.acknowledge(job['job_id'])
        assert all(r['human_acknowledged_at'] and not r['actual_version_verified'] for r in task.snapshot()['targets'])
        task.readback(job['job_id'])
        done=wait(task,'verified')
        assert all(r['actual_version_verified'] and r['human_acknowledged_at'] and r['readback_at'] for r in done['targets'])
        assert len({r['human_acknowledged_at'] for r in done['targets']})==1
        assert json.loads((tmp_path/(job['job_id']+'.json')).read_text())['status']=='verified'
    finally:task.cancel()


@pytest.mark.parametrize('mode',['unknown-before','unknown-after','mismatch','write-failed','check-03'])
def test_failure_unknown_mismatch_stop_later_targets(tmp_path,mode):
    calls=[]
    def reader(row,stop):
        calls.append(('read',row['index']))
        value=observation(row['index'])
        if mode=='unknown-before' or mode=='unknown-after' and row['status']=='verifying':value['status']='timeout'
        return value
    def writer(row,data,stop,progress):
        calls.append(('write',row['index']))
        if mode=='write-failed':raise OSError('synthetic interruption')
        return dict(selfcheck_response='ff0203' if mode=='check-03' else 'ff0201')
    task,selections,_,_=make_task(tmp_path,reader=reader,writer=writer)
    try:
        job=begin(task,selections)
        if mode in {'unknown-after','mismatch'}:
            wait(task,'awaiting_power');task.acknowledge(job['job_id']);task.readback(job['job_id'])
        result=wait(task,'version_unknown' if mode.startswith('unknown') else 'failed')
        if mode in {'unknown-after','mismatch'}:
            assert [index for action,index in calls if action=='write']==[0,1]
            assert all(r['status'] in {'failed','version_unknown'} for r in result['targets'])
        else:
            assert result['targets'][1]['status']=='not_run'
            assert all(index==0 for action,index in calls)
        assert not any(r['actual_version_verified'] for r in result['targets'])
    finally:task.cancel()


def test_preview_requires_confirm_and_rejects_changes_and_duplicate(tmp_path):
    identity={'name':'one'}
    task,selections,written,targets=make_task(tmp_path,auth=lambda:dict(identity))
    with pytest.raises(ValueError):task.preview([selections[0],selections[0]])
    preview=task.preview(selections)
    with pytest.raises(ValueError):task.start(preview['preview_id'],False)
    targets[0]['motor_id']=99
    with pytest.raises(ValueError,match='配置'):task.start(preview['preview_id'],True)
    assert not written
    targets[0]['motor_id']=1
    preview=task.preview(selections);identity['name']='two'
    with pytest.raises(ValueError,match='账号'):task.start(preview['preview_id'],True)
    assert not written


def test_cancel_shutdown_drain_and_restart_does_not_replay(tmp_path):
    gate=threading.Event()
    def writer(row,data,stop,progress):
        gate.set()
        stop.wait(2)
        raise InterruptedError('synthetic cancel')
    task,selections,_,_=make_task(tmp_path,writer=writer)
    begin(task,selections);assert gate.wait(1)
    value=task.cancel()
    assert not task._thread.is_alive() and value['status']=='interrupted' and value['targets'][1]['status']=='not_run'
    value['active']=True;value['targets'][0]['status']='writing'
    (tmp_path/(value['job_id']+'.json')).write_text(json.dumps(value))
    new,_,written,_=make_task(tmp_path)
    assert new.snapshot()['status']=='interrupted' and new.snapshot()['targets'][0]['status']=='interrupted_unverified' and written==[]


def test_close_timeout_retains_worker_and_prevents_new_task(tmp_path):
    started,gate=threading.Event(),threading.Event()
    def writer(row,data,stop,progress):
        started.set();gate.wait(2)
        raise InterruptedError('synthetic drain')
    task,selections,_,_=make_task(tmp_path,writer=writer)
    begin(task,selections);assert started.wait(1)
    try:
        with pytest.raises(TimeoutError):task.cancel(.01)
        assert task.active
        with pytest.raises(RuntimeError):task.preview(selections)
    finally:gate.set();task.cancel()


def test_global_batch_lock_prevents_cross_bus_parallel_writes(tmp_path):
    one,s1,w1,t1=make_task(tmp_path/'one')
    two,s2,w2,t2=make_task(tmp_path/'two');t2[0]['interface']='other-can'
    # Keep observation aligned with its final target.
    two.observations=lambda:[{**observation(i),'interface':t2[i]['interface']} for i in range(2)]
    try:
        begin(one,s1);wait(one,'awaiting_power')
        begin(two,s2);wait(two,'failed')
        assert w1==[0,1] and w2==[]
    finally:one.cancel();two.cancel()


def test_protocol_adapter_uses_vendor_flash_reset_and_vendor_check():
    p=protocol()
    class Adapter:
        instances=[]
        response=b'\xff\x02\x01'
        def __init__(self, interface):self.commands=[];self.queue=[];self.rx=p.IsoTpReceiver();self.closed=False;self.instances.append(self)
        def open(self):pass
        def close(self):self.closed=True
        def send_frame(self, can_id, data):
            assert can_id==0x1001
            message,flow=self.rx.feed(data)
            if flow:self.queue.append((0x80001002,flow))
            if message:
                self.commands.append(message)
                response=b'\xff\x00\x00\x08' if message[0]==p.CMD_CONNECT else (self.response if message[0]==p.CMD_USER else b'\xff')
                # Wrong standard/RTR/error/target traffic must be ignored.
                for wrong in (0x1002,0xc0001002,0xa0001002,0x80001004):self.queue.append((wrong,p.encode_sf(b'\xfe\x31')))
                self.queue.append((0x80001002,p.encode_sf(response)))
        def recv_frame(self):return self.queue.pop(0) if self.queue else None
    stop=threading.Event();progress=[]
    receipt=write_firmware(target(),b'X'*600,stop,lambda a,b:progress.append((a,b)),adapter_factory=Adapter)
    adapter=Adapter.instances[-1]
    assert receipt['bytes_written']==600 and receipt['reset_sent'] and adapter.closed
    assert progress==[(256,600),(512,600),(600,600)]
    codes=[command[0] for command in adapter.commands]
    assert codes.count(p.CMD_CONNECT)==1 and codes[-1]==p.CMD_PROGRAM_RESET
    Adapter.response=b'\xff\x02\x03'
    with pytest.raises(p.VerifyError,match='自校验'):write_firmware(target(),b'X',stop,lambda *a:None,adapter_factory=Adapter)
    assert Adapter.instances[-1].closed
    assert p.CMD_PROGRAM_RESET not in [c[0] for c in Adapter.instances[-1].commands]


def test_selected_subset_only_and_no_empty_target_pass(tmp_path):
    task,selections,written,_=make_task(tmp_path)
    with pytest.raises(ValueError):task.preview([])
    try:
        job=begin(task,selections[1:]);wait(task,'awaiting_power')
        assert written==[1] and len(task.snapshot()['targets'])==1
        task.acknowledge(job['job_id']);task.readback(job['job_id']);wait(task,'verified')
    finally:task.cancel()


def test_backend_bus_owner_retained_and_control_open_rejected_during_maintenance():
    from factory_hmi.core.backend import FakeMotorBackend, BackendInUseError, MotorSafetyError
    class ExclusiveFake(FakeMotorBackend):
        def _exclusive_resources(self):return ('isolated-ota-bus',)
    one,two=ExclusiveFake(1),ExclusiveFake(1)
    one.open_transport()
    with one.maintenance_transport():
        assert not one.connected
        with pytest.raises(BackendInUseError):one.open_transport()
        with pytest.raises(BackendInUseError):two.open_transport()
        with pytest.raises(BackendInUseError):one.close_transport()
    assert not one.connected # no automatic enable or reconnect
    two.open_transport();two.enable_motors()
    with pytest.raises(MotorSafetyError):
        with two.maintenance_transport():pytest.fail('enabled motor accepted')
    two.disconnect();one.disconnect()


def test_service_maintenance_blocks_config_enable_and_drains_on_close(tmp_path):
    from factory_hmi.core.service import FactoryService
    service=FactoryService(data_root=tmp_path/'service')
    task,selections,written,_=make_task(tmp_path/'task')
    service._maintenance=task
    begin(task,selections);wait(task,'awaiting_power')
    for operation in (lambda:service.configure(config_path='unused',limb='left_arm',backend='fake'),service.enable_motors,service.clear_configuration,service.reset_motor_fault):
        with pytest.raises(RuntimeError,match='维护占用'):operation()
    assert service.close()['closed']
    assert not task.active and not task._thread.is_alive()
    assert task.snapshot()['targets'][0]['status']=='written_unverified'
    assert task.snapshot()['targets'][1]['status']=='interrupted'


def test_read_only_version_transport_correlates_and_never_enables(monkeypatch):
    import struct
    from factory_hmi.core import motor_ota_transport as t
    frame=struct.Struct('<IBB2x64s')
    class Socket:
        def __init__(self,*args):self.queue=[];self.sent=[]
        def __enter__(self):return self
        def __exit__(self,*args):self.closed=True
        def setsockopt(self,*args):pass
        def bind(self,*args):pass
        def setblocking(self,*args):pass
        def send(self,raw):
            can_id,length,flags,data=frame.unpack(raw);self.sent.append(data[:length])
            assert can_id==1 and data[:2] in (b'\xe0\x31',b'\xe0\x1e')
            response=b'\xa0\x31'+b'1.0.8'.ljust(18,b'\0') if data[1]==0x31 else bytes.fromhex('a01e01000801000201000000')
            self.queue.extend([frame.pack(0x80000001,len(response),1,response),frame.pack(2,len(response),1,response),frame.pack(1,len(response),1,response)])
        def recv(self,*args):
            if not self.queue:raise BlockingIOError
            return self.queue.pop(0)
    bus=Socket();monkeypatch.setattr(t.select,'select',lambda *a:([bus],[],[]))
    row=t.read_version(target(),threading.Event(),socket_factory=lambda *a:bus)
    assert row['status']=='known' and row['raw_firmware']=='1.0.8' and row['hardware']=='1.0.2' and bus.closed
    assert bus.sent==[b'\xe0\x31',b'\xe0\x1e']


def test_duplicate_readback_does_not_trigger_second_batch(tmp_path):
    task,selections,written,_=make_task(tmp_path)
    try:
        job=begin(task,selections);wait(task,'awaiting_power');task.acknowledge(job['job_id'])
        task.readback(job['job_id'])
        with pytest.raises(ValueError):task.readback(job['job_id'])
        wait(task,'verified');assert written==[0,1]
    finally:task.cancel()


def test_backend_failed_close_quarantines_until_normal_disconnect():
    from factory_hmi.core.backend import FakeMotorBackend,BackendInUseError,MotorSafetyError
    class Failing(FakeMotorBackend):
        failed=True
        def _exclusive_resources(self):return ('isolated-close-quarantine',)
        def _close_transport_impl(self):
            if self.failed:raise OSError('synthetic transport close failure')
            super()._close_transport_impl()
    backend=Failing(1);other=Failing(1);backend.open_transport()
    try:
        with pytest.raises(OSError):
            with backend.maintenance_transport():pytest.fail('close failed but maintenance started')
        with pytest.raises(BackendInUseError):other.open_transport()
        with pytest.raises(BackendInUseError):backend.open_transport()
        with pytest.raises(MotorSafetyError):backend.enable_motors()
    finally:
        backend.failed=False;backend.close_transport()
    other.failed=False;other.open_transport();other.close_transport()


def test_journal_start_failure_never_starts_device_or_leaves_active_job(tmp_path,monkeypatch):
    task,selections,written,_=make_task(tmp_path)
    p=task.preview(selections)
    monkeypatch.setattr(task,'_save',Mock(side_effect=OSError('synthetic disk full')))
    with pytest.raises(OSError):task.start(p['preview_id'],True)
    assert not task.active and task._thread is None and written==[]


def test_policy_preparation_signs_real_evidence_envelope(signed,monkeypatch):
    import runpy
    downloads,m,data,policy,save,root=signed
    package_root=Path(__import__('factory_hmi').__file__).resolve().parents[1]
    prepare=runpy.run_path(str(package_root/'tools/prepare_motor_ota_policy.py'))['prepare']
    output=root.parents[2]/'prepared-policy.json'
    prepare(policy,root.parents[2]/'private.pem',output)
    monkeypatch.setenv('RP1_MOTOR_OTA_POLICY',str(output))
    assert artifact(downloads,'a'*32,observation())['policy']['entry']==policy['entries'][0]
    with pytest.raises(FileExistsError):prepare(policy,root.parents[2]/'private.pem',output)


def test_active_lifetime_includes_worker_epilogue_and_prevents_early_reuse(tmp_path):
    task,selections,written,_=make_task(tmp_path)
    original=task._run;epilogue=threading.Event();finish=threading.Event()
    def run():
        original();epilogue.set();finish.wait(2)
    task._run=run
    begin(task,selections);wait(task,'awaiting_power')
    task._stop.set()
    try:
        assert epilogue.wait(1)
        assert task.active and task.snapshot()['active']
        with pytest.raises(RuntimeError):task.preview(selections)
    finally:finish.set();task.cancel()
    assert not task.active and not task._thread.is_alive()


def test_reauthenticated_owner_can_read_interruption_but_cannot_replay(tmp_path):
    from factory_hmi.gateway.motor_maintenance import visible_job
    actor={'username':'executor','client_session':'old'}
    task,selections,_,_=make_task(tmp_path,auth=lambda:dict(actor))
    job=begin(task,selections);wait(task,'awaiting_power');task.cancel()
    actor['client_session']='new'
    assert visible_job(task,actor)['job_id']==job['job_id']
    with pytest.raises(ValueError):task.acknowledge(job['job_id'])
    assert visible_job(task,{'username':'other'})['status']=='other_account'


def test_ota_failure_diagnostics_saved_with_unstarted_following_motors(tmp_path):
    def writer(row, data, stop, progress):
        error = RuntimeError('升级握手失败，未发送擦除或固件数据')
        error.ota_diagnostics = {'stage': 'connect', 'bytes_written': 0, 'rx_frames': 0}
        raise error
    task, selections, _, _ = make_task(tmp_path, writer=writer)
    try:
        job = begin(task, selections)
        wait(task, 'failed')
        saved = json.loads((tmp_path / (job['job_id'] + '.json')).read_text())
        assert saved['targets'][0]['ota_diagnostics']['stage'] == 'connect'
        assert saved['targets'][0]['bytes_written'] == 0
        assert saved['targets'][1]['status'] == 'not_run'
    finally:
        task.cancel()
