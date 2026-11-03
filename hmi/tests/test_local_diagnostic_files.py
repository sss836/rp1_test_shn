"""Job-bound local reads against synthetic roots; no device, service or real home scan."""
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import unittest
from uuid import uuid4

from factory_hmi.core.diagnostic_files import DiagnosticFileReader
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from fastapi import HTTPException


class LocalFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project, self.state, self.outside = (self.root / name for name in ('project', 'state', 'outside'))
        for path in (self.project, self.state, self.outside): path.mkdir()
        self.reader = DiagnosticFileReader(self.state, roots=[self.project, self.state])
        self.scope = ('generation', 'owner', 'job')

    def tearDown(self): self.temp.cleanup()

    def request(self, operation, path=None, **values):
        return self.reader.run(self.scope, dict(operation=operation, path=str(path) if path is not None else None, **values))

    def test_capabilities_are_real_roots_and_explicit_read_only(self):
        result = self.request('capabilities')
        self.assertEqual(result['roots'], ['$PROJECT', '$STATE'])
        self.assertTrue(result['read_only'])
        self.assertFalse(result['os_permissions_bypassed'])
        self.assertEqual(result['max_chunk_bytes'], 65536)

    def test_read_chunks_hash_original_bytes_and_round_trip_path_after_privacy(self):
        from app.services.assistant_privacy import public_evidence
        path = self.project / '完整源码.py'
        path.write_bytes(b'line one\nline two\n')
        first = self.request('read', '$PROJECT/完整源码.py', limit=9)
        model_view = public_evidence(first)
        self.assertEqual(model_view['path'], '$PROJECT/完整源码.py')
        second = self.request('read', model_view['path'], offset=first['next_offset'], limit=65536)
        self.assertEqual(first['content'] + second['content'], path.read_text())
        self.assertEqual(first['sha256'], hashlib.sha256(b'line one\n').hexdigest())
        self.assertEqual(first['sha256_scope'], 'byte_range')
        self.assertFalse(first['eof']); self.assertTrue(second['eof'])
        self.assertEqual(second['unread_bytes'], 0)
        self.assertEqual(first['file_mtime_ns'], path.stat().st_mtime_ns)

    def test_default_state_alias_and_redacted_fake_home_path_is_never_read(self):
        (self.state / 'report.txt').write_text('fact')
        self.assertEqual(self.request('read', '$STATE/report.txt')['content'], 'fact')
        bad = self.request('read', '/home/[用户]/private.txt')
        self.assertEqual(bad['status'], 'error')
        self.assertIn('别名', bad['error'])

    def test_outside_file_directory_all_grants_are_bound_to_job_and_revoke(self):
        one = self.outside / 'one.csv'; one.write_text('observed')
        two = self.outside / 'two.csv'; two.write_text('second')
        self.assertEqual(self.request('read', one)['status'], 'permission_required')
        self.reader.grant(self.scope, one, 'file')
        self.assertEqual(self.request('read', one)['content'], 'observed')
        self.assertEqual(self.request('read', two)['status'], 'permission_required')
        self.reader.grant(self.scope, one, 'directory')
        self.assertEqual(self.request('read', two)['content'], 'second')
        other = self.reader.run(('generation', 'owner', 'other-job'), dict(operation='read', path=str(one)))
        self.assertEqual(other['status'], 'permission_required')
        self.reader.revoke(self.scope)
        self.assertEqual(self.request('read', one)['status'], 'permission_required')
        self.reader.grant(self.scope, one, 'all')
        self.assertEqual(self.request('read', two)['content'], 'second')
        self.reader.revoke()
        self.assertEqual(self.request('read', two)['status'], 'permission_required')

    def test_symlink_escape_is_not_implicitly_authorized(self):
        outside = self.outside / 'private.txt'; outside.write_text('private')
        (self.project / 'link').symlink_to(outside)
        result = self.request('read', '$PROJECT/link')
        self.assertEqual(result['status'], 'permission_required')
        self.assertEqual(result['path'], str(outside))
        self.reader.grant(self.scope, outside, 'file')
        self.assertEqual(self.request('read', '$PROJECT/link')['content'], 'private')
        alternate = self.outside / 'new.txt'; alternate.write_text('new private')
        (self.project / 'link').unlink(); (self.project / 'link').symlink_to(alternate)
        self.assertEqual(self.request('read', '$PROJECT/link')['status'], 'permission_required')

    def test_os_denial_remains_denial_and_no_permission_escalation(self):
        path = self.outside / 'blocked'; path.write_text('private')
        self.reader.grant(self.scope, path, 'file')
        with patch('factory_hmi.core.diagnostic_files.os.open', side_effect=PermissionError('synthetic OS denial')):
            result = self.request('read', path)
        self.assertEqual(result['status'], 'os_permission_denied')
        self.assertIn('可读副本', result['advice'])
        self.assertFalse(result['eof'])

    def test_fifo_and_device_paths_are_not_opened(self):
        fifo = self.project / 'fifo'; os.mkfifo(fifo)
        with patch('factory_hmi.core.diagnostic_files.os.open') as opened:
            result = self.request('read', fifo)
        self.assertEqual(result['status'], 'error')
        opened.assert_not_called()
        self.reader.grant(self.scope, self.outside, 'all')
        self.assertEqual(self.request('read', '/dev/null')['status'], 'unsupported_file')

    def test_list_offset_has_continuation_and_links_do_not_expand_authority(self):
        for i in range(4): (self.project / f'file-{i}.txt').write_text('data')
        first = self.request('list', '$PROJECT', limit=100)
        self.assertEqual(len(first['entries']), 1)
        self.assertFalse(first['eof'])
        second = self.request('list', '$PROJECT', offset=first['next_offset'], limit=65536)
        self.assertEqual(len(second['entries']), 3)
        self.assertTrue(second['eof'])
        self.assertTrue(all(item['path'].startswith('$PROJECT/') for item in second['entries']))

    def test_search_is_literal_chunked_reports_binary_and_symlink_unread(self):
        (self.project / 'one.txt').write_text('needle * is literal\n')
        (self.project / 'two.bin').write_bytes(b'\x00\xff')
        (self.project / 'dir-link').symlink_to(self.outside, target_is_directory=True)
        result = self.request('search', '$PROJECT', query='needle *', limit=65536)
        self.assertEqual(len(result['matches']), 1)
        self.assertEqual(result['matches'][0]['path'], '$PROJECT/one.txt')
        self.assertFalse(result['eof'])
        self.assertTrue(any(item['reason'] == 'binary_not_text_searched' for item in result['unread']))
        self.assertTrue(any(item['reason'] == 'symlink_directory_not_traversed' for item in result['unread']))
        direct = self.request('search', '$PROJECT/one.txt', query='needle *', offset=0, limit=7)
        self.assertFalse(direct['eof'])
        self.assertIn('byte range', direct['search_scope'])

    def test_redaction_does_not_rewrite_source_or_source_hash(self):
        path = self.project / 'config.txt'
        raw = b'api_key=private-value password=hidden-value user@example.com\n'
        path.write_bytes(raw)
        before = path.stat()
        result = self.request('read', path)
        self.assertNotIn('private-value', result['content'])
        self.assertNotIn('hidden-value', result['content'])
        self.assertNotIn('user@example.com', result['content'])
        self.assertEqual(result['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)

    def test_invalid_operations_and_limits_never_execute(self):
        for operation, values in [('execute', {}), ('read', {'limit':65537}), ('list', {'offset':-1}), ('read', {'limit':True})]:
            with self.subTest(operation=operation), patch('factory_hmi.core.diagnostic_files.os.open') as opened:
                self.assertEqual(self.request(operation, '$PROJECT', **values)['status'], 'invalid_request')
                opened.assert_not_called()

    def test_execution_summary_all_records_unknown_corrupt_and_ledger_unchanged(self):
        from factory_hmi.core.report import ReportManager
        root = self.state / 'executions'; root.mkdir()
        for index, status in enumerate(['passed','failed','blocked','unconfigured','not-known','corrupt']):
            folder = root / f'test-{index}'; (folder/'analysis').mkdir(parents=True)
            manifest = dict(test_id=folder.name, execution_uuid=str(uuid4()), sample_id='SYNTHETIC',
                            started_at='2026-10-30T00:00:00Z', ended_at='2026-10-30T01:00:00Z')
            (folder/'manifest.json').write_text(json.dumps(manifest))
            (folder/'analysis/summary.json').write_text('invalid' if status=='corrupt' else json.dumps(dict(verdict=dict(
                status=status, criteria_version='UNCONFIGURED' if status=='unconfigured' else 'hmi-runtime-v2',
                reasons=['synthetic failed reason'] if status=='failed' else ['synthetic interrupted'] if status=='blocked' else []))))
        ledger = root/'执行台账.csv'; ledger.write_bytes(b'original ledger bytes')
        before = {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns, p.stat().st_ino) for p in self.state.rglob('*') if p.is_file()}
        with patch.object(ReportManager, 'history', side_effect=AssertionError('unsafe path-based history must not be used')), patch.object(ReportManager, 'write_ledger', side_effect=AssertionError('not a diagnostic read')):
            result = self.request('execution_summary', '$STATE', limit=65536)
        self.assertEqual(result['history_visible_count'], 5)
        self.assertEqual(result['total_records'], 6)
        self.assertEqual(result['verdict_counts'], dict(passed=1, failed=1, blocked=1, unconfigured=1, unknown=1, corrupt=1))
        self.assertEqual(result['reasons'], {'synthetic failed reason':1, 'synthetic interrupted':1})
        self.assertEqual(result['failed_reasons'], {'synthetic failed reason':1})
        self.assertTrue(result['eof'])
        self.assertTrue(result['counts_complete'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(before, {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns, p.stat().st_ino) for p in self.state.rglob('*') if p.is_file()})

    def test_directory_batches_keep_continuation_without_unbounded_scan(self):
        for index in range(600): (self.project/f'entry-{index}.txt').write_text('fact')
        seen, offset = [], 0
        for _ in range(10):
            result=self.request('list','$PROJECT',offset=offset,limit=65536)
            self.assertLessEqual(len(result['entries']),512)
            seen.extend(item['name'] for item in result['entries'])
            if result['eof']: break
            self.assertGreater(result['next_offset'],offset)
            offset=result['next_offset']
        self.assertTrue(result['eof'])
        self.assertEqual(len(seen),600)
        self.assertEqual(len(set(seen)),600)

    def test_summary_counter_ranges_can_be_aggregated_without_counting_blocked_as_failed(self):
        root=self.state/'executions'; root.mkdir()
        for index,status in enumerate(['failed','blocked','unconfigured']):
            folder=root/f'test-{index}'; (folder/'analysis').mkdir(parents=True)
            (folder/'manifest.json').write_text(json.dumps(dict(test_id=folder.name)))
            (folder/'analysis/summary.json').write_text(json.dumps(dict(verdict=dict(status=status,criteria_version='v1',reasons=[status+' reason']))))
        first=self.request('execution_summary','$STATE',limit=1)
        self.assertFalse(first['eof']); self.assertFalse(first['counts_complete'])
        self.assertIsNone(first['total_records'])
        counters={}; failed={}; offset=0
        for _ in range(5):
            batch=self.request('execution_summary','$STATE',offset=offset,limit=1)
            self.assertEqual(batch['counter_range']['start_offset'],offset)
            for status,n in batch['verdict_counts'].items(): counters[status]=counters.get(status,0)+n
            for reason,n in batch['failed_reasons'].items(): failed[reason]=failed.get(reason,0)+n
            if batch['eof']: break
            offset=batch['next_offset']
        self.assertEqual(counters,dict(failed=1,blocked=1,unconfigured=1))
        self.assertEqual(failed,{'failed reason':1})

    def test_huge_first_reason_keeps_broker_receipt_bounded_and_reports_omissions(self):
        folder=self.state/'executions'/'huge'; (folder/'analysis').mkdir(parents=True)
        (folder/'manifest.json').write_text(json.dumps(dict(test_id='huge')))
        raw=json.dumps(dict(verdict=dict(status='failed',criteria_version='v'*120000,
            reasons=['x'*120000]+[f'reason-{i}' for i in range(20)]))).encode()
        (folder/'analysis/summary.json').write_bytes(raw)
        started=time.monotonic()
        result=self.request('execution_summary','$STATE',limit=1)
        self.assertLess(time.monotonic()-started,5,'single long reason must not overrun the local tool budget')
        self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),112000)
        self.assertEqual(result['verdict_counts'],dict(failed=1))
        self.assertFalse(result['reasons_complete'])
        self.assertEqual(result['omitted_reason_items'],13)
        self.assertTrue(result['records'][0]['criteria_version_truncated'])
        self.assertEqual(result['records'][0]['sha256'],hashlib.sha256(raw).hexdigest())
        self.assertTrue(result['eof'])

    def test_parent_directory_swap_after_authorization_never_reads_or_lists_outside(self):
        for operation in ('read', 'list', 'search'):
            with self.subTest(operation=operation):
                parent=self.project/operation; parent.mkdir()
                nested=parent/'nested'; nested.mkdir()
                (nested/'evidence.txt').write_text('authorized evidence')
                other=self.outside/operation; (other/'nested').mkdir(parents=True)
                (other/'nested'/'outside-secret.txt').write_text('NEVER_RETURN_SECRET')
                (other/'nested'/'evidence.txt').write_text('NEVER_RETURN_SECRET')
                target=nested/'evidence.txt' if operation=='read' else nested
                checked, release=threading.Event(),threading.Event()
                original=self.reader._allowed
                def allowed(scope,path):
                    value=original(scope,path)
                    if path==target and not checked.is_set():
                        checked.set(); release.wait(2)
                    return value
                result=[]
                with patch.object(self.reader,'_allowed',allowed):
                    worker=threading.Thread(target=lambda:result.append(self.request(operation,target,query='SECRET')))
                    worker.start(); self.assertTrue(checked.wait(1))
                    parent.rename(parent.with_name(operation+'-old')); parent.symlink_to(other,target_is_directory=True)
                    release.set(); worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertIn(result[0]['status'], {'error','partial'})
                self.assertNotIn('NEVER_RETURN_SECRET',json.dumps(result[0]))
                self.assertNotIn('outside-secret.txt',json.dumps(result[0]))

    def test_parent_swap_after_inode_anchor_is_not_reopened_as_outside_content(self):
        parent=self.project/'anchor'; parent.mkdir()
        target=parent/'data.txt'; target.write_text('authorized')
        other=self.outside/'anchor'; other.mkdir(); (other/'data.txt').write_text('NEVER_OUTSIDE')
        checked,release=threading.Event(),threading.Event()
        original=self.reader._check_fd
        def check(scope,fd,expected):
            value=original(scope,fd,expected)
            if expected==target and not checked.is_set():
                checked.set(); release.wait(2)
            return value
        result=[]
        with patch.object(self.reader,'_check_fd',check):
            worker=threading.Thread(target=lambda:result.append(self.request('read',target)))
            worker.start(); self.assertTrue(checked.wait(1))
            parent.rename(parent.with_name('anchor-old')); parent.symlink_to(other,target_is_directory=True)
            release.set(); worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]['status'],'error')
        self.assertNotIn('NEVER_OUTSIDE',json.dumps(result[0]))

    def test_private_label_split_and_mid_value_offsets_never_reveal_secret(self):
        for marker in ('password=', 'api_key=', 'cookie='):
            with self.subTest(marker=marker):
                secret='NEVER_VISIBLE_CREDENTIAL'
                raw=('prefix '+marker+secret+'\\npublic next line\\n').encode()
                path=self.project/'sensitive.env'; path.write_bytes(raw)
                for offset,limit in [(7,4),(len('prefix '+marker),5),(len('prefix '+marker)+5,12)]:
                    result=self.request('read',path,offset=offset,limit=limit)
                    self.assertEqual(result['sha256'],hashlib.sha256(raw[offset:offset+limit]).hexdigest())
                    self.assertNotIn(raw[offset:offset+limit].decode(), result['content'] or '')
                    self.assertEqual(result['redaction_status'],'sensitive_spans_withheld')
                    self.assertFalse(result['model_content_complete'])
                self.assertEqual(path.read_bytes(),raw)

    def test_pem_and_known_private_key_middle_offsets_return_metadata_only(self):
        for name in ('credentials.pem','id_ed25519','device.key'):
            raw=b'-----BEGIN ' + b'PRIVATE KEY-----\\nNEVER_VISIBLE_KEY_BODY\\n-----END PRIVATE KEY-----\\n'
            path=self.project/name; path.write_bytes(raw)
            result=self.request('read',path,offset=35,limit=8)
            self.assertIsNone(result['content'])
            self.assertEqual(result['redaction_status'],'private_key_body_withheld')
            self.assertEqual(result['sha256'],hashlib.sha256(raw[35:43]).hexdigest())
            self.assertEqual(path.read_bytes(),raw)


