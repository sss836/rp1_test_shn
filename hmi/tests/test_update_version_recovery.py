"""Post-update health checks only: no install, process start or hardware calls."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from factory_hmi import installer


@pytest.fixture
def health_check(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(installer.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(installer.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    executor = installer.SystemExecutor()
    pid = os.getpid()
    raw = Path(f'/proc/{pid}/stat').read_text()
    ticks = raw[raw.rfind(')') + 2:].split()[19]
    status = {'running': True, 'read_only': False, 'gateway_pid': pid, 'gateway': 'http://127.0.0.1:8766'}
    health = {'ok': True, 'software': {'running_version': '9.0.1', 'pid': pid, 'start_ticks': ticks},
              'snapshot': {'station': {'motors_enabled': False}}}
    monkeypatch.setattr(executor, 'status', lambda policy: status)
    monkeypatch.setattr(executor, 'installed_version', lambda: '9.0.1')
    @contextmanager
    def download(url):
        assert url == 'http://127.0.0.1:8766/api/v1/health'
        yield io.BytesIO(json.dumps(health).encode())
    monkeypatch.setattr(installer.updates, '_download', download)
    return executor, status, health, clock


def verify(executor, previous=None):
    return executor.verify_new_instance(SimpleNamespace(), {'version': '9.0.1'},
                                        previous or {'pid': 1, 'start_ticks': '1'})


def test_initial_launcher_status_error_is_retried_without_restart(health_check, monkeypatch):
    executor, status, health, clock = health_check
    calls = [0]
    def status_when_ready(policy):
        calls[0] += 1
        if calls[0] == 1:
            raise ValueError('launcher identity not published yet')
        return status
    monkeypatch.setattr(executor, 'status', status_when_ready)
    assert verify(executor) == health['software']
    assert calls[0] == 2 and clock[0] == .25


@pytest.mark.parametrize('failure', ['old_process', 'wrong_running_version', 'wrong_installed_version',
                                    'wrong_pid', 'wrong_ticks', 'enabled', 'invalid_address', 'bad_health'])
def test_invalid_identity_or_health_never_grants_success(health_check, monkeypatch, failure):
    executor, status, health, clock = health_check
    previous = None
    if failure == 'old_process':
        previous = {k: health['software'][k] for k in ('pid', 'start_ticks')}
    elif failure == 'wrong_running_version':
        health['software']['running_version'] = '9.0.0'
    elif failure == 'wrong_installed_version':
        monkeypatch.setattr(executor, 'installed_version', lambda: '9.0.0')
    elif failure == 'wrong_pid':
        health['software']['pid'] += 1
    elif failure == 'wrong_ticks':
        health['software']['start_ticks'] = '1'
    elif failure == 'enabled':
        health['snapshot']['station']['motors_enabled'] = True
    elif failure == 'invalid_address':
        status['gateway'] += '#untrusted'
    else:
        health['software'] = ['malformed']
    with pytest.raises(RuntimeError, match='核对超时'):
        verify(executor, previous)
    assert clock[0] == 35
