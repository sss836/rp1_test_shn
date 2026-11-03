"""Account/window-bound assistant transport. No device/controller references.

The copied browser session stays in memory. Network work holds neither the
coordinator lock nor the uploader/presence client lock. Generation checks discard
late results after logout, failed relogin, or account changes.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import math
import os
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, error, parse
from uuid import UUID, uuid4

from fastapi import HTTPException


class DiagnosticBridge:
    def __init__(self, data_root: Path):
        from factory_hmi.sync.presence import installation_id
        from factory_hmi.version import code_version
        self.installation = installation_id(data_root)
        self.version = code_version()  # loaded code, not mutable installed metadata
        self._lock = threading.RLock()
        self.login_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(2)
        self._binding = None
        self._uploads = {}
        self.data_root = Path(data_root).resolve()
        from factory_hmi.core.diagnostic_repairs import DiagnosticRepairer
        self.repairs = DiagnosticRepairer(data_root)
        self._settlement_lock = threading.RLock()
        from factory_hmi.core.diagnostic_files import DiagnosticFileReader
        self.files = DiagnosticFileReader(data_root)
        self._local_lock = threading.RLock()
        self._local_pending, self._local_results, self._local_processing = {}, {}, set()

    def revoke(self):
        with self._lock:
            self._binding = None
            for upload in self._uploads.values():
                if upload['phase'] not in {'uploaded','failed','cancelled'}:
                    upload['cancelled'] = True
                    upload['phase'] = 'cancelling'
        with self._local_lock:
            self.files.revoke()
            self._local_pending.clear()
            self._local_results.clear()

    def bind(self, client, owner: str, operator: str, station: str):
        from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
        clone = ReliabilityPlatformClient(client.base_url, timeout=5)
        with client._operator_lock:
            for cookie in client.cookies:
                clone.cookies.set_cookie(copy.copy(cookie))
        # Reuse software channel's no-redirect implementation with the snapshot.
        binding = {"generation": uuid4().hex, "owner": owner, "operator": operator,
                   "station": station, "client": clone}
        self.revoke()
        with self._lock:
            self._binding = binding

    def _local_scope(self, binding, job):
        return (binding['generation'], binding['owner'], job)

    def _local_clear(self, binding, job):
        scope = self._local_scope(binding, job)
        with self._lock: binding.setdefault('closed_jobs',set()).add(job)
        with self._local_lock:
            self.files.revoke(scope)
            for store in (self._local_pending, self._local_results):
                for key in list(store):
                    if key[0] == scope: store.pop(key, None)

    def _local_active(self, binding, job):
        with self._lock:
            if self._current(binding['owner'],binding['generation']) is not binding or job in binding.get('closed_jobs',()):
                raise HTTPException(409,'本窗口已停止这次读取，未继续查看或发送资料。')
        return True

    def _local_post(self, binding, job, request_id, result, expected_request_sha256=None):
        self._local_active(binding,job)
        # Keep a byte-identical receipt across a lost POST acknowledgement.
        result = self._bounded_local_result(result)
        key = (self._local_scope(binding, job), request_id)
        with self._local_lock:
            result = self._local_results.setdefault(key, result)
        payload = dict(request_id=request_id, installation_id=self.installation, window_id=binding['owner'], result=result)
        if expected_request_sha256: payload['expected_request_sha256'] = expected_request_sha256
        self._remote(binding, 'POST', f'/api/v1/assistant/jobs/{job}/local-results', payload)
        with self._local_lock:
            self._local_pending.pop(key, None)

    @staticmethod
    def _bounded_local_result(result, fallback_path=None):
        """A transport limit cannot erase real execution cost or imply completeness."""
        if 'execution_elapsed_ms' in result and (type(result['execution_elapsed_ms']) is not int or result['execution_elapsed_ms'] < 0):
            raise HTTPException(422,'实际检查耗时必须为非负整数毫秒，未提交不完整计时。')
        if len(json.dumps(result,ensure_ascii=False,allow_nan=False).encode()) <= 112000: return result
        from factory_hmi.core.diagnostic_files import receipt
        retained = {key:copy.deepcopy(result[key]) for key in ('path','root','scope','next_offset','execution_elapsed_ms',
                    'source_sha256','sha256_scope','algorithm_version') if key in result}
        if 'path' not in retained and fallback_path is not None: retained['path'] = fallback_path
        return receipt('partial',**retained,eof=False,complete=False,model_content_complete=False,
            result_transmission_complete=False,
            error='本次检查结果超过传输上限，只同步了检查范围；完整结果还没发给助手，需要继续分段核对。')

    def _local_poll(self, binding, job, result):
        if result.get('state') not in {'QUEUED', 'RUNNING', 'WAITING_PERMISSION'}:
            self._local_clear(binding, job)
            return result
        if job in binding.get('closed_jobs',()):
            return dict(result,local_requests=[],permission_requests=[],local_requests_note='本窗口已停止读取资料，正在核对任务的停止状态。')
        hmi = (result.get('context') or {}).get('hmi') or {}
        if hmi and (str(hmi.get('installation_id')) != self.installation or hmi.get('window_id') != binding['owner']):
            return dict(result, local_requests=[], local_requests_note='此任务来自另一配对窗口；可查看/停止，不能在本窗口代替读取。')
        query = parse.urlencode(dict(installation_id=self.installation, window_id=binding['owner']))
        try:
            requests = self._remote(binding, 'GET', f'/api/v1/assistant/jobs/{job}/local-requests?{query}')
        except HTTPException as exc:
            if exc.status_code in {401, 403}: raise
            return dict(result, local_requests=[], local_requests_error=str(exc.detail))
        if 'executor_authorized' in requests or any('request_payload' in item for item in requests.get('items', [])):
            return self._local_poll_durable(binding, job, result, requests)
        scope, permissions = self._local_scope(binding, job), []
        self._bind_reader_scope(binding,scope)
        for request_item in requests.get('items', [])[:3]:
            self._current(binding['owner'], binding['generation'])
            request_id = str(UUID(str(request_item['id'])))
            key = (scope, request_id)
            with self._local_lock:
                if key in self._local_processing: continue
                self._local_processing.add(key)
                cached = self._local_results.get(key)
            try:
                answer = cached or self.files.run(scope, request_item)
                self._current(binding['owner'], binding['generation'])
                if answer.get('status') == 'permission_required':
                    with self._local_lock: self._local_pending[key] = dict(request_item, path=answer['path'])
                    permissions.append(dict(request_item, path=answer['path'], status='permission_required'))
                else:
                    self._local_post(binding, job, request_id, answer)
            finally:
                with self._local_lock: self._local_processing.discard(key)
        return dict(result, local_requests=permissions)

    def _local_approve(self, binding, payload):
        from factory_hmi.core.diagnostic_files import receipt
        job, request_id = str(UUID(str(payload.get('id')))), str(UUID(str(payload.get('request_id'))))
        scope = self._local_scope(binding, job)
        key = (scope, request_id)
        with self._local_lock:
            item = self._local_pending.get(key)
            if item is None: raise HTTPException(409, '本机读取请求已过期或不属于当前任务。')
            if key in self._local_processing: raise HTTPException(409, '本机读取请求正在处理。')
            self._local_processing.add(key)
        try:
            if item.get('_durable'):
                if payload.get('expected_request_sha256') not in (None, item['request_sha256']):
                    raise HTTPException(409, '读取申请已变化，请刷新后重新核对。')
                request_key = str(UUID(str(payload.get('request_key') or uuid4())))
                result = self._remote(binding, 'POST', f'/api/v1/assistant/jobs/{job}/permissions/{request_id}/decision',
                    dict(request_key=request_key, expected_request_sha256=item['request_sha256'], scope=payload.get('scope')))
                self._current(binding['owner'], binding['generation'])
                return dict(accepted=True, permission=result, result=dict(status='permission_denied' if payload.get('scope') == 'deny' else 'permission_approved'))
            expires = item.get('expires_at')
            if expires and datetime.fromisoformat(expires.replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                raise HTTPException(409, '本机读取请求已过期。')
            choice = payload.get('scope')
            if choice == 'deny':
                answer = receipt('permission_denied', path=item.get('path'), eof=False, error='当前窗口用户拒绝本次读取。')
            elif choice in {'file', 'directory', 'all'}:
                # Use the genuine pending request path; payload cannot redirect it.
                if str(Path(item['path']).resolve()) != item['path']:
                    answer = receipt('path_changed', path=item['path'], eof=False, error='待授权路径已变为其他符号链接目标，未读取；请重新请求。')
                else:
                    self.files.grant(scope, item['path'], choice)
                    answer = self.files.run(scope, item)
            else:
                raise HTTPException(422, '未知只读授权范围。')
            self._current(binding['owner'], binding['generation'])
            self._local_post(binding, job, request_id, answer)
            return dict(accepted=True, result=answer)
        finally:
            with self._local_lock: self._local_processing.discard(key)

    def _current(self, owner, generation=None):
        with self._lock:
            binding = self._binding
            if (not binding or binding["owner"] != owner or
                    (generation is not None and generation != binding["generation"])):
                raise HTTPException(401, "诊断关联已撤销，请使用当前窗口登录平台。")
            return binding

    def _remote(self, binding, method, path, payload=None, raw=None, mime=None, filename=None, revoke_on_forbidden=True, upload_record=None):
        client = binding["client"]
        headers = {"Accept": "application/json", "Content-Type": mime or "application/json"}
        if method != "GET":
            for cookie in client.cookies:
                if cookie.name == "rp1_csrf":
                    headers["X-CSRF-Token"] = cookie.value
        if raw is not None:
            headers["X-Filename-UTF8"] = parse.quote(filename or "diagnostic-screenshot.png", safe="")
        body = raw if raw is not None else json.dumps(payload or {}).encode() if method != "GET" else None
        if upload_record is not None:
            headers['Content-Length'] = str(len(raw))
            body = self._upload_body(binding, upload_record, raw)
        req = request.Request(client.base_url + path, data=body, headers=headers, method=method)
        try:
            with client._software_opener().open(req, timeout=90 if raw is not None else 5) as response:
                content = response.read(2 * 1024 * 1024 + 1)
            if len(content) > 2 * 1024 * 1024:
                raise ValueError("oversized assistant response")
            if not content: return {}
            value = json.loads(content)
            return value["data"]
        except error.HTTPError as exc:
            if exc.code == 401 or (exc.code == 403 and revoke_on_forbidden):
                with self._lock:
                    if self._binding is binding:
                        self.revoke()
            raise HTTPException(exc.code if exc.code in (401, 403, 404, 409, 413, 422, 429, 503) else 502,
                                "平台诊断请求被拒绝，请核对账号权限、助手启用和计费配置。") from exc
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(502, "平台诊断暂不可用，请稍后重试；控制通道独立运行。") from exc

    def _file(self, binding, path):
        client = binding['client']
        req = request.Request(client.base_url + path, headers={'Accept': '*/*'}, method='GET')
        try:
            with client._software_opener().open(req, timeout=10) as response:
                raw = response.read(8 * 1024**2 + 1)
                mime = response.headers.get('Content-Type', 'application/octet-stream')
            if len(raw) > 8 * 1024**2:
                raise ValueError('文件预览或报告超过8MiB，请从平台页面下载。')
            return dict(content_base64=base64.b64encode(raw).decode(), mime=mime, sha256=hashlib.sha256(raw).hexdigest())
        except error.HTTPError as exc:
            raise HTTPException(exc.code if exc.code in (401,403,404) else 502, '文件不可用或权限已变化。') from exc

    def _draft(self, binding, payload, save=False):
        identifier = str(UUID(str(payload['thread_id']))) if payload.get('thread_id') else 'new'
        account = hashlib.sha256(binding['operator'].encode()).hexdigest()
        root = self.data_root / 'diagnostics' / 'drafts' / account
        path = root / (identifier + '.json')
        if save:
            body = str(payload.get('message', ''))
            if len(body) > 8000: raise HTTPException(422, '草稿最多8000字。')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_suffix('.tmp')
            with self._lock:
                self._current(binding['owner'], binding['generation'])
                with temporary.open('w', encoding='utf-8') as stream:
                    os.chmod(temporary, 0o600)
                    json.dump(dict(message=body, updated_at=datetime.now(timezone.utc).isoformat()), stream, ensure_ascii=False)
                os.replace(temporary, path)
            return dict(saved=True)
        try:
            value = json.loads(path.read_text())
            updated = datetime.fromisoformat(value['updated_at'])
            if (datetime.now(timezone.utc) - updated).days < 30: return value
        except (OSError, ValueError, KeyError): pass
        return dict(message='')

    def _settlement_store(self, identifier, value):
        """Private durable outbox; receipt retries never call repair.apply()."""
        root = self.repairs.state / 'settlements'
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / (str(UUID(str(identifier))) + '.json')
        temporary = path.with_suffix('.tmp')
        with self._settlement_lock:
            with temporary.open('w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(value, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
        return value

    def _settlement_intent(self, proposal):
        token = proposal.get('settlement_token')
        if not isinstance(token, str) or not token:
            raise HTTPException(409, '平台未提供持久回执结算凭据；未执行本机写入，请先升级完整服务。')
        return self._settlement_store(proposal['id'], dict(id=proposal['id'], actor_id=proposal['actor_id'], role=proposal['role'],
            installation_id=proposal['installation_id'], window_id=proposal['window_id'], proposal_sha256=proposal['proposal_sha256'],
            settlement_token=token, target_path=proposal.get('path'), kind=proposal.get('kind'), expected_before_sha256=proposal.get('expected_sha256'),
            expected_after_sha256=hashlib.sha256(proposal.get('replacement','').encode('utf-8')).hexdigest(), result=None, delivered=False))

    def _settlement_post(self, binding, value):
        self._current(binding['owner'], binding['generation'])
        result = self._remote(binding, 'POST', f"/api/v1/assistant/repairs/{value['id']}/receipt",
            dict(installation_id=value['installation_id'], window_id=value['window_id'],
                 result=value['result'], settlement_token=value['settlement_token']), revoke_on_forbidden=False)
        # A lost acknowledgement leaves the identical outbox available. Only
        # acknowledged receipts discard their token; original local audit stays.
        value = dict(value, delivered=True, settlement_token=None, delivered_at=datetime.now(timezone.utc).isoformat())
        self._settlement_store(value['id'], value)
        return result

    def _settlement_recover_readonly(self, value):
        """Recover save facts only from previously authorized target + backup.

        This path never calls apply(), replaces a target, or loads code. A
        missing/mismatched hash is unresolved, never evidence of no prior write.
        """
        try:
            if value.get('kind') not in {'code','config'} or not value.get('target_path'): return None
            path = self.repairs._path(value['target_path'])
            if value['kind'] == 'config':
                if path != self.repairs.profile.resolve(strict=True): return None
            elif value.get('role') != 'SYSTEM_ADMIN' or path.suffix != '.py' or not any(path.is_relative_to(root / 'factory_hmi') for root in self.repairs.roots):
                return None
            backup = self.repairs.state / (str(UUID(str(value['id']))) + '.before')
            def read_hash(target):
                import stat
                descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor,'rb') as stream:
                    info=os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 512*1024: return None
                    raw=stream.read(512*1024+1)
                    after=os.fstat(stream.fileno())
                    if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns): return None
                    return hashlib.sha256(raw).hexdigest()
            before, after = read_hash(backup), read_hash(path)
            if before != value.get('expected_before_sha256') or after != value.get('expected_after_sha256'): return None
            return dict(status='applied',saved=True,loaded=False,current_process_updated=False,replacement_completed=True,
                durability_verified=False,receipt_durable=False,after_hash_verified=True,
                source='paired_hmi_readonly_receipt_recovery',updated_at=datetime.now(timezone.utc).isoformat(),
                proposal_sha256=value['proposal_sha256'],installation_id=value['installation_id'],window_id=value['window_id'],
                before_sha256=before,after_sha256=after,backup_path=str(backup),
                message='已按原授权目标与备份哈希只读恢复保存事实；未重复写入、加载或重启，目录持久性未重新验证。')
        except (OSError,ValueError,KeyError): return None

    def _settlement_sync(self, binding):
        root = self.repairs.state / 'settlements'
        if not root.is_dir(): return dict(pending_saved=0, pending_other=0, delivered=0)
        actor = self._remote(binding, 'GET', '/api/v1/auth/me')['user']
        summary = dict(pending_saved=0, pending_other=0, delivered=0)
        attempted = 0
        for path in sorted(root.glob('*.json')):
            try:
                with self._settlement_lock: value = json.loads(path.read_text())
                if (value.get('delivered') or value.get('actor_id') != str(actor['id'])
                        or value.get('role') != actor['role'] or value.get('installation_id') != self.installation):
                    continue
                # If the process stopped between local receipt and outbox write,
                # recover the already completed byte-identical local receipt.
                # Never re-read or modify the proposal's target from this path.
                if value.get('result') is None:
                    receipt_path = self.repairs.state / (str(UUID(str(value['id']))) + '.json')
                    receipt = None
                    if receipt_path.is_file():
                        try:
                            candidate = json.loads(receipt_path.read_text())
                            if candidate.get('proposal_sha256') == value.get('proposal_sha256'): receipt = candidate
                        except (OSError,ValueError): pass
                    if receipt is None: receipt = self._settlement_recover_readonly(value)
                    if receipt is not None:
                        value = self._settlement_store(value['id'], dict(value, result=receipt))
                receipt = value.get('result')
                if receipt and attempted < 2:
                    attempted += 1
                    try:
                        self._settlement_post(binding, value); summary['delivered'] += 1
                        continue
                    except HTTPException:
                        # Keep durable bytes; a replacement account sees none.
                        self._current(binding['owner'], binding['generation'])
                summary['pending_saved' if receipt and receipt.get('saved') else 'pending_other'] += 1
            except (OSError, ValueError, KeyError):
                continue
        return summary

    def _upload_key(self, binding, identifier):
        try: identifier = str(UUID(str(identifier)))
        except (ValueError, TypeError): raise HTTPException(422, '上传编号无效。')
        return binding['generation'], binding['owner'], identifier

    @staticmethod
    def _upload_public(record):
        return {key: record[key] for key in ('upload_id','phase','bytes_sent','total_bytes','cancelled')}

    def _upload_action(self, binding, action, payload):
        key = self._upload_key(binding, payload.get('upload_id'))
        with self._lock:
            self._current(binding['owner'], binding['generation'])
            record = self._uploads.get(key)
            if action == 'upload-start':
                total = payload.get('total_bytes')
                if type(total) is not int or not 0 < total <= 32 * 1024**2: raise HTTPException(422, '上传文件须为1字节至32MiB。')
                if record: raise HTTPException(409, '此上传编号已使用，请重试新的上传编号。')
                for old_key, old in list(self._uploads.items()):
                    if time.monotonic() - old['created_at'] > 600:
                        self._uploads.pop(old_key, None)
                if len(self._uploads) >= 128: raise HTTPException(429, '上传记录繁忙，请稍后重试。')
                record = dict(upload_id=key[2], phase='prepared', bytes_sent=0, total_bytes=total, cancelled=False, created_at=time.monotonic())
                self._uploads[key] = record
            elif record is None:
                # A desktop poll may precede upload-start by a few milliseconds.
                return dict(upload_id=key[2],phase='not_started',bytes_sent=0,total_bytes=0,cancelled=False)
            elif action == 'upload-cancel':
                record['cancelled'] = True
                if record['phase'] not in {'uploaded','failed','cancelled'}: record['phase'] = 'cancelling'
            return self._upload_public(record)

    def _upload_check(self, binding, record):
        self._current(binding['owner'], binding['generation'])
        with self._lock:
            if record['cancelled']:
                raise HTTPException(409, dict(code='upload_cancelled',message='已取消上传；不绑定本任务。'))

    def _upload_body(self, binding, record, raw):
        """Count only blocks for which http.client.send has returned.

        Yielding lets urllib/http.client send each 64KiB block. The counter is
        advanced when the iterator resumes, never when a timer merely passes.
        This is transport handoff, not acknowledgement by the remote server.
        """
        with self._lock: record['phase'] = 'uploading'
        for offset in range(0, len(raw), 65536):
            self._upload_check(binding, record)
            chunk = raw[offset:offset+65536]
            yield chunk
            with self._lock: record['bytes_sent'] += len(chunk)
        self._upload_check(binding, record)
        with self._lock: record['phase'] = 'waiting_response'

    def call(self, owner, action, payload=None, raw=None, mime=None, filename=None):
        payload = payload or {}
        generation = payload.get("generation")
        if action != "status" and not generation:
            raise HTTPException(401, "缺少当前诊断关联。")
        binding = self._current(owner, generation)
        if action in {'upload-start','upload-progress','upload-cancel'}:
            return self._upload_action(binding, action, payload)
        if not self._slots.acquire(blocking=False):
            raise HTTPException(429, "诊断请求繁忙，请稍后重试。")
        try:
            if action == "status":
                # Real server permissions are checked on every refresh.
                capabilities = self._remote(binding, "GET", "/api/v1/assistant/capabilities")
                result = {"generation": binding["generation"], "operator": binding["operator"],
                          "hmi": {"installation_id": self.installation, "window_id": owner,
                                  "station": binding["station"], "running_version": self.version},
                          "capabilities": capabilities, "repair_settlement": self._settlement_sync(binding)}
            elif action == "submit":
                data = {k: payload[k] for k in ("request_key", "thread_id", "message", "attachments", "after_job_id") if k in payload}
                # The gateway supplies provenance, never accepts a different instance.
                data["context"] = {"view": "hmi-diagnostics", "kind": "MODULE", "hmi": {
                    "installation_id": self.installation, "window_id": owner,
                    "station": binding["station"], "running_version": self.version}}
                if payload.get("references"):
                    data["context"]["references"] = payload["references"]
                result = self._remote(binding, "POST", "/api/v1/assistant/jobs", data)
            elif action in {"job", "cancel", "history"}:
                identifier = str(UUID(str(payload.get("id"))))
                path = f"/api/v1/assistant/{'threads' if action == 'history' else 'jobs'}/{identifier}"
                if action == "cancel":
                    path += "/cancel"
                    # Fence already-running polls before waiting for the cloud
                    # acknowledgement. An old poll cannot reopen this scope.
                    self._local_clear(binding,identifier)
                result = self._remote(binding, "POST" if action == "cancel" else "GET", path)
                if action == 'job': result = self._local_poll(binding, identifier, result)
                elif action == 'cancel': self._local_clear(binding, identifier)
            elif action == 'local-approve':
                result = self._local_approve(binding, payload)
            elif action in {'local-renew','local-resume'}:
                job = str(UUID(str(payload.get('id'))))
                key = str(UUID(str(payload.get('request_key') or uuid4())))
                if action == 'local-resume':
                    result = self._remote(binding, 'POST', f'/api/v1/assistant/jobs/{job}/resume',dict(request_key=key))
                else:
                    identifier = str(UUID(str(payload.get('request_id'))))
                    result = self._remote(binding,'POST',f'/api/v1/assistant/jobs/{job}/permissions/{identifier}/retry',dict(request_key=key,expected_request_sha256=payload.get('expected_request_sha256')))
            elif action == "threads":
                result = self._remote(binding, "GET", "/api/v1/assistant/threads?" + parse.urlencode({'q': str(payload.get('q',''))[:200]}))
            elif action in {'draft-load', 'draft-save'}:
                result = self._draft(binding, payload, save=action == 'draft-save')
            elif action in {'events', 'steer'}:
                identifier = str(UUID(str(payload.get('id'))))
                path = f'/api/v1/assistant/jobs/{identifier}'
                if action == 'events':
                    result = self._remote(binding, 'GET', path + '/events?' + parse.urlencode({'after': max(0, int(payload.get('after', 0)))}))
                else:
                    data = {key: payload[key] for key in ('request_key','message','expected_job_id')}
                    result = self._remote(binding, 'POST', path + '/inputs', data)
            elif action in {'thread-rename', 'thread-delete'}:
                identifier = str(UUID(str(payload.get('id'))))
                result = self._remote(binding, 'PATCH' if action == 'thread-rename' else 'DELETE', f'/api/v1/assistant/threads/{identifier}',
                                      {'title': str(payload.get('title', ''))} if action == 'thread-rename' else None)
            elif action == 'references':
                if payload.get('kind') not in {'assets','stations','executions'}: raise HTTPException(422, '未知引用对象。')
                result = self._remote(binding, 'GET', '/api/v1/assistant/references?' + parse.urlencode({key: str(payload.get(key,''))[:200] for key in ('kind','q')}))
            elif action in {'feedback', 'faq', 'faq-publish', 'repairs', 'repair-confirm'}:
                if action == 'feedback':
                    result = self._remote(binding, 'POST', '/api/v1/assistant/feedback', {**{key: payload[key] for key in ('request_key','job_id','rating','note')}, 'share_for_faq': payload.get('share_for_faq') is True})
                elif action == 'faq':
                    result = self._remote(binding, 'GET', '/api/v1/assistant/faq?' + parse.urlencode({'q': str(payload.get('q',''))[:200]}))
                elif action == 'faq-publish':
                    result = self._remote(binding, 'POST', '/api/v1/assistant/faq', {key: payload[key] for key in ('request_key','feedback_id','question','answer','faq_id','expected_revision') if key in payload}, revoke_on_forbidden=False)
                elif action == 'repairs':
                    result = self._remote(binding, 'GET', f"/api/v1/assistant/jobs/{str(UUID(str(payload['id'])))}/repairs")
                else:
                    result = self._remote(binding, 'POST', f"/api/v1/assistant/repairs/{str(UUID(str(payload['id'])))}/confirm", {'proposal_sha256': payload['proposal_sha256']})
            elif action in {'repair-pending', 'repair-apply'}:
                query = parse.urlencode(dict(installation_id=self.installation, window_id=owner))
                pending = self._remote(binding, 'GET', '/api/v1/assistant/repairs/pending?' + query)
                if action == 'repair-pending':
                    result = pending
                else:
                    identifier = str(UUID(str(payload['id'])))
                    proposal = next((item for item in pending.get('items', []) if item['id'] == identifier), None)
                    if not proposal: raise HTTPException(409, '已确认提议已过期或不属于当前窗口。')
                    actor = self._remote(binding, 'GET', '/api/v1/auth/me')['user']
                    if str(actor['id']) != proposal['actor_id'] or actor['role'] != proposal['role']:
                        raise HTTPException(403, '当前账号和提议不一致。')
                    def current():
                        try: return self._current(owner, binding['generation']) is binding
                        except HTTPException: return False
                    existing = None
                    try:
                        journal = self.repairs.state / 'settlements' / (identifier + '.json')
                        with self._settlement_lock: candidate = json.loads(journal.read_text())
                        if (candidate.get('proposal_sha256') == proposal['proposal_sha256'] and candidate.get('actor_id') == str(actor['id'])
                                and isinstance(candidate.get('result'),dict)):
                            existing = dict(candidate, settlement_token=proposal['settlement_token'], delivered=False)
                    except (OSError,ValueError,KeyError): pass
                    if existing is not None:
                        # Preserve the identical terminal receipt even if the
                        # separate core audit write failed on the first attempt.
                        value, receipt = existing, existing['result']
                    else:
                        intent = self._settlement_intent(proposal)
                        receipt = self.repairs.apply(proposal, role=actor['role'], installation_id=self.installation, window_id=owner, still_current=current)
                        value = dict(intent, result=receipt)
                        try: self._settlement_store(identifier, value)
                        except OSError: pass  # durable intent enables read-only recovery
                    try:
                        result = self._settlement_post(binding, value)
                        result = dict(result, receipt=receipt, synchronization='delivered')
                    except (HTTPException, OSError):
                        if not current():
                            raise HTTPException(409, dict(code='repair_receipt_pending', saved=bool(receipt.get('saved')),
                                message='已保存，云端回执待同步；请用原账号登录后补报，不会重复写文件。' if receipt.get('saved') else '本机未保存；真实回执待原账号登录后同步。'))
                        result = dict(accepted=False, receipt=receipt, synchronization='pending')
            elif action in {'report', 'attachment-get'}:
                identifier = str(UUID(str(payload['id'])))
                if action == 'report':
                    fmt = payload.get('format', 'markdown')
                    if fmt not in {'markdown','csv','json'}: raise HTTPException(422, '未知报告格式。')
                    result = self._file(binding, f'/api/v1/assistant/jobs/{identifier}/report?format={fmt}')
                else:
                    result = self._file(binding, f'/api/v1/assistant/attachments/{identifier}')
            elif action == "upload":
                if mime not in {"image/png", "image/jpeg", "application/octet-stream"} or not raw or len(raw) > 32 * 1024**2:
                    raise HTTPException(422, "附件须为非空文件，单个最多32MiB。")
                filename = filename or ("diagnostic-screenshot.png" if mime == "image/png" else
                                        "diagnostic-screenshot.jpg" if mime == "image/jpeg" else "")
                if (not filename.strip() or filename in {".", ".."} or len(filename.encode("utf-8")) > 1024
                        or any(c in filename for c in "/\\")
                        or any(ord(c) < 32 or ord(c) == 127 for c in filename)):
                    raise HTTPException(422, "附件文件名无效。")
                if payload.get('upload_id'):
                    key = self._upload_key(binding, payload['upload_id'])
                    with self._lock:
                        record = self._uploads.get(key)
                        if record is None or record['phase'] not in {'prepared','cancelling'} or record['total_bytes'] != len(raw):
                            raise HTTPException(409, '上传编号不存在、已使用或文件大小不一致。')
                    try:
                        self._upload_check(binding, record)
                        result = self._remote(binding, "POST", "/api/v1/assistant/attachments", raw=raw,
                                              mime=mime, filename=filename, upload_record=record)
                        self._upload_check(binding, record)
                        with self._lock: record['phase'] = 'uploaded'
                    except BaseException:
                        with self._lock: record['phase'] = 'cancelled' if record['cancelled'] else 'failed'
                        raise
                else:
                    result = self._remote(binding, "POST", "/api/v1/assistant/attachments", raw=raw,
                                          mime=mime, filename=filename)
            else:
                raise HTTPException(404, "未知诊断操作。")
            # A pending response must never escape into a replacement account.
            if self._current(owner) is not binding:
                raise HTTPException(401, "诊断账号已变化。")
            return result
        finally:
            self._slots.release()

    def _permission_item(self, binding, job, job_state, response, item, actor):
        payload = item.get('request_payload')
        fields = {'id','job_id','actor_id','executor_session_id','installation_id','window_id','arguments','input_sha256','previous_request_id','expires_at'}
        if (response.get('executor_authorized') is not True or not isinstance(payload,dict) or set(payload) != fields):
            raise HTTPException(403, '平台未确认当前登录会话可读取这条申请。')
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False)
        if hashlib.sha256(canonical.encode()).hexdigest() != item.get('request_sha256'):
            raise HTTPException(409, '读取申请内容已变化，请重新核对。')
        for key in ('id','job_id','actor_id','executor_session_id','installation_id','window_id'):
            if key in item and str(item[key]) != str(payload[key]): raise HTTPException(409, '读取申请显示内容与实际目标不一致。')
        if (str(payload['job_id']) != job or str(payload['installation_id']) != self.installation
                or payload['window_id'] != binding['owner'] or str(payload['actor_id']) != str(actor['id'])
                or str(payload['executor_session_id']) != str(response.get('executor_session_id'))):
            raise HTTPException(403, '读取申请不属于当前账号、任务或配对窗口。')
        original_session = job_state.get('origin_session_id')
        if original_session and str(original_session) != str(payload['executor_session_id']):
            raise HTTPException(403, '读取申请来自不同执行登录，请回原窗口继续。')
        original_hash = job_state.get('input_hash')
        if original_hash and str(original_hash) != str(payload['input_sha256']):
            raise HTTPException(409, '原问题内容与读取申请不一致。')
        if not isinstance(item.get('arguments'),dict) or json.dumps(payload['arguments'],sort_keys=True,separators=(',',':'),allow_nan=False) != json.dumps(item['arguments'],sort_keys=True,separators=(',',':'),allow_nan=False):
            raise HTTPException(409, '读取申请参数已变化。')
        for key in ('operation','path'):
            if key in item and item[key] != item['arguments'].get(key): raise HTTPException(409, '读取申请路径或操作不一致。')
        identifier = str(UUID(str(payload['id'])))
        session = str(UUID(str(payload['executor_session_id'])))
        known = binding.setdefault('execution_sessions', {}).setdefault(job,session)
        if known != session: raise HTTPException(403, '原执行登录已经变化，未读取资料。')
        known_hash = binding.setdefault('permission_hashes', {}).setdefault((job,identifier),item['request_sha256'])
        if known_hash != item['request_sha256']: raise HTTPException(409,'同一读取申请不能更换目标或有效期，请重新申请。')
        expires = datetime.fromisoformat(str(payload['expires_at']).replace('Z','+00:00'))
        if expires.tzinfo is None: raise HTTPException(409, '读取申请有效期无效。')
        if item.get('expires_at') and datetime.fromisoformat(str(item['expires_at']).replace('Z','+00:00')) != expires:
            raise HTTPException(409,'读取申请有效期与实际内容不一致。')
        return dict(item, id=identifier, _durable=True, _expired=expires <= datetime.now(timezone.utc))

    def _permission_cached(self, binding, job, item, result=None):
        payload = item['request_payload']
        identity = {key: payload[key] for key in ('actor_id','executor_session_id','installation_id','window_id')}
        key = hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        root = self.data_root / 'diagnostics' / 'local-results' / key / job
        path = root / (item['id'] + '.json')
        if result is None:
            try:
                saved = json.loads(path.read_text())
                age = datetime.now(timezone.utc) - datetime.fromisoformat(saved['created_at'])
                if age.days >= 30:
                    path.unlink(); return None
                if (saved.get('request_sha256') == item['request_sha256'] and saved.get('identity') == identity
                        and saved.get('job_id') == job and saved.get('request_id') == item['id']):
                    return saved['result']
            except (OSError,ValueError,KeyError): pass
            return None
        self._local_active(binding,job)
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        temporary = path.with_suffix('.tmp')
        record = dict(identity=identity,job_id=job,request_id=item['id'],request_sha256=item['request_sha256'],
                      created_at=datetime.now(timezone.utc).isoformat(),result=result)
        with self._local_lock:
            with temporary.open('w',encoding='utf-8') as stream:
                os.chmod(temporary,0o600); json.dump(record,stream,ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary,path)
        return result

    def _approved_local_read(self, binding, job, item):
        from factory_hmi.core.diagnostic_files import receipt
        arguments = item['arguments']
        run_arguments = dict(arguments,_active_budget_ms=item.get('_active_budget_ms',120000))
        scope = self._local_scope(binding, job)
        self._bind_reader_scope(binding,scope)
        operation = arguments.get('operation')
        if item.get('_expired'):
            return None
        if run_arguments['_active_budget_ms'] <= 0:
            return receipt('budget_exhausted',eof=False,complete=False,error='本次检查的有效执行时间已用完，未继续读取资料。')
        if operation in {'capabilities','discover'}:
            return self.files.run(scope,run_arguments)
        value = arguments.get('path') or (str(self.files.data_root) if operation == 'execution_summary' else str(self.files.roots[0]) if self.files.roots else '')
        path = self.files._resolve(value)
        observed = self._local_pending.get((scope,item['id']),{}).get('display_path')
        if observed and str(path) != observed:
            return receipt('permission_required',path=str(path),resolved_path=str(path),operation=operation,eof=False,error='目标路径在显示后发生变化，请核对新路径后重新允许。')
        literal = None
        for alias, root in self.files.aliases.items():
            if str(value) == alias or str(value).startswith(alias + '/'):
                literal = Path(os.path.abspath(root / str(value)[len(alias):].lstrip('/'))); break
        if literal is None: literal = Path(os.path.abspath(Path(value).expanduser()))
        if literal != path and not any(path == root or path.is_relative_to(root) for root in self.files.roots):
            return receipt('permission_required',path=str(path),resolved_path=str(path),operation=operation,eof=False,
                error='此链接实际指向已登记诊断目录之外，请先核对真实路径并重新允许。')
        origin = item.get('approval_origin')
        if origin == 'default':
            if not self.files._allowed(scope,path):
                return receipt('permission_required',path=str(path),resolved_path=str(path),operation=operation,eof=False,
                    error='实际文件位于默认读取范围之外，请核对真实路径后重新允许。')
        elif origin in {'explicit','reuse'}:
            granted = item.get('approved_scope')
            root_value = item.get('scope_root')
            if granted == 'all':
                # All means registered diagnostic roots, not every OS file.
                if not any(path == root or path.is_relative_to(root) for root in self.files.roots):
                    return receipt('permission_required',path=str(path),resolved_path=str(path),operation=operation,eof=False,
                                   error='允许全部仅读取已登记的诊断目录；此文件需单独允许。')
            elif granted in {'file','directory'} and isinstance(root_value,str) and root_value:
                root = self.files._resolve(root_value)
                if Path(root_value).is_absolute() and Path(root_value) != root:
                    return receipt('permission_required',path=str(path),resolved_path=str(path),eof=False,error='允许的目录或文件已变为其他路径，请重新申请。')
                expected = path if granted == 'file' else path.parent if operation in {'read','csv_diagnosis','log_diagnosis'} else path
                if (granted == 'file' and root != path) or (granted == 'directory' and not (expected == root or expected.is_relative_to(root))):
                    return receipt('permission_required',path=str(path),resolved_path=str(path),operation=operation,eof=False,error='实际目标不在已经允许的范围内。')
                self.files.grant(scope,str(root),granted)
            else:
                return receipt('invalid_request',path=str(path),eof=False,error='平台未提供有效读取范围，未读取文件。')
        else:
            return receipt('invalid_request',path=str(path),eof=False,error='平台未说明此项允许来自哪里，未读取文件。')
        self._current(binding['owner'],binding['generation'])
        return self.files.run(scope,run_arguments)

    def _local_poll_durable(self, binding, job, result, response):
        self._local_active(binding,job)
        if response.get('executor_authorized') is not True: raise HTTPException(403,'当前登录不是原执行会话，未读取资料。')
        actor = self._remote(binding,'GET','/api/v1/auth/me')['user']
        if actor.get('enabled') is False or actor.get('role') not in {'SYSTEM_ADMIN','TEST_EXECUTOR'}:
            raise HTTPException(403,'账号权限已变化，未读取资料。')
        used = response.get('active_elapsed_ms',0)
        if type(used) is not int or used < 0: raise HTTPException(409,'平台返回的检查剩余时间无效，未继续读取。')
        remaining = max(0,120000-used)
        scope, display, states, heavy_done, attempted = self._local_scope(binding,job), [], [], False, 0
        for raw_item in response.get('items', [])[:50]:
            self._local_active(binding,job)
            item = self._permission_item(binding,job,result,response,raw_item,actor)
            item['_active_budget_ms'] = remaining
            if item['arguments'].get('path'):
                try: item['display_path'] = str(self.files._resolve(item['arguments']['path']))
                except (OSError,ValueError,RuntimeError): item['display_path'] = item['arguments']['path']
            states.append({key:value for key,value in item.items() if not key.startswith('_')})
            state = item.get('state')
            if item['_expired'] and state in {'pending','approved'}: states[-1]['state'] = 'expired'
            key = (scope,item['id'])
            if state != 'approved' or item['_expired']:
                if state == 'pending' and not item['_expired']:
                    # Retain the target initially shown, including across polls.
                    with self._local_lock: self._local_pending.setdefault(key,item)
                    display.append(item)
                continue
            if attempted >= 3: continue
            with self._local_lock:
                if key in self._local_processing: continue
                cached = self._local_results.get(key)
                self._local_processing.add(key)
            try:
                cached = cached if cached is not None else self._permission_cached(binding,job,item)
                heavy = item['arguments'].get('operation') in {'discover','csv_diagnosis','log_diagnosis'}
                if cached is None and heavy and heavy_done: continue
                if cached is None:
                    started = time.monotonic()
                    answer = self._approved_local_read(binding,job,item)
                    elapsed = max(0,math.ceil((time.monotonic()-started)*1000))
                    remaining = max(0,remaining-elapsed)
                    if answer is not None: answer = dict(answer,execution_elapsed_ms=elapsed)
                else: answer = cached
                if answer is None: continue
                attempted += 1
                if cached is None and heavy: heavy_done = True
                self._local_active(binding,job)
                answer = self._bounded_local_result(answer,item['arguments'].get('path'))
                with self._local_lock: answer = self._local_results.setdefault(key,answer)
                if cached is None:
                    try: self._permission_cached(binding,job,item,answer)
                    except OSError:
                        # A failed local cache does not turn a real read into
                        # "not read". Retain exactly this receipt in memory and
                        # try the platform; never mutate it on a lost ACK.
                        result = dict(result,local_results_note='本机检查缓存未保存；会继续同步本次实际结果。关闭窗口前请核对平台是否已收到，重新打开不能保证恢复这份本机缓存。')
                self._local_post(binding,job,item['id'],answer,item['request_sha256'])
            finally:
                with self._local_lock: self._local_processing.discard(key)
        return dict(result,local_requests=display,permission_requests=states,permission_waiting_timeout_seconds=1800)

    def _bind_reader_scope(self, binding, scope):
        def current():
            try: return self._local_active(binding,scope[2])
            except HTTPException: return False
        self.files.bind_scope(scope,current)
