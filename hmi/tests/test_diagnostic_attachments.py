"""Inert attachment bytes and isolated Qt/ASGI lifecycle; never starts device I/O."""
import asyncio
import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import quote, unquote
from uuid import uuid4

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication
from fastapi import HTTPException
from factory_hmi.client import GatewayClient, GatewayHTTPError
from factory_hmi.desktop.diagnostics_page import DiagnosticsPage
from factory_hmi.desktop.lease_heartbeat import LeaseHeartbeat
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from test_shared_diagnostics import Client, Manager


class AttachmentClient(Client):
    def __init__(self):
        super().__init__()
        self.uploads = []
        self.submits = []

    def diagnostic_attachment(self, raw, filename, generation):
        self.uploads.append((raw, filename, generation, threading.get_ident()))
        return {'result': {'id': str(uuid4()), 'name': filename, 'size': len(raw), 'kind': 'binary'}}

    def diagnostic(self, action, payload=None):
        if action == 'submit':
            self.submits.append(dict(payload, attachments=list(payload.get('attachments', []))))
        return super().diagnostic(action, payload)


class AttachmentDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.client = AttachmentClient()
        self.page = DiagnosticsPage(self.client)
        self.page.binding = self.client.status
        self.page._controls()
        self.releases = []

    def tearDown(self):
        for event in self.releases: event.set()
        self.page.stop()
        self.page.pool.waitForDone(3000)
        self.app.processEvents()
        self.page.deleteLater()
        self.directory.cleanup()

    def pump(self, condition, seconds=3):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents()
            if condition(): return
            time.sleep(.003)
        self.fail('Qt condition not reached')

    def file(self, name, data=b'raw CSV bytes,not a formal archive\n'):
        path = Path(self.directory.name) / name
        path.write_bytes(data)
        return path

    def test_multi_select_names_sizes_remove_and_no_early_upload(self):
        paths = [self.file('测量数据.csv'), self.file('source.tar.gz'), self.file('device.unknown')]
        with patch('factory_hmi.desktop.diagnostics_page.QFileDialog.getOpenFileNames', return_value=([str(p) for p in paths], '')):
            self.page._choose_attachments()
        self.assertEqual(len(self.page.attachments), 3)
        self.assertEqual(self.page.attachment_list.topLevelItem(0).text(0), '测量数据.csv')
        self.assertIn('KiB', self.page.attachment_list.topLevelItem(0).text(1))
        self.assertEqual(self.client.uploads, [])
        self.page.attachment_list.topLevelItem(1).setSelected(True)
        self.page._remove_attachments()
        self.assertEqual([a['filename'] for a in self.page.attachments], ['测量数据.csv', 'device.unknown'])
        self.assertTrue(all(p.exists() for p in paths))

    def test_count_includes_screenshot_and_oversized_empty_rejected(self):
        self.page._set_image((b'preview', 'image/png'))
        self.page._add_attachments([self.file('one.zip'), self.file('two.tar')])
        self.page._add_attachments([self.file('third.csv')])
        self.assertEqual(len(self.page.attachments), 2)
        self.assertIn('最多3个', self.page.identity.text())
        self.page._set_image(None)
        self.page.attachments.clear()
        large = self.file('large.bin')
        with large.open('wb') as stream: stream.truncate(32 * 1024**2 + 1)
        self.page._add_attachments([large])
        self.assertEqual(self.page.attachments, [])
        self.page._add_attachments([self.file('empty.txt', b'')])
        self.assertEqual(self.page.attachments, [])

    def test_file_dialog_results_after_logout_are_discarded(self):
        path = self.file('previous-account.csv')
        def selected(*args):
            self.page.revoke()
            return [str(path)], ''
        with patch('factory_hmi.desktop.diagnostics_page.QFileDialog.getOpenFileNames', side_effect=selected):
            self.page._choose_attachments()
        self.assertEqual(self.page.attachments, [])
        self.assertEqual(self.client.uploads, [])

    def test_attachment_only_sends_original_bytes_in_worker_and_clears_on_ack(self):
        source = self.file('参数包.tar.gz', b'\x00\xffopaque')
        before = source.stat()
        self.page._add_attachments([source])
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(self.client.uploads[0][:3], (b'\x00\xffopaque', '参数包.tar.gz', self.client.status['generation']))
        self.assertNotEqual(self.client.uploads[0][3], threading.get_ident())
        self.assertEqual(self.client.submits[0]['message'], '请分析所附文件。')
        self.assertEqual(len(self.client.submits[0]['attachments']), 1)
        self.assertEqual(self.page.attachments, [])
        self.assertEqual(source.read_bytes(), b'\x00\xffopaque')
        self.assertEqual(source.stat().st_mtime_ns, before.st_mtime_ns)

    def test_partial_upload_retry_reuses_ids_and_request_key(self):
        self.page._add_attachments([self.file('one.csv'), self.file('two.zip')])
        original = self.client.diagnostic_attachment
        attempts = []
        def upload(raw, name, generation):
            attempts.append(name)
            if attempts == ['one.csv', 'two.zip']:
                raise OSError('synthetic interrupted upload')
            return original(raw, name, generation)
        self.client.diagnostic_attachment = upload
        self.page._send()
        self.pump(lambda: not self.page._workers)
        key = self.page.submission['data']['request_key']
        first_id = self.page.submission['data']['attachments'][0]
        self.assertEqual(len(self.page.attachments), 2)
        self.assertEqual(self.client.submits, [])
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(attempts, ['one.csv', 'two.zip', 'two.zip'])
        self.assertEqual(self.client.submits[0]['request_key'], key)
        self.assertEqual(self.client.submits[0]['attachments'][0], first_id)

    def test_lost_job_ack_retry_does_not_reupload_or_change_key(self):
        self.page._add_attachments([self.file('test.csv')])
        original = self.client.diagnostic
        keys = []
        def call(action, payload=None):
            if action == 'submit':
                keys.append(payload['request_key'])
                if len(keys) == 1: raise OSError('synthetic lost job ack')
            return original(action, payload)
        self.client.diagnostic = call
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(len(self.page.attachments), 1)
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(self.client.uploads), 1)

    def test_file_changed_before_send_never_uploads(self):
        path = self.file('original.csv')
        self.page._add_attachments([path])
        path.write_bytes(b'changed measurement file')
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(self.client.uploads, [])
        self.assertIn('已变化', self.page.identity.text())
        self.assertEqual(len(self.page.attachments), 1)
        self.page._cancel()
        self.assertIsNone(self.page.submission)

    def test_cancel_upload_keeps_draft_and_stops_next_file_and_job(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.client.diagnostic_attachment
        def upload(*args):
            entered.set(); release.wait(2)
            return original(*args)
        self.client.diagnostic_attachment = upload
        self.page._add_attachments([self.file('one.csv'), self.file('two.zip')])
        self.page._send()
        self.pump(entered.is_set)
        self.assertTrue(self.page.cancel.isEnabled())
        self.page._cancel(); release.set()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(len(self.client.uploads), 1)
        self.assertEqual(self.client.submits, [])
        self.assertEqual(len(self.page.attachments), 2)
        self.assertIsNone(self.page.job)
        self.assertIsNone(self.page.submission)
        self.assertEqual(self.page._pending, set())

    def test_cancel_after_lost_job_ack_resolves_same_key_then_cancels(self):
        self.page._add_attachments([self.file('test.csv')])
        original = self.client.diagnostic
        keys = []
        def call(action, payload=None):
            if action == 'submit':
                keys.append(payload['request_key'])
                if len(keys) == 1: raise OSError('lost ack')
            return original(action, payload)
        self.client.diagnostic = call
        self.page._send()
        self.pump(lambda: not self.page._workers)
        self.page._cancel()
        self.pump(lambda: not self.page._workers)
        self.assertEqual(keys[0], keys[1])
        self.assertTrue(any(c[0] == 'cancel' for c in self.client.calls))
        self.assertEqual(len(self.client.uploads), 1)

    def test_logout_or_replacement_during_upload_never_submits_next_attachment(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def upload(*args):
            entered.set(); release.wait(2)
            return {'result': {'id': str(uuid4())}}
        self.client.diagnostic_attachment = upload
        self.page._add_attachments([self.file('old-private.csv'), self.file('second.zip')])
        self.page._send(); self.pump(entered.is_set)
        self.page.revoke()
        replacement = dict(self.client.status, generation='new-account')
        self.page.binding = replacement
        self.page._controls()
        release.set(); self.pump(lambda: not self.page._workers)
        self.assertEqual(self.client.submits, [])
        self.assertEqual(self.page.attachments, [])
        self.assertEqual(self.page.binding['generation'], 'new-account')
        self.assertEqual(self.page._pending, set())

    def test_forbidden_upload_revokes_private_draft(self):
        self.page._add_attachments([self.file('private.csv')])
        def upload(*_): raise GatewayHTTPError('revoked', status_code=403)
        self.client.diagnostic_attachment = upload
        self.page._send(); self.pump(lambda: not self.page._workers)
        self.assertIsNone(self.page.binding)
        self.assertEqual(self.page.attachments, [])
        self.assertEqual(self.page._pending, set())

    def test_named_screenshot_keeps_original_unicode_filename(self):
        self.page.image_name = '原始截图.png'
        self.page._set_image((b'opaque image bytes', 'image/png'))
        self.page._send(); self.pump(lambda: not self.page._workers)
        self.assertEqual(self.client.uploads[0][1], '原始截图.png')
        self.assertEqual(len(self.client.submits[0]['attachments']), 1)

    def test_cancel_while_job_post_in_flight_cancels_returned_job(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.client.diagnostic
        def call(action, payload=None):
            if action == 'submit':
                entered.set(); release.wait(2)
            return original(action, payload)
        self.client.diagnostic = call
        self.page.message.setPlainText('只读诊断')
        self.page._send(); self.pump(entered.is_set)
        self.page._cancel(); release.set()
        self.pump(lambda: not self.page._workers)
        self.assertTrue(any(c[0] == 'cancel' for c in self.client.calls))
        self.assertEqual(len(self.client.submits), 1)

    def test_blocked_upload_does_not_delay_ten_control_lease_callbacks(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def upload(*args):
            entered.set(); release.wait(2)
            return {'result': {'id': str(uuid4())}}
        self.client.diagnostic_attachment = upload
        self.page._add_attachments([self.file('large.tar.gz')])
        self.page._send(); self.pump(entered.is_set)
        client = GatewayClient('http://127.0.0.1:65534')
        client._lease_generation = 1
        client.observe_lease = lambda _: True
        lease = LeaseHeartbeat(client, lambda: True, self.page, manager=Manager())
        renewed = []; lease.renewed.connect(renewed.append)
        timer = QTimer(self.page); timer.setInterval(10); timer.timeout.connect(lease.pulse); timer.start()
        self.pump(lambda: len(renewed) >= 10)
        timer.stop(); lease.stop(); self.page._cancel(); release.set()
        self.pump(lambda: not self.page._workers)

    def test_local_read_permission_dialog_is_nonmodal_scoped_and_no_path_submission(self):
        self.page.job = str(uuid4())
        request_id = str(uuid4())
        value = dict(state='RUNNING', local_requests=[dict(id=request_id, operation='read', path='/external/source.py')])
        self.page._job_status(value)
        self.assertEqual(len(self.page._permission_dialogs), 1)
        dialog = self.page._permission_dialogs[0]
        self.assertFalse(dialog.isModal())
        from PySide6.QtWidgets import QComboBox, QLabel
        self.assertTrue(any('统一平台模型' in label.text() for label in dialog.findChildren(QLabel)))
        dialog.findChild(QComboBox).setCurrentIndex(1)
        pulses = []
        timer = QTimer(self.page); timer.setInterval(5); timer.timeout.connect(lambda: pulses.append(1)); timer.start()
        self.pump(lambda: len(pulses) >= 5)
        timer.stop()
        self.page._job_status(value)
        self.assertEqual(len(self.page._permission_dialogs), 1)
        dialog.accept()
        self.pump(lambda: not self.page._workers)
        payload = [payload for action, payload in self.client.calls if action == 'local-approve'][0]
        self.assertEqual(payload['scope'], 'directory')
        self.assertEqual(payload['request_id'], request_id)
        self.assertEqual(payload['generation'], self.client.status['generation'])
        self.assertNotIn('path', payload)

    def test_logout_closes_pending_permission_dialog_without_cross_account_result(self):
        self.page.job = str(uuid4())
        self.page._job_status(dict(state='RUNNING', local_requests=[dict(id=str(uuid4()), operation='read', path='/private/file')]))
        self.page.revoke()
        self.assertEqual(self.page._permission_dialogs, [])
        self.assertFalse(any(action == 'local-approve' for action, _ in self.client.calls))
        self.assertEqual(self.page._permission_prompted, set())

    def test_local_os_denial_is_explicit_not_generic_success(self):
        self.page._local_approved(dict(accepted=True, result=dict(status='os_permission_denied', error='OS拒绝', advice='请选择可读副本')))
        self.assertIn('OS拒绝', self.page.identity.text())
        self.assertIn('可读副本', self.page.identity.text())


class AttachmentTransportTests(unittest.TestCase):
    def test_client_unicode_header_raw_bytes_and_separate_timeout(self):
        client = GatewayClient('http://synthetic.invalid', client_id='window', operator_token='local-only')
        response = Mock(); response.read.return_value = b'{"result":{"id":"fixture"}}'
        response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=None)
        with patch('factory_hmi.client.request.urlopen', return_value=response) as opened:
            client.diagnostic_attachment(b'\x00\xffraw', '原始 报告.tar.gz', 'generation')
        req = opened.call_args.args[0]
        self.assertEqual(req.data, b'\x00\xffraw')
        self.assertEqual(req.get_header('Content-type'), 'application/octet-stream')
        self.assertEqual(unquote(req.get_header('X-filename-utf8')), '原始 报告.tar.gz')
        self.assertEqual(opened.call_args.kwargs['timeout'], 120)
        self.assertEqual(client.lease_heartbeat_timeout, .5)

    def test_client_rejects_oversize_empty_and_unsafe_filename_but_any_extension(self):
        client = GatewayClient('http://synthetic.invalid')
        for raw, name in [(b'', 'empty'), (b'x'*(32*1024**2+1), 'big'), (b'x', '../a'), (b'x', 'a\r\nheader'), (b'x', '.')]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                client.diagnostic_attachment(raw, name, 'generation')
        with patch.object(client, '_diagnostic_upload', return_value={}) as upload:
            client.diagnostic_attachment(b'opaque', 'anything.custom', 'generation')
            self.assertEqual(upload.call_args.args[1], 'anything.custom')

    def bridge(self, root):
        bridge = DiagnosticBridge(Path(root))
        bridge._binding = dict(owner='owner', generation='generation', client=SimpleNamespace())
        return bridge

    def test_bridge_filename_and_generation_checks_do_not_call_remote(self):
        with TemporaryDirectory() as root:
            bridge = self.bridge(root); bridge._remote = Mock(return_value={'id': str(uuid4())})
            for name in ['../a.zip', 'a\x00.csv', '.', '', 'a\\b.zip']:
                with self.subTest(name=name), self.assertRaises(HTTPException):
                    bridge.call('owner', 'upload', {'generation': 'generation'}, b'x', 'application/octet-stream', name)
            self.assertEqual(bridge._remote.call_count, 0)
            for owner, generation in [('other', 'generation'), ('owner', 'old')]:
                with self.assertRaises(HTTPException):
                    bridge.call(owner, 'upload', {'generation': generation}, b'x', 'application/octet-stream', 'valid.csv')
            self.assertEqual(bridge._remote.call_count, 0)

    def test_bridge_preserves_unicode_octet_and_blocks_late_replacement(self):
        with TemporaryDirectory() as root:
            bridge = self.bridge(root)
            def remote(binding, method, path, **kw):
                self.assertEqual(path, '/api/v1/assistant/attachments')
                self.assertEqual(kw, dict(raw=b'raw', mime='application/octet-stream', filename='原始.csv'))
                bridge._binding = dict(owner='owner', generation='new')
                return {'id': str(uuid4())}
            bridge._remote = remote
            with self.assertRaises(HTTPException):
                bridge.call('owner', 'upload', {'generation': 'generation'}, b'raw', 'application/octet-stream', '原始.csv')
            self.assertTrue(bridge._slots.acquire(blocking=False)); bridge._slots.release()

    def test_bridge_upload_budget_and_ascii_safe_unicode_header(self):
        with TemporaryDirectory() as root:
            bridge = self.bridge(root)
            response = Mock(); response.read.return_value = b'{"data":{"id":"fixture"}}'
            response.__enter__ = Mock(return_value=response); response.__exit__ = Mock(return_value=None)
            opener = Mock(); opener.open.return_value = response
            bridge._binding['client'] = SimpleNamespace(base_url='https://fixture.invalid', cookies=[], _software_opener=lambda: opener)
            result = bridge.call('owner', 'upload', {'generation': 'generation'}, b'raw', 'application/octet-stream', '日志 源码.tar')
            self.assertEqual(result, {'id': 'fixture'})
            req = opener.open.call_args.args[0]
            self.assertEqual(req.data, b'raw')
            self.assertEqual(unquote(req.get_header('X-filename-utf8')), '日志 源码.tar')
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 90)

    def test_gateway_streaming_limit_unicode_header_and_no_control_lease(self):
        import httpx
        from factory_hmi.gateway.app import create_app
        async def scenario(root):
            with patch.dict(os.environ, {'RP1_FACTORY_OPERATOR_TOKEN': 'test-local'}):
                app = create_app(controller=SimpleNamespace(), data_root=Path(root))
            calls = []
            def call(*args):
                calls.append(args)
                return {'id': str(uuid4()), 'name': args[-1], 'kind': 'archive', 'size': len(args[3])}
            app.state.runtime.diagnostics.call = call
            # No lifespan. No PLC/CAN startup. Identity is still passed to the bridge.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
                headers = {'X-RP1-Operator-Token': 'test-local', 'X-RP1-Client-ID': 'window', 'X-RP1-Diagnostic-Generation': 'generation', 'Content-Type': 'application/octet-stream', 'X-Filename-UTF8': quote('原始 包.zip', safe='')}
                response = await client.post('/api/v1/diagnostics/upload', content=b'opaque bytes', headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(calls[0][-1], '原始 包.zip')
                self.assertEqual(calls[0][0], 'window')
                self.assertEqual(calls[0][2], {'generation': 'generation'})
                response = await client.post('/api/v1/diagnostics/upload', content=b'x'*(32*1024**2+1), headers=headers)
                self.assertEqual(response.status_code, 413)
                self.assertEqual(len(calls), 1)
                headers['X-Filename-UTF8'] = '%FF'
                response = await client.post('/api/v1/diagnostics/upload', content=b'raw', headers=headers)
                self.assertEqual(response.status_code, 422)
                del headers['X-RP1-Operator-Token']
                response = await client.post('/api/v1/diagnostics/upload', content=b'raw', headers=headers)
                self.assertEqual(response.status_code, 401)
        with TemporaryDirectory() as root, patch.dict(os.environ, {'RP1_FACTORY_OPERATOR_TOKEN': 'test-local'}):
            asyncio.run(scenario(root))


if __name__ == '__main__': unittest.main()
