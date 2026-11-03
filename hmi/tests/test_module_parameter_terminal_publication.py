"""Ordinary/v2-Pn terminal snapshots follow durable journals; synthetic only."""
from copy import deepcopy
import json
import threading

import pytest

from test_module_parameters import make, preview
from test_pn_script_engine import scripted
from test_module_firmware_maintenance import wait


@pytest.mark.parametrize('profile', ['ordinary', 'script-pn'])
@pytest.mark.parametrize('terminal', ['checked', 'checked_with_differences', 'verified', 'failed', 'interrupted'])
def test_parameter_terminal_snapshot_never_overtakes_its_journal(tmp_path, profile, terminal):
    result = scripted(tmp_path) if profile == 'script-pn' else make(tmp_path)
    task, selections, writes, state, _, record = result[:6]
    entered, release = threading.Event(), threading.Event()
    if terminal == 'checked':
        for entry in record['template']['entries']:
            state[0][entry['key']] = deepcopy(entry['expected_raw'])
    prepared = preview(task, selections) if terminal not in {'checked', 'checked_with_differences'} else None
    persist = task._persist_and_release

    def gated_persist(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise TimeoutError('bounded synthetic parameter-publication gate')
        return persist(*args, **kwargs)

    task._persist_and_release = gated_persist
    try:
        if prepared is None:
            job = task.read(selections)
        else:
            if terminal == 'failed':
                def invalid_version(*args):
                    raise ValueError('synthetic version evidence disappeared')
                task.version_reader = invalid_version
            job = task.start(prepared['preview_id'], True)
            if terminal in {'verified', 'interrupted'}:
                wait(task, 'awaiting_power')
                if terminal == 'verified':
                    task.acknowledge(job['job_id'])
                    task.readback(job['job_id'])
                else:
                    task._stop.set()
        assert entered.wait(1)
        visible = task.snapshot()
        durable = json.loads((tmp_path / (job['job_id'] + '.json')).read_text())
        assert visible['status'] == durable['status']
        assert visible['status'] != terminal
        assert visible['active'] is True and task._serial_owned
        release.set()
        done = task.cancel()
        assert done['status'] == terminal
        assert done == json.loads((tmp_path / (job['job_id'] + '.json')).read_text())
        assert not task.active and not task._serial_owned
        if profile == 'script-pn':
            assert all(session.closed for session in result[7])
            assert writes == []
        elif terminal in {'checked', 'checked_with_differences', 'failed'}:
            assert writes == []
    finally:
        release.set()
        task.cancel()
