"""Cookie/CSRF -> real Pi -> synthetic SSE -> disposable PostgreSQL only."""
import hashlib
import json
import os
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.test_assistant_integration import environment,signed_in,normal_model
from tests.test_assistant_runtime import control,submit,wait
# Collect support flows in this module so one module-scoped isolated user set
# serves the whole suite; no repeated account fixture against a shared site.
from tests.test_assistant_support_integration import (
    test_feedback_owner_isolation_explicit_share_faq_admin_versions_and_replay,
    test_report_own_authoritative_source_hash_scope_unread_and_deleted_thread,
    test_repair_original_session_bound_explicit_confirm_queue_actual_receipt_replay,
    test_repair_rejects_code_for_executor_protected_config_invalid_hash_unpaired,
    test_retention_keeps_structured_fault_faq_and_hash_audit_but_erases_repair_source,
    test_repair_settlement_relogin_only_records_existing_receipt_with_bound_token,
)
from tests.test_assistant_permissions_integration import (
    test_permission_wait_persists_releases_budget_owner_web_approves_but_cannot_read,
    test_default_roots_reused_but_symlink_actual_target_requires_new_hash,
    test_expiry_retry_cancel_and_expired_original_session_do_not_gain_authority,
    test_actual_local_execution_budget_is_charged_once_waiting_is_free_and_limit_stops_resume,
    test_thirteenth_durable_tool_read_returns_409_through_real_grant_and_postgresql,
)
from tests.test_assistant_diagnosis_integration import (
    test_diagnosis_auto_reads_csv_tail_and_existing_unuploaded_logs_with_original_job,
    test_uploaded_csv_without_hmi_uses_real_frozen_inventory_for_proactive_analysis,
    test_waiting_for_two_hmi_reads_does_not_occupy_both_execution_workers,
    test_web_approval_then_original_hmi_actually_reads_external_temporary_file,
)

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL'),reason='dedicated synthetic stack required')]


@pytest.fixture(autouse=True)
def finance(environment):
    engine,_=environment
    with engine.begin() as connection:
        connection.execute(text('DELETE FROM integration.assistant_model_call'))
        connection.execute(text('DELETE FROM integration.assistant_budget_day'))
        connection.execute(text('UPDATE integration.assistant_finance SET blocked=false,reason=NULL'))


def running(client,identifier):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        response=client.get('/api/v1/assistant/jobs/'+identifier)
        assert response.status_code==200,response.text
        job=response.json()['data']
        if job['state']=='RUNNING':return job
        time.sleep(.02)
    raise AssertionError('synthetic task did not enter RUNNING')


def test_incremental_progress_is_ordered_scoped_and_replayed_without_reexecuting(environment):
    _,users=environment
    control(mode='pause_before_tool')
    with signed_in(users['first']) as first,signed_in(users['second']) as other:
        queued=submit(first);running(first,queued['id'])
        path='/api/v1/assistant/jobs/'+queued['id']+'/events'
        assert other.get(path).status_code==404
        done=wait(first,queued['id']);assert done['state']=='COMPLETED',done
        events=first.get(path).json()['data'];rows=events['items']
        assert rows and any(e['kind']=='tool' for e in rows)
        assert any(e['kind']=='text' and e['data']['text'] for e in rows)
        assert [r['seq'] for r in rows]==sorted(set(r['seq'] for r in rows))
        assert first.get(path,params={'after':events['next_seq']}).json()['data']['items']==[]
        assert first.get(path).json()['data']['items']==rows
        assert 'reasoning_content' not in json.dumps(rows)


