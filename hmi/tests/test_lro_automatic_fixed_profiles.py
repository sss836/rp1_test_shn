"""Automatic fixed-table selection; fake motor I/O only."""
import math
from dataclasses import replace
from unittest.mock import Mock
import pytest
from factory_hmi.core.backend import MotorSafetyError
from factory_hmi.core.lro_fixed_profiles import fixed_profile
from scripts.log_motor_telemetry import sample_row,csv_fieldnames
from test_lro_runtime_profiles import open_backend,OfflineLro

@pytest.mark.parametrize('versions',[(108,)*7,(109,)*7,(108,109,108,109,108,109,108)])
def test_all_old_all_new_and_mixed_without_manual_profile(open_backend,versions):
    motors=[OfflineLro(None if v==108 else 'V5.0.0.109',(0,1,v-100,0,1,0,0,0,1,17)) for v in versions]
    backend=open_backend(*[(m,None) for m in motors])
    # Four supported model IDs use the same version resolver; native per-model
    # encoding tables remain unchanged and have separate wire-level coverage.
    backend.entries=[replace(e,motor_model=i%4) for i,e in enumerate(backend.entries)]
    backend.prepare_motor_protocols()
    assert not any(m.enables for m in motors)
    expected=['legacy' if v==108 else 'v1.03' for v in versions]
    assert backend._selected_protocols==expected
    backend.enable_motors()
    assert all(m.enables==1 for m in motors)
    samples=backend.read_samples()
    for v,s in zip(versions,samples):
        if v==109:assert s.drive_enabled is True and s.iq_current_a==10 and math.isnan(s.torque_nm)
        else:assert s.drive_enabled is None and s.torque_nm==3
    row=sample_row(backend.entries,samples,elapsed_s=0,sample=1)
    assert set(row)==set(csv_fieldnames(backend.entries))
    assert all(f'{s.joint_name}_iq_current_a' in row for s in samples)

@pytest.mark.parametrize('label,numeric,expected',[
    ('108',(0,1,8,0,1,0,0,0,1,17),'legacy'),
    ('V5.0.0.109',(0,1,9,0,1,0,0,0,1,17),'v1.03'),
    (None,(0,1,8,0,1,0,0,0,1,17),'legacy'),
    (None,(0,1,9,0,1,0,0,0,1,17),'v1.03'),
])
def test_aliases_select_existing_table(open_backend,label,numeric,expected):
    m=OfflineLro(label,numeric);b=open_backend((m,None));b.prepare_motor_protocols()
    assert b._selected_protocols==[expected]
    assert b.motor_versions()[0]['configured_profile']=='auto (108/109)'

@pytest.mark.parametrize('label,numeric',[
    ('V5.0.0.109',(0,1,8,0,1,0,0,0,1,17)),
    ('108',(0,1,9,0,1,0,0,0,1,17)),
    ('110',(0,1,10,0,1,0,0,0,1,17)),
    (None,(0,1,9,0,1,1,0,0,1,17)),
])
def test_unknown_or_conflicting_version_does_not_enable(open_backend,label,numeric):
    m=OfflineLro(label,numeric);b=open_backend((m,None))
    with pytest.raises(MotorSafetyError):b.enable_motors()
    assert m.enables==0

def test_auto_109_keeps_disabled_feedback_requirement(open_backend):
    m=OfflineLro('V5.0.0.109',(0,1,9,0,1,0,0,0,1,17));b=open_backend((m,None))
    b.prepare_motor_protocols();m.read_only_status_responds=False
    with pytest.raises(MotorSafetyError,match='失能反馈'):b.enable_motors()
    assert m.enables==0

def test_auto_109_zero_uses_109_path(open_backend):
    m=OfflineLro('V5.0.0.109',(0,1,9,0,1,0,0,0,1,17));b=open_backend((m,None))
    b.prepare_motor_protocols();b.refresh_feedback()
    result=b.submit_motor_zero(0)
    assert m.zero_requests==1 and m.enables==0
