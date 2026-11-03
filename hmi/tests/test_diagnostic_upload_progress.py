"""Real transport iterator and localhost-only slow-response upload fixtures."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from fastapi import HTTPException
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
from factory_hmi.client import GatewayClient, GatewayHTTPError


class UploadBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(); self.bridge=DiagnosticBridge(Path(self.directory.name))
        self.bridge._binding=dict(owner='window',generation='gen',operator='fixture',station='S',client=SimpleNamespace())
        self.identifier=str(uuid4()); self.raw=b'x'*(65536*2+19)
        self.bridge.call('window','upload-start',dict(generation='gen',upload_id=self.identifier,total_bytes=len(self.raw)))
        self.key=('gen','window',self.identifier); self.record=self.bridge._uploads[self.key]
    def tearDown(self): self.directory.cleanup()
    def progress(self): return self.bridge.call('window','upload-progress',dict(generation='gen',upload_id=self.identifier))

    def test_real_iterator_counts_after_consumed_chunks_and_never_time_based(self):
        body=iter(self.bridge._upload_body(self.bridge._binding,self.record,self.raw))
        first=next(body); self.assertEqual(len(first),65536); self.assertEqual(self.progress()['bytes_sent'],0)
        second=next(body); self.assertEqual(len(second),65536); self.assertEqual(self.progress()['bytes_sent'],65536)
        final=next(body); self.assertEqual(len(final),19); self.assertEqual(self.progress()['bytes_sent'],131072)
        with self.assertRaises(StopIteration): next(body)
        self.assertEqual(self.progress()['bytes_sent'],len(self.raw)); self.assertEqual(self.progress()['phase'],'waiting_response')
        self.assertEqual(first+second+final,self.raw)

    def test_cooperative_cancel_stops_before_next_block_and_generation_cannot_poll(self):
        body=iter(self.bridge._upload_body(self.bridge._binding,self.record,self.raw)); next(body)
        self.bridge.call('window','upload-cancel',dict(generation='gen',upload_id=self.identifier))
        with self.assertRaises(HTTPException): next(body)
        self.assertEqual(self.progress()['bytes_sent'],65536)
        with self.assertRaises(HTTPException): self.bridge.call('window','upload-progress',dict(generation='old',upload_id=self.identifier))
        with self.assertRaises(HTTPException): self.bridge.call('other','upload-cancel',dict(generation='gen',upload_id=self.identifier))

    def test_revoke_stops_generator_and_new_attempt_requires_new_upload_id(self):
        body=iter(self.bridge._upload_body(self.bridge._binding,self.record,self.raw)); next(body); self.bridge.revoke()
        with self.assertRaises(HTTPException): next(body)
        self.assertTrue(self.record['cancelled'])
        self.bridge._binding=dict(owner='window',generation='gen',operator='fixture',station='S',client=SimpleNamespace())
        with self.assertRaises(HTTPException): self.bridge.call('window','upload-start',dict(generation='gen',upload_id=self.identifier,total_bytes=len(self.raw)))
        new=str(uuid4()); result=self.bridge.call('window','upload-start',dict(generation='gen',upload_id=new,total_bytes=len(self.raw)))
        self.assertEqual(result['upload_id'],new); self.assertEqual(result['bytes_sent'],0)

    def test_urllib_real_iterator_has_explicit_content_length_and_100_waits_response(self):
        observations=[]
        response=Mock(); response.read.return_value=json.dumps({'data':{'id':str(uuid4())}}).encode()
        response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=None)
        def opened(req,**kwargs):
            self.assertEqual(req.get_header('Content-length'),str(len(self.raw)))
            observations.append(b''.join(req.data))
            self.assertEqual(self.progress()['phase'],'waiting_response')
            self.assertEqual(self.progress()['bytes_sent'],len(self.raw))
            return response
        opener=SimpleNamespace(open=opened)
        self.bridge._binding['client']=SimpleNamespace(base_url='http://synthetic.invalid',cookies=[],_software_opener=lambda:opener)
        result=self.bridge.call('window','upload',dict(generation='gen',upload_id=self.identifier),self.raw,'application/octet-stream','fixture.csv')
        self.assertIn('id',result); self.assertEqual(observations,[self.raw]); self.assertEqual(self.progress()['phase'],'uploaded')

    def test_real_localhost_slow_response_cancel_never_returns_attachment_id(self):
        ready,release=threading.Event(),threading.Event(); received=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                total=int(self.headers['Content-Length']); parts=[]
                while total:
                    raw=self.rfile.read(min(total,16384)); parts.append(raw); total-=len(raw); time.sleep(.001)
                received.append(b''.join(parts)); ready.set(); release.wait(3)
                raw=json.dumps({'data':{'id':str(uuid4())}}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        self.bridge._binding['client']=ReliabilityPlatformClient(f'http://127.0.0.1:{server.server_port}',timeout=5)
        result=[]
        def upload():
            try: result.append(self.bridge.call('window','upload',dict(generation='gen',upload_id=self.identifier),self.raw,'application/octet-stream','fixture.csv'))
            except Exception as exc: result.append(exc)
        worker=threading.Thread(target=upload); worker.start()
        try:
            self.assertTrue(ready.wait(3)); state=self.progress()
            self.assertEqual(state['bytes_sent'],len(self.raw)); self.assertEqual(state['phase'],'waiting_response')
            self.bridge.call('window','upload-cancel',dict(generation='gen',upload_id=self.identifier))
            self.assertEqual(self.progress()['phase'],'cancelling'); release.set(); worker.join(3)
            self.assertEqual(received,[self.raw]); self.assertEqual(len(result),1); self.assertIsInstance(result[0],HTTPException)
            self.assertEqual(self.progress()['phase'],'cancelled')
        finally:
            release.set(); worker.join(3); server.shutdown(); server.server_close(); thread.join(2)

    def test_client_keeps_old_signature_and_new_method_sets_scoped_header(self):
        client=GatewayClient('http://synthetic.invalid',client_id='window',operator_token='local')
        client.diagnostic=Mock(return_value={})
        response=Mock(); response.read.return_value=b'{"result":{"id":"fixture"}}'; response.__enter__=Mock(return_value=response); response.__exit__=Mock(return_value=None)
        with patch('factory_hmi.client.request.urlopen',return_value=response) as opened:
            client.diagnostic_attachment_progress(b'raw','fixture.csv','gen',self.identifier)
        self.assertEqual(client.diagnostic.call_args.args[0],'upload-start')
        self.assertEqual(opened.call_args.args[0].get_header('X-rp1-diagnostic-upload-id'),self.identifier)
        with patch.object(client,'_diagnostic_upload',return_value={}): client.diagnostic_attachment(b'raw','fixture.csv','gen')
