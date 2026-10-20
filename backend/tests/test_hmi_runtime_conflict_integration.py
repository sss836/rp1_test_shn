"""Real cookie and PostgreSQL regressions; synthetic metadata and mock telemetry only."""
import copy
import os

import pytest
from uuid import uuid4

from sqlalchemy import text

from test_hmi_presence_integration import environment, find_station, heartbeat_payload, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated database required")]


def context_for(admin):
    suffix = uuid4().hex[:8].upper()

    def create(path, payload):
        response = admin.post('/api/v1/setup/' + path, json={**payload, 'reason': 'Isolated runtime overlap regression'})
        assert response.status_code == 201, response.text
        return response.json()['data']

    case = next(item for item in admin.get('/api/v1/setup/catalog').json()['data']
                if item['target_part_code'] == 'ARM' and item['enabled'] and item['version_id'])
    number = int(suffix, 16) % 900 + 100
    sample = create('assets', dict(asset_code=f'RP1.3-ARM-{number:03d}', asset_kind='MODULE',
        target_part_code='ARM', product_family='QA', serial_number=suffix, display_name='Synthetic overlap sample',
        model='QA', configuration_fingerprint='QA-' + suffix))
    program = create('programs', dict(program_code='QA-RC-' + suffix, name='QA'))
    campaign = create('campaigns', dict(program_id=program['id'], campaign_code='QA-RC-' + suffix, name='QA', asset_kind='MODULE'))
    station = create('stations', dict(site_code='QA-RC-' + suffix, site_name='QA', lab_code='RC', lab_name='QA',
        station_code='QA-RC-' + suffix, name='QA', station_type='MODULE'))
    return create('campaigns/' + campaign['id'] + '/contexts', dict(asset_id=sample['id'], configuration_id=sample['configuration_id'],
        test_case_version_id=case['version_id'], station_id=station['id'], cycle_code='QA-RC-' + suffix))['context']


def payload_for(context, *, seconds=120):
    payload = heartbeat_payload()
    payload['runs'][0].update({k: v for k, v in context.items() if k != 'segment_id'})
    payload['runs'][0].update(execution_uuid=str(uuid4()), active_seconds=seconds, sample_id='SYNTHETIC')
    return payload


def send(client, route, payload, **run_changes):
    value = copy.deepcopy(payload)
    value['runs'][0].update(run_changes)
    response = client.put(route, json=value)
    assert response.status_code == 200, response.text
    return response.json()['data']


def overlapping_pair(admin):
    context = context_for(admin)
    first, second = payload_for(context), payload_for(context, seconds=2)
    first_route, second_route = ['/api/v1/hmi/presence/' + str(uuid4()) for _ in range(2)]
    first_ack = send(admin, first_route, first)
    second_ack = send(admin, second_route, second)
    second['sequence'] = 2
    send(admin, second_route, second, state='completed')
    return first_route, first, first_ack['executions'][0], second_ack['executions'][0]


