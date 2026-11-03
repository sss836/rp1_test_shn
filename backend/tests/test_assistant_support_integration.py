"""Actual Cookie/CSRF, PostgreSQL RLS and API tests in the dedicated QA stack.

Jobs are deterministic synthetic finished diagnostics, so no model is called.
The shared fixture refuses any database/HTTP target outside its isolated stack.
"""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.test_assistant_integration import environment, signed_in

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not os.getenv('ASSISTANT_QA_ADMIN_URL'), reason='dedicated isolated stack required')]


def seed_job(engine, username, *, hmi=True, reply='Synthetic diagnosis'):
    identifier, thread, installation = uuid4(), uuid4(), uuid4()
    context = {'view': 'diagnostics', 'hmi': {'installation_id': str(installation), 'window_id': 'support-window',
               'station': 'synthetic', 'running_version': 'synthetic-p01'}} if hmi else {'view': 'home'}
    with engine.begin() as connection:
        user = connection.execute(text('SELECT id FROM iam.app_user WHERE username=:name'), {'name': username}).scalar_one()
        session = connection.execute(text('SELECT public_id FROM iam.auth_session WHERE user_id=:id '
                                          'AND revoked_at IS NULL ORDER BY id DESC LIMIT 1'), {'id': user}).scalar_one()
        connection.execute(text('INSERT INTO integration.assistant_thread(id,user_id) VALUES(:id,:user)'),
                           {'id': thread, 'user': user})
        connection.execute(text('''INSERT INTO integration.assistant_job
            (id,thread_id,user_id,origin_session_id,request_hash,context,state,result,input_hash,input_snapshot,finished_at)
            VALUES(:id,:thread,:user,:session,:hash,CAST(:context AS jsonb),'COMPLETED',CAST(:result AS jsonb),:hash,
                   CAST(:snapshot AS jsonb),now())'''),
            {'id': identifier, 'thread': thread, 'user': user, 'session': session, 'hash': 'a'*64,
             'context': json.dumps(context), 'snapshot': json.dumps({'attachments': [{'sha256': 'b'*64, 'name': 'synthetic.csv'}]}),
             'result': json.dumps({'reply': reply, 'cards': [{'source': 'synthetic-test', 'data': {'version': 'fixture-v1'}}],
                                  'attachment_read_coverage': {'complete': False, 'unread_bytes': 17}})})
    return {'id': str(identifier), 'thread_id': str(thread), 'installation_id': str(installation), 'window_id': 'support-window'}


def propose(client, job, *, kind='config', original=None, replacement=None, path=None):
    original = original or 'profiles: [{key: synthetic, label: Before, backend: mock}]\n'
    replacement = replacement or original.replace('Before', 'After')
    payload = {'request_key': str(uuid4()), 'kind': kind,
               'path': path or 'factory_hmi/config/reliability_platform.yaml',
               'expected_sha256': hashlib.sha256(original.encode()).hexdigest(),
               'original': original, 'replacement': replacement, 'reason': 'Synthetic label compatibility repair'}
    response = client.post('/api/v1/assistant/jobs/' + job['id'] + '/repairs', json=payload)
    return response, payload