class LocalBridgeTests(unittest.TestCase):
    def fixture(self, root):
        state, project, outside = (Path(root)/name for name in ('state','project','outside'))
        for p in (state,project,outside): p.mkdir()
        bridge = DiagnosticBridge(state)
        bridge.files = DiagnosticFileReader(state, roots=[project,state])
        binding = dict(owner='window',generation='generation',operator='synthetic',station='A9')
        bridge._binding = binding
        job, request_id = str(uuid4()), str(uuid4())
        return bridge, binding, project, outside, job, request_id

    def install_remote(self, bridge, job, item):
        posts, calls = [], []
        def remote(binding, method, path, payload=None, **kw):
            calls.append((method, path))
            if path == f'/api/v1/assistant/jobs/{job}': return dict(id=job, state='RUNNING')
            if '/local-requests?' in path: return dict(job_id=job, items=[item])
            if path.endswith('/local-results'):
                posts.append(payload); return dict(accepted=True, replayed=False)
            if path.endswith('/cancel'): return dict(id=job,state='CANCELLED')
            raise AssertionError(path)
        bridge._remote = remote
        return posts, calls

    def test_authorized_read_posts_bound_identity_without_approval(self):
        with TemporaryDirectory() as root:
            bridge, binding, project, outside, job, rid = self.fixture(root)
            (project/'source.txt').write_text('source fact')
            item = dict(id=rid,operation='read',path='$PROJECT/source.txt',offset=0,limit=65536)
            posts, calls = self.install_remote(bridge,job,item)
            result = bridge.call('window','job',dict(generation='generation',id=job))
            self.assertEqual(result['local_requests'], [])
            self.assertEqual(posts[0]['result']['content'], 'source fact')
            self.assertEqual(posts[0]['installation_id'], bridge.installation)
            self.assertEqual(posts[0]['window_id'], 'window')
            self.assertIn('window_id=window', calls[1][1])

    def test_outside_requires_ui_grant_uses_saved_path_not_forged_payload(self):
        with TemporaryDirectory() as root:
            bridge, binding, project, outside, job, rid = self.fixture(root)
            target = outside/'data.csv'; target.write_text('outside fact')
            item = dict(id=rid,operation='read',path=str(target),offset=0,limit=65536)
            posts, _ = self.install_remote(bridge,job,item)
            result = bridge.call('window','job',dict(generation='generation',id=job))
            self.assertEqual(result['local_requests'][0]['path'], str(target))
            self.assertEqual(posts, [])
            answer = bridge.call('window','local-approve',dict(generation='generation',id=job,request_id=rid,scope='file',path='/forged/path'))
            self.assertEqual(answer['result']['content'], 'outside fact')
            self.assertEqual(len(posts), 1)
            bridge.call('window','cancel',dict(generation='generation',id=job))
            self.assertEqual(bridge.files.run(('generation','window',job), item)['status'], 'permission_required')

    def test_denial_and_revoke_are_final_without_reading(self):
        with TemporaryDirectory() as root:
            bridge, binding, project, outside, job, rid = self.fixture(root)
            target=outside/'file'; target.write_text('not read')
            item=dict(id=rid,operation='read',path=str(target))
            posts,_=self.install_remote(bridge,job,item)
            bridge.call('window','job',dict(generation='generation',id=job))
            answer=bridge.call('window','local-approve',dict(generation='generation',id=job,request_id=rid,scope='deny'))
            self.assertEqual(answer['result']['status'],'permission_denied')
            self.assertNotIn('content',posts[0]['result'])
            bridge.revoke()
            self.assertEqual(bridge._local_pending,{})
            self.assertEqual(bridge._local_results,{})
            with self.assertRaises(HTTPException): bridge.call('window','local-approve',dict(generation='generation',id=job,request_id=rid,scope='all'))

    def test_lost_result_ack_reposts_identical_receipt(self):
        with TemporaryDirectory() as root:
            bridge,binding,project,outside,job,rid=self.fixture(root)
            (project/'source').write_text('fact')
            item=dict(id=rid,operation='read',path='$PROJECT/source')
            posts,_=self.install_remote(bridge,job,item)
            original=bridge._remote
            def remote(*args,**kwargs):
                result=original(*args,**kwargs)
                if args[2].endswith('/local-results') and len(posts)==1: raise HTTPException(502,'synthetic lost ack')
                return result
            bridge._remote=remote
            with self.assertRaises(HTTPException):bridge.call('window','job',dict(generation='generation',id=job))
            bridge.call('window','job',dict(generation='generation',id=job))
            self.assertEqual(posts[0],posts[1])
            self.assertEqual(bridge._local_processing,set())

    def test_permission_error_after_grant_is_reported_and_not_reprompted(self):
        with TemporaryDirectory() as root:
            bridge,binding,project,outside,job,rid=self.fixture(root)
            target=outside/'file'; target.write_text('private')
            item=dict(id=rid,operation='read',path=str(target))
            posts,_=self.install_remote(bridge,job,item)
            bridge.call('window','job',dict(generation='generation',id=job))
            with patch('factory_hmi.core.diagnostic_files.os.open',side_effect=PermissionError('synthetic denied')):
                bridge.call('window','local-approve',dict(generation='generation',id=job,request_id=rid,scope='file'))
            self.assertEqual(posts[0]['result']['status'],'os_permission_denied')
            result=bridge.call('window','job',dict(generation='generation',id=job))
            self.assertEqual(result['local_requests'],[])
            self.assertEqual(posts[0],posts[1])

    def test_symlink_replacement_after_permission_request_is_not_granted(self):
        with TemporaryDirectory() as root:
            bridge,binding,project,outside,job,rid=self.fixture(root)
            target=outside/'file'; target.write_text('old')
            item=dict(id=rid,operation='read',path=str(target))
            posts,_=self.install_remote(bridge,job,item)
            bridge.call('window','job',dict(generation='generation',id=job))
            other=outside/'other'; other.write_text('new')
            target.unlink(); target.symlink_to(other)
            bridge.call('window','local-approve',dict(generation='generation',id=job,request_id=rid,scope='all'))
            self.assertEqual(posts[0]['result']['status'],'path_changed')
            self.assertNotIn('content',posts[0]['result'])


if __name__=='__main__':unittest.main()
