import csv
import io
from uuid import uuid4

from app.schemas.archives import ArchiveLedgerFilters
from app.services.test_record_export import archive_csv, archive_row, page_rows
from app.test_record_csv import FIELDS


def record(**changes):
    value=dict(id=str(uuid4()),execution_uuid=str(uuid4()),archive_code='ARC-1',test_name='REL-ARM-002',
        bench_id='A8',serial_number='000AbC',origin='HMI',data_domain='FACTORY',
        facts=dict(active_seconds=0,completed_cycles=0,automatic_verdict='passed',data_quality='PARTIAL'),
        acceptance=dict(criteria_version='UNCONFIGURED'),manifest={},submission_status='PENDING')
    return {**value,**changes}


def test_platform_export_preserves_missing_values_zeroes_and_does_not_treat_approval_as_pass():
    result=archive_row(record(submission_status='APPROVED'))
    assert tuple(result)==FIELDS
    assert result['样品编号']=='000AbC' and result['机型']==result['开始时间']==result['测试人员']==''
    assert result['是否通过'].startswith('待复核')
    assert '有效运行=0秒' in result['实际结果'] and '审批状态=APPROVED' in result['备注']
    assert '原归档未保留项目编号' in result['备注']


def test_human_verdict_and_declared_times_remain_distinct_from_collected_facts():
    result=archive_row(record(current_verification=dict(tester='核实员',verdict='failed',
        declared_started_at='2026-11-04T10:00:00+08:00',declared_ended_at='2026-11-04T10:01:00+08:00',
        declared_active_seconds=10,notes='存在差异',discrepancy_reason='人工复核')))
    assert result['是否通过']=='不通过' and result['测试人员']=='核实员'
    assert '采集自动判定=passed' in result['实际结果'] and '人员核实=failed' in result['实际结果']
    assert '人工申报有效时长=10秒' in result['实际结果']
    assert result['开始时间'].endswith('+08:00') and '人工复核' in result['备注']


class Result:
    def __init__(self, rows):self.rows=rows
    def mappings(self):return self.rows


class Session:
    def __init__(self, version,asset):self.version=version;self.asset=asset;self.calls=[]
    def execute(self, sql, values):
        self.calls.append(values)
        return Result([self.version] if 'versions' in values else [self.asset])


def test_explicit_catalog_version_and_sample_link_are_used_without_sn_guessing():
    version=str(uuid4());asset=str(uuid4())
    session=Session(dict(case_code='REL-ARM-002',name='带载老化',asset_kind='MODULE',target_part_code='ARM',
        domain='RELIABILITY',version_id=version,version='bound-v1',decision_rules={}),
        dict(id=asset,asset_code='RP1.3-ARM-003',model=None))
    item=record(serial_number=None,classification=dict(test_case_version_id=version,asset_id=asset))
    result=list(page_rows(session,[item]))[0]
    assert result['测试项目编号']=='REL-ARM-002' and result['测试体系']=='MOD'
    assert result['样品编号']=='RP1.3-ARM-003'
    assert result['通过标准'].startswith('标准待补充') # do not substitute current catalog rules
    assert len(session.calls)==2


def test_all_pages_keep_filters_and_stream_full_records(monkeypatch):
    from app.repositories.archives import ArchiveRepository
    import app.services.test_record_export as module
    seen=[]
    def page(self,**filters):
        seen.append(filters)
        start=filters['before'] or 0
        end=min(start+100,1007)
        return dict(items=[record(execution_uuid=f'run-{i}') for i in range(start,end)],next_cursor=end if end<1007 else None)
    monkeypatch.setattr(ArchiveRepository,'list',page)
    monkeypatch.setattr(module,'page_rows',lambda session,items:(archive_row(i) for i in items))
    content=b''.join(archive_csv(None,ArchiveLedgerFilters(data_domain='FACTORY',sn='000AbC',submission_status='PENDING')))
    rows=list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    assert len(rows)==1007 and rows[-1]['记录编号']=='run-1006'
    assert all(x['data_domain']=='FACTORY' and x['sn']=='000AbC' and x['submission_status']=='PENDING' and x['limit']==100 for x in seen)
    assert len(seen)==11


def test_history_unknown_placeholder_zero_is_not_exported_as_measurement():
    result=archive_row(record(origin='HISTORY',facts=dict(active_seconds=0,completed_cycles=0,
        exception_count=0,automatic_verdict='unconfigured',data_quality='MISSING',clock_quality='MISSING')))
    assert '有效运行=0' not in result['实际结果'] and '完成循环=0' not in result['实际结果']
    assert '未提供' in result['实际结果'] and result['开始时间']==''
