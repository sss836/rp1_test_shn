"""One outstanding parameter query; no retries, enabling or power control.

The supplied Pn response FF FE 11 <u32> does NOT echo its parameter number.
Default decoding preserves that value as unverified. Only an explicitly
published script profile may use the serial timing window below. It does NOT
prove a parameter-number echo or arbitrary delayed-response safety.
"""
import hashlib
import json
import os
from pathlib import Path
import uuid
import select
import socket
import struct
import time
import threading
from factory_hmi.motor_parameter_contract import spec, PN_SCRIPT_PROFILE
from .motor_maintenance import now

FRAME=struct.Struct('<IBB2x64s')

def cancelled(stop):
    if stop.is_set():raise InterruptedError('参数操作已取消；不自动重试')

def query_payload(key):
    s=spec(key)
    return bytes([0xc0,0x11])+s['pn'].to_bytes(2,'big') if s['pn'] else bytes([0xe0,s['query']])

def write_payload(entry, profile=None):
    s=spec(entry['key'])
    if s['pn'] is not None:
        if profile!=PN_SCRIPT_PROFILE:raise ValueError('禁止Pn写入；需要已发布且完整版本匹配的脚本兼容配置')
        values=entry['expected_raw']
        if len(values)!=1 or type(values[0])is not int or not 0<=values[0]<=0xffffffff:raise ValueError('Pn仅接受一个uint32原始值，不重复缩放')
        return b'\xc0\x10'+s['pn'].to_bytes(2,'big')+values[0].to_bytes(4,'big')
    return bytes([0xc0,s['config']])+b''.join(v.to_bytes(2,'big',signed=s['signed']) for v in entry['expected_raw'])

def decode(key,can_id,payload,target):
    s=spec(key)
    if can_id!=target['motor_id']:return None
    if s['pn'] is not None:
        if len(payload)>=7 and payload[:3]==b'\xff\xfe\x11':
            return dict(status='association_unknown',raw=[int.from_bytes(payload[3:7],'big')],
                        error='Pn响应不带编号；保留原始值但不能核对Pn归属，禁止计通过或写入')
        return None
    size=4 if s['scalar'] else 6
    if len(payload)<size or payload[0]&0xe0!=0xa0 or payload[1]!=s['query']:return None
    return dict(status='read',raw=[int.from_bytes(payload[i:i+2],'big',signed=s['signed']) for i in range(2,size,2)])

def exchange(target,payload,stop,key=None,*,socket_factory=socket.socket,ready=select.select,timeout=.5):
    cancelled(stop)
    with socket_factory(socket.AF_CAN,socket.SOCK_RAW,socket.CAN_RAW) as bus:
        bus.setsockopt(getattr(socket,'SOL_CAN_RAW',101),getattr(socket,'CAN_RAW_FD_FRAMES',5),1)
        bus.setsockopt(getattr(socket,'SOL_CAN_RAW',101),1,struct.pack('=II',target['motor_id'],0xffffffff))
        bus.bind((target['interface'],));bus.setblocking(False)
        for _ in range(256):
            cancelled(stop)
            try:bus.recv(72)
            except BlockingIOError:break
        else:raise RuntimeError('总线持续积压，无法建立干净的参数请求边界')
        cancelled(stop)
        wire=FRAME.pack(target['motor_id'],len(payload),1,payload)
        if bus.send(wire)!=len(wire):raise OSError('CAN参数请求未完整发送')
        if key is None:return dict(sent_at=now(),request_hex=payload.hex())
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            cancelled(stop)
            if not ready([bus],[],[],min(.02,max(0,deadline-time.monotonic())))[0]:continue
            frame=bus.recv(72)
            if len(frame)!=72:continue
            can_id,length,flags,data=FRAME.unpack(frame)
            if not 2<=length<=64:continue
            result=decode(key,can_id,data[:length],target)
            if result is not None:return dict(result,observed_at=now(),response_hex=data[:length].hex(),request_hex=payload.hex(),interface=target['interface'],motor_id=target['motor_id'])
    return dict(status='timeout',raw=None,observed_at=now(),error='参数读取超时；不自动重试')

def read_parameter(target,entry,stop):return exchange(target,query_payload(entry['key']),stop,entry['key'])
def write_parameter(target,entry,stop):return exchange(target,write_payload(entry),stop)


class SessionQuarantinedError(RuntimeError):
    """Uncertain Pn evidence; stop the batch without retrying a device write."""
    def __init__(self, message, evidence=None):
        super().__init__(message)
        self.evidence = dict(evidence or {}, status='association_unknown', error=message)


def _quarantine_path(root, interface):
    return Path(root) / (hashlib.sha256(interface.encode()).hexdigest()+'.json')


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path):
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


