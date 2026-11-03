"""Real sessions and checked synthetic CSVs in a dedicated disposable database."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import os
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_archives import archive_payload
from test_archives_integration import RAW,headers,create,upload,verify,submit,classify
from test_hmi_presence_integration import environment,signed_in,heartbeat_payload
from test_submissions_integration import context,review

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='new disposable database required')]

def payload(ctx,mode='FACTORY',sn='000aBc001'):
    return {**archive_payload(raw=RAW,context={k:ctx[k] for k in ('campaign_id','cycle_id','segment_id','asset_id','configuration_id','test_case_version_id','station_id')}), 'data_domain':mode,'serial_number':sn,'bench_id':ctx['station_code']}

def ctx_with_code(client,engine):
    ctx=context(client)
    with engine.connect() as db:ctx['station_code']=db.execute(text('SELECT station_code FROM test.station WHERE public_id=:id'),{'id':ctx['station_id']}).scalar_one()
    return ctx

def ready(client,p,ctx):
    item=upload(client,create(client,p));assert item['processing_state']=='READY',item
    r=verify(client,item,ctx['station_id']);assert r.status_code==200,r.text
    return r.json()['data']

def factory_review(client,item,decision='APPROVE',key=None):
    return client.post(f"/api/v1/archives/{item['id']}/factory-review",json={'expected_submission_revision':item['submission_revision'],'decision':decision,'reason':'Synthetic factory review'},headers=headers(key))

def stats(client,sn):
    r=client.get('/api/v1/factory/statistics',params={'sn':sn});assert r.status_code==200,r.text
    return r.json()['data']

def test_unregistered_sn_real_approval_revision_idempotency_and_stats(environment):
    engine,users=environment
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author,signed_in(users['second']) as other,signed_in(users['viewer']) as viewer:
        ctx=ctx_with_code(admin,engine);sn='000aBc-'+uuid4().hex
        with engine.connect() as db:before={t:db.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ('test.asset','test.test_campaign','test.configuration_snapshot')}
        p=payload(ctx,sn=sn);key=uuid4().hex
        item=create(author,p,key);assert item['data_domain']=='FACTORY' and item['data_domain_source']=='EXPLICIT' and item['serial_number']==sn
        assert create(author,p,key)['id']==item['id']
        assert author.post('/api/v1/archives',json={**p,'serial_number':'different'},headers=headers(key)).status_code==409
        assert submit(author,item).status_code in (409,422)
        item=upload(author,item);r=verify(author,item,ctx['station_id']);assert r.status_code==200,r.text;item=r.json()['data']
        assert item['classification_state']=='CLASSIFIED'
        assert submit(other,item).status_code==403
        key=uuid4().hex;r=submit(author,item,key=key);assert r.status_code==200,r.text;pending=r.json()['data']
        assert pending['can_review'] is True and pending['submission_status']=='PENDING'
        assert submit(author,item,key=key).status_code==200
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code==404
        assert stats(author,sn)=={'total_count':1,'pending_count':1,'approved_count':0,'passed_count':0,'failed_count':0,'blocked_count':0,'active_seconds':0}
        assert verify(author,pending,ctx['station_id']).status_code==409
        assert factory_review(viewer,pending).status_code==403
        rejected=factory_review(author,pending,'REJECT');assert rejected.status_code==200,rejected.text;item=rejected.json()['data']
        assert item['submission_status']=='REJECTED'
        assert factory_review(author,pending).status_code==409
        item=verify(author,item,ctx['station_id']).json()['data']
        r=submit(author,item,expected=1);assert r.status_code==200,r.text;item=r.json()['data'];assert item['submission_revision']==2
        key=uuid4().hex;r=factory_review(author,item,key=key);assert r.status_code==200,r.text
        assert factory_review(author,item,key=key).status_code==200
        assert factory_review(author,item).status_code==409
        assert [entry['decision'] for entry in r.json()['data']['factory_review_history']]==['REJECTED','APPROVED']
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code==200
        assert stats(author,sn)=={'total_count':1,'pending_count':0,'approved_count':1,'passed_count':1,'failed_count':0,'blocked_count':0,'active_seconds':2}
        assert stats(viewer,sn)==stats(author,sn)
        second=ready(author,payload(ctx,sn=sn),ctx);assert second['execution_uuid']!=item['execution_uuid']
        assert stats(author,sn)['total_count']==2
        for t,n in before.items():
            with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM '+t)).scalar_one()==n
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM test.test_execution WHERE acquisition_uuid IN (:one,:two)'),{'one':item['execution_uuid'],'two':second['execution_uuid']}).scalar_one()==0


def test_domain_history_bench_consistency_filters_permissions_and_time_window(environment):
    engine,users=environment
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author,signed_in(users['second']) as other:
        ctx=ctx_with_code(admin,engine);otherctx=ctx_with_code(admin,engine);sn='000Case-'+uuid4().hex
        item=create(author,payload(ctx,'RESEARCH',sn));assert item['data_domain']=='RESEARCH' # context bench starts A; explicit wins
        item=upload(author,item)
        assert verify(author,item,otherctx['station_id']).status_code==422
        r=verify(author,item,ctx['station_id']);assert r.status_code==200,r.text;item=r.json()['data']
        original=item['manifest'];body={'expected_revision':item['revision'],'data_domain':'FACTORY','serial_number':sn,'bench_id':ctx['station_code'],'reason':'Synthetic domain correction'}
        assert other.post(f"/api/v1/archives/{item['id']}/domain-classification",json=body,headers=headers()).status_code==403
        key=uuid4().hex;r=author.post(f"/api/v1/archives/{item['id']}/domain-classification",json=body,headers=headers(key));assert r.status_code==200,r.text;item=r.json()['data']
        assert item['data_domain_source']=='CORRECTION' and len(item['domain_history'])==2 and item['manifest']==original
        assert author.post(f"/api/v1/archives/{item['id']}/domain-classification",json=body,headers=headers(key)).status_code==200
        assert author.post(f"/api/v1/archives/{item['id']}/domain-classification",json=body,headers=headers()).status_code==409
        start=item['facts']['started_at'];end=(datetime.fromisoformat(start)+timedelta(seconds=1)).isoformat()
        params={'data_domain':'FACTORY','sn':sn,'bench':ctx['station_code'],'started_from':start,'started_to':end,'limit':1}
        r=author.get('/api/v1/archives',params=params);assert r.status_code==200,r.text;assert [i['id'] for i in r.json()['data']['items']]==[item['id']]
        assert author.get('/api/v1/archives',params={**params,'started_to':start}).status_code==422
        assert author.get('/api/v1/archives',params={'started_from':'2026-01-01T00:00:00'}).status_code==422
        assert author.get('/api/v1/archives',params={**params,'sn':sn.upper()}).json()['data']['items']==[]
        legacy=archive_payload(raw=RAW);legacy.pop('data_domain');legacy['station_label']='A-free-label-is-not-evidence'
        old=create(author,legacy);assert old['data_domain']=='UNCLASSIFIED' and old['data_domain_source']=='NO_EVIDENCE'
        p=archive_payload(raw=RAW);p.pop('data_domain');p['bench_id']='B-'+uuid4().hex
        assert create(author,p)['data_domain']=='RESEARCH'
        p={**p,'execution_uuid':str(uuid4()),'bench_id':'A-'+uuid4().hex};assert create(author,p)['data_domain']=='FACTORY'


def test_factory_parallel_submit_and_review_have_one_winner(environment):
    engine,users=environment
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author:
        ctx=ctx_with_code(admin,engine);item=ready(author,payload(ctx,sn=uuid4().hex),ctx)
    def send():
        with signed_in(users['first']) as client:return submit(client,item).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(lambda _:send(),range(2)))==[200,409]
    with signed_in(users['first']) as author:item=author.get(f"/api/v1/archives/{item['id']}").json()['data']
    def decide():
        with signed_in(users['first']) as client:return factory_review(client,item).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(lambda _:decide(),range(2)))==[200,409]


def test_migrated_legacy_approved_factory_keeps_evidence_and_old_pending_can_be_rejected(environment):
    import json
    from pathlib import Path
    from app.core.config import get_settings
    seed=json.loads((Path(get_settings().archive_root)/'factory_legacy_seed.json').read_text())
    engine,_=environment
    with signed_in(seed['users']['admin']) as admin:
        old=seed['records']['approved_a']['archive'];item=admin.get(f"/api/v1/archives/{old['id']}").json()['data']
        assert item['data_domain']=='FACTORY' and item['data_domain_source']=='HISTORICAL_BENCH'
        assert item['manifest']==old['manifest'] and item['facts']==old['facts'] and item['submission_status']=='APPROVED'
        result=admin.get('/api/v1/factory/statistics',params={'bench':item['bench_id']}).json()['data']
        assert result['approved_count']==1 and result['passed_count']==1 and result['active_seconds']==2
        assert not item['factory_review_history']
        r=admin.post(f"/api/v1/archives/{item['id']}/domain-classification",json={'expected_revision':item['revision'],'data_domain':'RESEARCH','reason':'Synthetic forbidden approved switch'},headers=headers())
        assert r.status_code==403,r.text
        oldp=seed['records']['pending_a']['archive'];pending=admin.get(f"/api/v1/archives/{oldp['id']}").json()['data']
        assert pending['data_domain']=='FACTORY' and pending['submission_status']=='PENDING'
        assert review(admin,{'id':pending['submission_id'],'revision':pending['submission_revision']}).status_code==422
        assert review(admin,{'id':pending['submission_id'],'revision':pending['submission_revision']},'REJECTED').status_code==200
        pending=admin.get(f"/api/v1/archives/{pending['id']}").json()['data']
        corrected=admin.post(f"/api/v1/archives/{pending['id']}/domain-classification",json={'expected_revision':pending['revision'],'data_domain':'FACTORY','serial_number':'000LegacySn','reason':'Synthetic declare original SN'},headers=headers())
        assert corrected.status_code==200,corrected.text
        r=submit(admin,corrected.json()['data']);assert r.status_code==200,r.text
        assert factory_review(admin,r.json()['data']).status_code==200
        unknown=admin.get(f"/api/v1/archives/{seed['records']['unknown']['archive']['id']}").json()['data']
        assert unknown['data_domain']=='UNCLASSIFIED'
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM integration.factory_submission WHERE archive_id=(SELECT id FROM integration.execution_archive WHERE public_id=:id)'),{'id':old['id']}).scalar_one()==0


def test_research_inputs_exclude_migrated_factory_and_unknown_and_cache_policy_is_required(environment):
    import json
    from pathlib import Path
    from uuid import UUID
    from app.core.config import get_settings
    from app.core.db import SessionLocal
    from app.repositories.mtbf import MtbfRepository
    from app.schemas.mtbf import CampaignMtbfConfigRequest
    from app.services.worker import _interval_rows,_failure_rows,process_job,claim_job
    seed=json.loads((Path(get_settings().archive_root)/'factory_legacy_seed.json').read_text());engine,_=environment
    with engine.connect() as db:actor=db.execute(text('SELECT public_id FROM iam.app_user WHERE username=:name'),{'name':seed['users']['admin']}).scalar_one()
    for name,wanted in [('approved_a',0),('approved_b',1)]:
        ctx=seed['records'][name]['context']
        with SessionLocal.begin() as session:
            session.execute(text("SELECT iam.set_request_context(:id,'factory-partition-calculation','isolated deterministic worker verification')"),{'id':actor})
            repository=MtbfRepository(session);repository.upsert_config(UUID(ctx['campaign_id']),'OVERALL',CampaignMtbfConfigRequest(target_hours=1000),None)
            cid,sid=session.execute(text("SELECT c.id,s.id FROM test.test_campaign c JOIN reliability.mtbf_scope s ON s.asset_kind=c.asset_kind AND s.scope_code='OVERALL' AND s.status='PUBLISHED' WHERE c.public_id=:id"),{'id':ctx['campaign_id']}).one()
            now=datetime.now(timezone.utc)
            rows=_interval_rows(session,cid,sid,now,now);assert len(rows)==wanted
            assert len(_failure_rows(session,cid,sid,now,now))==wanted
            session.execute(text("SELECT reliability.enqueue_campaign_recompute(:c,:s,'factory-partition-qa',NULL)"),{'c':cid,'s':sid})
            job=claim_job(session,'factory-partition-test');assert job is not None
            process_job(session,job)
            current=repository.get_current_result(UUID(ctx['campaign_id']),'OVERALL')
            assert current.total_test_duration_hours==pytest.approx(wanted*2/3600)
            assert current.eligible_exposure_hours==pytest.approx(wanted*2/3600) and current.relevant_failure_count==wanted
            run=session.execute(text('SELECT run.input_summary FROM reliability.current_mtbf_result r JOIN reliability.calculation_run run ON run.id=r.calculation_run_id WHERE r.campaign_id=:id'),{'id':cid}).scalar_one()
            assert run['data_domain_policy_version']=='research-only-v1' and 'data_domain_epoch' in run
        # Simulate an existing old cache marker, retaining all snapshot facts.
        with engine.begin() as db:db.execute(text('UPDATE reliability.current_mtbf_result SET data_domain_policy_version=NULL WHERE campaign_id=:id'),{'id':cid})
        with SessionLocal.begin() as session:
            session.execute(text("SELECT iam.set_request_context(:id,'factory-cache-check','synthetic cache compatibility check')"),{'id':actor})
            hidden=MtbfRepository(session).get_current_result(UUID(ctx['campaign_id']),'OVERALL')
            assert hidden.calculation_status=='NOT_CALCULATED' and hidden.run_id is None and hidden.total_test_duration_hours==0


def test_presence_explicit_mode_wins_and_factory_never_fabricates_research_context(environment):
    engine,users=environment
    with signed_in(users['admin']) as admin:
        ctx=ctx_with_code(admin,engine)
        connection=str(uuid4());raw=heartbeat_payload();identity=str(uuid4())
        run={**raw['runs'][0],**{k:ctx[k] for k in ('campaign_id','cycle_id','asset_id','configuration_id','test_case_version_id','station_id')},'execution_uuid':identity,'data_domain':'RESEARCH','bench_id':ctx['station_code'],'serial_number':'Existing research sample','operator_id':'display-only'}
        p={**raw,'bench_id':ctx['station_code'],'runs':[run]}
        r=admin.put('/api/v1/hmi/presence/'+connection,json=p);assert r.status_code==200,r.text
        assert len(r.json()['data']['executions'])==1 # A bench, explicitly RESEARCH
        changed={**p,'sequence':2,'runs':[{**run,'data_domain':'FACTORY'}]}
        assert admin.put('/api/v1/hmi/presence/'+connection,json=changed).status_code==422
        request={**archive_payload(raw=RAW),'execution_uuid':identity,'data_domain':'FACTORY','serial_number':'000SN','bench_id':ctx['station_code']}
        assert admin.post('/api/v1/archives',json=request,headers=headers()).status_code==422
        assert admin.delete('/api/v1/hmi/presence/'+connection).status_code==200
        # Full-looking UUID context is not a reason to create a research projection for factory.
        factory_id=str(uuid4());factory={**p,'installation_id':str(uuid4()),'bench_id':'B-FACTORY','runs':[{**run,'execution_uuid':factory_id,'data_domain':'FACTORY','bench_id':'B-FACTORY','serial_number':'000SN'}]}
        r=admin.put('/api/v1/hmi/presence/'+str(uuid4()),json=factory);assert r.status_code==200,r.text;assert r.json()['data']['executions']==[]
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:id'),{'id':factory_id}).scalar_one()==0
        # Online factory SN needs no directory entry and no R&D UUID context.
        bare={**raw,'installation_id':str(uuid4()),'bench_id':'B-UNREGISTERED','runs':[{**raw['runs'][0],'data_domain':'FACTORY','execution_uuid':str(uuid4()),'sample_id':'000unregisteredAb','serial_number':'000unregisteredAb','bench_id':'B-UNREGISTERED'}]}
        r=admin.put('/api/v1/hmi/presence/'+str(uuid4()),json=bare);assert r.status_code==200,r.text
        listed=admin.get('/api/v1/hmi/stations').json()['data']['items'];item=next(i for i in listed if i['installation_id']==bare['installation_id'])
        assert item['online'] is True and item['runs'][0]['serial_number']=='000unregisteredAb'


def test_migration_explicit_import_mode_wins_without_rewriting_approval_or_csv(environment):
    import hashlib,json
    from pathlib import Path
    from app.core.config import get_settings
    from app.services.archive_storage import ArchiveStorage
    engine,_=environment
    seed=json.loads((Path(get_settings().archive_root)/'factory_legacy_seed.json').read_text())
    with signed_in(seed['users']['admin']) as admin:
        for label,domain,source in [('approved_explicit_research_a','RESEARCH','EXPLICIT'),('approved_explicit_factory_b','FACTORY','EXPLICIT'),
            ('approved_derived_research_a','FACTORY','HISTORICAL_BENCH')]:
            old=seed['records'][label]['archive']
            response=admin.get(f"/api/v1/archives/{old['id']}");assert response.status_code==200,response.text
            item=response.json()['data']
            assert (item['data_domain'],item['data_domain_source'])==(domain,source)
            assert item['manifest']==old['manifest'] and item['facts']==old['facts']
            assert item['submission_id']==old['submission_id'] and item['submission_status']=='APPROVED'
            raw=ArchiveStorage(get_settings().archive_root).path(item['execution_uuid'],'raw.csv').read_bytes()
            assert raw==RAW and hashlib.sha256(raw).hexdigest()==old['sha256']
            with engine.connect() as db:
                assert db.execute(text('SELECT d.data_domain FROM integration.execution_domain_evidence d JOIN test.test_execution e ON e.id=d.execution_id WHERE e.public_id=:id'),{'id':item['execution_id']}).scalar_one()==domain


@pytest.mark.parametrize('bench,wanted',[(None,'UNCLASSIFIED'),('unknown','UNCLASSIFIED'),('NONE','UNCLASSIFIED'),
    ('11111111-1111-4111-8111-111111111111','UNCLASSIFIED'),('A free label','UNCLASSIFIED'),('a10','FACTORY'),('B-20','RESEARCH')])
def test_database_bench_evidence_agrees_with_hmi_and_old_payloads(environment,bench,wanted):
    from factory_hmi.data_domain import domain_metadata
    engine,users=environment
    assert domain_metadata({'bench_id':bench})['data_domain']==wanted
    with engine.connect() as db:
        assert db.execute(text('SELECT integration.domain_from_bench(:bench)'),{'bench':bench}).scalar_one()==wanted
    with signed_in(users['first']) as author:
        p=archive_payload(raw=RAW);p.pop('data_domain')
        if bench is not None:p['bench_id']=bench
        assert create(author,p)['data_domain']==wanted


def test_viewer_rls_revoked_submitter_and_failed_write_roll_back(environment):
    engine,users=environment
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author,signed_in(users['viewer']) as viewer:
        ctx=ctx_with_code(admin,engine);sn='001Revoke'+uuid4().hex
        item=ready(author,payload(ctx,sn=sn),ctx)
        assert stats(viewer,sn)['total_count']==0
        item=submit(author,item).json()['data']
        assert stats(viewer,sn)==dict(total_count=0,pending_count=0,approved_count=0,passed_count=0,failed_count=0,blocked_count=0,active_seconds=0)
        with engine.begin() as db:db.execute(text('UPDATE iam.app_user SET enabled=false WHERE username=:name'),{'name':users['first']})
        key=uuid4().hex
        assert factory_review(admin,item,key=key).status_code==403
        unchanged=admin.get(f"/api/v1/archives/{item['id']}").json()['data']
        assert unchanged['submission_status']=='PENDING' and unchanged['factory_review_history']==[]
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM integration.partition_request WHERE idempotency_key=:key'),{'key':key}).scalar_one()==0
        assert factory_review(admin,item,'REJECT').status_code==200
        assert stats(admin,sn)['approved_count']==0


def test_partition_correction_invalidates_cache_and_racing_calculation_is_hidden(environment,monkeypatch):
    from uuid import UUID
    from app.core.db import SessionLocal
    from app.repositories.mtbf import MtbfRepository
    from app.schemas.mtbf import CampaignMtbfConfigRequest
    from app.services import worker
    engine,users=environment
    with signed_in(users['admin']) as admin:
        ctx=ctx_with_code(admin,engine);identity=str(uuid4())
        raw=heartbeat_payload()
        run={**raw['runs'][0],**{k:ctx[k] for k in ('campaign_id','cycle_id','asset_id','configuration_id','test_case_version_id','station_id')},
            'execution_uuid':identity,'data_domain':'RESEARCH','bench_id':ctx['station_code']}
        response=admin.put('/api/v1/hmi/presence/'+str(uuid4()),json={**raw,'bench_id':ctx['station_code'],'runs':[run]})
        assert response.status_code==200,response.text
        item=ready(admin,{**payload(ctx,'RESEARCH'),'execution_uuid':identity},ctx)
        with engine.connect() as db:
            actor=db.execute(text('SELECT public_id FROM iam.app_user WHERE username=:name'),{'name':users['admin']}).scalar_one()
        def calculate():
            with SessionLocal.begin() as session:
                session.execute(text("SELECT iam.set_request_context(:id,'cache-race-qa','synthetic isolated calculation')"),{'id':actor})
                repo=MtbfRepository(session);repo.upsert_config(UUID(ctx['campaign_id']),'OVERALL',CampaignMtbfConfigRequest(target_hours=1000),None)
                cid,sid=session.execute(text("SELECT c.id,s.id FROM test.test_campaign c JOIN reliability.mtbf_scope s ON s.asset_kind=c.asset_kind AND s.scope_code='OVERALL' AND s.status='PUBLISHED' WHERE c.public_id=:id"),{'id':ctx['campaign_id']}).one()
                session.execute(text("SELECT reliability.enqueue_campaign_recompute(:c,:s,'cache-race-qa',NULL)"),{'c':cid,'s':sid})
                job=worker.claim_job(session,'cache-race-qa');assert job
                worker.process_job(session,job)
                return repo.get_current_result(UUID(ctx['campaign_id']),'OVERALL')
        first=calculate();assert first.run_id is not None
        original_rows=worker._interval_rows
        corrected=[]
        def rows_then_change_domain(*args,**kwargs):
            rows=original_rows(*args,**kwargs)
            response=admin.post(f"/api/v1/archives/{item['id']}/domain-classification",json={
                'expected_revision':item['revision'],'data_domain':'FACTORY','serial_number':'001RaceAb','bench_id':ctx['station_code'],
                'reason':'Synthetic concurrent correction after input capture'},headers=headers())
            assert response.status_code==200,response.text
            corrected.append(response.json()['data'])
            return rows
        with monkeypatch.context() as patch:
            patch.setattr(worker,'_interval_rows',rows_then_change_domain)
            raced=calculate()
        assert len(corrected)==1 and raced.run_id is None and raced.calculation_status=='NOT_CALCULATED'
        final=calculate();assert final.run_id is not None and final.run_id!=first.run_id
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM reliability.calculation_run run JOIN test.test_campaign c ON c.id=run.campaign_id WHERE c.public_id=:id'),{'id':ctx['campaign_id']}).scalar_one()==3


def test_research_read_models_exclude_factory_and_unknown_from_counts_durations_and_failures(environment):
    import json
    from pathlib import Path
    from app.core.config import get_settings
    engine,_=environment
    seed=json.loads((Path(get_settings().archive_root)/'factory_legacy_seed.json').read_text())
    with signed_in(seed['users']['admin']) as admin:
        for label,wanted in [('approved_a',0),('approved_b',1),('approved_explicit_research_a',1),('approved_explicit_factory_b',0)]:
            ctx=seed['records'][label]['context']
            response=admin.get(f"/api/v1/assets/{ctx['asset_id']}");assert response.status_code==200,response.text
            item=response.json()['data']
            assert item['execution_count']==wanted
            assert item['event_count']==wanted
            assert item['asset']['durations']['total_duration_seconds']==wanted*2
            response=admin.get('/api/v1/test-cases/durations',params={'asset':ctx['asset_id']});assert response.status_code==200,response.text
            assert response.json()['data']['totals']['total_duration_seconds']==wanted*2
            response=admin.get(f"/api/v1/executions/{seed['records'][label]['archive']['execution_id']}")
            assert response.status_code==(200 if wanted else 404),response.text
        response=admin.get('/api/v1/dashboard/home');assert response.status_code==200,response.text
        assert not any('事件存在无效或缺失数据' in warning for warning in response.json()['data']['anomalies'])
