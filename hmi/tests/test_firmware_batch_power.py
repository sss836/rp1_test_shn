"""Batch completion, one power-cycle action, no hardware access."""
from copy import deepcopy
import json
import threading
import pytest
from unittest.mock import Mock
from factory_hmi.core.motor_maintenance import ModuleFirmwareMaintenance
from factory_hmi.client import GatewayClient
from test_module_firmware_maintenance import make_task, begin, wait, observation


def test_seven_motors_write_before_one_power_request_and_one_click_verifies_all(tmp_path):
    task,selections,written,_=make_task(tmp_path,n=7)
    before_next=threading.Event();continue_writes=threading.Event();old_writer=task.writer
    def writer(row,*args):
        if row['index']==1:
            before_next.set()
            assert continue_writes.wait(2)
        return old_writer(row,*args)
    task.writer=writer
    try:
        job=begin(task,selections);assert before_next.wait(1)
        current=task.snapshot()
        assert written==[0] and current['targets'][0]['status']=='written_awaiting_batch_power'
        assert current['status']=='writing'
        with pytest.raises(ValueError):task.confirm_power_and_readback(job['job_id'],True)
        continue_writes.set();pending=wait(task,'awaiting_power')
        assert written==list(range(7)) and not any(r['actual_version_verified'] for r in pending['targets'])
        assert all(r['write_receipt'] and r['write_finished_at'] for r in pending['targets'])
        task.confirm_power_and_readback(job['job_id'],True)
        done=wait(task,'verified')
        assert all(r['human_acknowledged_at'] and r['readback_at'] and r['actual_version_verified'] for r in done['targets'])
        assert len({r['human_acknowledged_at'] for r in done['targets']})==1
        assert task.confirm_power_and_readback(job['job_id'],True)['status']=='verified'
        assert written==list(range(7))
    finally:continue_writes.set();task.cancel()


@pytest.mark.parametrize('fault',['failed-write','unknown-version','mismatch-version'])
def test_partial_outcomes_distinguish_written_unverified_and_not_run(tmp_path,fault):
    task,selections,written,_=make_task(tmp_path,n=3)
    writer,reader=task.writer,task.reader
    reads=[]
    def write(row,*args):
        if fault=='failed-write' and row['index']==1:raise OSError('synthetic write error')
        return writer(row,*args)
    def read(row,stop):
        value=reader(row,stop)
        if row['status']=='verifying':
            reads.append(row['index'])
            if row['index']==0 and fault=='unknown-version':value['status']='timeout'
            if row['index']==0 and fault=='mismatch-version':value['raw_firmware']='still-old'
        return value
    task.writer,task.reader=write,read
    try:
        job=begin(task,selections)
        if fault=='failed-write':
            done=wait(task,'failed')
            assert written==[0] and [r['status'] for r in done['targets']]==['written_unverified','failed','not_run']
            assert not done.get('power_confirmed_readback_at')
        else:
            wait(task,'awaiting_power');task.confirm_power_and_readback(job['job_id'],True)
            done=wait(task,'version_unknown' if fault=='unknown-version' else 'failed')
            assert reads==[0,1,2] and written==[0,1,2]
            assert [r['actual_version_verified'] for r in done['targets']]==[False,True,True]
    finally:task.cancel()


def test_confirmation_journal_failure_never_wakes_worker_and_retry_is_valid(tmp_path):
    task,selections,written,_=make_task(tmp_path)
    save=task._save
    try:
        job=begin(task,selections);wait(task,'awaiting_power')
        def fail():raise OSError('synthetic disk failure')
        task._save=fail
        with pytest.raises(OSError):task.confirm_power_and_readback(job['job_id'],True)
        assert not task._read.is_set() and task.snapshot()['status']=='awaiting_power'
        assert all(not r['human_acknowledged_at'] for r in task.snapshot()['targets'])
        task._save=save;task.confirm_power_and_readback(job['job_id'],True);wait(task,'verified')
    finally:task._save=save;task.cancel()


def test_confirm_keeps_permission_boundary_and_never_uses_old_job(tmp_path):
    task,selections,written,_=make_task(tmp_path)
    try:
        job=begin(task,selections);wait(task,'awaiting_power')
        for job_id,confirmed in [('wrong-job',True),(job['job_id'],False)]:
            with pytest.raises(ValueError):task.confirm_power_and_readback(job_id,confirmed)
        with pytest.raises(ValueError):task.confirm_power_and_readback(job['job_id'],True,local=True)
        assert not task._read.is_set()
    finally:task.cancel()


def test_restart_between_writes_never_replays_or_marks_success(tmp_path):
    task,selections,*_=make_task(tmp_path)
    task._job={'job_id':'interrupted','active':True,'status':'writing','targets':[{'status':'written_awaiting_batch_power','actual_version_verified':False},{'status':'pending','actual_version_verified':False}]}
    task._save()
    recovered=ModuleFirmwareMaintenance(tmp_path,lambda:[],lambda:[],lambda:{},None,None,None,None)
    assert [r['status'] for r in recovered.snapshot()['targets']]==['interrupted_unverified','not_run']
    assert not recovered.active


def test_client_accepts_combined_action_and_preserves_local_flag():
    client=GatewayClient('http://127.0.0.1:1');client._request=Mock(return_value={})
    client.motor_maintenance_action('confirm-power-and-readback',{'job_id':'synthetic','confirmed':True},local=True)
    client._request.assert_called_once_with('POST','/api/v1/motor-maintenance/confirm-power-and-readback?local=true',payload={'job_id':'synthetic','confirmed':True})
