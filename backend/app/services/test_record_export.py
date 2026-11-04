"""Standard record projection over authorized archive pages; no evidence writes."""
from sqlalchemy import text as sql
from app.test_record_csv import catalog, chunks, criteria, row, text


def page_rows(session, items):
    definitions = catalog()
    versions = [str((item.get('classification') or {}).get('context', {}).get('test_case_version_id') or
                    (item.get('classification') or {}).get('test_case_version_id')) for item in items]
    versions = [value for value in versions if value != 'None']
    codes = [item['test_name'] for item in items]
    published = session.execute(sql('''SELECT tc.case_code,tc.name,tc.asset_kind,tc.target_part_code,tc.domain,
        v.public_id AS version_id,v.version,v.decision_rules
        FROM catalog.test_case tc JOIN catalog.test_case_version v ON v.test_case_id=tc.id
        WHERE (v.public_id=ANY(CAST(:versions AS uuid[])) OR tc.case_code=ANY(CAST(:codes AS text[])))
        AND v.status='PUBLISHED' ORDER BY v.published_at DESC NULLS LAST,v.id DESC'''),
        {'versions': versions, 'codes': codes}).mappings()
    by_version, by_code = {}, {}
    for entry in published:
        entry = dict(entry)
        entry['test_system'] = 'MOD' if entry['asset_kind'] == 'MODULE' else 'SYS'
        entry['test_category'] = entry['target_part_code'] + ' / ' + entry['domain']
        by_version[str(entry['version_id'])] = entry
        by_code.setdefault(entry['case_code'], entry)
    asset_ids = []
    for item in items:
        context = (item.get('classification') or {}).get('context') or item.get('classification') or {}
        if context.get('asset_id'):
            asset_ids.append(str(context['asset_id']))
    assets = {str(asset['id']): dict(asset) for asset in session.execute(sql('''
        SELECT a.public_id AS id,a.asset_code,wp.model
        FROM test.asset a LEFT JOIN test.whole_machine_profile wp ON wp.asset_id=a.id
        WHERE a.public_id=ANY(CAST(:ids AS uuid[]))'''), {'ids': asset_ids}).mappings()}
    for item in items:
        context = (item.get('classification') or {}).get('context') or item.get('classification') or {}
        definition = by_version.get(str(context.get('test_case_version_id'))) or by_code.get(item['test_name']) or {}
        code = definition.get('case_code', '')
        if code:
            definitions[code] = definition
        asset = assets.get(str(context.get('asset_id')), {})
        yield archive_row({**item, 'registered_sample': asset.get('asset_code'), 'registered_model': asset.get('model')}, definition, definitions)


def archive_row(item, definition=None, definitions=None):
    definition = definition or {}
    facts = item.get('facts') or {}
    verification = item.get('current_verification') or {}
    acceptance = item.get('acceptance') or {}
    manifest = item.get('manifest') or {}
    analysis = manifest.get('analysis') or {}
    actual = [label + '=' + text(facts[key]) + unit for key, label, unit in (
        ('active_seconds', '采集有效运行', '秒'), ('completed_cycles', '采集完成循环', '次'),
        ('exception_count', '采集故障计数', ''), ('machine_status', '运行状态', ''),
        ('automatic_verdict', '采集自动判定', ''), ('data_quality', '采集质量', '')) if facts.get(key) is not None and not (item.get('origin') == 'HISTORY' and facts.get('data_quality') == 'MISSING' and key in {'active_seconds','completed_cycles','exception_count'})]
    if item.get('origin') == 'HISTORY' and facts.get('data_quality') == 'MISSING':
        actual.append('历史原始运行时长、循环和故障计数未提供；平台占位零值不作为实测值')
    if facts.get('summary'):
        actual.append(text(facts['summary']))
    if analysis.get('verdict'):
        actual.append('平台解析判定=' + text(analysis['verdict']))
    if verification.get('verdict'):
        actual.append('人员核实=' + text(verification['verdict']))
    for key, label, unit in [('declared_active_seconds', '人工申报有效时长', '秒'),
                              ('declared_completed_cycles', '人工申报循环', '次'),
                              ('declared_exception_count', '人工申报故障计数', '')]:
        if verification.get(key) is not None:
            actual.append(label + '=' + text(verification[key]) + unit)
    notes = ['归档编号=' + text(item.get('archive_code')), '平台执行编号=' + text(item.get('execution_code')),
             '数据分区=' + text(item.get('data_domain')), '上传状态=' + text(item.get('upload_state')),
             '解析状态=' + text(item.get('processing_state')), '审批状态=' + text(item.get('submission_status') or '未送审'),
             '原始文件SHA256=' + text(item.get('sha256'))]
    if verification.get('notes'):
        notes.append('核实说明=' + text(verification['notes']))
    if verification.get('discrepancy_reason'):
        notes.append('差异原因=' + text(verification['discrepancy_reason']))
    if item.get('processing_error'):
        notes.append('解析错误=' + text(item['processing_error']))
    if not definition:
        notes.append('原归档未保留项目编号，未按样品或文件名推测')
    verdict = verification.get('verdict')
    if not verdict:
        automatic = facts.get('automatic_verdict')
        verdict = '待复核（自动判定=' + text(automatic) + '）' if automatic else '待复核'
    return row(dict(record_id=item.get('execution_uuid') or item.get('archive_code'),
        hmi_id=item.get('bench_id') or item.get('station_label'), sample_id=item.get('serial_number') or item.get('registered_sample'),
        model=item.get('registered_model'), data_source={'HMI': '上位机', 'HISTORY': '历史导入', 'OTHER': '其他'}.get(item.get('origin'), item.get('origin')),
        test_system=definition.get('test_system', ''), test_category=definition.get('test_category', ''),
        record_level='项目', case_code=definition.get('case_code', ''), original_check_id='',
        test_content=definition.get('name') or item.get('test_name'),
        started_at=facts.get('started_at') or verification.get('declared_started_at'),
        ended_at=facts.get('ended_at') or verification.get('declared_ended_at'),
        tester=verification.get('tester'), actual_result='；'.join(actual),
        criteria_text=criteria(acceptance), criteria_version=acceptance.get('criteria_version', 'UNCONFIGURED'),
        verdict=verdict, evidence='/api/v1/archives/' + text(item.get('id')),
        notes='；'.join(notes)), definitions)


def archive_csv(session, filters):
    from app.repositories.archives import ArchiveRepository
    def records():
        before = None
        while True:
            page = ArchiveRepository(session).list(**{('query' if key == 'q' else key): value
                for key, value in filters.model_dump(exclude_none=True).items()}, before=before, limit=100)
            yield from page_rows(session, page['items'])
            before = page['next_cursor']
            if before is None:
                break
    return chunks(records())