def archive_pn_quarantine(root, interfaces, *, reason):
    """Called only after a user's explicit CAN-down action is verified.

    Archives host-channel uncertainty, not a detected motor power cycle. The
    next session must establish its own quiet window; no device command here.
    """
    root=Path(root);records=[]
    for interface in interfaces:
        path=_quarantine_path(root,interface)
        if not path.exists():continue
        value=json.loads(path.read_text(encoding='utf-8'))
        archive=root/'history'/(path.stem+'-'+uuid.uuid4().hex+'.json')
        _atomic_json(archive,dict(value,rearmed_at=now(),rearm_reason=reason,
                                  motor_power_cycle_detected=False))
        path.unlink();_sync_directory(root)
        records.append(str(archive))
    return records


class ScriptParameterSession:
    """One task, one outstanding Pn request, full-interface observation.

    This is the owner's script-compatible protocol, NOT numbered replies.
    Timeout/contention/malformed/duplicate/cancelled requests poison the whole
    interface persistently. Closing/restarting a task never clears the poison.
    A receive window can detect observed ambiguity, not a delayed answer that
    arrives inside a later clean window without any identifying wire field.
    """
    def __init__(self, *, socket_factory=socket.socket, ready=select.select,
                 monotonic=time.monotonic, sleep=time.sleep, timeout=.5,
                 quiet_period=.05, quarantine_root=None):
        if timeout<=0 or quiet_period<0:raise ValueError('invalid Pn timing budget')
        self.socket_factory,self.ready=socket_factory,ready
        self.monotonic,self.sleep=monotonic,sleep
        self.timeout,self.quiet_period=timeout,quiet_period
        self.root=Path(quarantine_root) if quarantine_root is not None else None
        self.buses={};self.dirty={};self.closed=False;self._operation_lock=threading.Lock()

    def _poison(self, interface, message, evidence):
        evidence=dict(evidence,interface=interface,quarantined_at=now(),status='association_unknown',error=message)
        self.dirty[interface]=evidence
        if self.root is not None:
            try:_atomic_json(_quarantine_path(self.root,interface),evidence)
            except OSError as exc:
                evidence['persistence_error']=str(exc)
                message+='；隔离记录保存失败，禁止继续本任务'
        raise SessionQuarantinedError(message,evidence)

    def _guard(self, target, entry, stop):
        if self.closed:raise RuntimeError('Pn会话已关闭')
        if entry.get('_protocol_profile')!=PN_SCRIPT_PROFILE or spec(entry['key'])['pn'] is None:
            raise ValueError('仅允许已发布脚本兼容模板的Pn项进入此会话')
        interface=target['interface']
        if interface in self.dirty or self.root is not None and _quarantine_path(self.root,interface).exists():
            evidence=self.dirty.get(interface,{})
            if not evidence and self.root is not None:
                try:evidence=json.loads(_quarantine_path(self.root,interface).read_text(encoding='utf-8'))
                except (OSError,ValueError):evidence={'error':'隔离记录不可读；保留隔离，不自动清除'}
            raise SessionQuarantinedError('Pn通道已隔离；先断开电机连接并显式断开CAN，确认接口关闭后重新连接',evidence)
        cancelled(stop)
        return interface

    def ensure_available(self,target,entry,stop):
        """Preflight every selected Pn channel before any item is written."""
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError('Pn会话已有在途操作；拒绝并发检查')
        try:
            interface=self._guard(target,entry,stop)
            if interface not in self.buses:
                evidence=dict(interface=interface,protocol_profile=PN_SCRIPT_PROFILE,
                              association_method='single_flight_time_window',parameter_number_echoed=False,
                              phase='preflight_observation',observed_at=now())
                try:
                    bus=self._bus(interface)
                    self._window(bus,target,stop,self.quiet_period,evidence,False)
                except Exception as exc:self._poison(interface,str(exc),evidence)
        finally:self._operation_lock.release()

    def _bus(self, interface):
        if interface not in self.buses:
            bus=self.socket_factory(socket.AF_CAN,socket.SOCK_RAW,socket.CAN_RAW)
            self.buses[interface]=bus  # Keep failed setup handles for cleanup.
            bus.setsockopt(getattr(socket,'SOL_CAN_RAW',101),getattr(socket,'CAN_RAW_FD_FRAMES',5),1)
            bus.setsockopt(getattr(socket,'SOL_CAN_RAW',101),getattr(socket,'CAN_RAW_RECV_OWN_MSGS',4),0)
            # Default all-ID filter intentionally retained: other Pn clients
            # and other motor IDs are contention, not telemetry to discard.
            bus.bind((interface,));bus.setblocking(False)
        return self.buses[interface]

    def _window(self,bus,target,stop,duration,evidence,accept_reply):
        deadline=self.monotonic()+duration;reply=None;count=0
        while self.monotonic()<deadline:
            cancelled(stop)
            if not self.ready([bus],[],[],min(.02,max(0,deadline-self.monotonic())))[0]:continue
            try:packet=bus.recv(FRAME.size)
            except BlockingIOError:continue
            count+=1
            if count>4096:raise RuntimeError('Pn观察窗口报文持续积压')
            if len(packet)!=FRAME.size:continue
            can_id,length,flags,padded=FRAME.unpack(packet)
            if not 1<=length<=64:continue
            payload=padded[:length]
            is_reply=payload[:2]==b'\xff\xfe'
            is_request=len(payload)>=2 and payload[:2] in (b'\xc0\x10',b'\xc0\x11')
            if not is_reply and not is_request:continue
            evidence.setdefault('observed_pn_frames',[]).append(dict(can_id=can_id,response_hex=payload.hex(),observed_at=now(),monotonic=self.monotonic()))
            if is_request:raise RuntimeError('观察到其他Pn请求；停止以避免串配')
            if not accept_reply:raise RuntimeError('请求边界存在残留Pn回复；无法核对归属')
            if can_id!=target['motor_id'] or length<7 or payload[:3]!=b'\xff\xfe\x11':
                raise RuntimeError('Pn响应ID、类型或长度不符')
            if reply is not None:raise RuntimeError('同一窗口出现多份Pn回复；无法核对归属')
            reply=dict(status='read',raw=[int.from_bytes(payload[3:7],'big')],
                       response_hex=payload.hex(),observed_at=now())
        cancelled(stop)
        return reply

    def _exchange(self,target,entry,stop,write):
        interface=self._guard(target,entry,stop)
        payload=write_payload(entry,profile=PN_SCRIPT_PROFILE) if write else query_payload(entry['key'])
        evidence=dict(interface=interface,motor_id=target['motor_id'],key=entry['key'],
                      protocol_profile=PN_SCRIPT_PROFILE,association_method='single_flight_time_window',
                      parameter_number_echoed=False,request_id=uuid.uuid4().hex,
                      request_hex=payload.hex(),request_started_at=now())
        try:
            bus=self._bus(interface)
            self._window(bus,target,stop,self.quiet_period,evidence,False)
            cancelled(stop)
            # A process crash during/after send must also survive a new task.
            if self.root is not None:_atomic_json(_quarantine_path(self.root,interface),dict(evidence,status='in_flight'))
            packet=FRAME.pack(target['motor_id'],len(payload),1,payload)
            evidence['sent_at']=now()
            if bus.send(packet)!=len(packet):raise OSError('Pn请求未完整发送；设备接收状态不确定')
            result=self._window(bus,target,stop,.30 if write else self.timeout,evidence,not write)
            if not write and result is None:raise TimeoutError('Pn读取超时；停止后续项，不自动重试')
            evidence['window_ended_at']=now()
            if self.root is not None:
                _quarantine_path(self.root,interface).unlink();_sync_directory(self.root)
            return dict(evidence,**(result or {}))
        except Exception as exc:
            self._poison(interface,str(exc),evidence)

    def _operation(self,target,entry,stop,write):
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError('Pn会话已有在途操作；拒绝并发请求')
        try:return self._exchange(target,entry,stop,write)
        finally:self._operation_lock.release()

    def read(self,target,entry,stop):return self._operation(target,entry,stop,False)
    def write(self,target,entry,stop):return self._operation(target,entry,stop,True)

    def _check_idle(self,stop):
        if self.closed:raise RuntimeError('Pn会话已关闭')
        for interface,bus in self.buses.items():
            if interface in self.dirty:
                raise SessionQuarantinedError('Pn通道已隔离，停止后续操作',self.dirty[interface])
            evidence=dict(interface=interface,protocol_profile=PN_SCRIPT_PROFILE,
                          association_method='single_flight_time_window',parameter_number_echoed=False,
                          phase='idle_observation',observed_at=now())
            try:self._window(bus,dict(interface=interface,motor_id=-1),stop,self.quiet_period,evidence,False)
            except InterruptedError:raise
            except Exception as exc:self._poison(interface,str(exc),evidence)

    def check_idle(self,stop):
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError('Pn会话已有在途操作；不能并发观察')
        try:self._check_idle(stop)
        finally:self._operation_lock.release()

    def close(self):
        if not self._operation_lock.acquire(blocking=False):
            raise RuntimeError('Pn操作尚未结束；保留会话，等待操作退出后关闭')
        try:
            # A late queued Pn must not disappear when closing the last socket.
            # On first detection persist and raise; a later explicit cleanup
            # closes the already-quarantined handle without replaying a request.
            for interface,bus in list(self.buses.items()):
                if interface not in self.dirty:
                    evidence=dict(interface=interface,protocol_profile=PN_SCRIPT_PROFILE,
                                  association_method='single_flight_time_window',parameter_number_echoed=False,
                                  phase='close_observation',observed_at=now())
                    try:self._window(bus,dict(interface=interface,motor_id=-1),threading.Event(),self.quiet_period,evidence,False)
                    except Exception as exc:self._poison(interface,str(exc),evidence)
                bus.close()  # On failure retain handle for explicit cleanup retry.
                del self.buses[interface]
            self.closed=True
        finally:self._operation_lock.release()