def test_feedback_owner_isolation_explicit_share_faq_admin_versions_and_replay(environment):
    engine, users = environment
    with signed_in(users['first']) as first, signed_in(users['second']) as other, signed_in(users['admin']) as admin:
        job = seed_job(engine, users['first'])
        payload = {'request_key': str(uuid4()), 'job_id': job['id'], 'rating': 'issue',
                   'note': 'Synthetic error secret=fixture someone@example.com', 'share_for_faq': False}
        result = first.post('/api/v1/assistant/feedback', json=payload)
        assert result.status_code == 201, result.text
        feedback = result.json()['data']
        assert 'fixture' not in feedback['note'] and 'someone@example.com' not in feedback['note']
        assert first.post('/api/v1/assistant/feedback', json=payload).json()['data']['replayed']
        assert first.post('/api/v1/assistant/feedback', json={**payload, 'note': 'changed'}).status_code == 409
        assert other.post('/api/v1/assistant/feedback', json={**payload, 'request_key': str(uuid4())}).status_code == 404
        assert feedback['id'] not in {x['id'] for x in other.get('/api/v1/assistant/feedback').json()['data']['items']}
        assert first.get('/api/v1/assistant/feedback?shared=true').status_code == 403
        faq = {'request_key': str(uuid4()), 'feedback_id': feedback['id'], 'question': 'Synthetic FAQ', 'answer': 'Original answer'}
        assert admin.post('/api/v1/assistant/faq', json=faq).status_code == 404
        shared = first.post('/api/v1/assistant/feedback', json={**payload, 'request_key': str(uuid4()), 'share_for_faq': True}).json()['data']
        faq['feedback_id'] = shared['id']
        assert first.post('/api/v1/assistant/faq', json=faq).status_code == 403
        published = admin.post('/api/v1/assistant/faq', json=faq)
        assert published.status_code == 201, published.text
        version1 = published.json()['data']
        assert admin.post('/api/v1/assistant/faq', json=faq).json()['data']['replayed']
        version2 = admin.post('/api/v1/assistant/faq', json={**faq, 'request_key': str(uuid4()),
                               'faq_id': version1['id'], 'expected_revision': 1, 'answer': 'Revised answer'}).json()['data']
        assert version2['revision'] == 2
        assert admin.post('/api/v1/assistant/faq', json={**faq, 'request_key': str(uuid4()),
                          'faq_id': version1['id'], 'expected_revision': 1, 'answer': 'Stale overwrite'}).status_code == 409
        assert admin.post('/api/v1/assistant/faq', json={**faq, 'request_key': str(uuid4()),
                          'faq_id': version1['id'], 'answer': 'Missing baseline'}).status_code == 409
        listed = first.get('/api/v1/assistant/faq?q=Synthetic').json()['data']['items']
        item = next(x for x in listed if x['id'] == version1['id'])
        assert item['answer'] == 'Revised answer' and item['revision'] == 2
        contenders = [{**faq, 'request_key': str(uuid4()), 'faq_id': version1['id'],
                       'expected_revision': 2, 'answer': 'Concurrent answer ' + str(index)} for index in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda candidate: admin.post('/api/v1/assistant/faq', json=candidate), contenders))
        assert sorted(response.status_code for response in responses) == [201, 409]
        assert next(response.json()['data']['revision'] for response in responses if response.status_code == 201) == 3
    with engine.begin() as connection:
        assert connection.execute(text('SELECT count(*) FROM integration.assistant_faq_version WHERE id=:id'),
                                  {'id': version1['id']}).scalar_one() == 3
        with pytest.raises(DBAPIError):
            connection.execute(text('UPDATE integration.assistant_faq_version SET answer=:answer WHERE id=:id'),
                               {'answer': 'overwrite', 'id': version1['id']})


def test_report_own_authoritative_source_hash_scope_unread_and_deleted_thread(environment):
    engine, users = environment
    with signed_in(users['first']) as first, signed_in(users['second']) as other:
        job = seed_job(engine, users['first'], reply='=Synthetic formula-looking reply')
        for format in ('json', 'csv', 'markdown'):
            response = first.get('/api/v1/assistant/jobs/' + job['id'] + '/report?format=' + format)
            assert response.status_code == 200, response.text
            assert hashlib.sha256(response.content).hexdigest() == response.headers['X-Content-SHA256']
            assert response.headers['Cache-Control'] == 'no-store'
        report = first.get('/api/v1/assistant/jobs/' + job['id'] + '/report?format=json').json()
        assert report['attachments'][0]['sha256'] == 'b'*64
        assert report['scope']['hmi']['running_version'] == 'synthetic-p01'
        assert report['read_coverage']['complete'] is False and report['formal_archive'] is False
        assert other.get('/api/v1/assistant/jobs/' + job['id'] + '/report').status_code == 404
        removed = first.delete('/api/v1/assistant/threads/' + job['thread_id'])
        assert removed.status_code in (200, 204), removed.text
        assert first.get('/api/v1/assistant/jobs/' + job['id'] + '/report').status_code == 404


