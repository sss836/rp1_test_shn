"""Private companion to test-robot-factory-bridge.py; only disposable storage."""
from contextlib import closing
import csv
from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import time
from uuid import uuid4

from argon2 import PasswordHasher
import httpx
from sqlalchemy import create_engine, text


def main():
    admin_url = os.environ['HMI_TEST_ADMIN_URL']
    if not admin_url.rsplit('/', 1)[-1].startswith('rp1_presence_robot_bridge'):
        raise RuntimeError('Only the disposable bridge database is allowed')
    from rp1_monitor.factory_bridge import Outbox, PlatformClient, result_upload
    from rp1_monitor.factory_store import FactoryStore

    engine = create_engine(admin_url)
    password = os.environ['QA_BRIDGE_PASSWORD']
    username = 'robot-bridge-qa-' + uuid4().hex[:12]
    with engine.begin() as db:
        uid = db.execute(text("INSERT INTO iam.app_user(username,display_name,role,principal_kind,must_change_password) VALUES(:name,'隔离桥接验收','TEST_EXECUTOR','HUMAN',false) RETURNING id"), {'name': username}).scalar_one()
        db.execute(text('INSERT INTO iam.user_credential(user_id,password_hash) VALUES(:id,:hash)'),
                   {'id': uid, 'hash': PasswordHasher().hash(password)})
        before = db.execute(text('SELECT count(*) FROM test.test_execution')).scalar_one()

    server = subprocess.Popen(['python', '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
                               '--port', '8000', '--log-level', 'warning', '--no-access-log'])
    base = 'http://127.0.0.1:8000'
    try:
        for _ in range(100):
            try:
                if httpx.get(base + '/healthz').status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if server.poll() is not None:
                raise RuntimeError('Isolated API did not start')
            time.sleep(.1)
        else:
            raise RuntimeError('Isolated API startup timed out')

        client = PlatformClient(base, allow_http=True)
        client.login(username, password)
        source, robot, sn = 'QA-PC-01', 'qa-robot', '000QaSyntheticSn'
        snapshot = {'protocol': 'reliability_v1', 'source_id': source, 'source_kind': 'real', 'robots': [{
            'robot_id': robot, 'serial_number': sn, 'model': '24轴',
            'station_name': '隔离合成工位', 'online': True,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'data_fresh': True}]}
        client.publish_presence(snapshot)
        with httpx.Client(base_url=base) as human, tempfile.TemporaryDirectory(prefix='robot-bridge-source-') as tmp:
            login = human.post('/api/v1/auth/login', json={'username': username, 'password': password})
            assert login.status_code == 200, login.text
            human.headers['X-CSRF-Token'] = human.cookies['rp1_csrf']

            def get(path):
                response = human.get(path)
                assert response.status_code == 200, response.text
                return response.json()['data']

            assert any(item['source_id'] == source and item['robot_id'] == robot and item['online'] for item in get('/api/v1/factory/robots')['items'])
            with closing(FactoryStore(Path(tmp) / 'factory')) as store:
                store.save_board_snapshot(sn, robot, 'ros2', {})
                store.set_model_dof(sn, 24)
                store.save_estop_pairing(sn, 'pass')
                row = next(item for item in store.report_rows(sn) if item['row_key'] == 'SYS-012')
                store.save_report_edits(sn, [{'row_key': row['row_key'], 'source_token': row['source_token'],
                                             'fields': {'actual_result': '隔离测试合成结果一', 'operator': '隔离验收员',
                                                        'verdict': '通过', 'notes': '仅用于独立软件验收'}}], station_id=source)
                path = Path(store.export_result_summary(sn, Path(tmp) / 'exports'))
                raw = path.read_bytes()
                parsed = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
                original_ids = [entry['记录编号'] for entry in parsed]
                metadata, frozen = result_upload(path, source=source, robot_id=robot, kind='real',
                                                verified=True, completed=True, submit=True,
                                                reason='隔离原生导出到平台的真实HTTP验收')
                assert frozen == raw
                key = uuid4().hex
                item = client.upload_result(metadata, raw, key)
                assert item['status'] == 'PENDING' and item['serial_number'] == sn, item
                assert client.upload_result(metadata, raw, key)['id'] == item['id']
                assert human.get(f"/api/v1/factory/robot-result-archives/{item['id']}/file").content == raw
                robots = get('/api/v1/factory/robots')['items']
                current = next(entry for entry in robots if entry['source_id'] == source and entry['robot_id'] == robot)
                assert not current['cases'], 'Pending summaries must not enter the approved case projection'

                response = human.post(f"/api/v1/factory/robot-result-archives/{item['id']}/review",
                                      json={'decision': 'APPROVE', 'expected_revision': item['revision'], 'reason': '隔离汇总审核'},
                                      headers={'Idempotency-Key': uuid4().hex})
                assert response.status_code == 200, response.text
                current = next(entry for entry in get('/api/v1/factory/robots')['items'] if entry['source_id'] == source and entry['robot_id'] == robot)
                codes = {entry['case_code'] for entry in current['cases']}
                assert 'SYS-012' in codes and 'SYS-009' not in codes, current

                store.save_report_edits(sn, [{'row_key': row['row_key'], 'source_token': row['source_token'],
                                             'fields': {'actual_result': '隔离测试合成结果二'}}], station_id=source)
                changed = Path(store.export_result_summary(sn, Path(tmp) / 'exports'))
                changed_raw = changed.read_bytes()
                assert changed_raw != raw
                assert [entry['记录编号'] for entry in csv.DictReader(io.StringIO(changed_raw.decode('utf-8-sig')))] == original_ids
                update = dict(metadata, filename=changed.name, size_bytes=len(changed_raw),
                              sha256=hashlib.sha256(changed_raw).hexdigest(), reason='隔离同源同记录更正追加版本')
                revised = client.upload_result(update, changed_raw, uuid4().hex)
                assert revised['id'] != item['id'] and revised['status'] == 'PENDING', revised
                revised_detail = get(f"/api/v1/factory/robot-result-archives/{revised['id']}")
                assert any(entry['version'] > 1 for entry in revised_detail['rows']), revised_detail
                # A new explicit submission after rejection must make a new
                # request, while transport retries retain the frozen task key.
                with closing(Outbox(Path(tmp) / 'bridge' / 'outbox.sqlite')) as box:
                    first_task = box.enqueue(base, str(client.user['id']), update, changed_raw)
                    receipt = box.drain(client)[0]['receipt']
                    assert receipt['id'] == revised['id']
                    rejected = human.post(f"/api/v1/factory/robot-result-archives/{revised['id']}/review",
                                          json={'decision': 'REJECT', 'expected_revision': revised['revision'], 'reason': '隔离验证同原件驳回重提'},
                                          headers={'Idempotency-Key': uuid4().hex})
                    assert rejected.status_code == 200, rejected.text
                    second_task = box.enqueue(base, str(client.user['id']), update, changed_raw)
                    assert second_task != first_task
                    resubmitted = box.drain(client)[0]['receipt']
                    assert resubmitted['id'] != revised['id'] and resubmitted['status'] == 'PENDING', resubmitted
                assert human.get(f"/api/v1/factory/robot-result-archives/{item['id']}/file").content == raw
                assert path.read_bytes() == raw, 'The bridge must retain the original local export'

            snapshot['robots'][0]['online'] = False
            client.publish_presence(snapshot)
            current = next(entry for entry in get('/api/v1/factory/robots')['items'] if entry['source_id'] == source and entry['robot_id'] == robot)
            assert current['online'] is False
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM test.test_execution')).scalar_one() == before
        print('PASS: native 17-column export → real cookie/CSRF HTTP bridge → immutable FACTORY archive → independent review → approved case card; retry, appended corrections, same-file rejected resubmission; no research execution writes', flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        engine.dispose()


if __name__ == '__main__':
    main()