def test_steering_keeps_frozen_input_and_original_session_without_write_expansion(environment):
    engine,users=environment;control(mode='pause_before_tool')
    with signed_in(users['first']) as first,signed_in(users['second']) as other:
        queued=submit(first,'查询工位并解释时间');running(first,queued['id'])
        path='/api/v1/assistant/jobs/'+queued['id']+'/inputs'
        payload={'request_key':str(uuid4()),'expected_job_id':queued['id'],'message':'请先说明资料更新时间，补充不授权建档'}
        assert other.post(path,json=payload).status_code==404
        accepted=first.post(path,json=payload);assert accepted.status_code==200,accepted.text
        assert first.post(path,json=payload).json()['data']['replayed']
        changed=first.post(path,json={**payload,'message':'改成另一条'});assert changed.status_code==409
        with signed_in(users['first']) as new_session:
            rejected=new_session.post(path,json={**payload,'request_key':str(uuid4())});assert rejected.status_code==403,rejected.text
        done=wait(first,queued['id']);assert done['state']=='COMPLETED',done
        with engine.connect() as connection:
            job=connection.execute(text('SELECT input_snapshot,input_hash,origin_session_id,rounds FROM integration.assistant_job WHERE id=:id'),{'id':queued['id']}).mappings().one()
            snapshot=json.dumps(job['input_snapshot'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            assert hashlib.sha256(snapshot.encode()).hexdigest()==job['input_hash']
            assert '补充不授权建档' not in snapshot
            assert 1<=job['rounds']<=6
            assert connection.execute(text('SELECT count(*) FROM integration.setup_receipt WHERE request_key=:id'),{'id':queued['id']}).scalar_one()==0
            assert connection.execute(text('SELECT consumed_at FROM integration.assistant_input WHERE request_key=:key'),{'key':payload['request_key']}).scalar_one()


def test_queued_followup_starts_after_parent_and_does_not_replay_it(environment):
    engine,users=environment;control(mode='pause_before_tool')
    with signed_in(users['first']) as client:
        parent=submit(client);running(client,parent['id'])
        response=client.post('/api/v1/assistant/jobs',json={'request_key':str(uuid4()),'thread_id':parent['thread_id'],
            'message':'仅查询下一条工位状态','context':{'view':'stations','kind':'MODULE'},'after_job_id':parent['id']})
        assert response.status_code==202,response.text
        child=response.json()['data'];time.sleep(.15)
        waiting=client.get('/api/v1/assistant/jobs/'+child['id']).json()['data']
        assert waiting['state']=='QUEUED' and waiting['rounds']==0
        before=wait(client,parent['id']);after=wait(client,child['id'])
        assert before['state']==after['state']=='COMPLETED'
        with engine.connect() as connection:
            rows={str(r['id']):r for r in connection.execute(text('SELECT id,started_at,finished_at FROM integration.assistant_job WHERE id IN (:first,:second)'),{'first':parent['id'],'second':child['id']}).mappings()}
            assert rows[child['id']]['started_at']>=rows[parent['id']]['finished_at']
            sources=connection.execute(text('SELECT execution_sources FROM integration.assistant_job WHERE id=:id'),{'id':child['id']}).scalar_one()
            actual_parent=connection.execute(text('SELECT result FROM integration.assistant_job WHERE id=:id'),{'id':parent['id']}).scalar_one()
            metadata=sources['previous_task_result']
            assert metadata['job_id']==parent['id'] and metadata['provided_scope']=='reply_only'
            encoded=json.dumps(actual_parent,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            assert metadata['source_result_sha256']==hashlib.sha256(encoded.encode()).hexdigest()
        report=client.get('/api/v1/assistant/jobs/'+child['id']+'/report?format=json')
        assert report.status_code==200,report.text
        assert parent['id'] in report.text and metadata['provided_result_sha256'] in report.text


def test_unconsumed_supplement_survives_terminal_budget_boundary(environment):
    from tests.test_assistant_support_integration import seed_job
    engine,users=environment
    with signed_in(users['first']) as client:
        job=seed_job(engine,users['first'])
        key=uuid4()
        with engine.begin() as connection:
            owner=connection.execute(text('SELECT user_id,origin_session_id FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            connection.execute(text("UPDATE integration.assistant_job SET state='RUNNING',rounds=6,deadline=now()+interval '1 minute' WHERE id=:id"),{'id':job['id']})
            connection.execute(text('''INSERT INTO integration.assistant_input(request_key,job_id,user_id,session_id,message,delivered_at)
                VALUES(:key,:job,:owner,:session,:message,now())'''),{'key':key,'job':job['id'],'owner':owner['user_id'],'session':owner['origin_session_id'],'message':'尚未用于后续模型请求的补充'})
        refused=client.post('/api/v1/assistant/jobs/'+job['id']+'/inputs',json={'request_key':str(uuid4()),'expected_job_id':job['id'],'message':'第六轮不可继续接收'})
        assert refused.status_code==409,refused.text
        with engine.begin() as connection:
            public_user=connection.execute(text('SELECT public_id FROM iam.app_user WHERE id=:id'),{'id':owner['user_id']}).scalar_one()
            connection.execute(text("SELECT integration.assistant_finish(:id,:user,'TIMED_OUT',CAST(:result AS jsonb))"),{'id':job['id'],'user':public_user,'result':json.dumps({'reply':'预算已用尽','cards':[]})})
        final=client.get('/api/v1/assistant/jobs/'+job['id']).json()['data']
        card=next(c for c in final['result']['cards'] if c['type']=='unprocessed_input')
        assert card['data']['items']==[{'request_key':str(key),'message':'尚未用于后续模型请求的补充'}]
        assert final['steering_closed'] is True


def test_restart_maintenance_retains_unprocessed_input_without_reexecuting(environment):
    from tests.test_assistant_support_integration import seed_job
    engine,users=environment
    with signed_in(users['first']) as client:
        job=seed_job(engine,users['first']);key=uuid4()
        with engine.begin() as connection:
            owner=connection.execute(text('SELECT user_id,origin_session_id FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            connection.execute(text("UPDATE integration.assistant_job SET state='RUNNING',result=NULL,finished_at=NULL,deadline=now()+interval '1 minute' WHERE id=:id"),{'id':job['id']})
            connection.execute(text('''INSERT INTO integration.assistant_input(request_key,job_id,user_id,session_id,message)
                VALUES(:key,:job,:owner,:session,'重启前已确认接收的补充')'''),{'key':key,'job':job['id'],'owner':owner['user_id'],'session':owner['origin_session_id']})
            connection.execute(text('SELECT integration.assistant_maintenance(true)'))
            connection.execute(text('SELECT integration.assistant_maintenance(true)'))
        final=client.get('/api/v1/assistant/jobs/'+job['id']).json()['data']
        assert final['state']=='INTERRUPTED' and final['steering_closed']
        cards=[c for c in final['result']['cards'] if c['type']=='unprocessed_input']
        assert len(cards)==1 and cards[0]['data']['items'][0]['request_key']==str(key)
        history=client.get('/api/v1/assistant/threads/'+job['thread_id']).json()['data']
        messages=history['messages'] if isinstance(history,dict) else history
        assert len([m for m in messages if m.get('job_id')==job['id'] and m['role']=='assistant'])==1
        with engine.connect() as connection:
            assert connection.execute(text('SELECT count(*) FROM integration.assistant_model_call WHERE job_id=:id'),{'id':job['id']}).scalar_one()==0


def test_restart_does_not_revive_expired_private_input_or_context(environment):
    from tests.test_assistant_support_integration import seed_job
    engine,users=environment
    with signed_in(users['first']) as client:
        job=seed_job(engine,users['first']);marker='expired-private-supplement-never-revive'
        with engine.begin() as connection:
            owner=connection.execute(text('SELECT user_id,origin_session_id FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            connection.execute(text("UPDATE integration.assistant_job SET state='RUNNING',result=NULL,created_at=now()-interval '31 days',finished_at=NULL WHERE id=:id"),{'id':job['id']})
            connection.execute(text('''INSERT INTO integration.assistant_input(request_key,job_id,user_id,session_id,message,created_at)
                VALUES(:key,:job,:owner,:session,:message,now()-interval '31 days')'''),{'key':uuid4(),'job':job['id'],'owner':owner['user_id'],'session':owner['origin_session_id'],'message':marker})
            connection.execute(text('SELECT integration.assistant_maintenance(true)'))
            result=connection.execute(text('SELECT state,result,context,input_snapshot FROM integration.assistant_job WHERE id=:id'),{'id':job['id']}).mappings().one()
            assert result['state']=='INTERRUPTED' and result['result'] is None and result['context']=={} and result['input_snapshot'] is None
            assert connection.execute(text('SELECT count(*) FROM integration.assistant_input WHERE job_id=:id'),{'id':job['id']}).scalar_one()==0
            assert connection.execute(text('SELECT count(*) FROM integration.assistant_message WHERE job_id=:id'),{'id':job['id']}).scalar_one()==0
        history=client.get('/api/v1/assistant/threads/'+job['thread_id'])
        assert marker not in history.text and job['installation_id'] not in history.text


def test_new_chat_activity_does_not_extend_old_private_title_retention(environment):
    from tests.test_assistant_support_integration import seed_job
    engine,users=environment
    with signed_in(users['first']):
        job=seed_job(engine,users['first'])
        with engine.begin() as connection:
            connection.execute(text("UPDATE integration.assistant_thread SET title='expired private first question',title_updated_at=now()-interval '31 days',updated_at=now() WHERE id=:id"),{'id':job['thread_id']})
            connection.execute(text('SELECT integration.assistant_maintenance(false)'))
            assert connection.execute(text('SELECT title FROM integration.assistant_thread WHERE id=:id'),{'id':job['thread_id']}).scalar_one()==''


def test_titles_search_delete_busy_and_report_access_share_same_visibility(environment):
    _,users=environment;control(mode='pause_before_tool')
    with signed_in(users['first']) as first,signed_in(users['second']) as second:
        queued=submit(first);running(first,queued['id'])
        thread='/api/v1/assistant/threads/'+queued['thread_id']
        assert first.delete(thread).status_code==409
        assert second.patch(thread,json={'title':'伪造标题'}).status_code==404
        assert first.patch(thread,json={'title':'手臂诊断资料'}).status_code==200
        assert any(r['id']==queued['thread_id'] for r in first.get('/api/v1/assistant/threads',params={'q':'手臂诊断'}).json()['data'])
        wait(first,queued['id'])
        assert first.delete(thread).status_code==204
        assert first.get(thread).status_code==404
        assert first.get('/api/v1/assistant/jobs/'+queued['id']).status_code==404
        assert first.get('/api/v1/assistant/jobs/'+queued['id']+'/report?format=json').status_code==404
        assert all(r['id']!=queued['thread_id'] for r in first.get('/api/v1/assistant/threads').json()['data'])


def test_object_reference_visibility_validated_at_submission(environment):
    _,users=environment
    with signed_in(users['first']) as first:
        for kind in ['assets','stations','executions']:
            response=first.get('/api/v1/assistant/references',params={'kind':kind,'q':''})
            assert response.status_code==200,response.text
            assert all(r['kind']==kind and r['id'] and r['label'] for r in response.json()['data']['items'])
        response=first.post('/api/v1/assistant/jobs',json={'request_key':str(uuid4()),'message':'读取引用',
            'context':{'references':[{'kind':'assets','id':str(uuid4()),'label':'不存在对象'}]}})
        assert response.status_code==404,response.text


def test_hmi_platform_roundtrip_real_cookie_upload_feedback_report_repair_and_receipt_recovery(environment,tmp_path,monkeypatch):
    """Actual HMI transport + API/RLS + Pi/mock + temporary local text only.

    One injected post-write logout exercises an authentic new-login settlement;
    it never targets an installed HMI file, running control service or device.
    """
    import copy
    import base64
    from fastapi import HTTPException
    from factory_hmi.gateway.diagnostics import DiagnosticBridge
    from factory_hmi.core.diagnostic_repairs import DiagnosticRepairer
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    from tests.test_assistant_support_integration import propose
    _,users=environment
    root=tmp_path/'synthetic-hmi';profile=root/'factory_hmi/config/reliability_platform.yaml'
    profile.parent.mkdir(parents=True)
    original='profiles: [{key: synthetic, label: Before, backend: mock}]\n'
    replacement=original.replace('Before','After');profile.write_text(original)
    bridge=DiagnosticBridge(tmp_path/'state')
    bridge.repairs=DiagnosticRepairer(tmp_path/'state',code_roots=[root],profile_path=profile)
    owner='synthetic-roundtrip-window'
    def bind(client,window):
        platform=ReliabilityPlatformClient(os.environ['ASSISTANT_QA_HTTP_URL'])
        for cookie in client.cookies.jar:platform.cookies.set_cookie(copy.copy(cookie))
        bridge.bind(platform,window,users['first'],'SYNTHETIC-ROUNDTRIP')
        return bridge.call(window,'status')['generation']
    with signed_in(users['first']) as first:
        generation=bind(first,owner)
        upload_id=str(uuid4());content=b'Only synthetic diagnostic facts.\n'
        bridge.call(owner,'upload-start',{'generation':generation,'upload_id':upload_id,'total_bytes':len(content)})
        attachment=bridge.call(owner,'upload',{'generation':generation,'upload_id':upload_id},raw=content,mime='application/octet-stream',filename='synthetic-roundtrip.txt')
        progress=bridge.call(owner,'upload-progress',{'generation':generation,'upload_id':upload_id})
        assert progress['bytes_sent']==len(content) and progress['phase']=='uploaded'
        queued=bridge.call(owner,'submit',{'generation':generation,'request_key':str(uuid4()),'message':'查询工位并解释附件证据来源','attachments':[attachment['id']]})
        assert wait(first,queued['id'])['state']=='COMPLETED'
        feedback=bridge.call(owner,'feedback',{'generation':generation,'request_key':str(uuid4()),'job_id':queued['id'],'rating':'helpful','note':'Only synthetic roundtrip feedback','share_for_faq':False})
        assert feedback['share_for_faq'] is False
        shared=bridge.call(owner,'feedback',{'generation':generation,'request_key':str(uuid4()),'job_id':queued['id'],'rating':'issue','note':'Synthetic share-consented FAQ source','share_for_faq':True})
        with signed_in(users['admin']) as admin:
            platform=ReliabilityPlatformClient(os.environ['ASSISTANT_QA_HTTP_URL'])
            for cookie in admin.cookies.jar:platform.cookies.set_cookie(copy.copy(cookie))
            faq_bridge=DiagnosticBridge(tmp_path/'faq-state')
            faq_bridge.bind(platform,'faq-window',users['admin'],'SYNTHETIC-FAQ')
            faq_generation=faq_bridge.call('faq-window','status')['generation']
            fields={'generation':faq_generation,'request_key':str(uuid4()),'feedback_id':shared['id'],'question':'Synthetic native FAQ roundtrip','answer':'Original synthetic answer'}
            faq=faq_bridge.call('faq-window','faq-publish',fields)
            revision=faq_bridge.call('faq-window','faq-publish',{**fields,'request_key':str(uuid4()),'faq_id':faq['id'],'expected_revision':1,'answer':'Revised synthetic answer'})
            assert revision['revision']==2
            assert any(item['id']==faq['id'] and item['revision']==2 for item in bridge.call(owner,'faq',{'generation':generation,'q':'Synthetic native FAQ'})['items'])
        report=bridge.call(owner,'report',{'generation':generation,'id':queued['id'],'format':'json'})
        raw=base64.b64decode(report['content_base64'])
        assert hashlib.sha256(raw).hexdigest()==report['sha256']
        document=json.loads(raw)
        assert document['formal_archive'] is False and document['attachments'][0]['sha256']==attachment['sha256']
        response,_=propose(first,queued,original=original,replacement=replacement)
        assert response.status_code==201,response.text
        proposal=response.json()['data']
        bridge.call(owner,'repair-confirm',{'generation':generation,'id':proposal['id'],'proposal_sha256':proposal['proposal_sha256']})
        actual_post=bridge._settlement_post
        def logout_after_write(*args):
            bridge.revoke()
            raise HTTPException(401,'injected logout after actual temporary-file replacement')
        monkeypatch.setattr(bridge,'_settlement_post',logout_after_write)
        with pytest.raises(HTTPException) as exc:
            bridge.call(owner,'repair-apply',{'generation':generation,'id':proposal['id']})
        assert exc.value.status_code==409 and exc.value.detail['saved'] is True
        assert profile.read_text()==replacement
        identity=profile.stat()
        monkeypatch.setattr(bridge,'_settlement_post',actual_post)
    with signed_in(users['first']) as relogged:
        new_window='synthetic-relogin-window'
        generation=bind(relogged,new_window)
        receipts=relogged.get('/api/v1/assistant/jobs/'+queued['id']+'/repairs').json()['data']['items']
        receipt=next(r for r in receipts if r['id']==proposal['id'])
        assert receipt['state']=='applied' and receipt['receipt']['saved'] is True
        assert bridge.call(new_window,'repair-pending',{'generation':generation})['items']==[]
        bridge.call(new_window,'status')
        assert profile.read_text()==replacement
        assert (profile.stat().st_ino,profile.stat().st_mtime_ns)==(identity.st_ino,identity.st_mtime_ns)
