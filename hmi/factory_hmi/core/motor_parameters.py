"""Independent read/compare and explicitly confirmed per-item parameter writes.

Inherits the accepted retryable journal-before-release lifecycle, not OTA logic.
Both engines share SERIAL_LOCK and the backend's owner-capability reservation.
"""
from copy import deepcopy
from datetime import datetime,timezone
import threading
import time
import uuid
import logging
from factory_hmi.motor_parameter_contract import validate,applicable,display,sha,pn_profile,spec,PN_SCRIPT_PROFILE,SCHEMA_V1
from .motor_maintenance import (ModuleFirmwareMaintenance, SERIAL_LOCK, now, digest, generation_for_target,
                                confirmed_legacy_108, verified_legacy_readback)
from .parameter_reference import reference_fields,script_reference,SCRIPT_REFERENCE_SHA256
from .motor_parameter_transport import ScriptParameterSession,SessionQuarantinedError
from .production_parameters import ProductionParameterSession, is_production_record, compare


def parameter_template_applicable(template, observation, *, local=False):
    if applicable(template, observation):
        return True
    if (not local or not confirmed_legacy_108(observation, observation)
            or template['model'] != observation.get('model') or template['schema'] != SCHEMA_V1
            or any(spec(entry['key'])['pn'] is not None for entry in template['entries'])):
        return False
    # In a signed local template, the literal 108 identifies the approved
    # generation. Its remaining fields must match actual numeric readback.
    return any(row['raw_firmware'] == '108'
               and all(observation.get(key) == row[key] for key in ('software', 'hardware', 'protocol'))
               for row in template['applicability'])


class _ParameterReservation:
    """Retryable ordered close while the accepted base owns SERIAL_LOCK.

    Socket/session close must finish before the backend reservation is released.
    A partially failed close retains this wrapper and resumes cleanup, never any
    parameter request, on the existing explicit cancel/service-close path.
    """
    def __init__(self,base):
        self.base=base
        self.session=None
        self.production_session=None
        self.session_closed=False
        self.base_closed=False
        self.exit_failed=False

    def __enter__(self):
        self.base.__enter__()
        return self

    def close(self):
        if self.session is not None and not self.session_closed:
            self.session.close()
            self.session_closed=True
        if self.production_session is not None:
            self.production_session.close()
            self.production_session = None
        if not self.base_closed:
            close=getattr(self.base,'close',None)
            if callable(close):
                close()
            else:
                if self.exit_failed:
                    raise RuntimeError('维护资源退出失败；需要检查原资源，不自动解除隔离')
                try:self.base.__exit__(None,None,None)
                except Exception:
                    self.exit_failed=True
                    raise
            self.base_closed=True

