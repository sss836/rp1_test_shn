import csv
import math
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from factory_hmi.core.backend import FakeMotorBackend
from factory_hmi.core.motor_versions import MotorVersions
from factory_hmi.core.torque_estimation import decode_kt, estimate, SUPPORTED_MODELS
from factory_hmi.core.playback import _RunStatistics
from factory_hmi.core.telemetry_hub import TelemetryHub
from factory_hmi.core.streaming_metrics import analyze
from factory_hmi.models import MotorSample, StationConfig
from scripts.log_motor_telemetry import csv_fieldnames, sample_row
from test_lro_runtime_profiles import OfflineLro, open_backend, EXPECTED


def entry(model=0, **kwargs):
    return replace(FakeMotorBackend(1).entries[0], motor_type='LRO', motor_model=model,
                   interface='offline0', interface_type='canfd', **kwargs)


@pytest.mark.parametrize('payload,expected', [('a0160134',3.08),('a01600da00000000',2.18),('a01600ae',1.74),('a0160089',1.37)])
def test_production_encoding_matches_actual_readback(payload,expected):
    assert decode_kt(bytes.fromhex(payload)) == expected


@pytest.mark.parametrize('payload',['','a01601','a01700ae','a11600ae','a0160000'])
def test_reject_missing_bad_response_or_zero(payload):
    with pytest.raises(ValueError): decode_kt(bytes.fromhex(payload))


@pytest.mark.parametrize('model,kt', [(0,1.59),(1,2.18),(2,3.08),(3,4.32)])
def test_four_models_use_each_actual_kt_not_template(model,kt):
    e=entry(model)
    row=dict(status='known',interface=e.interface,motor_id=e.motor_id,model=SUPPORTED_MODELS[model],torque_kt_nm_a=kt,kt_monotonic=10)
    assert estimate(e,row,-2.5,now=11)==(-2.5*kt,kt,'device_kt_times_iq')
    for key,value in [('interface','other'),('motor_id',100),('model','wrong'),('torque_kt_nm_a',None),('kt_monotonic',-200)]:
        assert math.isnan(estimate(e,{**row,key:value},2.5,now=11)[0])


class ActualKtMotor(OfflineLro):
    def __init__(self,kt):
        super().__init__('V5.0.0.109', numeric=(0,1,9,0,1,0,0,0,1,17))
        self.kt=kt; self.queries=[]
    def request_version(self,code):
        self.queries.append(code)
        return True if code==0x16 else super().request_version(code)
    def version_payload(self,code):
        if code==0x16: return bytes.fromhex('a016') + self.kt.to_bytes(2,'big')
        return super().version_payload(code)


def wait_kt(backend):
    end=time.monotonic()+2
    while time.monotonic()<end:
        if all(r.get('torque_kt_nm_a') is not None or r.get('raw_firmware')=='108' for r in backend.motor_versions()): return
        time.sleep(.01)
    pytest.fail('Kt worker did not finish')


def test_mixed_backend_real_samples_records_curves_peaks(open_backend,tmp_path):
    old,new=OfflineLro('108',numeric=None),ActualKtMotor(159)
    backend=open_backend((old,None),(new,None))
    wait_kt(backend)
    backend.prepare_motor_protocols()
    backend.refresh_feedback()
    samples=backend.read_samples()
    assert samples[0].torque_nm==3 and not samples[0].torque_is_estimated
    assert samples[1].torque_nm==15.9 and samples[1].iq_current_a==10
    assert samples[1].torque_kt_nm_a==1.59 and samples[1].torque_is_estimated
    assert old.enables==new.enables==0
    hub=TelemetryHub();hub.publish(samples)
    values=hub.read()['frames'][-1]['values'];name=samples[1].joint_name
    assert values[name+'/torque_nm'] is None
    assert values[name+'/estimated_torque_nm']==15.9
    assert values[name+'/iq_current_a']==10
    stats=_RunStatistics(StationConfig(limb='left_arm',entries=backend.entries,raw={}))
    stats.update(samples,np.zeros(2));result=stats.snapshot()['joints']
    assert result[1]['torque_peak_nm']==15.9 and result[1]['torque_is_estimated']
    assert not result[0]['torque_is_estimated']
    row=sample_row(backend.entries,samples,elapsed_s=0,sample=0)
    assert set(row)==set(csv_fieldnames(backend.entries))
    assert math.isnan(row[name+'_torque_nm'])
    assert row[name+'_estimated_torque_nm']==15.9 and row[name+'_torque_kt_nm_a']==1.59
    path=tmp_path/'raw.csv'
    with path.open('w') as f:
        writer=csv.DictWriter(f,fieldnames=csv_fieldnames(backend.entries));writer.writeheader();writer.writerow(row)
    joints=analyze(path,dict(active_seconds=.05,record_rate_hz=20))[-1]
    assert joints[1]['torque_peak_abs_nm']==15.9 and joints[1]['torque_is_estimated']
    assert joints[0]['torque_peak_abs_nm']==3 and not joints[0].get('torque_is_estimated',False)


