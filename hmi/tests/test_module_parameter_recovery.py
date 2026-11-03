"""Restart recovery keeps every synthetic journal truthful; never sends a frame."""
from copy import deepcopy
import json

import pytest

from test_module_parameters import make, preview


@pytest.mark.parametrize('mode', ['read', 'write'])
def test_restart_recovers_each_inflight_parameter_journal(tmp_path, mode):
    task, selections, writes, *_ = make(tmp_path)
    p = preview(task, selections)
    original_history = {path.name: path.read_bytes() for path in tmp_path.glob('*.json')}
    inflight = []
    for index in range(2):
        rows = deepcopy(p['targets'])
        rows[0]['status'] = 'reading' if mode == 'read' else 'writing'
        if mode == 'write':
            rows[0]['power_cycle_required'] = True
            rows[0]['items'][0].update(status='writing', write_status='sending')
        job = dict(job_id=str(index + 1) * 32, mode=mode, actor=deepcopy(p['actor']),
                   active=True, status=rows[0]['status'], targets=rows)
        path = tmp_path / (job['job_id'] + '.json')
        path.write_text(json.dumps(job))
        inflight.append(path)

    recovered, *_ = make(tmp_path)
    try:
        assert not recovered.active and not writes
        assert recovered._thread is None
        for path in inflight:
            job = json.loads(path.read_text())
            assert job['active'] is False and job['status'] == 'interrupted'
            row = job['targets'][0]
            assert row['status'] == 'interrupted_unverified'
            first, second = row['items']
            assert first['status'] == ('interrupted_unverified' if mode == 'write' else 'not_run')
            assert first['write_status'] == ('send_uncertain_unverified' if mode == 'write' else 'not_written')
            assert second['status'] == 'not_run' and second['write_status'] == 'not_written'
            assert job['summary'] == dict(not_written=1 if mode == 'write' else 2,
                                         send_uncertain_unverified=1 if mode == 'write' else 0,
                                         written_unverified=0, written_verified=0)
        assert all((tmp_path / name).read_bytes() == data for name, data in original_history.items())
    finally:
        recovered.cancel()
