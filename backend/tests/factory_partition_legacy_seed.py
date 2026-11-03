"""Run ONLY before migration 0052 with pre-partition API modules, using real cookies."""
import json
import hashlib
from pathlib import Path
from uuid import uuid4
from sqlalchemy import text
from test_archives import archive_payload
from test_archives_integration import RAW,create,upload,verify,submit
from test_hmi_presence_integration import environment,signed_in
from test_submissions_integration import context,review
from test_setup_integration import post


def test_seed_pre_partition_approval_evidence(environment):
    engine,users=environment
    result={'users':users,'records':{}}
    with signed_in(users['admin']) as admin:
        for label in ('approved_a','pending_a','approved_b','approved_explicit_research_a','approved_explicit_factory_b','approved_derived_research_a'):
            ctx=context(admin)
            if label.endswith('_b'):
                tag=uuid4().hex[:10]
                station=post(admin,'stations',{'site_code':'B-'+tag,'site_name':'Synthetic baseline site','lab_code':'B-'+tag,'lab_name':'Synthetic baseline lab','station_code':'B-'+tag,'name':'Synthetic B bench','station_type':'MODULE'})
                ctx['station_id']=station['id']
            fields=('campaign_id','cycle_id','segment_id','asset_id','configuration_id','test_case_version_id','station_id')
            p=archive_payload(context={k:ctx[k] for k in fields},raw=RAW)
            assert 'data_domain' not in p
            item=upload(admin,create(admin,p));r=verify(admin,item,ctx['station_id']);assert r.status_code==200,r.text;item=r.json()['data']
            r=submit(admin,item);assert r.status_code==200,r.text;item=r.json()['data']
            if label.startswith('approved'):
                r=review(admin,{'id':item['submission_id'],'revision':item['submission_revision']});assert r.status_code==200,r.text
                item=admin.get(f"/api/v1/archives/{item['id']}").json()['data']
            if label.startswith('approved'):
                with engine.begin() as db:
                    execution=db.execute(text('SELECT * FROM test.test_execution WHERE public_id=:id'),{'id':item['execution_id']}).mappings().one()
                    interval=db.execute(text('SELECT id FROM test.runtime_interval WHERE execution_id=:id'),{'id':execution['id']}).scalar_one()
                    assessment=db.execute(text("INSERT INTO reliability.exposure_assessment(campaign_id,asset_id,runtime_interval_id,configuration_snapshot_id,eligible,assignment_status,quality_status,assessment_method) VALUES(:c,:a,:r,:cfg,true,'CONFIRMED','VALID','synthetic attached exposure') RETURNING id"),{'c':execution['campaign_id'],'a':execution['asset_id'],'r':interval,'cfg':execution['configuration_snapshot_id']}).scalar_one()
                    scope=db.execute(text("SELECT id FROM reliability.mtbf_scope WHERE asset_kind='MODULE' AND scope_code='OVERALL' AND status='PUBLISHED' ORDER BY version DESC LIMIT 1")).scalar_one()
                    db.execute(text("INSERT INTO reliability.exposure_scope_assignment(campaign_id,assessment_id,scope_id,inclusion_status,rationale) VALUES(:c,:a,:s,'INCLUDED','Synthetic scope')"),{'c':execution['campaign_id'],'a':assessment,'s':scope})
                    event=db.execute(text("INSERT INTO test.test_event(campaign_id,asset_id,cycle_id,execution_id,event_type,normalized_time,clock_quality,data_quality) VALUES(:c,:a,:cy,:e,'SYNTHETIC_FAILURE',:at,'VALID',:quality) RETURNING id"),{'c':execution['campaign_id'],'a':execution['asset_id'],'cy':execution['cycle_id'],'e':execution['id'],'at':execution['normalized_started_at'],'quality':'INVALID' if label in ('approved_a','approved_explicit_factory_b') else 'VALID'}).scalar_one()
                    catalog=db.execute(text("INSERT INTO catalog.error_catalog_version(version,status) VALUES(:v,'PUBLISHED') RETURNING id"),{'v':'SYNTHETIC-'+uuid4().hex}).scalar_one()
                    error=db.execute(text("INSERT INTO catalog.error_code(catalog_version_id,unified_code,domain,subsystem,category,title) VALUES(:c,'SYNTHETIC','SOFTWARE','QA','QA','Synthetic isolated failure') RETURNING id"),{'c':catalog}).scalar_one()
                    failure=db.execute(text("INSERT INTO reliability.interruption(campaign_id,asset_id,event_id,classification,review_status,primary_error_code_id,started_at) VALUES(:c,:a,:e,'FAILED','CONFIRMED',:error,:at) RETURNING id"),{'c':execution['campaign_id'],'a':execution['asset_id'],'e':event,'error':error,'at':execution['normalized_started_at']}).scalar_one()
                    db.execute(text("INSERT INTO reliability.interruption_scope_assignment(interruption_id,scope_id,status,rationale) VALUES(:i,:s,'INCLUDED','Synthetic failure scope')"),{'i':failure,'s':scope})
            result['records'][label]={'archive':item,'context':ctx}
            if 'explicit' in label or 'derived' in label:
                # New, synthetic import provenance is inserted before 0052.
                # The archive, CSV, verification and real approval stay untouched.
                raw={'data_domain':'RESEARCH' if 'research' in label else 'FACTORY','serial_number':'001LegacyAb'}
                if 'derived' in label:raw['data_domain_source']='HISTORICAL_BENCH'
                with engine.begin() as db:
                    raw['bench_id']=db.execute(text('SELECT station_code FROM test.station WHERE public_id=:id'),{'id':ctx['station_id']}).scalar_one()
                    source=db.execute(text("INSERT INTO integration.ingestion_source(source_code,source_type) VALUES(:code,'LEGACY_IMPORT') RETURNING id"),{'code':'SYNTHETIC-'+uuid4().hex}).scalar_one()
                    encoded=json.dumps(raw,sort_keys=True)
                    db.execute(text("INSERT INTO integration.execution_import_record(source_id,source_execution_key,execution_id,source_file,source_row_number,row_hash,raw_payload) SELECT :source,:key,id,'synthetic.json',2,:hash,CAST(:raw AS jsonb) FROM test.test_execution WHERE public_id=:id"),{'source':source,'key':uuid4().hex,'hash':hashlib.sha256(encoded.encode()).hexdigest(),'raw':encoded,'id':item['execution_id']})
        # A free-form label does not create any historical bench evidence.
        item=create(admin,{**archive_payload(raw=RAW),'station_label':'A-not-a-real-code'})
        result['records']['unknown']={'archive':item}
    Path('/archive/factory_legacy_seed.json').write_text(json.dumps(result,default=str))
