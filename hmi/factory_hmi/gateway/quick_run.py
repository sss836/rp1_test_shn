"""Offline quick verification with station protections and optional PLC power.

A sequence holds the station command guard. Stop/disconnect can still preempt
motion through the runtime epoch; a lightweight watcher also cancels long
operations when the independent client lease heartbeat expires.
"""
from __future__ import annotations

from copy import deepcopy
import logging
import math
import threading
import time
import uuid

from factory_hmi.core.config import preview_station_config, load_station_config
from factory_hmi.quick_profiles import preset

logger = logging.getLogger(__name__)


class QuickRun:
    def __init__(self, runtime, hub, owner: str):
        self.runtime, self.hub, self.owner = runtime, hub, owner
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher = None
        self._prepared = None
        self._epoch = None
        self._channels = []
        self._power_source = 'plc'
        self._value = dict(phase='idle', detail='请选择部位', profile=None, active=False,
                           power_source='plc', failure=None, failed_phase=None)

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._value)

    def _state(self, phase: str, detail: str, *, active: bool = True):
        with self._lock:
            if all(self._value.get(k) == v for k, v in dict(phase=phase, detail=detail, active=active).items()):
                return
            if phase == 'fault' and not self._value.get('failure'):
                self._value.update(failure=detail, failed_phase=self._value['phase'])
            self._value.update(phase=phase, detail=detail, active=active)
        logger.info('quick verification: owner=%s phase=%s detail=%s', self.owner, phase, detail)

    def prepare(self, key: str, interfaces: list[str], backend: str) -> dict:
        from factory_hmi.gateway.schemas import ConfigureRequest
        if self.snapshot()['active']:
            raise RuntimeError('请先结束当前快速验证')
        with self._lock:
            self._value.update(failure=None, failed_phase=None)
        profile = preset(key)
        buses = interfaces or profile['interfaces']
        if len(buses) != len(profile['interfaces']):
            raise ValueError('CAN 分组数量与部位不一致')
        if key == 'waist_hip' and (buses[0] != buses[2] or buses[1] != buses[3] or buses[0] == buses[1]):
            raise ValueError('腰胯同侧两个电机必须共用 CAN，左右侧必须使用不同 CAN')
        path = self.runtime._allowed_config_path(profile['config_path'])
        preview = preview_station_config(key, path)
        if len(preview['motors']) != profile['motor_count'] or len(preview['bus_groups']) != len(buses):
            raise ValueError('网关电机配置数量与内置轨迹不匹配')
        # Keep the gateway's merged control defaults and calibrated motor YAML.
        can = load_station_config(key, path).raw.get('factory_hmi', {}).get('can', {})
        bindings = [dict(index=i, interface=name, mode='canfd' if can.get('fd', True) else 'can',
                         bitrate=int(can.get('bitrate', 1_000_000)),
                         dbitrate=int(can.get('dbitrate', 5_000_000)) if can.get('fd', True) else None) for i, name in enumerate(buses)]
        request = ConfigureRequest(config_path=profile['config_path'], limb=key,
                                   backend=backend, bus_bindings=bindings)
        self._prepared = None
        self.hub.configure(self.owner, self.runtime, request)
        # Only the hash-verified bundled asset reaches this internal import.
        # Generic user-supplied imports keep their existing path restrictions.
        trajectory = self.runtime._invoke(('import_trajectory',), str(profile['trajectory_path']))
        self._prepared = dict(profile, interfaces=list(buses), trajectory=trajectory)
        with self._lock:
            self._value.update(profile={k: v for k, v in profile.items() if k != 'trajectory_path'}, interfaces=list(buses))
        self._state('ready', '内置轨迹已读取，等待运行', active=False)
        return self.snapshot()

    def invalidate(self):
        if self.snapshot()['active']:
            raise RuntimeError('请先结束当前快速验证，再修改模块或轨迹')
        self._prepared = None
        self._state('idle', '模块或轨迹已变化，请重新选择快速验证部位', active=False)

    def _checkpoint(self):
        with self.runtime._motion_epoch_guard:
            cancelled = self._epoch != self.runtime._motion_epoch or self.runtime._motion_interrupts_pending
        if self._stop.is_set() or cancelled:
            raise RuntimeError('快速验证已被结束或失能操作取消')
        if self.runtime.lease.snapshot().expired:
            raise RuntimeError('窗口心跳已失效，停止快速验证')

    def _plc_request(self, operation: str, payload: dict):
        self._checkpoint()
        plc = self.hub.plc
        snapshot = plc.snapshot()
        context = snapshot['coordination']
        response = plc.coordination.execute(operation, payload, user='factory-quick',
            client_id=self.owner, request_id=str(uuid.uuid4()),
            expected_epoch=context['epoch'], expected_revision=context['revision'])
        request_id = response['request']['request_id']
        budget = snapshot['timings']['startup_budget_ms'] if operation == 'start' else snapshot['timings']['command_timeout_ms']
        deadline = time.monotonic() + budget / 1000 + 2
        while time.monotonic() < deadline:
            self._checkpoint()
            current = plc.snapshot()
            if current['communication_state'] != 'LIVE' or current['stale']:
                raise RuntimeError('PLC 通信中断，快速验证已停止')
            records = current['coordination']['commands']
            record = next((r for r in records if r['request_id'] == request_id), None)
            if record and record['state'] != 'pending':
                if record['state'] != 'confirmed':
                    raise RuntimeError('PLC 未确认：' + record['detail'])
                return
            self._stop.wait(.1)
        raise RuntimeError('等待 PLC 实际供电反馈超时')

    def _power(self):
        if self._power_source == 'external':
            self._state('starting', '使用外部供电，准备连接 CAN 并核对电机反馈')
            return
        plc = self.hub.plc
        snapshot = plc.snapshot()
        if snapshot['access_mode'] != 'control':
            raise RuntimeError('PLC 供电模式需要控制权限，当前为只读监控；请核对所选供电方式')
        if snapshot['communication_state'] != 'LIVE':
            reason = str(snapshot.get('last_error') or '')
            raise RuntimeError('PLC 通信未就绪（' + str(snapshot['communication_state']) + '）'
                               + ('：' + reason if reason else '') + '；请检查 PLC 配电柜连接')
        if snapshot['stale']:
            raise RuntimeError('PLC 状态已过期，不能确认实际供电；请检查 PLC 配电柜连接')
        mapping = {value: int(key.removeprefix('DUT')) for key, value in plc.config.dut_can_map.items()}
        try:
            self._channels = sorted({mapping[name] for name in self._prepared['interfaces']})
        except KeyError as exc:
            raise ValueError('所选 CAN 没有对应的 PLC 通道') from exc
        if snapshot['phase'] == 'OFF':
            self._state('powering', '按网关默认参数供电，等待 PLC 实际反馈')
            self._plc_request('start', {'channels': self._channels, 'voltage': None, 'current': None})
        elif snapshot['phase'] == 'RUNNING':
            for channel in self._channels:
                if not plc.snapshot()['status']['ChannelPermit'][channel-1]:
                    self._state('powering', f'等待 CH{channel} 供电确认')
                    self._plc_request('channel', {'channel': channel, 'enabled': True})
        else:
            raise RuntimeError('PLC 当前处于故障或其他操作中，请先在配电柜页处理')
        self._check_power()

    def _check_power(self):
        if self._power_source == 'external':
            return
        value = self.hub.plc.snapshot()
        status = value['status']
        if (value['communication_state'] != 'LIVE' or value['stale'] or value['phase'] != 'RUNNING'
                or not status['PS1FeedbackValid'] or status['PS1ActualOutput'] is not True
                or not status['MainReady'] or any(not status['ChannelPermit'][n-1] for n in self._channels)):
            raise RuntimeError('PLC 实际供电许可无效，已停止本窗口测试')

    def start(self, hours: float, safety_confirmed: bool, sample: str, power_source: str = 'plc') -> dict:
        if power_source not in {'external', 'plc'}:
            raise ValueError('供电方式必须为 external 或 plc')
        if not safety_confirmed:
            raise ValueError('请确认现场支撑、运动范围及物理急停安全')
        if not math.isfinite(hours) or hours <= 0:
            raise ValueError('运行时间必须为大于零的有限小时数')
        if self._prepared is None or self.snapshot()['phase'] != 'ready':
            raise RuntimeError('请先选择部位并读取内置轨迹')
        if not sample.strip():
            raise ValueError('请填写本地样机标识')
        current = self.runtime.snapshot()
        if (current.get('trajectory') or {}).get('sha256') != self._prepared['sha256']:
            raise RuntimeError('轨迹已变化，请重新选择快速验证部位')
        if (current.get('station') or {}).get('limb') != self._prepared['id']:
            raise RuntimeError('模块配置已变化，请重新选择部位')
        self._stop = threading.Event()
        self._power_source = power_source
        self._channels = []
        with self._lock:
            self._value.update(power_source=power_source, failure=None, failed_phase=None)
        with self.runtime._motion_epoch_guard:
            self._epoch = self.runtime._motion_epoch
        self._state('starting', '正在准备本地快速验证')
        self._watcher = threading.Thread(target=self._watch, args=(self._stop,), name='rp1-quick-'+self.owner[-8:], daemon=True)
        self._watcher.start()
        try:
            self._power()
            for phase, detail, names, kwargs in [
                ('connecting', '连接 CAN', ('can_connect',), {}),
                ('discovering', '扫描电机并核对反馈', ('discover_motors',), {}),
                ('enabling', '确认使能电机', ('enable_motors',), {'physical_estop_confirmed': True}),
                ('returning', '平滑回默认位：半 KP 1 秒，正常 KP 2 秒', ('reset',), {}),
                ('preflight', '核对首帧、限位和轨迹速度', ('arm',), {}),
            ]:
                self._checkpoint()
                self._check_power()
                self._state(phase, detail)
                self.runtime._invoke(names, **kwargs)
            self._checkpoint()
            self._check_power()
            self.runtime._invoke(('start',), speed=1.0, loop=True, cycles=0, duration_hours=hours,
                record=True, recording_scope='local', record_rate_hz=20.0,
                robot_id=sample.strip(), test_case_id='QUICK-'+self._prepared['id'].upper(),
                test_name='工厂快速验证 · '+self._prepared['label'] + (' · 外部供电' if power_source == 'external' else ' · PLC 供电'),
                stage_code='QUICK_EXTERNAL' if power_source == 'external' else 'QUICK_PLC',
                stage_name='离线快速验证 · 外部供电' if power_source == 'external' else '离线快速验证 · PLC 供电')
            self._checkpoint()
            self._state('running', '1 倍速循环运行；记录保存在本机')
            return self.snapshot()
        except Exception as exc:
            self._stop.set()
            errors = self._cleanup()
            self._state('fault', str(exc) + ('；清理未确认：'+ '；'.join(errors) if errors else ''), active=False)
            logger.warning('quick verification failed: owner=%s power_source=%s phase=%s error=%s',
                           self.owner, self._power_source, self.snapshot()['failed_phase'], exc)
            raise

    def _cleanup(self) -> list[str]:
        errors = []
        try:
            if self.runtime.controller.controller.state.value in {'running', 'paused'}:
                self.runtime._invoke(('stop',), reason='factory quick verification ended')
        except Exception as exc:
            errors.append(str(exc))
        for names in [('disable_motors', 'disable'), ('disconnect',), ('can_disconnect',)]:
            try:
                self.runtime._invoke(names)
            except Exception as exc:
                errors.append(str(exc))
        # Keep ownership if any cleanup failed: never let another window take
        # a bus whose previous motor shutdown is unconfirmed.
        if not errors:
            self.hub.release_interfaces(self.hub._key(self.owner))
        return errors

    def cancel(self):
        self._stop.set()
        self.runtime.controller.request_motion_stop()

    def end(self) -> dict:
        self._stop.set()
        errors = self._cleanup()
        self._prepared = None
        if errors:
            self._state('fault', '结束清理未确认：'+'；'.join(errors), active=False)
            raise RuntimeError(self.snapshot()['detail'])
        failure = self.snapshot().get('failure')
        detail = ('已结束，电机失能并断开 CAN；外部电源由现场管理' if self._power_source == 'external' else
                  '已结束，电机失能并断开 CAN；PLC 共用供电保持当前状态')
        self._state('ended', (failure + '；' if failure else '') + detail, active=False)
        return self.snapshot()

    def _watch(self, stop_event):
        try:
            while not stop_event.wait(.1):
                if self._stop is not stop_event:
                    return
                phase = self.snapshot()['phase']
                expired = self.runtime.lease.snapshot().expired
                if expired:
                    self.runtime.controller.request_motion_stop()
                if phase not in {'running', 'paused'}:
                    continue
                state = self.runtime.controller.controller.state.value
                failure = '窗口心跳已失效' if expired else ''
                if not failure and state not in {'completed', 'fault', 'disconnected'}:
                    try:
                        self._check_power()
                    except Exception as exc:
                        failure = str(exc)
                if failure or state in {'completed', 'fault', 'disconnected'}:
                    with self.runtime._lease_guard:
                        if stop_event.is_set() or self._stop is not stop_event:
                            return
                        stop_event.set()
                        errors = self._cleanup()
                        successful = state == 'completed' and not failure and not errors
                        self._state('completed' if successful else 'fault',
                            '已达到设定时长，电机已失能，记录保存在本机' if successful else
                            '；'.join([failure or '设备运行已中止']+errors), active=False)
                    return
                self._state('paused' if state == 'paused' else 'running',
                            '已暂停，可继续或结束' if state == 'paused' else '1 倍速循环运行；记录保存在本机')
        except Exception:
            logger.exception('quick verification watcher failed: owner=%s', self.owner)
            stop_event.set()
            with self.runtime._lease_guard:
                if self._stop is not stop_event:
                    return
                errors = self._cleanup()
                self._state('fault', '运行监控异常；'+'；'.join(errors), active=False)