def test_stale_overlap_does_not_block_other_login_or_worker_and_keeps_evidence(environment):
    engine, users = environment
    with signed_in(users['admin']) as owner:
        route, payload, broken, other = overlapping_pair(owner)
        with engine.begin() as db:
            original = db.execute(text('SELECT row_to_json(ri)::text FROM test.runtime_interval ri JOIN test.test_execution e ON e.id=ri.execution_id WHERE e.public_id=:id'), {'id': other['execution_id']}).scalar_one()
            db.execute(text("UPDATE integration.hmi_presence SET last_seen_at=clock_timestamp()-interval '46 seconds' WHERE connection_id=CAST(:id AS uuid)"), {'id': route.rsplit('/', 1)[-1]})
        # Before the repair this login succeeds, then its first heartbeat returns 409.
        with signed_in(users['second']) as unrelated:
            idle = heartbeat_payload()
            idle['runs'] = []
            response = unrelated.put('/api/v1/hmi/presence/' + str(uuid4()), json=idle)
            assert response.status_code == 200, response.text
            assert find_station(unrelated, idle['installation_id'])['online']
        with engine.begin() as db:
            assert db.execute(text('SELECT test.expire_stale_live_executions()')).scalar_one() == 0
            assert db.execute(text('SELECT test.expire_stale_live_executions()')).scalar_one() == 0
            assert db.execute(text('SELECT row_to_json(ri)::text FROM test.runtime_interval ri JOIN test.test_execution e ON e.id=ri.execution_id WHERE e.public_id=:id'), {'id': other['execution_id']}).scalar_one() == original
            evidence = db.execute(text('SELECT c.*, e.status, e.reported_active_seconds FROM integration.hmi_runtime_conflict c JOIN test.test_execution e ON e.id=c.execution_id WHERE e.public_id=:id'), {'id': broken['execution_id']}).mappings().one()
            assert evidence['status'] == 'RUNNING'
            assert float(evidence['reported_active_seconds']) == 120
            assert evidence['evidence']['constraint'] == 'runtime_interval_asset_id_tstzrange_excl'
            assert len(evidence['evidence']['conflicting_intervals']) == 1
            assert len(evidence['conflicting_interval_ids']) == 1
            assert db.execute(text('SELECT count(*) FROM test.runtime_interval ri JOIN test.test_execution e ON e.id=ri.execution_id WHERE e.public_id=:id'), {'id': broken['execution_id']}).scalar_one() == 0
        mine = find_station(owner, payload['installation_id'])
        assert len(mine['runtime_conflicts']) == 1
        with signed_in(users['second']) as unrelated:
            assert find_station(unrelated, payload['installation_id']) is None
        # The application role has read access only, and cannot invoke the trusted close helper.
        with engine.begin() as db:
            assert not db.execute(text("SELECT has_function_privilege('rp1_app','test.try_close_live_execution(bigint,text,numeric)','EXECUTE')")).scalar_one()
            assert not db.execute(text("SELECT has_table_privilege('rp1_app','integration.hmi_runtime_conflict','UPDATE')")).scalar_one()
            assert not db.execute(text("SELECT has_table_privilege('rp1_app','integration.hmi_runtime_conflict','DELETE')")).scalar_one()
        assert owner.delete(route).status_code == 200


def test_own_overlap_closure_is_visible_and_idempotent_without_losing_monitoring(environment):
    engine, users = environment
    with signed_in(users['admin']) as owner:
        route, payload, broken, _ = overlapping_pair(owner)
        for sequence in (2, 3):
            payload['sequence'] = sequence
            ack = send(owner, route, payload, state='completed', active_seconds=130)
            assert len(ack['runtime_conflicts']) == 1
            assert ack['executions'][0]['status'] == 'RUNNING'
            assert find_station(owner, payload['installation_id'])['online']
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM integration.hmi_runtime_conflict c JOIN test.test_execution e ON e.id=c.execution_id WHERE e.public_id=:id'), {'id': broken['execution_id']}).scalar_one() == 1
            assert db.execute(text("SELECT count(*) FROM audit.change_log a WHERE a.table_name='hmi_runtime_conflict' AND a.operation='INSERT' AND a.after_data->>'execution_id'=(SELECT id::text FROM test.test_execution WHERE public_id=:id)"), {'id': broken['execution_id']}).scalar_one() == 1
        assert owner.delete(route).status_code == 200
        with engine.begin() as db:
            db.execute(text('SELECT test.expire_stale_live_executions()'))
            assert db.execute(text('SELECT reported_active_seconds FROM test.test_execution WHERE public_id=:id'), {'id': broken['execution_id']}).scalar_one() == 130
            assert db.execute(text('SELECT count(*) FROM integration.hmi_runtime_conflict c JOIN test.test_execution e ON e.id=c.execution_id WHERE e.public_id=:id'), {'id': broken['execution_id']}).scalar_one() == 1


def test_conflicting_rollover_does_not_assign_old_identity_to_new_acquisition(environment):
    engine, users = environment
    with signed_in(users['admin']) as owner:
        route, payload, _, _ = overlapping_pair(owner)
        next_uuid = str(uuid4())
        payload['sequence'] = 2
        ack = send(owner, route, payload, execution_uuid=next_uuid, active_seconds=1)
        assert ack['executions'] == []
        assert len(ack['runtime_conflicts']) == 1
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=CAST(:id AS uuid)'), {'id': next_uuid}).scalar_one() == 0
        assert owner.delete(route).status_code == 200


