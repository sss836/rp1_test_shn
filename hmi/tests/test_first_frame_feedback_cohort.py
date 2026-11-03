"""Synthetic clock and asynchronous cached feedback; no devices or sockets."""
import threading
from types import SimpleNamespace
import numpy as np
import pytest
from factory_hmi.models import MotorSample
from factory_hmi.core.first_frame_approach import FirstFrameApproach
from factory_hmi.core.playback import PlaybackController, PlaybackError


def simulate(monkeypatch, *, frozen=None, bad_position=None, bad_speed=None):
    clock={'now':100.,'read':0};count=7
    stamps=np.full(count,99.95)
    plan=FirstFrameApproach(np.full(count,.1),np.zeros(count),.01,.03,.03,.2,.5,np.ones(count))
    commands=[]
    def read(*args,**kwargs):
        index=clock['read']%count;clock['read']+=1
        if index!=frozen:stamps[index]=clock['now']
        return [MotorSample(index=i,motor_id=i+1,joint_name=f'joint-{i}',
                pos_rad=.1 if i==bad_position else 0.,spd_rad_s=.1 if i==bad_speed else 0.,
                timestamp_s=stamp,feedback_age_s=clock['now']-stamp) for i,stamp in enumerate(stamps)]
    engine=SimpleNamespace(_approach_plan=plan,config=SimpleNamespace(entries=list(range(count)),control_rate_hz=200),
        _stop_event=threading.Event(),_finish_event=threading.Event(),_recorder=None,_lock=threading.RLock(),
        _completion={},_completion_evidence={},_dispatch=lambda *args:commands.append(args[0].copy()),
        _sample_feedback=read,_tick=lambda next_tick,period:clock.update(now=next_tick))
    monkeypatch.setattr('factory_hmi.core.playback.time.monotonic',lambda:clock['now'])
    return engine,commands


def test_each_joint_can_advance_on_a_different_control_tick(monkeypatch):
    engine,commands=simulate(monkeypatch)
    assert PlaybackController._approach_first_frame(engine) is True
    assert engine._completion['approach_completed']
    evidence=engine._completion_evidence['first_frame_approach']
    assert evidence['elapsed_s']>=.2 and len(evidence['arrival_feedback'])==7
    assert commands and all(np.all(np.isfinite(command)) for command in commands)


def test_one_frozen_joint_never_completes_arrival(monkeypatch):
    engine,_=simulate(monkeypatch,frozen=6)
    with pytest.raises(PlaybackError):PlaybackController._approach_first_frame(engine)
    assert not engine._completion.get('approach_completed')


@pytest.mark.parametrize('kind',['bad_position','bad_speed'])
def test_arrival_tolerances_still_block_start_and_report_actual_reason(monkeypatch,kind):
    engine,_=simulate(monkeypatch,**{kind:2})
    with pytest.raises(PlaybackError) as error:PlaybackController._approach_first_frame(engine)
    evidence=engine._completion_evidence['first_frame_approach']
    assert evidence['outcome']=='timeout' and len(evidence['last_feedback'])==7
    assert not evidence['within_position' if kind=='bad_position' else 'within_speed'][2]
    assert '位置' in str(error.value) if kind=='bad_position' else '速度' in str(error.value)
    assert not engine._completion.get('approach_completed')
