"""Actual HMI sealing/outbox and backend routes, with real cookies and fake motors.

Only the HTTP transport is adapted to TestClient; payload construction, uploads,
database authorization, parsing, verification, submission and review are real.
"""
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.core.config import get_settings
from app.services.archive_storage import ArchiveStorage
from app.services.archive_worker import run_once
from factory_hmi.core.service import FactoryService
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient, ReliabilityUploaderWorker
from factory_hmi.sync.uploader import PlatformError
from test_factory_partition_integration import factory_review, stats, ctx_with_code
from test_archives_integration import classify, submit
from test_submissions_integration import grant
from test_hmi_presence_integration import environment, signed_in, PASSWORD

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='new disposable database required')]


class InProcessPlatform(ReliabilityPlatformClient):
    def __init__(self, client):
        super().__init__('http://isolated.invalid','')
        self.http=client
        self.calls=[]

    def _call(self,method,path,*,payload=None,body=None,idempotency_key=None,service_auth=True,**kwargs):
        assert service_auth is False
        assert not path.startswith('/api/v1/ingestion')
        self.calls.append((method,path,payload))
        headers={'Idempotency-Key':idempotency_key} if idempotency_key else {}
        csrf=self.http.cookies.get('rp1_csrf')
        if csrf:headers['X-CSRF-Token']=csrf
        if body is not None:
            headers['Content-Type']='application/octet-stream'
            response=self.http.request(method,path,content=body,headers=headers)
        else:response=self.http.request(method,path,json=payload,headers=headers)
        if response.status_code>=400:raise PlatformError(response.text,status=response.status_code)
        value=response.json()['data']
        if path.endswith('/complete'):
            # Run the real central parser against the dedicated temporary files.
            for _ in range(5):
                if not run_once():break
        return value


def test_fake_sn_retests_seal_rebuild_upload_verify_approve_and_preserve_originals(environment,tmp_path,monkeypatch):
    engine,users=environment
    monkeypatch.delenv('RP1_FACTORY_BENCH_ID',raising=False)
    monkeypatch.delenv('RP1_FACTORY_STATION_ID',raising=False)
    hmi=Path('/hmi')
    service=FactoryService(data_root=tmp_path/'workstation')
    service.configure(config_path=str(hmi/'scripts/config/arm_motors.yaml'),limb='left_arm',backend='fake')
    trajectory=tmp_path/'synthetic.npz'
    np.savez(trajectory,motor_pos=np.zeros((31,7)),fps=np.asarray([100.]),time=np.arange(31)/100.,limb=np.asarray(['left_arm']))
    sn='000Ab/aZ+复测'
    try:
        with signed_in(users['admin']) as admin,TestClient(app,client=('127.0.0.1',50001)) as session:
            # Use a real non-A station: explicit factory mode still wins.
            from test_setup_integration import post
            from uuid import uuid4
            ctx=ctx_with_code(admin,engine)
            code=ctx['station_code']
            station={'id':ctx['station_id']}
            grant(admin,engine,users['first'],ctx['campaign_id'])
            client=InProcessPlatform(session)
            worker=ReliabilityUploaderWorker(service.outbox,client)
            worker.authenticate(users['first'],PASSWORD)
            identities=[]
            durations=[]
            for index in range(2):
                if index:service.prepare_next_run()
                service.import_trajectory(str(trajectory))
                if index==0:service.connect()
                service.arm()
                service.start(test_case_id='INSPECT',data_domain='FACTORY',bench_id=code,robot_id=sn,operator_id=users['first'],loop=False)
                assert service.controller.wait(timeout=5)
                for thread in list(service._report_threads):
                    thread.join(5);assert not thread.is_alive()
                assert not service._report_errors
                history=service.execution_history_page(data_domain='FACTORY',sn=sn,bench=code)
                assert history['total']==index+1
                record=history['items'][0];test_id=record['test_id'];identity=record['execution_uuid'];identities.append(identity)
                directory=service.report_manager.execution_dir(test_id)
                manifest=(directory/'manifest.json').read_bytes();raw=(directory/'telemetry/raw.csv').read_bytes()
                summary=json.loads((directory/'analysis/summary.json').read_text())
                durations.append(summary['timing']['active_seconds'])
                service.report_manager.rebuild(test_id,actor=users['first'],reason='Synthetic rebuild before explicit verification')
                assert (directory/'manifest.json').read_bytes()==manifest
                service.submit_execution(test_id,review={'tester':'Synthetic operator','bench':code,'station_id':station['id'],
                    'outcome':'成功' if index==0 else '失败','notes':'Synthetic checked original CSV',
                    'discrepancy_reason':'Synthetic explicit verification of unconfigured rules','test_name':'Synthetic factory test','actual_object':sn})
                service.outbox.update(identity,submission_owner=users['first'])
                for _ in range(4):
                    service.outbox.update(identity,next_attempt_at=0)
                    worker.process_once()
                    if service.outbox.get(identity).state=='awaiting_classification':break
                item=service.outbox.get(identity)
                assert item.state=='awaiting_classification',item.last_error
                archive=client.archive(item.upload_id)
                assert archive['serial_number']==sn and archive['bench_id']==code and archive['data_domain']=='FACTORY'
                assert archive['classification'] is None and archive['execution_id'] is None
                # Explicit platform context and submission, with no automatic R&D asset creation.
                with signed_in(users['first']) as owner:
                    attached=classify(owner,archive,ctx);assert attached.status_code==200,attached.text
                    sent=submit(owner,attached.json()['data']);assert sent.status_code==200,sent.text
                    archive=sent.json()['data']
                service.outbox.update(identity,next_attempt_at=0);worker.process_once()
                assert service.outbox.get(identity).state=='pending_review'
                assert stats(admin,sn)['approved_count']==index
                response=factory_review(admin,archive);assert response.status_code==200,response.text
                assert response.json()['data']['factory_review_history'][-1]['decision']=='APPROVED'
                service.outbox.update(identity,next_attempt_at=0);worker.process_once()
                assert service.outbox.get(identity).state=='approved'
                assert ArchiveStorage(get_settings().archive_root).path(identity,'raw.csv').read_bytes()==raw
                assert hashlib.sha256(raw).hexdigest()==archive['sha256']
                assert (directory/'manifest.json').read_bytes()==manifest
                assert client.archive(item.upload_id)['manifest']['original_sha256']==archive['sha256']
            assert len(set(identities))==2
            result=stats(admin,sn)
            assert result['total_count']==result['approved_count']==2
            assert result['passed_count']==result['failed_count']==1
            assert result['active_seconds']==pytest.approx(sum(durations)) and result['active_seconds']>0
            assert not any('/submissions' in path for _,path,_ in client.calls)
            with engine.connect() as db:
                assert db.execute(text('SELECT count(*) FROM test.test_execution WHERE acquisition_uuid IN (:one,:two)'),{'one':identities[0],'two':identities[1]}).scalar_one()==0
    finally:
        service.disconnect()