class ModuleParameterMaintenance(ModuleFirmwareMaintenance):
    def __init__(self,root,targets,observations,authorize,template,reserve,writer,reader,version_reader,*,script_session_factory=ScriptParameterSession,pn_quarantine_root=None,authorize_local=None,local_template=None,production_session_factory=ProductionParameterSession):
        super().__init__(root,targets,observations,authorize,None,reserve,writer,reader,authorize_local=authorize_local)
        self.template,self.version_reader=template,version_reader
        self.local_template = local_template
        self.script_session_factory=script_session_factory
        self.pn_quarantine_root=pn_quarantine_root if pn_quarantine_root is not None else self.root/'pn-quarantine'
        self._script_session=None
        self.production_session_factory=production_session_factory
        self._production_session=None

    def _recover_interrupted_job(self,value):
        # The base loader visits every in-flight journal. Normalize parameter
        # items before that journal's first save, not just the last loaded job.
        # This hook reads no template, authorizes no account and sends no frame.
        super()._recover_interrupted_job(value)
        for row in value['targets']:
            if row.get('status')=='reading':row['status']='interrupted_unverified'
            for item in row.get('items',[]):
                if item.get('write_status')=='sending':
                    item['write_status']='send_uncertain_unverified'
                if item.get('status')=='writing':item['status']='interrupted_unverified'
                elif item.get('status')=='pending':item['status']='not_run'
        value['summary']={key:sum(i.get('write_status')==key for r in value['targets'] for i in r.get('items',[])) for key in ('not_written','send_uncertain_unverified','written_unverified','written_verified')}

    def _template_record(self, template_id, observation, *, local):
        lookup = self.local_template if local else self.template
        if lookup is None:
            raise ValueError('本机尚未配置签名参数模板目录')
        return lookup(template_id, observation)

    def _selections(self,selections,configuration_sha256=None,*,local=False):
        if not isinstance(selections,list) or not selections:raise ValueError('请选择至少一台电机与已发布模板')
        if any(not isinstance(s,dict) or set(s)!={'index','template_id'} or type(s['index'])is not int for s in selections):raise ValueError('仅接受当前实例目标索引及模板编号')
        indices=[r['index'] for r in selections]
        if len(set(indices))!=len(indices):raise ValueError('重复维护目标')
        targets=self.targets();configuration=digest(targets)
        if configuration_sha256 is not None and configuration_sha256!=configuration:
            raise ValueError('当前有效配置已变化，请刷新电机列表')
        by_index={r['index']:r for r in targets};obs={r['index']:r for r in self.observations()};rows=[]
        for selection in selections:
            if selection['index'] not in by_index:raise ValueError('目标不属于当前配对实例')
            target=by_index[selection['index']];before=obs.get(target['index'],{})
            self._identity(target,before)
            record=self._template_record(selection['template_id'],before,local=local)
            t=validate(record['template'],published=True)
            if record['sha256'] != sha(t) or not parameter_template_applicable(t, before, local=local):
                raise ValueError('模板哈希或实际版本适用性不符；本机核实的 108 需要适配普通参数的签名模板')
            profile=pn_profile(t)
            items=[]
            for entry in t['entries']:
                item=dict(entry,status='pending',write_status='not_written',actual=None,immediate=None,post_power=None,**display(entry,None),**reference_fields(entry['key']))
                if local and is_production_record(record):
                    item['_production_script'] = True
                    item['comparison_rule'] = ('产线脚本：整数相等' if spec(entry['key'])['pn'] is not None or entry['key']=='can_timeout'
                                               else '产线脚本：每个边界绝对误差 < max(0.001, |目标值|×1%+0.001)')
                if profile and spec(entry['key'])['pn'] is not None:
                    item['_protocol_profile']=profile
                items.append(item)
            rows.append(dict(target,before=before,template_record=record,status='pending',
                power_cycle_required=False,human_acknowledged_at=None,post_power_verified=False,
                items=items))
        return rows,configuration

    @staticmethod
    def _identity(target,observation):
        if observation.get('status')!='known' or any(observation.get(k)!=target.get(k) for k in ('index','interface','motor_id','model')):
            raise ValueError('实际版本未知或不属于当前有效目标')
        try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(observation['observed_at'].replace('Z','+00:00'))).total_seconds()
        except (ValueError,KeyError,TypeError) as exc:raise ValueError('实际版本时间无效') from exc
        if not 0<=age<=180:raise ValueError('实际版本证据已过期')

    def read(self,selections,*,configuration_sha256=None,local=False):
        with self._lock:
            if self.active:raise RuntimeError('已有维护任务')
            actor=self._authorize(local);rows,configuration=self._selections(selections,configuration_sha256,local=local)
            self._preview=None
            return self._launch('read',rows,actor,configuration,local=local)

    def read_reference(self, indices, configuration_sha256, *, local=False):
        """Inspect selected motors without a published template or any write path."""
        with self._lock:
            if self.active:
                raise RuntimeError('已有维护任务')
            self._preview = None
            actor = self._authorize(local)
            targets = self.targets()
            configuration = digest(targets)
            if configuration_sha256 != configuration:
                raise ValueError('当前有效配置已变化，请刷新电机列表')
            if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                    or len(indices) != len(set(indices))):
                raise ValueError('请选择非空且不重复的电机')
            by_index = {row['index']:row for row in targets}
            observations = {row['index']:row for row in self.observations()}
            rows = []
            for index in indices:
                if index not in by_index:
                    raise ValueError('所选电机不属于当前模块')
                target, before = by_index[index], observations.get(index, {})
                generation = generation_for_target(target, before, require_protocol=False)
                if generation is None and local and confirmed_legacy_108(target, before):
                    generation = 108
                if generation not in (108,109) or generation == 109 and not before.get('protocol'):
                    raise ValueError(f"{target['joint_name']} 未取得所需的 108/109 实际版本")
                reference = script_reference(target['model'])
                items = []
                for entry in reference['entries']:
                    pn = spec(entry['key'])['pn'] is not None
                    if pn and generation == 108:
                        continue
                    item = dict(entry,status='pending',write_status='not_written',actual=None,
                                immediate=None,post_power=None,**display(entry,None),**reference_fields(entry['key']))
                    if pn:
                        item['_protocol_profile'] = PN_SCRIPT_PROFILE
                    items.append(item)
                rows.append(dict(target,before=before,firmware_generation=generation,
                                 reference_source=reference['source'],reference_sha256=SCRIPT_REFERENCE_SHA256,
                                 status='pending',power_cycle_required=False,items=items))
            return self._launch('read_reference',rows,actor,configuration,local=local)

    def preview(self,read_job_id,*,selections=None,local=False):
        with self._lock:
            if self.active:raise RuntimeError('等待读取任务结束再预览写入')
            self._require_job(read_job_id, local=local)
            j=self._job
            if j.get('status') not in {'checked','checked_with_differences'} or j.get('transport_error'):
                raise ValueError('读取任务未完整结束或存在传输归属错误；请先处理隔离并重新读取，禁止写入')
            if j.get('mode')!='read' or j['configuration_sha256']!=digest(self.targets()):raise ValueError('需要当前配置下独立读取的实际旧值')
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(j['ended_at'])).total_seconds()
            if not 0<=age<=120:raise ValueError('旧值已过期，请重新读取')
            targets = j['targets']
            if selections is not None:
                if not isinstance(selections, list) or not selections:
                    raise ValueError('请选择有差异的电机')
                by_index = {row['index']: row for row in targets}
                targets = []
                seen = set()
                for selection in selections:
                    if (not isinstance(selection, dict) or set(selection) != {'index', 'template_id'}
                            or type(selection['index']) is not int or selection['index'] in seen):
                        raise ValueError('写入目标无效或重复')
                    index = selection['index']
                    row = by_index.get(index)
                    if row is None or row['template_record']['template']['template_id'] != selection['template_id']:
                        raise ValueError('所选电机或模板不在本次读取结果中，请重新读取比较')
                    if not any(item.get('status') == 'mismatch' for item in row['items']):
                        raise ValueError('所选电机没有已确认的参数差异')
                    seen.add(index)
                    targets.append(row)
            rows=deepcopy(targets)
            if not rows or any(not r['items'] or any((i.get('actual') or {}).get('status')!='read' for i in r['items']) for r in rows):raise ValueError('存在未知、跳过、无响应或归属未核验参数；禁止写入')
            for row in rows:
                current=self._template_record(row['template_record']['template']['template_id'],row['before'],local=local)
                if current!=row['template_record']:raise ValueError('模板版本已变化，请重新读取')
                row.update(status='pending',power_cycle_required=False,human_acknowledged_at=None,post_power_verified=False)
                for item in row['items']:
                    item.update(old_raw=deepcopy(item['actual']['raw']),status='pending',write_status='not_written',immediate=None,post_power=None)
            self._preview=dict(preview_id=uuid.uuid4().hex,actor=self._authorize(local),configuration_sha256=j['configuration_sha256'],targets=rows,expires_monotonic=time.monotonic()+120,read_job_id=read_job_id)
            if local:
                self._preview['local'] = True
            return deepcopy({k:v for k,v in self._preview.items() if k!='expires_monotonic'})

    def start(self,preview_id,confirmed,*,local=False):
        with self._lock:
            p=self._preview
            if self.active or confirmed is not True or not p or p['preview_id']!=preview_id or time.monotonic()>p['expires_monotonic']:raise ValueError('请重新读取、预览并明确确认写入')
            if p.get('local', False)!=local or self._authorize(local)!=p['actor'] or digest(self.targets())!=p['configuration_sha256']:raise ValueError('维护来源、账号或有效配置已变化')
            for row in p['targets']:
                if self._template_record(row['template_record']['template']['template_id'],row['before'],local=local)!=row['template_record']:raise ValueError('模板已变化，请重新读取')
            self._preview=None
            return self._launch('write',p['targets'],p['actor'],p['configuration_sha256'],local=local)

    def acknowledge(self,job_id,*,local=False):
        with self._lock:
            if self._job and self._job.get('status')=='awaiting_power':
                if self._job.get('production_batch_power'):
                    actor = self._require_job(job_id, local=local)
                    for row in self._job['targets']:
                        if row['status'] == 'awaiting_power':
                            row.update(human_acknowledged_at=now(), human_acknowledged_by=actor)
                    self._save()
                    return self.snapshot()
                return super().acknowledge(job_id, local=local)
            self._require_job(job_id, local=local)
            if self.active or self._job.get('mode')!='write':
                raise ValueError('当前没有可记录的写后人工声明')
            rows=[r for r in self._job['targets'] if r.get('power_cycle_required')]
            if not rows:raise ValueError('没有已发送或发送不确定的参数写入')
            for row in rows:
                row['human_acknowledged_at']=now()
                row['human_acknowledged_by']=self._authorize(local)
            # An acknowledgement of a failed/partial job is audit only. It
            # never resumes writes or changes a failed verification to passed.
            self._save()
            return self.snapshot()

    def _launch(self,mode,rows,actor,configuration,*,local=False):
        self._job=dict(job_id=uuid.uuid4().hex,mode=mode,actor=actor,configuration_sha256=configuration,
                       active=True,status='queued',created_at=now(),targets=deepcopy(rows),atomic_rollback=False,
                       instance_state_root=str(self.root.resolve()))
        if local:
            self._job['local'] = True
        self._stop.clear();self._read.clear()
        try:self._save()
        except OSError:
            self._job.update(active=False,status='failed',error='报告落盘失败；未启动设备任务')
            raise
        self._thread=threading.Thread(target=self._run,name='factory-motor-parameters',daemon=True)
        self._thread.start()
        return self.snapshot()

    def _validate_current(self,row):
        self._check()
        self._check_script_idle(row=row,phase='before_version_check')
        if digest(self.targets())!=self._job['configuration_sha256']:raise ValueError('当前有效配置已变化')
        observation=self.version_reader(row,self._stop)
        local = self._job.get('local', False)
        numeric_legacy = local and confirmed_legacy_108(row, row['before'], check_age=False)
        if numeric_legacy:
            observation = verified_legacy_readback(row, row['before'], observation)
        self._identity(row,observation)
        if self._job['mode']=='read_reference':
            generation = 108 if numeric_legacy else generation_for_target(
                row, observation, require_protocol=row['firmware_generation'] == 109)
            if (generation != row['firmware_generation']
                    or any(observation.get(key)!=row['before'].get(key) for key in ('raw_firmware','software','hardware','protocol'))):
                raise ValueError('实际版本已变化，请刷新后重新检查')
        else:
            t=row['template_record']['template']
            if (not parameter_template_applicable(t, observation, local=local)
                    or self._template_record(t['template_id'], observation, local=local) != row['template_record']):
                raise ValueError('模板发布状态或实际版本适用性已变化')
        row['version_before_operation']=observation

    @staticmethod
    def _script_item(item):
        return not item.get('_production_script') and item.get('_protocol_profile')==PN_SCRIPT_PROFILE and spec(item['key'])['pn'] is not None

    def _transport_failure(self,item,phase,result,error):
        """Persist association evidence before failing the complete task."""
        result=deepcopy(result)
        result.setdefault('status','association_unknown')
        result.setdefault('raw',None)
        if result.get('status')=='read':
            result.update(reported_status='read',status='association_unknown')
        with self._lock:
            item['transport_error']=dict(phase=phase,evidence=deepcopy(result),error=str(error))
            if phase in {'actual','actual_before_operation','immediate','post_power'}:
                item[phase]=deepcopy(result)
                if phase in {'actual','immediate'}:
                    item.update(status=compare(item,result),**display(item,result.get('raw')))
                elif phase=='post_power':item['post_power_status']=compare(item,result)
            self._save()

    def _check_script_idle(self,*,row=None,item=None,phase):
        if self._script_session is None:return
        try:self._script_session.check_idle(self._stop)
        except SessionQuarantinedError as exc:
            if item is not None:
                self._transport_failure(item,phase,deepcopy(exc.evidence),exc)
            else:
                failure=dict(phase=phase,evidence=deepcopy(exc.evidence),error=str(exc))
                with self._lock:
                    self._job['transport_error']=failure
                    if row is not None:row['transport_error']=deepcopy(failure)
                    self._save()
            raise

    def _retain_cleanup_failure(self,exc):
        # A final quiet-window conflict is a failed verification, even when the
        # preceding terminal journal had already been saved as checked/verified.
        # Keep the original session and reservation for explicit close retry.
        if isinstance(exc,SessionQuarantinedError):
            with self._lock:
                self._job.update(status='failed',error=str(exc),
                    transport_error=dict(phase='session_close',evidence=deepcopy(exc.evidence),error=str(exc)))
                try:self._save()
                except Exception as save_error:
                    self._job['transport_error']['journal_error']=str(save_error)
                    logging.getLogger(__name__).exception('Pn close failure journal remains quarantined')
        super()._retain_cleanup_failure(exc)

    def _read_item(self,row,item,phase):
        if item.get('_production_script'):
            return self._production_session.read(row,item,self._stop)
        scripted=self._script_item(item)
        if not scripted:self._check_script_idle(row=row,item=item,phase=phase)
        try:
            result=(self._script_session.read if scripted else self.reader)(row,item,self._stop)
        except SessionQuarantinedError as exc:
            evidence=deepcopy(exc.evidence)
            self._transport_failure(item,phase,evidence,exc)
            raise
        if not scripted:self._check_script_idle(row=row,item=item,phase=phase)
        if scripted and (not isinstance(result,dict) or result.get('status')!='read'
                or result.get('association_method')!='single_flight_time_window'
                or result.get('parameter_number_echoed') is not False
                or result.get('protocol_profile')!=PN_SCRIPT_PROFILE
                or not isinstance(result.get('raw'),list) or len(result['raw'])!=1
                or type(result['raw'][0]) is not int or not 0<=result['raw'][0]<=0xffffffff):
            if not isinstance(result,dict):result=dict(status='association_unknown',raw=None,error='Pn传输返回格式无效')
            error=RuntimeError(f"{item['key']} 脚本Pn读取未建立明确单请求关联；停止全批，不自动重试")
            self._transport_failure(item,phase,result,error)
            raise error
        return result

    def _write_item(self,row,item):
        if item.get('_production_script'):
            return self._production_session.write(row,item,self._stop)
        scripted=self._script_item(item)
        if not scripted:self._check_script_idle(row=row,item=item,phase='write')
        try:
            receipt=(self._script_session.write if scripted else self.writer)(row,item,self._stop)
        except SessionQuarantinedError as exc:
            self._transport_failure(item,'write',deepcopy(exc.evidence),exc)
            raise
        if not scripted:self._check_script_idle(row=row,item=item,phase='write')
        return receipt

    def _production_row(self, row, read_only):
        session = self._production_session
        session.begin_motor(self._stop)
        # Read before writing and keep each actual value in the journal. This
        # prevents a stale UI preview from silently replacing changed values.
        for item in row['items']:
            self._check()
            result = session.read(row,item,self._stop)
            with self._lock:
                item['actual_before_operation'] = result
                if read_only:
                    item.update(actual=result,status=compare(item,result),**display(item,result.get('raw')))
                self._save()
            if not read_only and (result.get('status') != 'read' or result.get('raw') != item['old_raw']):
                raise ValueError(f"{item['key']} 写前实际值变化或不可读取，请重新读取比较")
        if read_only:
            with self._lock:
                row['status']='checked';self._save()
            return
        # Same per-motor sequence as production run(): all ordinary parameters,
        # model Pn values, E71; 100ms per write; then 300ms settle and readback.
        for item in row['items']:
            self._check()
            with self._lock:
                item.update(write_status='sending',status='writing',write_started_at=now())
                row['power_cycle_required']=True;self._save()
            receipt=session.write(row,item,self._stop)
            with self._lock:
                item.update(write_receipt=receipt,write_status='written_unverified',status='written_unverified');self._save()
        session.settle_writes()
        for item in row['items']:
            self._check()
            result=session.read(row,item,self._stop)
            with self._lock:
                item.update(immediate=result,status=compare(item,result));self._save()
            if item['status']!='passed':
                raise RuntimeError(f"{row['interface']} ID {row['motor_id']} {item['key']} 写后回读不符；停止后续电机")
            with self._lock:
                item['write_status']='written_verified';self._save()
        with self._lock:
            row['status']='written_awaiting_batch_power';self._save()

    def _production_power_readback(self, rows):
        with self._lock:
            self._job.update(status='awaiting_power',production_batch_power=True)
            for row in rows:row['status']='awaiting_power'
            self._save()
        while not self._read.wait(.1):
            self._check()
        self._read.clear()
        for row in rows:
            self._current_production_row=row
            self._production_session.begin_motor(self._stop)
            self._validate_current(row)
            for item in row['items']:
                self._check()
                result=self._production_session.read(row,item,self._stop)
                with self._lock:
                    item['post_power']=result;item['post_power_status']=compare(item,result);self._save()
                if item['post_power_status']!='passed':
                    raise RuntimeError(f"{row['interface']} ID {row['motor_id']} {item['key']} 重新上电后回读不符")
            with self._lock:
                row.update(post_power_verified=True,status='verified');self._save()

    def _run(self):
        self._current_production_row=None
        mode = self._job['mode']
        reference_only = mode == 'read_reference'
        read_only = mode in ('read', 'read_reference')
        current=None;current_item=None;terminal='reference_checked' if reference_only else ('checked' if read_only else 'verified');error=None
        try:
            if not SERIAL_LOCK.acquire(blocking=False):raise RuntimeError('本机已有固件或参数维护任务')
            self._serial_owned=True;self._script_session=None
            self._reservation=_ParameterReservation(self.reserve());self._reservation.__enter__()
            production_rows=[r for r in self._job['targets'] if all(i.get('_production_script') for i in r['items'])]
            if production_rows:
                self._production_session=self.production_session_factory()
                self._reservation.production_session=self._production_session
            if any(self._script_item(item) for row in self._job['targets'] for item in row['items']):
                self._script_session=self.script_session_factory(quarantine_root=self.pn_quarantine_root)
                self._reservation.session=self._script_session
                # Check every selected Pn interface before any ordinary entry
                # can be read or written. The session has no open buses yet, so
                # idle observation alone cannot see a persistent quarantine.
                for row in self._job['targets']:
                    for item in row['items']:
                        if self._script_item(item):
                            current,current_item=row,item
                            try:self._script_session.ensure_available(row,item,self._stop)
                            except SessionQuarantinedError as exc:
                                self._transport_failure(item,'preflight',deepcopy(exc.evidence),exc)
                                raise
                current=None;current_item=None
            for row in self._job['targets']:
                current=row;current_item=None;self._validate_current(row)
                with self._lock:
                    row['status']=self._job['status']='reading' if read_only else 'writing';self._save()
                if row in production_rows:
                    self._production_row(row,read_only)
                    continue
                for item in row['items']:
                    current_item=item
                    self._check()
                    result=self._read_item(row,item,'actual' if read_only else 'actual_before_operation')
                    with self._lock:
                        item['actual_before_operation']=result
                        if read_only:
                            status=compare(item,result)
                            if reference_only and status in ('passed','mismatch'):
                                status = 'read' if row['firmware_generation']==108 else (
                                    'reference_match' if status=='passed' else 'reference_difference')
                            item.update(actual=result,status=status,**display(item,result.get('raw')));self._save()
                            continue
                    if result.get('status')!='read' or result.get('raw')!=item['old_raw']:raise ValueError(f"{item['key']} 写前实际值变化或不可核对；停止，需重新读取")
                    self._check()
                    with self._lock:
                        item.update(write_status='sending',status='writing',write_started_at=now());row['power_cycle_required']=True;self._save()
                    receipt=self._write_item(row,item)
                    with self._lock:
                        item.update(write_receipt=receipt,write_status='written_unverified');self._save()
                    self._check()
                    immediate=self._read_item(row,item,'immediate')
                    with self._lock:
                        item.update(immediate=immediate,status=compare(item,immediate));self._save()
                    if item['status']!='passed':raise RuntimeError(f"{item['key']} 写后即时回读未通过；停止后续项，不整体回滚")
                    with self._lock:
                        item['write_status']='written_verified';self._save()
                if read_only:
                    with self._lock:row['status']='checked';self._save()
                    continue
                with self._lock:
                    row['status']=self._job['status']='awaiting_power';self._save()
                current_item=None
                while not self._read.wait(.1):
                    if self._stop.is_set():raise InterruptedError('等待人工操作时已取消；参数未完成再次核验')
                self._read.clear();self._validate_current(row)
                for item in row['items']:
                    current_item=item
                    self._check();result=self._read_item(row,item,'post_power')
                    with self._lock:
                        item['post_power']=result;item['post_power_status']=compare(item,result);self._save()
                    if item['post_power_status']!='passed':raise RuntimeError('人工确认后独立回读不符或未知；不能宣称重新上电后核验通过')
                with self._lock:row.update(post_power_verified=True,status='verified');self._save()
            if production_rows and not read_only:
                self._production_power_readback(production_rows)
            self._check_script_idle(row=current,item=current_item,phase='before_terminal')
            if read_only:
                good = {'read','reference_match'} if reference_only else {'passed'}
                if any(i['status'] not in good for r in self._job['targets'] for i in r['items']):
                    terminal = 'reference_checked_with_findings' if reference_only else 'checked_with_differences'
        except Exception as exc:
            if self._current_production_row is not None:current=self._current_production_row
            terminal='interrupted' if isinstance(exc,InterruptedError) else 'failed';error=str(exc)
        finally:
            # The shared persistence boundary publishes the terminal outcome
            # only while its journal is committed under the same state lock.
            def finalize():
                self._job.update(active=False,status=terminal,ended_at=now())
                if error:
                    self._job['error']=error
                    if current is not None:current.update(status=terminal,error=error)
                    if current_item is not None:
                        current_item['error']=error
                        if current_item['status'] in {'pending','writing'}:current_item['status']=terminal
                for row in self._job['targets']:
                    if row['status']=='pending':row['status']='not_run'
                    for item in row['items']:
                        if item['status']=='pending':item['status']='not_run'
                        elif item['status']=='writing':item['status']=terminal
                        if item['write_status']=='sending':item['write_status']='send_uncertain_unverified'
                self._job['summary']={key:sum(i['write_status']==key for r in self._job['targets'] for i in r['items']) for key in ('not_written','send_uncertain_unverified','written_unverified','written_verified')}
            try:self._persist_and_release(finalize=finalize)
            except Exception as exc:
                self._retain_cleanup_failure(exc);logging.getLogger(__name__).exception('parameter finalization quarantined')