def test_repair_original_session_bound_explicit_confirm_queue_actual_receipt_replay(environment):
    engine, users = environment
    with signed_in(users['first']) as first, signed_in(users['second']) as other:
        job = seed_job(engine, users['first'])
        created, payload = propose(first, job)
        assert created.status_code == 201, created.text
        proposal = created.json()['data']
        assert proposal['state'] == 'proposed' and proposal['diff'] and proposal['role'] == 'TEST_EXECUTOR'
        assert first.post('/api/v1/assistant/jobs/' + job['id'] + '/repairs', json=payload).json()['data']['replayed']
        assert other.get('/api/v1/assistant/jobs/' + job['id'] + '/repairs').status_code == 404
        params = {'installation_id': job['installation_id'], 'window_id': job['window_id']}
        assert first.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items'] == []
        confirm = '/api/v1/assistant/repairs/' + proposal['id'] + '/confirm'
        assert first.post(confirm, json={'proposal_sha256': '0'*64}).status_code == 409
        with signed_in(users['first']) as replacement_login:
            assert replacement_login.post(confirm, json={'proposal_sha256': proposal['proposal_sha256']}).status_code == 403
            assert replacement_login.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items'] == []
        confirmed = first.post(confirm, json={'proposal_sha256': proposal['proposal_sha256']})
        assert confirmed.status_code == 200, confirmed.text
        pending = first.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items']
        assert any(x['id'] == proposal['id'] and x['proposal_sha256'] == proposal['proposal_sha256'] for x in pending)
        assert first.get('/api/v1/assistant/repairs/pending', params={**params, 'window_id': 'another'}).json()['data']['items'] == []
        receipt = {**params, 'result': {'status': 'applied', 'before_sha256': payload['expected_sha256'],
            'after_sha256': hashlib.sha256(payload['replacement'].encode()).hexdigest(),
            'backup_path': '/synthetic/backup', 'message': 'Isolated synthetic receipt, no real file changed'}}
        url = '/api/v1/assistant/repairs/' + proposal['id'] + '/receipt'
        assert first.post(url, json={**receipt, 'window_id': 'another'}).status_code == 403
        assert first.post(url, json={**receipt, 'result': {'status': 'applied'}}).status_code == 422
        saved = first.post(url, json=receipt)
        assert saved.status_code == 200, saved.text
        assert saved.json()['data']['state'] == 'applied'
        assert first.post(url, json=receipt).json()['data']['replayed']
        assert first.post(url, json={**receipt, 'result': {'status': 'failed', 'message': 'different'}}).status_code == 409
        assert first.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items'] == []
    with engine.connect() as connection:
        metadata = connection.execute(text("SELECT metadata FROM audit.security_event WHERE event_type='ASSISTANT_REPAIR_RECORD' "
                                           "AND metadata->>'id'=:id"), {'id': proposal['id']}).scalars().all()
        assert len(metadata) == 3 and not any('original' in x or 'replacement' in x for x in metadata)


def test_repair_rejects_code_for_executor_protected_config_invalid_hash_unpaired(environment):
    engine, users = environment
    with signed_in(users['first']) as first, signed_in(users['admin']) as admin:
        job = seed_job(engine, users['first'])
        assert propose(first, job, kind='code', original='a=1\n', replacement='a=2\n', path='factory_hmi/view.py')[0].status_code == 403
        original = 'profiles: [{key: synthetic, label: Before, backend: mock}]\n'
        assert propose(first, job, original=original, replacement=original.replace('mock', 'real'))[0].status_code == 403
        response, payload = propose(first, job)
        assert response.status_code == 201
        assert first.post('/api/v1/assistant/jobs/' + job['id'] + '/repairs',
                          json={**payload, 'request_key': str(uuid4()), 'expected_sha256': 'f'*64}).status_code == 422
        unpaired = seed_job(engine, users['first'], hmi=False)
        assert propose(first, unpaired)[0].status_code == 422
        admin_job = seed_job(engine, users['admin'])
        assert propose(admin, admin_job, kind='code', original='a=1\n', replacement='a=2\n', path='factory_hmi/view.py')[0].status_code == 201


def test_retention_keeps_structured_fault_faq_and_hash_audit_but_erases_repair_source(environment):
    engine, users = environment
    with signed_in(users['admin']) as admin:
        job = seed_job(engine, users['admin'])
        feedback = admin.post('/api/v1/assistant/feedback', json={'request_key': str(uuid4()), 'job_id': job['id'],
                          'rating': 'issue', 'note': 'Long-term structured synthetic issue'}).json()['data']
        faq = admin.post('/api/v1/assistant/faq', json={'request_key': str(uuid4()), 'feedback_id': feedback['id'],
                          'question': 'Retention synthetic FAQ', 'answer': 'Long-term shared answer'}).json()['data']
        created, _ = propose(admin, job)
        assert created.status_code == 201, created.text
        proposal = created.json()['data']
        with engine.begin() as connection:
            connection.execute(text("UPDATE integration.assistant_repair SET created_at=now()-interval '31 days',"
                                    "expires_at=now()-interval '30 days' WHERE id=:id"), {'id': proposal['id']})
            connection.execute(text('SELECT integration.assistant_maintenance(false)'))
            row = connection.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id'),
                                     {'id': proposal['id']}).mappings().one()
            assert row['original'] is None and row['replacement'] is None and row['diff'] is None
            assert row['state'] == 'rejected' and row['proposal_sha256'] == proposal['proposal_sha256']
            assert connection.execute(text('SELECT id FROM integration.assistant_feedback WHERE id=:id'),
                                      {'id': feedback['id']}).scalar_one()
            assert connection.execute(text('SELECT id FROM integration.assistant_faq_version WHERE id=:id'),
                                      {'id': faq['id']}).scalar_one()


