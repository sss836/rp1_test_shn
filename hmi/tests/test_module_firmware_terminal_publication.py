"""Terminal publication must follow its durable journal; no device is opened."""
import json
import threading

import pytest

from test_module_firmware_maintenance import begin, make_task, observation, wait


@pytest.mark.parametrize('terminal', ['verified', 'failed', 'version_unknown', 'interrupted'])
def test_terminal_snapshot_never_overtakes_its_journal(tmp_path, terminal):
    entered, release = threading.Event(), threading.Event()
    kwargs = {}
    if terminal == 'failed':
        kwargs['writer'] = lambda *args: {'selfcheck_response': 'ff0202'}
    if terminal == 'version_unknown':
        kwargs['reader'] = lambda row, stop: {**observation(row['index']), 'status': 'timeout'}
    task, selections, written, _ = make_task(tmp_path, n=1, **kwargs)
    persist = task._persist_and_release

    def gated_persist(*args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise TimeoutError('bounded synthetic terminal-publication gate')
        return persist(*args, **kwargs)

    task._persist_and_release = gated_persist
    try:
        job = begin(task, selections)
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
        assert len(written) <= 1  # cleanup never replays the device writer
    finally:
        release.set()
        task.cancel()


def test_snapshot_waits_for_terminal_atomic_save_not_only_memory_update(tmp_path):
    entered, release, observed = threading.Event(), threading.Event(), threading.Event()
    task, selections, written, _ = make_task(tmp_path, n=1)
    save = task._save
    snapshots = []

    def blocked_save():
        if task._job.get('ended_at'):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('bounded synthetic fsync/replace gate')
        return save()

    def observe():
        snapshots.append(task.snapshot())
        observed.set()

    task._save = blocked_save
    reader = threading.Thread(target=observe, name='synthetic-terminal-observer')
    try:
        job = begin(task, selections)
        wait(task, 'awaiting_power')
        task.acknowledge(job['job_id'])
        task.readback(job['job_id'])
        assert entered.wait(1)
        assert json.loads((tmp_path / (job['job_id'] + '.json')).read_text())['status'] == 'verifying'
        reader.start()
        assert not observed.wait(.05)
        release.set()
        assert observed.wait(1)
        assert snapshots[0]['status'] == 'verified'
        assert json.loads((tmp_path / (job['job_id'] + '.json')).read_text())['status'] == 'verified'
        task.cancel()
        assert written == [0]
    finally:
        release.set()
        if reader.ident is not None:
            reader.join(1)
        task.cancel()
