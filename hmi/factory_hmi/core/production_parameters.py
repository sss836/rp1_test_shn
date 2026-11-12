"""Offline integration of the user's unmodified production parameter script.

Wire encoding, numeric tables, response decoding and comparisons come directly
from that script. The HMI adds selection, exclusive ownership, bounded process
lifetime, cancellation, persistence and post-power readback, never motor enable.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from factory_hmi.motor_parameter_contract import (
    SOURCE_SHA256, MODELS, SCHEMA_V1, SCHEMA_V2, PN_SCRIPT_PROFILE, sha, spec, validate,
    compare as template_compare,
)

SOURCE_KIND = 'bundled-production-script'
_SOURCE_PATH = Path(__file__).resolve().parents[2] / 'motors/scripts/deploy_motorlro_params.py'
_load_lock = threading.Lock()


def load_script(*, isolated=False):
    # The package contains the exact user-provided file, not a generated rewrite.
    if hashlib.sha256(_SOURCE_PATH.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError('内置产线参数脚本校验失败，请修复安装包')
    with _load_lock:
        name = '_rp1_production_params'
        if not isolated and name in sys.modules:
            return sys.modules[name]
        name += f'_{time.monotonic_ns()}' if isolated else ''
        loader = importlib.util.spec_from_file_location(name, _SOURCE_PATH)
        module = importlib.util.module_from_spec(loader)
        sys.modules[name] = module
        try:
            loader.loader.exec_module(module)
        finally:
            if isolated:
                sys.modules.pop(name, None)
        return module


def entries_for(model, generation):
    p = load_script()
    if model not in MODELS or generation not in (108, 109):
        raise ValueError('没有适用于该型号和版本的产线参数')
    model_id = MODELS.index(model)
    entries = []
    for key, _ in p.PARAM_DEFS:
        low, high = p.expected_values(p.MODEL_PARAMS[model_id], key)
        raw = [p.value_to_raw(key, high)] if key in p.SINGLE_VALUE_KEYS else [p.value_to_raw(key, low), p.value_to_raw(key, high)]
        entries.append(dict(key=key, expected_raw=raw, tolerance_raw=0))
    # The script's Pn read/write commands are documented V109+. For 108 use
    # the same script's eight ordinary parameters; never guess a Pn protocol.
    if generation == 109:
        for number, value in (*p.PN_SETTINGS, *p.MODEL_PN_SETTINGS.get(model_id, ()),
                              (p.PNE71_PARAMETER_NUMBER, p.PNE71_OVERVOLTAGE_LEVEL)):
            entries.append(dict(key=f'pn_{number:04x}', expected_raw=[value], tolerance_raw=0))
    return entries


def production_record(observation):
    from .motor_maintenance import confirmed_legacy_108, generation_for_target
    if observation.get('status') != 'known' or observation.get('model') not in MODELS:
        return None
    if observation.get('hardware') != '0.1.0' or observation.get('protocol') != '0.0.1.17':
        return None
    software = observation.get('numeric_software') or observation.get('software')
    raw_version = observation.get('raw_firmware')
    if software == '0.1.8' and (confirmed_legacy_108(observation, observation)
                                or generation_for_target(observation, observation) == 108):
        generation = 108
    elif software == '0.1.9' and generation_for_target(observation, observation) == 109:
        generation = 109
    else:
        return None
    model = observation['model']
    applicability = {key: observation[key] for key in ('software', 'hardware', 'protocol')}
    applicability['raw_firmware'] = raw_version or '108'
    identifier = sha([SOURCE_KIND, SOURCE_SHA256, model, generation, applicability])[:32]
    t = dict(schema=SCHEMA_V2 if generation == 109 else SCHEMA_V1,
             template_id=identifier, version=f'production-{generation}', model=model,
             source_sha256=SOURCE_SHA256, source='bundled:motors/scripts/deploy_motorlro_params.py',
             applicability=[applicability], entries=entries_for(model, generation),
             confirmation=dict(evidence_sha256=SOURCE_SHA256,
                               notes='用户指定产线脚本与四款型号参数；来自本机软件包，不冒充平台签发。'))
    if generation == 109:
        t['protocol_profile'] = PN_SCRIPT_PROFILE
    t = validate(t, published=True)
    return dict(template=t, sha256=sha(t), published_by=SOURCE_KIND,
                published_at='2026-11-13T00:00:00+08:00', execution_source=SOURCE_KIND)


def is_production_record(record):
    return record.get('execution_source') == SOURCE_KIND


def compare(entry, result):
    if not entry.get('_production_script'):
        return template_compare(entry, result)
    raw, expected = result.get('raw'), entry['expected_raw']
    if result.get('status') != 'read' or not isinstance(raw, list) or len(raw) != len(expected) or any(type(v) is not int for v in raw):
        return result.get('status', 'unknown') if result.get('status') != 'read' else 'unknown'
    s = spec(entry['key'])
    if s['pn'] is not None or entry['key'] == 'can_timeout':
        return 'passed' if raw == expected else 'mismatch'
    p = load_script()
    return 'passed' if all(p.close_enough(a / s['scale'], b / s['scale']) for a, b in zip(raw, expected)) else 'mismatch'


class ProductionParameterSession:
    def __init__(self):
        self.script = load_script(isolated=True)
        self._stop = None
        self._deadline = None
        self.evidence = {}
        original_capture = self.script.capture_response
        original_send = self.script.send_frame
        # Scope substitutions to this one script instance; never monkey-patch
        # process-wide time/subprocess or another station's control loop.
        self.script.time = SimpleNamespace(sleep=self.sleep)
        self.script.subprocess = SimpleNamespace(**{k:getattr(subprocess, k) for k in
            ('PIPE', 'Popen', 'CalledProcessError', 'TimeoutExpired')}, run=self._run_command)
        def capture(iface, motor_id, send_request):
            self.check()
            output = original_capture(iface, motor_id, send_request)
            self.evidence['candump'] = output
            return output
        def send(iface, iface_type, motor_id, payload, verbose):
            self.check()
            self.evidence.setdefault('requests', []).append(dict(interface=iface, motor_id=motor_id, payload=payload))
            return original_send(iface, iface_type, motor_id, payload, verbose)
        self.script.capture_response = capture
        self.script.send_frame = send

    def _run_command(self, *args, **kwargs):
        self.check()
        return subprocess.run(*args, timeout=2, **kwargs)

    def check(self):
        if self._stop is not None and self._stop.is_set():
            raise InterruptedError('参数维护已取消；不自动重试写入')
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise TimeoutError('单台产线参数操作超过120秒；停止后续写入')

    def sleep(self, seconds):
        self.check()
        if self._stop is None:
            time.sleep(seconds)
        elif self._stop.wait(seconds):
            self.check()
        self.check()

    def begin_motor(self, stop):
        self._stop = stop
        self._deadline = time.monotonic() + 120
        self.check()

    def _target(self, row, item, stop):
        self._stop = stop
        self.check()
        generation = 109 if row['template_record']['template']['schema'] == SCHEMA_V2 else 108
        expected = {entry['key']: entry for entry in entries_for(row['model'], generation)}
        if item['key'] not in expected or item['expected_raw'] != expected[item['key']]['expected_raw']:
            raise ValueError('所选参数与该电机型号的产线脚本不符')
        self.evidence = dict(source_sha256=SOURCE_SHA256, implementation=SOURCE_KIND,
                             observed_at=datetime.now(timezone.utc).isoformat())
        return self.script.MotorTarget(row['interface'], row.get('interface_type', 'canfd'),
                                       row['motor_id'], MODELS.index(row['model']))

    def read(self, row, item, stop):
        target = self._target(row, item, stop)
        p, s, key = self.script, spec(item['key']), item['key']
        if s['pn'] is None:
            actual = p.query_param(target, p.QUERY_CODE[key], key, False)
            raw = None if actual is None else [p.value_to_raw(key, v) for v in (actual[1:] if key in p.SINGLE_VALUE_KEYS else actual)]
        else:
            actual = p.query_pn(target, s['pn'], False)
            raw = None if actual is None else [actual]
        return dict(status='read' if raw is not None else 'timeout', raw=raw,
                    association_method='production_script_single_request', parameter_number_echoed=False if s['pn'] else None,
                    **deepcopy(self.evidence))

    def write(self, row, item, stop):
        target = self._target(row, item, stop)
        p, key, s = self.script, item['key'], spec(item['key'])
        if s['pn'] is None:
            low, high = p.expected_values(p.MODEL_PARAMS[target.model], key)
            p.write_param(target, key, p.value_to_raw(key, low), p.value_to_raw(key, high), False)
        elif s['pn'] == p.PNE71_PARAMETER_NUMBER:
            p.write_pn_e71_overvoltage(target, False)
        else:
            p.send_frame(target.iface, target.iface_type, target.motor_id,
                         p.build_pn_write_frame(s['pn'], item['expected_raw'][0]), False)
        self.sleep(p.WRITE_DELAY_S)
        return dict(sent=True, **deepcopy(self.evidence))

    def settle_writes(self):
        self.sleep(self.script.WRITE_SETTLE_DELAY_S)

    def close(self):
        # capture_response always closes its owned candump in finally. No
        # persistent socket/child survives a request, success or failure.
        self._deadline = None