def test_repair_settlement_relogin_only_records_existing_receipt_with_bound_token(environment):
    engine, users = environment
    with signed_in(users['first']) as original, signed_in(users['second']) as other:
        job = seed_job(engine, users['first'])
        created, payload = propose(original, job)
        assert created.status_code == 201, created.text
        proposal = created.json()['data']
        confirm_url = '/api/v1/assistant/repairs/' + proposal['id'] + '/confirm'
        assert original.post(confirm_url, json={'proposal_sha256': proposal['proposal_sha256']}).status_code == 200
        params = {'installation_id': job['installation_id'], 'window_id': job['window_id']}
        pending = original.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items']
        delivery = next(item for item in pending if item['id'] == proposal['id'])
        token = delivery['settlement_token']
        assert token.startswith('v1.') and len(token) == 67
        assert 'settlement_token' not in proposal
        assert 'settlement_token' not in original.get('/api/v1/assistant/jobs/' + job['id'] + '/repairs').text
        receipt = {**params, 'settlement_token': token, 'result': {'status': 'applied',
            'before_sha256': payload['expected_sha256'], 'after_sha256': hashlib.sha256(payload['replacement'].encode()).hexdigest(),
            'backup_path': '/synthetic/already-saved-backup',
            'message': 'Synthetic existing terminal receipt only; no filesystem action occurs in this test'}}
        receipt_url = '/api/v1/assistant/repairs/' + proposal['id'] + '/receipt'
        assert original.post('/api/v1/auth/logout').status_code == 204
        # Model a delayed receipt whose chat/source has reached retention. Its
        # immutable operation hashes survive; receiving it cannot execute code.
        with engine.begin() as connection:
            connection.execute(text("UPDATE integration.assistant_repair SET created_at=now()-interval '31 days',"
                "expires_at=now()-interval '30 days' WHERE id=:id"), {'id': proposal['id']})
            connection.execute(text("UPDATE integration.assistant_job SET created_at=now()-interval '31 days' WHERE id=:id"), {'id': job['id']})
            connection.execute(text('SELECT integration.assistant_maintenance(false)'))
            row = connection.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id'), {'id': proposal['id']}).mappings().one()
            assert row['original'] is None and row['replacement'] is None and row['state'] == 'rejected'
        assert other.post(receipt_url, json=receipt).status_code == 404
        with signed_in(users['first']) as replacement:
            assert replacement.get('/api/v1/assistant/repairs/pending', params=params).json()['data']['items'] == []
            assert replacement.post(confirm_url, json={'proposal_sha256': proposal['proposal_sha256']}).status_code in (403, 410)
            assert replacement.post(receipt_url, json={k: v for k, v in receipt.items() if k != 'settlement_token'}).status_code == 403
            assert replacement.post(receipt_url, json={**receipt, 'settlement_token': 'v1.' + '0'*64}).status_code == 403
            assert replacement.post(receipt_url, json={**receipt, 'window_id': 'different-window'}).status_code == 403
            recorded = replacement.post(receipt_url, json=receipt)
            assert recorded.status_code == 200, recorded.text
            assert recorded.json()['data'] == {'accepted': True, 'replayed': False, 'state': 'applied'}
            assert replacement.post(receipt_url, json=receipt).json()['data']['replayed']
            different = {**receipt, 'result': {**receipt['result'], 'message': 'A different receipt must never replace the first'}}
            assert replacement.post(receipt_url, json=different).status_code == 409
    with engine.connect() as connection:
        row = connection.execute(text('SELECT state,receipt_sha256,confirmed_at FROM integration.assistant_repair WHERE id=:id'),
                                 {'id': proposal['id']}).mappings().one()
        assert row['state'] == 'applied' and row['receipt_sha256'] and row['confirmed_at']