def test_refresh_and_reconnect_do_not_reuse_old_kt(open_backend):
    m=ActualKtMotor(308);b=open_backend((m,None));wait_kt(b);b.prepare_motor_protocols()
    assert b.read_samples()[0].torque_nm==30.8
    m.kt=231;b.request_motor_version_refresh()
    end=time.monotonic()+2
    while b.motor_versions()[0].get('torque_kt_nm_a')!=2.31 and time.monotonic()<end:time.sleep(.01)
    assert b.read_samples()[0].torque_nm==pytest.approx(23.1)
    b.close_transport();m.kt=0;b.open_transport()
    end=time.monotonic()+2
    while not b.motor_versions()[0].get('kt_error') and time.monotonic()<end:time.sleep(.01)
    b.prepare_motor_protocols();sample=b.read_samples()[0]
    assert math.isnan(sample.torque_nm) and 'Kt' in sample.torque_source
    assert sample.iq_current_a==10


def test_109_factory_summary_and_sealed_curves_keep_estimate_explicit(tmp_path):
    import json
    from factory_hmi.core.report import ReportManager
    from factory_hmi.core.factory_quality import freeze_factory_quality
    from factory_hmi.gateway.local_curves import sealed_raw_curves
    from test_factory_quality import context_for_factory, write_measurements
    ctx=context_for_factory('left_arm');ctx['factory_quality_policy']=freeze_factory_quality(ctx)
    path=tmp_path/'acquired.csv'
    names=[e['joint_name'] for e in ctx['entries']]
    write_measurements(path,joints=names,active_seconds=ctx['active_seconds'])
    with path.open() as f:
        r=csv.DictReader(f);fields=r.fieldnames;rows=list(r)
    for name in names:
        fields.extend([name+'_estimated_torque_nm',name+'_iq_current_a',name+'_torque_kt_nm_a'])
        for row in rows:
            row[name+'_torque_nm']='nan';row[name+'_estimated_torque_nm']=3.08
            row[name+'_iq_current_a']=1;row[name+'_torque_kt_nm_a']=3.08
    with path.open('w') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    before=path.read_bytes();manager=ReportManager(tmp_path/'reports');manager.finalize(path,ctx)
    summary=manager.summary(ctx['test_id'])
    assert path.read_bytes()==before
    assert all(j['torque_is_estimated'] and j['torque_peak_abs_nm']==3.08 for j in summary['joint_metrics'])
    with manager.history_index.connect() as db:
        payload=json.loads(db.execute('SELECT payload FROM runs LIMIT 1').fetchone()[0])
    run_id=payload.get('execution_uuid')
    # Synthetic manager row exposes this run to the same historical query used by the local page.
    if not run_id:
        run_id='offline-estimate';payload['execution_uuid']=run_id
        with manager.history_index.connect() as db:db.execute('UPDATE runs SET payload=?',(json.dumps(payload),))
    metric=names[0]+'/estimated_torque_nm'
    result=sealed_raw_curves(SimpleNamespace(report_manager=manager),run_id,metric,'0','1','归档')
    assert result['series'][0]['id']==metric and result['series'][0]['unit']=='N·m'
    assert any(v==3.08 for _,v in result['series'][0]['points'])
