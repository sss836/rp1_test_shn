"""Synthetic UI and transport lifecycle checks; no hardware/network required."""
import json
import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
from uuid import uuid4

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QApplication
from factory_hmi.client import GatewayClient, GatewayHTTPError
from factory_hmi.desktop.diagnostics_page import DiagnosticsPage
from factory_hmi.desktop.lease_heartbeat import LeaseHeartbeat
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from fastapi import HTTPException


class Client:
    def __init__(self):
        self.calls = []
        self.status = dict(generation='fixture-generation',operator='合成执行员',hmi=dict(
            installation_id=str(uuid4()),window_id='synthetic-window',station='SYNTHETIC-A10',running_version='SOURCE-ONLY'),capabilities={})

    def diagnostic(self, action, payload=None):
        self.calls.append((action,payload))
        data = self.status if action == 'status' else [] if action == 'threads' else dict(id=str(uuid4()),thread_id=str(uuid4()),state='QUEUED')
        return {'result':data}

    def diagnostic_screenshot(self, raw, mime, generation):
        self.calls.append(('image',(len(raw),mime,generation)))
        return {'result':{'id':str(uuid4())}}


class Reply(QObject):
    finished = Signal()
    def attribute(self, _): return 200
    def error(self): return QNetworkReply.NoError
    def readAll(self): return json.dumps({'ok':True,'lease':{'generation':1}}).encode()
    def abort(self): self.finished.emit()


class Manager:
    def __init__(self): self.calls = 0
    def post(self, *_):
        self.calls += 1
        reply = Reply()
        QTimer.singleShot(1,reply.finished.emit)
        return reply


class DiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])

    def pump(self, condition, seconds=2):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            self.app.processEvents()
            if condition(): return
            time.sleep(.005)
        self.fail('GUI condition not reached')

    def setUp(self):
        self.client = Client()
        self.page = DiagnosticsPage(self.client)

    def tearDown(self):
        self.page.stop()
        self.page.pool.waitForDone(3000)
        self.app.processEvents()
        self.page.deleteLater()

    def test_login_links_without_popup_image_preview_submit_and_evidence(self):
        self.page.activate()
        self.pump(lambda:self.page.binding is not None and not self.page._workers)
        self.assertIn('SYNTHETIC-A10',self.page.identity.text())
        image = QImage(60,40,QImage.Format.Format_RGB32)
        image.fill(0x33aadd)
        self.app.clipboard().setImage(image)
        self.page._paste()
        self.assertIsNotNone(self.page.image)
        self.assertFalse(any(c[0]=='image' for c in self.client.calls))
        self.page.message.setPlainText('请查看合成截图')
        self.page._send()
        self.pump(lambda:self.page.job not in (None,'submitting'))
        self.assertEqual([c[0] for c in self.client.calls][-2:],['image','submit'])
        self.assertEqual(self.client.calls[-1][1]['attachments'].__len__(),1)
        self.page._job_status({'state':'COMPLETED','result':{'reply':'合成截图已查看，非真实模型结论。','cards':[{'title':'工位状态','source':'synthetic-fixture','updated_at':'2026-10-30','data':{'items':[]}}]}})
        self.assertIn('synthetic-fixture',self.page.transcript.toPlainText())
        output = os.getenv('DIAGNOSTICS_SCREENSHOT')
        if output:
            self.page.resize(1040,740)
            self.page.show()
            self.app.processEvents()
            self.assertTrue(self.page.grab().save(output))
        self.page.revoke()
        self.assertNotIn('合成截图已查看',self.page.transcript.toPlainText())
        self.assertIsNone(self.page.image)

    def test_late_result_cannot_repopulate_after_logout(self):
        release = threading.Event()
        self.page._run('late',lambda:(release.wait(1),{'result':self.client.status})[1],lambda value:setattr(self.page,'binding',value))
        self.page.revoke()
        release.set()
        self.pump(lambda:not self.page._workers)
        self.assertIsNone(self.page.binding)
        self.assertFalse(self.page.send.isEnabled())

    def test_two_blocked_diagnostic_workers_do_not_block_lease_callbacks(self):
        release = threading.Event()
        for n in range(2): self.assertTrue(self.page._run(str(n),lambda:release.wait(1),lambda _:None))
        self.assertFalse(self.page._run('third',lambda:None,lambda _:None))
        client = GatewayClient('http://127.0.0.1:65534')
        client._lease_generation = 1
        client.observe_lease = lambda _:True
        manager = Manager()
        lease = LeaseHeartbeat(client,lambda:True,self.page,manager=manager)
        renewed = []
        lease.renewed.connect(renewed.append)
        timer = QTimer(self.page)
        timer.setInterval(20)
        timer.timeout.connect(lease.pulse)
        timer.start()
        self.pump(lambda:len(renewed)>=10)
        self.assertIsNot(self.page.pool,QThreadPoolGlobal())
        timer.stop()
        lease.stop()
        release.set()
        self.pump(lambda:not self.page._workers)

    def test_permission_failure_clears_pending_and_private_content(self):
        self.page.binding = self.client.status
        self.page.transcript.setPlainText('旧账号私人资料')
        def denied(): raise GatewayHTTPError('revoked',status_code=403)
        self.page._run('status',denied,lambda _:self.fail('unauthorized success'))
        self.pump(lambda:not self.page._workers)
        self.assertIsNone(self.page.binding)
        self.assertNotIn('旧账号私人资料',self.page.transcript.toPlainText())

    def test_submit_retry_reuses_original_key_after_lost_acknowledgement(self):
        self.page.binding = self.client.status
        self.page.message.setPlainText('查询合成工位')
        attempts = []
        def request(action, payload=None):
            attempts.append(payload['request_key'])
            if len(attempts) == 1:
                raise OSError('synthetic lost acknowledgement')
            return {'result': {'id':payload['request_key'],'thread_id':str(uuid4()),'state':'QUEUED'}}
        self.client.diagnostic = request
        self.page._send()
        self.pump(lambda:not self.page._workers)
        self.assertIsNotNone(self.page.submission)
        self.assertEqual(self.page.send.text(),'重试确认')
        self.page._send()
        self.pump(lambda:not self.page._workers)
        self.assertEqual(attempts[0],attempts[1])
        self.assertIsNone(self.page.submission)

    def test_bridge_generation_and_replacement_block_late_result(self):
        with TemporaryDirectory() as directory:
            bridge = DiagnosticBridge(Path(directory))
            a = {'owner':'a','generation':'a','operator':'a','station':'A10'}
            bridge._binding = a
            with self.assertRaises(HTTPException): bridge.call('b','status')
            with self.assertRaises(HTTPException): bridge.call('a','submit',{'generation':'stale'})
            def remote(*_args,**_kwargs):
                bridge._binding = {'owner':'a','generation':'b'}
                return {}
            bridge._remote = remote
            with self.assertRaises(HTTPException): bridge.call('a','status')

    def test_gateway_login_logout_and_failed_relogin_revoke_real_binding(self):
        from factory_hmi.gateway.schemas import PlatformLoginRequest
        from factory_hmi.gateway.app import create_app
        from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'RP1_FACTORY_OPERATOR_TOKEN':'synthetic-local-token'}):
            app = create_app(controller=SimpleNamespace(), data_root=Path(directory))
            hub = app.state.runtime
            hub._platform._uploader = SimpleNamespace(client=ReliabilityPlatformClient('https://fixture.invalid'))
            hub._platform.login = Mock(return_value={'operator':'synthetic-user'})
            hub._platform.logout = Mock(return_value={'status':'logged_out'})
            hub.diagnostics._remote = Mock(return_value={'mode':'assistant_runtime'})
            # No lifespan startup: this check cannot start PLC, CAN or workers.
            routes = {r.path:r.endpoint for r in app.routes if hasattr(r,"endpoint")}
            req = SimpleNamespace(client=None)
            login = {'username':'synthetic-user','password':'synthetic-password','station_id':'A10'}
            self.assertEqual(routes['/api/v1/platform/login'](PlatformLoginRequest(**login), req, 'a', 'synthetic-local-token')['result']['operator'], 'synthetic-user')
            linked = hub.diagnostics.call('a','status')
            self.assertEqual(linked['hmi']['station'],'A10')
            with self.assertRaises(HTTPException): hub.diagnostics.call('b','status')
            self.assertEqual(routes['/api/v1/platform/logout'](req,'a','synthetic-local-token')['result']['status'],'logged_out')
            with self.assertRaises(HTTPException): hub.diagnostics.call('a','status')
            self.assertEqual(routes['/api/v1/platform/login'](PlatformLoginRequest(**login), req, 'a', 'synthetic-local-token')['result']['operator'], 'synthetic-user')
            hub._platform.login.side_effect = RuntimeError('synthetic failed login')
            with self.assertRaises(RuntimeError): routes['/api/v1/platform/login'](PlatformLoginRequest(**login),req,'a','synthetic-local-token')
            self.assertIsNone(hub.diagnostics._binding)
            with self.assertRaises(HTTPException): routes['/api/v1/platform/logout'](req,'a',None)

    def test_failed_login_clears_identity_and_logout_without_presence_revokes_server(self):
        from factory_hmi.gateway.app import _PlatformCoordinator
        from factory_hmi.gateway.schemas import PlatformLoginRequest
        from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
        with patch.dict(os.environ, {'RP1_FACTORY_PLATFORM_MODE':'reliability_v1'}):
            coordinator = _PlatformCoordinator(Mock())
            coordinator._identity = 'previous-user'
            coordinator._uploader = SimpleNamespace(authenticate=Mock(side_effect=RuntimeError('synthetic failed auth')))
            with self.assertRaises(RuntimeError): coordinator.login(PlatformLoginRequest(username='new-user',password='synthetic-password'))
            self.assertEqual(coordinator._identity,'')
        client = SimpleNamespace(logout_operator=Mock())
        worker = ReliabilityUploaderWorker(Mock(),client,presence=None)
        worker._identity = 'previous-user'
        worker.clear_credentials()
        client.logout_operator.assert_called_once()
        self.assertEqual(worker._identity,'')


def QThreadPoolGlobal():
    from PySide6.QtCore import QThreadPool
    return QThreadPool.globalInstance()


if __name__ == '__main__': unittest.main()
