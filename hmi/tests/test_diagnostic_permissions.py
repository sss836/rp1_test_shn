"""Durable read approval using synthetic sessions/files and inert Qt only."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QPushButton
from fastapi import HTTPException
from factory_hmi.client import GatewayClient
from factory_hmi.core.diagnostic_files import DiagnosticFileReader
from factory_hmi.desktop.diagnostics_page import DiagnosticsPage
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from test_diagnostic_attachments import AttachmentClient


def sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class PermissionFixture:
    def __init__(self,root):
        self.root=Path(root);self.state=self.root/'state';self.project=self.root/'project';self.outside=self.root/'outside'
        for path in (self.state,self.project,self.outside):path.mkdir()
        self.actor=str(uuid4());self.session=str(uuid4());self.job=str(uuid4());self.owner=str(uuid4())
        self.binding=dict(owner=self.owner,generation=str(uuid4()),operator='synthetic',station='mock')
        self.items=[];self.posts=[];self.calls=[];self.decisions=[];self.state_name='WAITING_PERMISSION';self.complete_on_post=False
        self.bridge=self.new_bridge()

    def new_bridge(self):
        bridge=DiagnosticBridge(self.state)
        bridge.files=DiagnosticFileReader(self.state,roots=[self.project,self.state])
        bridge._binding=dict(self.binding)
        bridge._remote=self.remote
        return bridge

    def item(self,operation='read',path=None,*,state='pending',origin=None,scope=None,scope_root=None,expired=False):
        args=dict(operation=operation,offset=0,limit=65536)
        if path is not None:args['path']=str(path)
        expires=(datetime.now(timezone.utc)+timedelta(minutes=-1 if expired else 30)).isoformat()
        payload=dict(id=str(uuid4()),job_id=self.job,actor_id=self.actor,executor_session_id=self.session,
            installation_id=self.bridge.installation,window_id=self.owner,arguments=copy.deepcopy(args),
            input_sha256='a'*64,previous_request_id=None,expires_at=expires)
        item=dict(payload,request_payload=payload,request_sha256=sha(payload),state=state,arguments=args,
            operation=operation,path=str(path) if path is not None else None,expires_at=expires,
            approval_origin=origin,approved_scope=scope,scope_root=scope_root,
            allowed_scopes=['file','directory','all','deny'],purpose='核对本次模拟故障',
            share_notice='读取内容会发送给模型分析，不修改文件。')
        self.items.append(item);return item

    def remote(self,binding,method,path,payload=None,**kwargs):
        self.calls.append((method,path,copy.deepcopy(payload)))
        if path==f'/api/v1/assistant/jobs/{self.job}':
            return dict(id=self.job,state=self.state_name,origin_session_id=self.session,input_hash='a'*64,
                context=dict(hmi=dict(installation_id=self.bridge.installation,window_id=self.owner)))
        if '/local-requests?' in path:
            return dict(executor_authorized=True,executor_session_id=self.session,items=copy.deepcopy(self.items))
        if path=='/api/v1/auth/me':return dict(user=dict(id=self.actor,role='TEST_EXECUTOR',enabled=True))
        if path.endswith('/decision'):
            item=next(item for item in self.items if '/'+item['id']+'/' in path)
            self.decisions.append(copy.deepcopy(payload))
            if payload['expected_request_sha256']!=item['request_sha256']:raise HTTPException(409,'changed')
            if item['state']!='pending':raise HTTPException(409,'already decided')
            item.update(state='denied' if payload['scope']=='deny' else 'approved',approval_origin='explicit',approved_scope=payload['scope'])
            if payload['scope']=='file':item['scope_root']=item['path']
            elif payload['scope']=='directory':item['scope_root']=str(Path(item['path']).parent)
            return dict(permission=copy.deepcopy(item))
        if path.endswith('/local-results'):
            self.posts.append(copy.deepcopy(payload))
            if self.complete_on_post:next(item for item in self.items if item['id']==payload['request_id'])['state']='completed'
            return dict(accepted=True,replayed=False)
        if path.endswith('/retry'):return dict(id=str(uuid4()),state='pending')
        if path.endswith('/resume'):return dict(id=self.job,state='QUEUED')
        if path.endswith('/cancel'):self.state_name='CANCELLED';return dict(id=self.job,state=self.state_name)
        raise AssertionError(path)

    def call(self,action='job',**payload):
        return self.bridge.call(self.owner,action,dict(id=self.job,generation=self.bridge._binding['generation'],**payload))


class DurablePermissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.fx=PermissionFixture(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_pending_reads_nothing_decision_only_persists_then_approved_reads(self):
        fx=self.fx;target=fx.outside/'data.csv';target.write_text('observed evidence')
        item=fx.item(path=target)
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('pending must not read')):
            waiting=fx.call()
            approved=fx.call('local-approve',request_id=item['id'],scope='file',request_key=str(uuid4()),expected_request_sha256=item['request_sha256'],path='/forged')
        self.assertEqual(len(waiting['local_requests']),1);self.assertEqual(fx.posts,[])
        self.assertEqual(approved['result']['status'],'permission_approved')
        self.assertNotIn('path',fx.decisions[0])
        fx.call()
        self.assertEqual(fx.posts[0]['result']['content'],'observed evidence')
        self.assertEqual(fx.posts[0]['expected_request_sha256'],item['request_sha256'])

    def test_tampered_identity_arguments_hash_and_expiry_never_read(self):
        fx=self.fx;target=fx.project/'source.txt';target.write_text('safe')
        original=fx.item(path='$PROJECT/source.txt',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        variants=[lambda item:item['arguments'].update(path='/other'),
                  lambda item:item.update(window_id='other-window'),
                  lambda item:item.update(request_sha256='0'*64),
                  lambda item:item.update(expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()),
                  lambda item:item['request_payload'].update(actor_id=str(uuid4())),
                  lambda item:item['request_payload'].update(executor_session_id=str(uuid4()))]
        for change in variants:
            with self.subTest(change=change):
                item=copy.deepcopy(original);change(item);fx.items=[item]
                with patch.object(fx.bridge.files,'run',side_effect=AssertionError('untrusted request')):
                    with self.assertRaises(HTTPException):fx.call()
        self.assertEqual(fx.posts,[])

    def test_disabled_role_or_changed_origin_session_does_not_read(self):
        fx=self.fx;fx.item(operation='capabilities',state='approved',origin='default',scope='all')
        old=fx.bridge._remote
        for field in ('role','session','executor'):
            def changed(*args,field=field,**kw):
                value=old(*args,**kw)
                if args[2]=='/api/v1/auth/me' and field=='role':value['user']['role']='VIEWER'
                if args[2].endswith('/'+fx.job) and field=='session':value['origin_session_id']=str(uuid4())
                if '/local-requests?' in args[2] and field=='executor':value['executor_authorized']=False
                return value
            fx.bridge._remote=changed
            with self.subTest(field=field),patch.object(fx.bridge.files,'run',side_effect=AssertionError('wrong executor')):
                with self.assertRaises(HTTPException):fx.call()
        self.assertEqual(fx.posts,[])

    def test_default_alias_symlink_outside_reports_actual_path_without_grant(self):
        fx=self.fx;target=fx.outside/'real.txt';target.write_text('not granted')
        (fx.project/'link').symlink_to(target)
        fx.item(path='$PROJECT/link',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        fx.call();result=fx.posts[0]['result']
        self.assertEqual(result['status'],'permission_required');self.assertEqual(result['resolved_path'],str(target))
        self.assertNotIn('content',result)

    def test_path_replaced_between_prompt_and_approval_requires_new_request(self):
        fx=self.fx;old=fx.outside/'old.txt';old.write_text('old');new=fx.outside/'new.txt';new.write_text('new')
        item=fx.item(path=old);fx.call()
        old.unlink();old.symlink_to(new)
        fx.call('local-approve',request_id=item['id'],scope='file')
        fx.call();self.assertEqual(fx.posts[0]['result']['status'],'permission_required')
        self.assertEqual(fx.posts[0]['result']['resolved_path'],str(new));self.assertNotIn('content',fx.posts[0]['result'])

    def test_repeated_poll_cannot_replace_target_already_shown_for_same_request(self):
        fx=self.fx;old=fx.outside/'old.txt';old.write_text('old');new=fx.outside/'new.txt';new.write_text('new')
        link=fx.project/'alias';link.symlink_to(old)
        item=fx.item(path='$PROJECT/alias');fx.call()
        link.unlink();link.symlink_to(new);fx.call()  # User is still viewing the first nonmodal dialog.
        saved=fx.bridge._local_pending[(fx.bridge._local_scope(fx.bridge._binding,fx.job),item['id'])]
        self.assertEqual(saved['display_path'],str(old))
        fx.call('local-approve',request_id=item['id'],scope='file');fx.call()
        self.assertEqual(fx.posts[0]['result']['status'],'permission_required');self.assertNotIn('content',fx.posts[0]['result'])

    def test_web_alias_approval_without_local_prompt_requires_outside_real_path(self):
        fx=self.fx;target=fx.outside/'x.txt';target.write_text('private')
        (fx.project/'link').symlink_to(target)
        fx.item(path='$PROJECT/link',state='approved',origin='explicit',scope='file',scope_root='$PROJECT/link')
        fx.call();self.assertEqual(fx.posts[0]['result']['status'],'permission_required')
        self.assertEqual(fx.posts[0]['result']['resolved_path'],str(target));self.assertNotIn('content',fx.posts[0]['result'])

    def test_all_scope_keeps_registered_roots_and_private_caches_excluded(self):
        fx=self.fx;outside=fx.outside/'data.txt';outside.write_text('private')
        fx.item(path=outside,state='approved',origin='explicit',scope='all')
        private=fx.state/'diagnostics';private.mkdir();cached=private/'private.txt';cached.write_text('settlement token')
        fx.item(path=cached,state='approved',origin='explicit',scope='file',scope_root=str(cached))
        fx.call();self.assertEqual(fx.posts[0]['result']['status'],'permission_required')
        self.assertEqual(fx.posts[1]['result']['status'],'unsupported_file')
        self.assertFalse(any('settlement token' in str(result) for result in fx.posts))

    def test_os_denial_is_failure_receipt_and_does_not_claim_read(self):
        fx=self.fx;path=fx.outside/'x.csv';path.write_text('time_s,v\n0,1\n')
        fx.item('csv_diagnosis',path,state='approved',origin='explicit',scope='file',scope_root=str(path))
        with patch.object(fx.bridge.files,'_open_fd',side_effect=PermissionError('synthetic OS refusal')):fx.call()
        self.assertEqual(fx.posts[0]['result']['status'],'os_permission_denied')
        self.assertFalse(fx.posts[0]['result']['eof'])

    def test_expiry_denial_and_completed_display_without_reads_and_explicit_retry_resume(self):
        fx=self.fx;expired=fx.item(path='/never',state='approved',origin='explicit',scope='file',scope_root='/never',expired=True)
        fx.item(path='/denied',state='denied');fx.item(path='/done',state='completed')
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('no live approved request')):value=fx.call()
        self.assertEqual(value['local_requests'],[]);self.assertEqual(value['permission_requests'][0]['state'],'expired')
        retry_key=str(uuid4());fx.call('local-renew',request_id=expired['id'],expected_request_sha256=expired['request_sha256'],request_key=retry_key)
        resume_key=str(uuid4());fx.call('local-resume',request_key=resume_key)
        self.assertEqual(fx.calls[-2][2]['request_key'],retry_key);self.assertEqual(fx.calls[-1][2]['request_key'],resume_key)

    def test_restart_resends_exact_cached_evidence_without_new_file_read_or_cookies(self):
        fx=self.fx;path=fx.project/'source.txt';path.write_text('original observation')
        fx.item(path='$PROJECT/source.txt',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        original=fx.bridge._remote
        def lost(*args,**kw):
            value=original(*args,**kw)
            if args[2].endswith('/local-results'):raise HTTPException(502,'lost acknowledgement')
            return value
        fx.bridge._remote=lost
        with self.assertRaises(HTTPException):fx.call()
        path.write_text('changed after original read')
        fx.bridge=fx.new_bridge();fx.bridge._binding['generation']=str(uuid4())
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('must settle stored evidence')):fx.call()
        self.assertEqual(fx.posts[0],fx.posts[1]);self.assertEqual(fx.posts[1]['result']['content'],'original observation')
        files=list((fx.state/'diagnostics/local-results').rglob('*.json'));self.assertEqual(len(files),1)
        record=json.loads(files[0].read_text());self.assertNotIn('client',record);self.assertNotIn('cookie',files[0].read_text().lower())
        self.assertEqual(files[0].stat().st_mode&0o777,0o600)

    def test_local_cache_failure_preserves_actual_result_in_memory(self):
        fx=self.fx;(fx.project/'x').write_text('actual')
        fx.item(path='$PROJECT/x',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        original=fx.bridge._permission_cached
        def fail_write(*args,**kw):
            if len(args)>3:raise OSError('synthetic cache disk failure')
            return original(*args,**kw)
        with patch.object(fx.bridge,'_permission_cached',side_effect=fail_write):value=fx.call()
        self.assertEqual(fx.posts[0]['result']['content'],'actual');self.assertIn('未保存',value['local_results_note'])
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('reuse memory')):fx.call()
        self.assertEqual(fx.posts[0],fx.posts[1])

    def test_heavy_scan_one_per_poll_light_operations_still_progress(self):
        fx=self.fx
        for name in ('a.csv','b.csv'):(fx.project/name).write_text('time_s,v\n0,1\n2,3\n')
        for name in ('a.csv','b.csv'):fx.item('csv_diagnosis','$PROJECT/'+name,state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        fx.item('capabilities',state='approved',origin='default',scope='all');fx.complete_on_post=True
        original=fx.bridge.files.run
        with patch.object(fx.bridge.files,'run',wraps=original) as run:
            fx.call();self.assertEqual([x.args[1]['operation'] for x in run.call_args_list],['csv_diagnosis','capabilities'])
            fx.call();self.assertEqual([x.args[1]['operation'] for x in run.call_args_list].count('csv_diagnosis'),2)
        self.assertEqual(len(fx.posts),3);self.assertTrue(fx.posts[0]['result']['complete'])

    def test_only_actual_local_execution_is_timed_and_replay_keeps_original_elapsed(self):
        fx=self.fx;(fx.project/'x').write_text('actual')
        fx.item(path='$PROJECT/x',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        original=fx.bridge._approved_local_read
        def slow(*args,**kwargs):
            time.sleep(.015);return original(*args,**kwargs)
        with patch.object(fx.bridge,'_approved_local_read',side_effect=slow):fx.call()
        elapsed=fx.posts[0]['result']['execution_elapsed_ms'];self.assertGreaterEqual(elapsed,15);self.assertLess(elapsed,1000)
        with patch.object(fx.bridge,'_approved_local_read',side_effect=AssertionError('replay is not new execution')):fx.call()
        self.assertEqual(fx.posts[0],fx.posts[1]);self.assertEqual(fx.posts[1]['result']['execution_elapsed_ms'],elapsed)
        fx.bridge=fx.new_bridge()
        with patch.object(fx.bridge,'_approved_local_read',side_effect=AssertionError('disk replay is not execution')):fx.call()
        self.assertEqual(fx.posts[2],fx.posts[0])

    def test_trusted_budget_only_internal_argument_does_not_change_frozen_request(self):
        fx=self.fx;(fx.project/'x.csv').write_text('time_s,v\n0,1\n1,3\n')
        item=fx.item('csv_diagnosis','$PROJECT/x.csv',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        frozen=copy.deepcopy(item['arguments']);expected=item['request_sha256'];remote=fx.bridge._remote
        def remaining(*args,**kw):
            value=remote(*args,**kw)
            if '/local-requests?' in args[2]:value['active_elapsed_ms']=119999
            return value
        fx.bridge._remote=remaining;original=fx.bridge.files.run
        with patch.object(fx.bridge.files,'run',wraps=original) as run:fx.call()
        self.assertEqual(run.call_args.args[1]['_active_budget_ms'],1)
        self.assertEqual(item['arguments'],frozen);self.assertEqual(item['request_sha256'],expected)
        self.assertNotIn('_active_budget_ms',fx.posts[0]['result'].get('request_arguments',{}))

    def test_zero_budget_performs_no_filesystem_read_and_reports_exhausted(self):
        fx=self.fx;fx.item('csv_diagnosis','$PROJECT/not-opened.csv',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        remote=fx.bridge._remote
        def empty(*args,**kw):
            value=remote(*args,**kw)
            if '/local-requests?' in args[2]:value['active_elapsed_ms']=120000
            return value
        fx.bridge._remote=empty
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('budget exhausted cannot scan')):fx.call()
        self.assertEqual(fx.posts[0]['result']['status'],'budget_exhausted');self.assertFalse(fx.posts[0]['result']['complete'])

    def test_logout_during_read_discards_content_and_cannot_revive_old_scope(self):
        fx=self.fx;path=fx.project/'source.txt';path.write_text('actual')
        fx.item(path='$PROJECT/source.txt',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        entered,release=threading.Event(),threading.Event();errors=[];original=fx.bridge.files._bytes
        def blocked(*args,**kw):entered.set();release.wait(2);return original(*args,**kw)
        def poll():
            try:fx.call()
            except Exception as exc:errors.append(exc)
        # _read uses an anchored stream directly, so block at its FD opening.
        original_open=fx.bridge.files._open_fd
        def opening(*args,**kw):entered.set();release.wait(2);return original_open(*args,**kw)
        with patch.object(fx.bridge.files,'_open_fd',side_effect=opening):
            thread=threading.Thread(target=poll);thread.start();self.assertTrue(entered.wait(1));fx.bridge.revoke();release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(fx.posts,[]);self.assertTrue(errors)
        scope=(fx.binding['generation'],fx.owner,fx.job)
        with self.assertRaises(ValueError):fx.bridge.files.grant(scope,path,'all')

    def test_late_decision_after_account_change_and_cancel_never_grants(self):
        fx=self.fx;item=fx.item(path=str(fx.outside/'missing'));fx.call()
        generation=fx.bridge._binding['generation'];fx.bridge.revoke()
        with self.assertRaises(HTTPException):fx.bridge.call(fx.owner,'local-approve',dict(generation=generation,id=fx.job,request_id=item['id'],scope='all'))
        self.assertEqual(fx.decisions,[]);self.assertEqual(fx.bridge.files._grants,{})

    def test_cancel_fences_inflight_poll_before_it_can_rebind_or_read(self):
        fx=self.fx;(fx.project/'x').write_text('must not read')
        fx.item(path='$PROJECT/x',state='approved',origin='default',scope='directory',scope_root='$PROJECT')
        entered,release=threading.Event(),threading.Event();errors=[];remote=fx.bridge._remote
        def slow(*args,**kw):
            value=remote(*args,**kw)
            if '/local-requests?' in args[2]:entered.set();release.wait(2)
            return value
        fx.bridge._remote=slow
        def poll():
            try:fx.call()
            except Exception as exc:errors.append(exc)
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('cancelled poll cannot read')):
            thread=threading.Thread(target=poll);thread.start();self.assertTrue(entered.wait(1))
            fx.call('cancel');release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(fx.posts,[])
        self.assertTrue(any(isinstance(exc,HTTPException) and exc.status_code==409 for exc in errors))
        scope=fx.bridge._local_scope(fx.bridge._binding,fx.job)
        with self.assertRaises(ValueError):fx.bridge._bind_reader_scope(fx.bridge._binding,scope)

    def test_cancel_network_failure_still_stops_local_read_and_can_retry_stop(self):
        fx=self.fx;fx.item('capabilities',state='approved',origin='default',scope='all');remote=fx.bridge._remote
        def lost(*args,**kw):
            if args[2].endswith('/cancel'):raise HTTPException(502,'synthetic lost cancel response')
            return remote(*args,**kw)
        fx.bridge._remote=lost
        with self.assertRaises(HTTPException):fx.call('cancel')
        with patch.object(fx.bridge.files,'run',side_effect=AssertionError('local stop persists while cloud state uncertain')):value=fx.call()
        self.assertEqual(value['state'],'WAITING_PERMISSION');self.assertIn('停止读取',value['local_requests_note']);self.assertEqual(fx.posts,[])
        fx.bridge._remote=remote;self.assertEqual(fx.call('cancel')['state'],'CANCELLED')

    def test_new_operations_route_through_gateway_client_with_separate_timeouts(self):
        client=GatewayClient('http://127.0.0.1:1',timeout=1)
        with patch.object(client,'_request',return_value={}) as request:
            client.diagnostic('local-renew',{'id':str(uuid4())});self.assertEqual(request.call_args.kwargs['timeout'],30)
            client.diagnostic('local-resume',{'id':str(uuid4())});self.assertEqual(request.call_args.kwargs['timeout'],30)
            client.diagnostic('job',{'id':str(uuid4())});self.assertEqual(request.call_args.kwargs['timeout'],30)
            client.lease_heartbeat();self.assertEqual(request.call_args.kwargs['timeout'],client.lease_heartbeat_timeout)

    def test_oversize_local_post_keeps_real_elapsed_scope_and_cursor(self):
        fx=self.fx;rid=str(uuid4())
        result=dict(status='read',path='$STATE/example.csv',scope=dict(start=0,end_exclusive=600000,total=700000),
                    next_offset=600000,execution_elapsed_ms=8123,complete=True,eof=True,content='汉'*50000)
        fx.bridge._local_post(fx.bridge._binding,fx.job,rid,result,'a'*64)
        sent=fx.posts[0]['result']
        self.assertEqual(sent['execution_elapsed_ms'],8123);self.assertEqual(sent['scope'],result['scope'])
        self.assertEqual(sent['path'],result['path']);self.assertEqual(sent['next_offset'],600000)
        for field in ('complete','eof','model_content_complete','result_transmission_complete'):self.assertFalse(sent[field])
        self.assertLess(len(json.dumps(sent,ensure_ascii=False).encode()),112000);self.assertNotIn('content',sent)
        self.assertTrue(result['complete']);self.assertEqual(result['content'],'汉'*50000)

    def test_oversize_durable_execution_keeps_cost_in_cache_and_identical_replay(self):
        fx=self.fx;fx.item('log_diagnosis','$STATE/example.log',state='approved',origin='default',scope='directory',scope_root='$STATE')
        scope=dict(unit='original_bytes',start=0,end_exclusive=500000,total=700000)
        def large(*args,**kw):
            time.sleep(.005)
            return dict(status='partial',path='$STATE/example.log',scope=scope,next_offset=500000,
                        complete=False,eof=False,data=dict(matches=[dict(text='汉'*50000)]))
        with patch.object(fx.bridge,'_approved_local_read',side_effect=large):fx.call()
        first=fx.posts[0]['result'];self.assertGreaterEqual(first['execution_elapsed_ms'],5)
        self.assertEqual(first['scope'],scope);self.assertEqual(first['next_offset'],500000);self.assertFalse(first['model_content_complete'])
        fx.bridge=fx.new_bridge()
        with patch.object(fx.bridge,'_approved_local_read',side_effect=AssertionError('oversize cache is still a real receipt')):fx.call()
        self.assertEqual(fx.posts[0],fx.posts[1])

    def test_invalid_elapsed_is_not_coerced_or_silently_removed(self):
        fx=self.fx
        for invalid in (True,'8123',-1,1.5):
            with self.subTest(invalid=invalid),self.assertRaises(HTTPException):
                fx.bridge._local_post(fx.bridge._binding,fx.job,str(uuid4()),dict(status='read',execution_elapsed_ms=invalid,content='汉'*50000))
        self.assertEqual(fx.posts,[])


class NativePermissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.client=AttachmentClient();self.page=DiagnosticsPage(self.client);self.page.timer.stop();self.page.upload_timer.stop()
        self.page.binding=self.client.status;self.page.job=str(uuid4());self.page._controls()
    def tearDown(self):
        self.page.stop();self.page.pool.waitForDone(3000);self.app.processEvents();self.page.deleteLater()
    def pump(self,predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate():return
            time.sleep(.003)
        self.fail('Qt condition not reached')
    def row(self,state='pending'):
        return dict(id=str(uuid4()),operation='csv_diagnosis',path='/synthetic/measure.csv',display_path='/synthetic/measure.csv',
            state=state,request_sha256='f'*64,purpose='核对漏采',allowed_scopes=['file','directory','deny'],
            share_notice='读取内容会发送给模型分析；不修改文件。')
    def test_waiting_preserves_job_evidence_nonmodal_approval_binds_hash(self):
        row=self.row();job=self.page.job
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[row],local_requests=[row],result=dict(reply='已查到前半段数据')))
        self.assertEqual(self.page.job,job);self.assertTrue(self.page.cancel.isEnabled());self.assertFalse(self.page.message.isReadOnly())
        self.assertIn('已查到前半段数据',self.page.transcript.toPlainText())
        self.assertEqual(len(self.page._permission_dialogs),1);dialog=self.page._permission_dialogs[0]
        self.assertEqual(dialog.windowModality(),Qt.WindowModality.NonModal)
        self.assertIn('模型', ' '.join(label.text() for label in dialog.findChildren(QLabel)))
        calls=[];self.client.diagnostic=lambda action,payload=None:calls.append((action,payload)) or {'result':{'status':'permission_approved'}}
        dialog.accept();self.pump(lambda:not self.page._workers)
        self.assertEqual(calls[0][0],'local-approve');self.assertEqual(calls[0][1]['expected_request_sha256'],row['request_sha256'])
        self.assertNotIn('path',calls[0][1]);self.assertIn('接下来',self.page.identity.text())

    def test_stage_not_duplicated_web_decision_closes_prompt_without_deny(self):
        row=self.row();stage=dict(state='WAITING_PERMISSION',permission_requests=[row],local_requests=[row],result=dict(reply='中间结论'))
        self.page._job_status(stage);self.page._job_status(stage)
        self.assertEqual(self.page.transcript.toPlainText().count('中间结论'),1)
        calls=[];self.client.diagnostic=lambda *args:calls.append(args) or {}
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[dict(row,state='approved')],local_requests=[]))
        self.app.processEvents();self.assertEqual(self.page._permission_dialogs,[]);self.assertEqual(calls,[])

    def test_expired_retry_and_completed_resume_use_server_actions(self):
        row=self.row('expired');done=self.row('completed');calls=[]
        self.client.diagnostic=lambda action,payload=None:calls.append((action,payload)) or {'result':{}}
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[row,done],local_requests=[]))
        button=self.page.permission_list.itemWidget(self.page.permission_list.topLevelItem(0),2);self.assertEqual(button.text(),'重新申请')
        button.click();self.pump(lambda:not self.page._workers)
        self.assertEqual(calls[0][0],'local-renew');self.assertEqual(calls[0][1]['expected_request_sha256'],row['request_sha256'])
        self.assertFalse(self.page.permission_resume.isHidden());self.page.permission_resume.click();self.pump(lambda:not self.page._workers)
        self.assertEqual(calls[-1][0],'local-resume')

    def test_waiting_history_restores_active_and_logout_clears_pending_ui_without_decision(self):
        row=self.row();job=self.page.job;self.page.job=None
        self.page._restore_active_jobs([dict(id=job,state='WAITING_PERMISSION',created_at='2026-11-01T01:00:00Z')])
        self.assertEqual(self.page.job,job);self.assertTrue(self.page.cancel.isEnabled())
        self.page._local_permissions([row]);calls=[];self.client.diagnostic=lambda *args:calls.append(args) or {}
        self.page.revoke();self.app.processEvents()
        self.assertFalse(self.page._permission_dialogs);self.assertTrue(self.page.permission_list.isHidden());self.assertEqual(calls,[])

    def test_approved_read_waits_for_real_data_without_repeat_permission_prompt(self):
        text=self.page._preparation_summary({'status':'waiting_for_data','phase':'awaiting_permission','evidence':[]})
        self.assertIn('不需要重复确认',text)
        self.assertNotIn('需要你允许',text)

    def test_preparation_card_human_statistics_gaps_and_collapsed_source_details(self):
        data=dict(phase='collecting',continuation=True,evidence_complete=False,root_cause_confirmed=False,unselected_file_count=2,
            evidence=[dict(type='local_diagnostic',data=dict(path='$STATE/measure.csv',operation='csv_diagnosis',complete=False,
                data=dict(rows_analyzed=15001,gap_count=1,time_basis='numeric_unit_unknown',numeric_statistics=[dict(column='current',min=1,max=999)]))),
                dict(type='local_diagnostic',data=dict(path='$STATE/comm.log',complete=True,data=dict(lines_analyzed=5003,match_count=2,matches=[dict(text='timeout waiting for heartbeat')])) )],
            issues=[dict(reason='本机日志尚未全部检查',path='$STATE/comm.log')])
        self.page._display('assistant','正在检查', [dict(type='diagnosis_preparation',title='诊断已查证范围',data=data,source='machine_source')])
        body=self.page.transcript.toPlainText()
        for expected in ('15001行','999','1处采样间隔异常','5003行日志','时间单位或时区','2份候选文件未检查','暂未确认完整原因'):
            self.assertIn(expected,body)
        self.assertNotIn('numeric_statistics',body);self.assertFalse(self.page.evidence.topLevelItem(0).isExpanded())
        self.assertFalse(self.page.evidence_note.isHidden())

    def test_uploaded_csv_temporal_unknown_and_range_remain_visible_in_summary(self):
        data=dict(evidence_complete=False,evidence=[dict(title='上传的测量CSV',data=dict(data=dict(
            range=dict(rows_analyzed=30),full_file_statistics=False,
            columns=[dict(name='current',minimum=1,maximum=9)],
            time_analysis=dict(time_basis='relative_origin_unknown',gap_count=2))))])
        text=self.page._preparation_summary(data)
        for expected in ('30行','本次已读范围','current范围：1～9','2处采样间隔异常','时间起点尚未确定'):
            self.assertIn(expected,text)
        detail=data['evidence'][0]['data']['data']
        detail.update(time_analysis=dict(time_basis='mixed_time_bases'),unprocessed_numeric_column_indices=[64,65],
                      numeric_statistics=[dict(column='value',min=-1e308,max=1e308,numeric_overflow=True)])
        text=self.page._preparation_summary(data)
        for expected in ('不同的时间口径','2列未做数值统计','没有把未知值算成零'):self.assertIn(expected,text)

    def test_approved_expired_and_sync_failure_are_distinct_business_states(self):
        row=self.row('approved')
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[row],local_requests=[]))
        self.assertIn('允许不代表已经读到内容',self.page.progress.text())
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[dict(row,state='expired')],local_requests=[]))
        self.assertIn('重新申请',self.page.progress.text())
        self.page._job_status(dict(state='WAITING_PERMISSION',permission_requests=[],local_requests=[],local_results_note='本机检查缓存未保存；正在同步。'))
        self.assertIn('未保存',self.page.identity.text());self.assertIn('连接恢复后',self.page.progress.text())


if __name__=='__main__':unittest.main()
