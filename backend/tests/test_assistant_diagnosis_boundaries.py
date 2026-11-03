"""Read-only reviewer probes using synthetic CSV bytes and temp files."""
import csv
import io
import json
import time
from uuid import uuid4

import pytest
from app.schemas.assistant_chat import DiagnosticAttachmentArguments
from app.services.assistant_attachment_queries import _csv
from app.services import assistant_diagnosis as diagnosis
from factory_hmi.core.diagnostic_files import DiagnosticFileReader
from factory_hmi.core.diagnostic_analysis import scan_csv,csv_result


def test_extreme_finite_time_baseline_stays_finite_uploaded_csv():
    raw=b'time_s,value\n-1e308,1\n0,2\n1e308,3\n'
    result=_csv(raw,DiagnosticAttachmentArguments(attachment_id=uuid4(),operation='csv_statistics'))
    json.dumps(result,allow_nan=False)


def test_extreme_finite_time_baseline_stays_finite_native_csv():
    state={}
    assert scan_csv(io.BytesIO(b'time_s,value\n-1e308,1\n0,2\n1e308,3\n'),state,time.monotonic()+5,lambda s:s)
    json.dumps(csv_result(state),allow_nan=False)


def test_native_csv_parser_limit_returns_failure_evidence_not_raised_exception(tmp_path):
    root=tmp_path/'state';root.mkdir()
    path=root/'malformed.csv'
    # Multiline fields stay below the physical-line cap but exceed csv's own
    # field-size cap. A durable task must receive a failure receipt, not retry
    # an exception forever without charging/settling its actual scan time.
    path.write_text('time_s,value\n0,"'+('long logical field\n'*20)+'"\n')
    previous=csv.field_size_limit(128)
    try:
        reader=DiagnosticFileReader(root,roots=[root])
        result=reader.run(('review','owner','job'),{'operation':'csv_diagnosis','path':'$STATE/malformed.csv'})
        assert result['status'] in {'error','partial','invalid_csv'} and result.get('eof') is False
        json.dumps(result,allow_nan=False)
    finally:csv.field_size_limit(previous)


def test_lost_native_aggregate_uses_new_bound_attempt_instead_of_old_completed_prefix(monkeypatch):
    target='$STATE/measurements.csv'
    def card(status,**data):
        return {'type':'local_diagnostic','data':{'status':status,'operation':'csv_diagnosis',
            'requested_path':target,**data}}
    ledger={
        (0,None):card('partial',complete=False,next_offset=100),
        (100,None):card('analysis_restart_required',complete=False,next_offset=0),
    }
    fresh=[]
    def local(_item,args):
        key=(args.get('offset',0),args.get('query'))
        if key in ledger:return ledger[key]
        assert key[0]==0 and json.loads(key[1])['analysis_attempt']==1
        fresh.append(dict(args))
        ledger[key]=card('read',complete=True,data={'rows_analyzed':15001,'gap_count':1})
        return ledger[key]
    monkeypatch.setattr(diagnosis.local,'evidence',local)
    item={'id':'synthetic','context':{'hmi':{'window_id':'window'}},'diagnostic_question':'检查CSV',
          'reads':5,'attachment_metadata':[],'diagnosis_state':{'capabilities_done':True,'discovery_done':True,
          'selected_files':[{'path':target,'kind':'csv'}],'local_index':0,'local_offset':100}}
    final=diagnosis.prepare(item,{})['data']
    assert fresh and final['evidence_complete']
    assert final['evidence'][-1]['data']['data']['rows_analyzed']==15001
