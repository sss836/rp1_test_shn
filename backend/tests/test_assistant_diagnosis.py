import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.services import assistant_attachments as storage
from app.services import assistant_diagnosis as diagnosis
from app.services.assistant_attachment_queries import _csv
from app.schemas.assistant_chat import DiagnosticAttachmentArguments
from app.services.assistant_time_evidence import TimeEvidence


def test_uploaded_csv_tail_is_actually_scanned_and_original_is_unchanged(tmp_path,monkeypatch):
    monkeypatch.setattr(storage,'get_settings',lambda:SimpleNamespace(assistant_attachment_root=str(tmp_path)))
    actor=SimpleNamespace(public_id=uuid4())
    raw=('time_s,value\n'+''.join(f'{i},1\n' for i in range(15000))+'15002,9876\n').encode()
    meta=storage.upload(actor,raw,'measurements.csv','text/csv')
    item={'id':str(uuid4()),'actor':actor,'context':{},'attachment_metadata':[meta],'diagnostic_question':'检查CSV缺失和连接超时'}
    first=diagnosis.prepare(item,{})['data']
    assert first['continuation']
    evidence=first['evidence'][0]
    assert evidence['source_sha256']==hashlib.sha256(raw).hexdigest()
    data=evidence['data'];assert data['full_file_statistics'] and data['range']['rows_analyzed']==15001
    assert data['columns'][1]['maximum']==9876
    assert data['time_analysis']['gap_count']==1
    assert data['time_analysis']['gap_examples'][0]['data_row']==15000
    assert storage.read(actor,meta['id'])[1]==raw
    final=diagnosis.prepare(item,{})['data']
    assert final['status']=='partial' and not final['root_cause_confirmed']
    assert '未关联' in final['issues'][0]['reason']


def test_numeric_unknown_clock_duplicate_and_backwards_are_observations():
    evidence=TimeEvidence(['time','value'])
    for i,value in enumerate(['0','1','1','0','4']):evidence.add(i,[value,'0'])
    result=evidence.result(True)
    assert result['time_basis']=='numeric_unit_unknown' and result['output_unit'] is None
    assert result['duplicate_times']==1 and result['backward_times']==1
    assert '不能直接' in result['causality']


def test_timezone_absence_and_mixed_basis_cannot_be_aligned():
    evidence=TimeEvidence(['timestamp'])
    evidence.add(0,['2026-11-01T01:00:00']);evidence.add(1,['2026-11-01T01:00:01+08:00'])
    assert evidence.result(True)['time_basis']=='mixed_time_bases'


def test_default_readable_logs_are_collected_instead_of_requesting_upload(monkeypatch):
    calls=[]
    def local(_item,args):
        calls.append(args)
        data=({'status':'read','roots':['$STATE']} if args['operation']=='capabilities' else
              {'status':'read','complete':True,'roots':['$STATE'],'candidates':[
                  {'path':'$STATE/current.csv','kind':'csv'},{'path':'$STATE/communication.log','kind':'log'}]} if args['operation']=='discover' else
              {'status':'read','complete':True,'operation':args['operation'],'requested_path':args['path'],
               'data':{'rows_analyzed':15001,'gap_count':1} if args['operation']=='csv_diagnosis' else
                      {'lines_analyzed':6000,'matches':[{'text':'ConnectionRefusedError: connection refused'}]}})
        return {'type':'local_diagnostic','data':data}
    monkeypatch.setattr(diagnosis.local,'evidence',local)
    item={'id':'synthetic','context':{'hmi':{'window_id':'synthetic'}},'diagnostic_question':'CSV缺失和连接超时','reads':1}
    for _ in range(5):
        result=diagnosis.prepare(item,{})['data']
        if not result['continuation']:break
    assert result['status']=='ready' and result['evidence_complete']
    assert {'csv_diagnosis','log_diagnosis'}<={c['operation'] for c in calls}
    assert not result['issues'];assert not result['root_cause_confirmed']


def test_pending_permission_does_not_advance_scan_or_invent_evidence(monkeypatch):
    pending={'type':'local_diagnostic','data':{'phase':'awaiting_permission','status':'permission_required','permission_request':{'id':'request'}}}
    monkeypatch.setattr(diagnosis.local,'evidence',lambda *_:pending)
    item={'context':{'hmi':{'window_id':'synthetic'}}}
    result=diagnosis.prepare(item,{})['data']
    assert result['phase']=='awaiting_permission';assert result['pending_permissions']==[{'id':'request'}]
    assert not result['evidence'];assert not item['diagnosis_state'].get('capabilities_done')


def test_already_authorized_read_is_waiting_for_data_without_asking_user_again(monkeypatch):
    pending={'type':'local_diagnostic','data':{'phase':'awaiting_permission','status':'permission_required','permission_request':{'id':'request','state':'approved'}}}
    monkeypatch.setattr(diagnosis.local,'evidence',lambda *_:pending)
    result=diagnosis.prepare({'context':{'hmi':{'window_id':'synthetic'}}},{})['data']
    assert result['status']=='waiting_for_data' and result['phase']=='awaiting_permission'
    assert not result['evidence_complete']


def test_os_denial_or_no_logs_is_explicit_and_not_a_new_admin_prompt(monkeypatch):
    def local(_item,args):
        return {'type':'local_diagnostic','data':{'status':'read','complete':True,'roots':['$STATE']} if args['operation']=='capabilities' else
            {'status':'os_permission_denied','candidates':[],'complete':False,'roots':['$STATE'],'errors':[{'reason':'OS拒绝'}]}}
    monkeypatch.setattr(diagnosis.local,'evidence',local)
    item={'context':{'hmi':{'window_id':'synthetic'}}}
    result=diagnosis.prepare(item,{})['data']
    assert result['status']=='partial' and not result['pending_permissions']
    assert any('未找到' in issue['reason'] for issue in result['issues'])


def test_empty_csv_is_not_complete_measurement_evidence():
    data=_csv(b'time_s,value\n',DiagnosticAttachmentArguments(attachment_id=uuid4(),operation='csv_statistics'))
    assert not data['has_measurements'];assert data['time_analysis']['start'] is None


def test_lost_native_cursor_issues_a_new_bound_attempt_instead_of_reusing_old_prefix(monkeypatch):
    calls=[]
    def local(_item,args):
        calls.append(args)
        data=({'status':'analysis_restart_required','next_offset':0,'complete':False} if args['offset']==100 else
              {'status':'read','complete':True,'data':{'rows_analyzed':15001},'source_sha256':'new-version'} if args.get('query') else
              {'status':'partial','next_offset':100,'complete':False,'data':{'rows_analyzed':1}})
        return {'type':'local_diagnostic','data':{**data,'operation':args['operation'],'requested_path':args['path']}}
    monkeypatch.setattr(diagnosis.local,'evidence',local)
    item={'context':{'hmi':{'window_id':'synthetic'}},'diagnosis_state':{'capabilities_done':True,'discovery_done':True,
        'selected_files':[{'path':'$STATE/x.csv','kind':'csv'}],'local_index':0,'local_offset':100}}
    result=diagnosis.prepare(item,{})['data']
    assert len(calls)==2 and calls[1]['offset']==0 and calls[1]['query']=='{"analysis_attempt":1}'
    assert result['evidence_complete'] and result['evidence'][0]['data']['source_sha256']=='new-version'
