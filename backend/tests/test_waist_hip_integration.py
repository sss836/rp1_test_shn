import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from test_hmi_presence_integration import environment, heartbeat_payload, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated database required")]


def test_waist_hip_identity_reads_context_and_mock_execution_stay_separate_from_lower(environment):
    engine, users = environment
    suffix = uuid4().hex[:8].upper()
    with signed_in(users['admin']) as admin:
        def create(path, payload):
            response = admin.post('/api/v1/setup/' + path, json={**payload, 'reason': 'Isolated waist hip regression'})
            assert response.status_code == 201, response.text
            return response.json()['data']

        parts = admin.get('/api/v1/setup/options').json()['data']['parts']
        assert next(p for p in parts if p['code'] == 'WAISTHIP')['name'] == '腰胯'
        payload = dict(asset_code='RP1.3-WAISTHIP-001', asset_kind='MODULE', target_part_code='WAISTHIP', product_family='QA', serial_number=suffix, display_name='Isolated waist hip sample', model='WAISTHIP', configuration_fingerprint='QA-' + suffix)
        mismatch = admin.post('/api/v1/setup/assets', json={**payload, 'target_part_code': 'LOWER', 'reason': 'Isolated mismatch'})
        assert mismatch.status_code == 422
        sample = create('assets', payload)
        listed = admin.get('/api/v1/assets', params={'asset_kind': 'MODULE', 'target_part_code': 'WAISTHIP'})
        assert listed.status_code == 200, listed.text
        assert [row['code'] for row in listed.json()['data']['items']] == [payload['asset_code']]
        detail = admin.get('/api/v1/assets/' + sample['id'])
        assert detail.status_code == 200, detail.text
        assert detail.json()['data']['asset']['target_part_code'] == 'WAISTHIP'

        with engine.begin() as db:
            case_id = db.execute(text("INSERT INTO catalog.test_case(case_code,name,asset_kind,domain,evidence_type,target_part_code) VALUES (:code,'QA waist hip','MODULE','RELIABILITY','RUNTIME_AND_METRIC','WAISTHIP') RETURNING id"), {'code': 'QA-WAISTHIP-' + suffix}).scalar_one()
            version = db.execute(text("INSERT INTO catalog.test_case_version(test_case_id,version,procedure_spec,stage_definitions,status) VALUES (:id,'QA','{}','[]','PUBLISHED') RETURNING public_id"), {'id': case_id}).scalar_one()
        program = create('programs', {'program_code': 'QA-WH-' + suffix, 'name': 'QA'})
        plan = create('campaigns', {'program_id': program['id'], 'campaign_code': 'QA-WH-' + suffix, 'name': 'QA', 'asset_kind': 'MODULE'})
        station = create('stations', {'site_code': 'QA-WH-' + suffix, 'site_name': 'QA', 'lab_code': 'WH', 'lab_name': 'QA', 'station_code': 'QA-WH-' + suffix, 'name': 'QA', 'station_type': 'MODULE'})
        context = create('campaigns/' + plan['id'] + '/contexts', {'asset_id': sample['id'], 'configuration_id': sample['configuration_id'], 'test_case_version_id': str(version), 'station_id': station['id'], 'cycle_code': 'QA-WH-' + suffix})['context']
        response = admin.get('/api/v1/dashboard/home')
        assert response.status_code == 200, response.text
        scope = next(item for group in response.json()['data']['groups'] for item in group['objects'] if item['target_part_code'] == 'WAISTHIP')
        assert scope['part_asset_count'] == 1

        heartbeat = heartbeat_payload()
        heartbeat['bench_id'] = 'QA-WH-' + suffix
        heartbeat['runs'][0].update({key: value for key, value in context.items() if key != 'segment_id'})
        heartbeat['runs'][0].update(sample_id=payload['asset_code'], case_code='QA-WAISTHIP-' + suffix, test_case='waist_hip')
        route = '/api/v1/hmi/presence/' + str(uuid4())
        response = admin.put(route, json=heartbeat)
        assert response.status_code == 200, response.text
        execution = response.json()['data']['executions'][0]
        assert '_RP1.3-WAISTHIP-001_' in execution['execution_code']
        assert admin.get('/api/v1/executions/' + execution['execution_id']).status_code == 200
        heartbeat['sequence'] += 1
        heartbeat['runs'][0]['state'] = 'completed'
        assert admin.put(route, json=heartbeat).status_code == 200