def test_ownership_violation_still_rolls_back_and_other_workers_skip_locked_rows(environment):
    engine, users = environment
    with signed_in(users['admin']) as owner, signed_in(users['second']) as stranger:
        context = context_for(owner)
        payload = payload_for(context, seconds=1)
        route = '/api/v1/hmi/presence/' + str(uuid4())
        execution = send(owner, route, payload)['executions'][0]
        takeover = stranger.put('/api/v1/hmi/presence/' + str(uuid4()), json=payload)
        assert takeover.status_code == 403
        assert find_station(stranger, payload['installation_id']) is None
        with engine.begin() as locked:
            locked.execute(text("UPDATE integration.hmi_presence SET last_seen_at=clock_timestamp()-interval '46 seconds' WHERE connection_id=CAST(:id AS uuid)"), {'id': route.rsplit('/', 1)[-1]})
        with engine.begin() as locked:
            locked.execute(text('SELECT id FROM test.test_execution WHERE public_id=:id FOR UPDATE'), {'id': execution['execution_id']})
            with engine.begin() as worker:
                worker.execute(text("SET LOCAL statement_timeout='1s'"))
                worker.execute(text('SELECT test.expire_stale_live_executions()')).scalar_one()
        with engine.begin() as worker:
            assert worker.execute(text('SELECT test.expire_stale_live_executions()')).scalar_one() >= 1
        with engine.connect() as db:
            assert db.execute(text('SELECT status FROM test.test_execution WHERE public_id=:id'), {'id': execution['execution_id']}).scalar_one() == 'COMPLETED'
        assert owner.delete(route).status_code == 200


def test_concurrent_interval_commit_is_caught_without_rolling_back_presence(environment):
    from concurrent.futures import ThreadPoolExecutor
    import time

    engine, users = environment
    with signed_in(users['admin']) as owner:
        context = context_for(owner)
        first, rival = payload_for(context), payload_for(context, seconds=2)
        first_route, rival_route = ['/api/v1/hmi/presence/' + str(uuid4()) for _ in range(2)]
        broken = send(owner, first_route, first)['executions'][0]
        rival_execution = send(owner, rival_route, rival)['executions'][0]
        with engine.connect() as pending, ThreadPoolExecutor(max_workers=1) as pool:
            transaction = pending.begin()
            try:
                pending.execute(text("""
                    INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,
                        started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
                    SELECT campaign_id,asset_id,cycle_id,id,'NATIVE',normalized_started_at,
                        normalized_started_at+interval '2 seconds',2,'VALID','PARTIAL','qa-concurrent'
                    FROM test.test_execution WHERE public_id=:id
                """), {'id': rival_execution['execution_id']})
                first['sequence'] = 2
                result = pool.submit(send, owner, first_route, first, state='completed')
                # Wait for a real PostgreSQL exclusion-check lock wait, not an arbitrary sleep.
                deadline = time.monotonic() + 3
                observed_wait = False
                while time.monotonic() < deadline:
                    with engine.connect() as observer:
                        observed_wait = bool(observer.execute(text("SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE query LIKE 'SELECT test.sync_live_runs%' AND wait_event_type='Lock')")).scalar_one())
                    if observed_wait:
                        break
                    if result.done():
                        break
                    time.sleep(.01)
                assert observed_wait, 'Regression did not exercise the concurrent exclusion check'
                transaction.commit()
                ack = result.result(timeout=3)
                assert len(ack['runtime_conflicts']) == 1
                assert ack['executions'][0]['status'] == 'RUNNING'
            finally:
                if transaction.is_active:
                    transaction.rollback()
        assert find_station(owner, first['installation_id'])['online']
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM test.runtime_interval ri JOIN test.test_execution e ON e.id=ri.execution_id WHERE e.public_id=:id'), {'id': broken['execution_id']}).scalar_one() == 0
        assert owner.delete(first_route).status_code == 200
        assert owner.delete(rival_route).status_code == 200
