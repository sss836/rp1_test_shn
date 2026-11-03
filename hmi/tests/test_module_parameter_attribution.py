"""Bounded synthetic audit regression: errors belong to the current target."""
import json
import pytest
from test_module_parameters import make,preview,finish
from test_module_firmware_maintenance import wait,observation

@pytest.mark.parametrize('failure',['version','authorization'])
def test_second_target_preflight_failure_keeps_first_verified_receipt(tmp_path,failure):
    task,selections,writes,*_=make(tmp_path,2)
    p=preview(task,selections)
    original_authorize=task.authorize
    error='SECOND_TARGET_'+failure.upper()+'_UNAVAILABLE'
    def versions(row,stop):
        if failure=='version' and row['index']==1:raise ValueError(error)
        return observation(row['index'])
    def authorize():
        if failure=='authorization' and task._job and task._job.get('mode')=='write' and task._job['targets'][0]['status']=='verified':
            raise PermissionError(error)
        return original_authorize()
    task.version_reader=versions;task.authorize=authorize
    try:
        task.start(p['preview_id'],True)
        job=wait(task,'awaiting_power')
        task.acknowledge(job['job_id']);task.readback(job['job_id'])
        job=finish(task)
        first,second=job['targets']
        assert job['status']=='failed' and job['error']==error
        assert writes==[(0,'kt'),(0,'kp')]
        assert first['status']=='verified' and first['post_power_verified'] is True
        assert 'error' not in first
        assert all('error' not in item for item in first['items']),first['items']
        assert all(item['post_power_status']=='passed' and item['write_status']=='written_verified' for item in first['items'])
        assert second['status']=='failed' and second['error']==error
        assert second['power_cycle_required'] is False and second['post_power_verified'] is False
        assert all(item['write_status']=='not_written' and item['status']=='not_run' and 'error' not in item for item in second['items'])
        persisted=json.loads((task.root/(job['job_id']+'.json')).read_text())
        assert persisted['targets']==job['targets']
        assert persisted['summary']==dict(not_written=2,send_uncertain_unverified=0,written_unverified=0,written_verified=2)
    finally:
        task.cancel()
