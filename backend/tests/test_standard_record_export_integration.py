"""Real session/RLS and SQL projection, only in the disposable PostgreSQL harness."""
import csv
import io
import os
from uuid import uuid4
import pytest
from app.test_record_csv import FIELDS
from test_archives import archive_payload
from test_archives_integration import RAW,create,upload,verify
from test_hmi_presence_integration import environment,signed_in
from test_factory_partition_integration import ctx_with_code

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('HMI_TEST_ADMIN_URL'),reason='disposable PostgreSQL required')]


def test_standard_csv_route_retains_real_auth_filters_and_archive_evidence(environment):
    engine,users=environment
    sn='000AbC-'+uuid4().hex
    with signed_in(users['admin']) as admin,signed_in(users['first']) as author,signed_in(users['viewer']) as viewer:
        ctx=ctx_with_code(admin,engine)
        payload={**archive_payload(raw=RAW),'data_domain':'FACTORY','serial_number':sn,
                 'bench_id':ctx['station_code'],'test_name':'REL-ARM-002'}
        original=upload(author,create(author,payload))
        verified=verify(author,original,ctx['station_id'])
        assert verified.status_code==200,verified.text
        original=author.get('/api/v1/archives/'+original['id']).json()['data']
        response=author.get('/api/v1/archive-ledger.csv',params={'data_domain':'FACTORY','sn':sn})
        assert response.status_code==200,response.text
        assert response.headers['x-rp1-csv-schema']=='rp1-test-record-v2'
        assert response.headers['cache-control']=='no-store'
        reader=csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')))
        assert reader.fieldnames==list(FIELDS)
        rows=list(reader)
        assert len(rows)==1 and rows[0]['样品编号']==sn
        assert rows[0]['记录编号']==original['execution_uuid']
        assert rows[0]['测试项目编号']=='REL-ARM-002'
        assert rows[0]['是否通过']=='通过' and '审批状态=未送审' in rows[0]['备注']
        assert rows[0]['测试人员'] and rows[0]['结束时间']
        assert author.get('/api/v1/archives/'+original['id']).json()['data']==original
        research=author.get('/api/v1/archive-ledger.csv',params={'data_domain':'RESEARCH','sn':sn})
        assert research.status_code==200 and sn.encode() not in research.content
        limited=viewer.get('/api/v1/archive-ledger.csv',params={'data_domain':'FACTORY','sn':sn})
        assert limited.status_code in (200,403) and sn.encode() not in limited.content
