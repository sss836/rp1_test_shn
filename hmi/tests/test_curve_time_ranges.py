"""Synthetic saved files only: long ranges never touch controller/device I/O."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import threading
import pytest
from fastapi import HTTPException
from factory_hmi.core.curve_cache import CurveCache, LEVELS, KEEP
from factory_hmi.core.history_index import HistoryIndex
from factory_hmi.core.telemetry_hub import TelemetryHub
from factory_hmi.gateway.local_curves import curves
from factory_hmi.gateway.preview import PreviewSessions


KEY='left_elbow_joint/position_rad'
COLUMN='left_elbow_joint_pos_rad'


def window(t, *, width=1, mean=2., count=20, low=1., high=3., invalid=0):
    return dict(schema_version='window-stat-v1', start_s=t, end_s=t+width,
                metrics={COLUMN:dict(mean=mean,count=count,min=low,max=high,invalid=invalid)})


def write_windows(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w') as f:
        for row in rows:f.write(json.dumps(row)+'\n')


def ingest_all(cache, path, end):
    for _ in range(100):
        progress=cache.ingest(path,'run',end,True)
        if not progress['indexing']:return
    raise AssertionError('index did not complete within representative bounded fixture')


def active(tmp_path, rows, durable):
    records=tmp_path/'records';records.mkdir()
    output=records/'test.csv'
    write_windows(output.with_suffix('.segments')/'windows.jsonl',rows)
    output.with_suffix('.checkpoint.json').write_text(json.dumps({'last_time_s':durable}))
    manager=SimpleNamespace(history_index=HistoryIndex(tmp_path/'history.sqlite3'))
    service=SimpleNamespace(data_root=tmp_path,record_root=records,report_manager=manager,
        _record_identity={'execution_uuid':'run','test_id':'test'},snapshot=Mock(side_effect=AssertionError('device snapshot forbidden')))
    return service,output


def complete_query(service, **kw):
    for _ in range(100):
        result=curves(service,'run',KEY,'','', 'test',**kw)
        if not result.get('indexing'):return result
    raise AssertionError('query remained incomplete')


@pytest.mark.parametrize('span',[60,600,3600])
def test_active_recent_ranges_use_only_durable_saved_windows(tmp_path,span):
    # Two hours of real-shaped saved statistics; no controller is constructed.
    service,_=active(tmp_path,(window(n) for n in range(7200)),7200.)
    result=complete_query(service,window=str(span))
    assert result['start_s']==7200-span and result['end_s']==7200
    assert result['source']=='durable_recording_windows'
    assert len(result['series'][0]['points'])<=600
    assert any(value==pytest.approx(2.) for _,value in result['series'][0]['points'] if value is not None)
    service.snapshot.assert_not_called()


def test_all_and_custom_beyond_one_day_are_not_truncated_to_sixty_seconds(tmp_path):
    service,_=active(tmp_path,(window(n*3600,width=3600) for n in range(72)),72*3600.)
    result=complete_query(service,window='all')
    assert result['start_s']==0 and result['end_s']==72*3600
    assert result['series'][0]['points'][-1][0]>2*86400*1000
    custom=curves(service,'run',KEY,'3600',str(2*86400),'custom')
    assert custom['start_s']==3600 and custom['end_s']==2*86400
    assert len(custom['series'][0]['points'])<=600


def test_index_progress_is_explicit_and_memory_and_disk_structure_are_bounded(tmp_path):
    path=tmp_path/'windows.jsonl';write_windows(path,(window(n,high=9999 if n==1 else 3.) for n in range(3000)))
    cache=CurveCache(tmp_path/'cache.sqlite3')
    try:
        progress=cache.ingest(path,'run',3000,True,byte_budget=512)
        assert progress['indexing'] and progress['indexed_bytes']<progress['source_bytes']
        ingest_all(cache,path,3000)
        assert cache.db.execute('SELECT count(*) FROM bins').fetchone()[0]<=KEEP*len(LEVELS)
        assert cache.db.execute('PRAGMA cache_size').fetchone()[0]==-2048
        assert cache.db.execute('PRAGMA max_page_count').fetchone()[0]==32768
        overview=cache.query({KEY:COLUMN},{KEY:'rad'},0,3000)['series'][0]
        assert any(p[2]==9999 for p in overview['extrema'])
        before=cache.db.execute('SELECT sum(length(payload)) FROM bins').fetchone()[0]
        ingest_all(cache,path,3000)
        assert cache.db.execute('SELECT sum(length(payload)) FROM bins').fetchone()[0]==before
    finally:cache.close()


def test_unsynced_tail_is_not_shown_and_later_checkpoint_resumes_without_duplicates(tmp_path):
    service,output=active(tmp_path,(window(n) for n in range(4)),2.5)
    first=complete_query(service,window='all')
    assert first['saved_until_s']==2.
    output.with_suffix('.checkpoint.json').write_text(json.dumps({'last_time_s':4.}))
    after=complete_query(service,window='all')
    assert after['saved_until_s']==4.
    assert max(v for _,v in after['series'][0]['points'] if v is not None)==pytest.approx(2.)


def test_weighted_mean_extrema_and_invalid_bins_are_preserved(tmp_path):
    path=tmp_path/'windows.jsonl'
    write_windows(path,[window(0,mean=1,count=1,low=-7,high=99),window(1,mean=3,count=3,high=1234),window(2,invalid=1)])
    cache=CurveCache(tmp_path/'cache.sqlite3')
    try:
        ingest_all(cache,path,3)
        series=cache.query({KEY:COLUMN},{KEY:'rad'},0,3)['series'][0]
        assert [p[1] for p in series['points']]==[1.,3.,None]
        assert series['extrema'][0][1]==-7 and series['extrema'][1][2]==1234
        assert series['extrema'][2][1:]==[None,None]
        merged=json.loads(cache.db.execute('SELECT payload FROM bins WHERE level=10 AND bucket=0').fetchone()[0])['metrics'][COLUMN]
        assert merged[0]/merged[1]==pytest.approx((1*1+3*3+2*20)/(1+3+20))
    finally:cache.close()


def test_run_change_replaces_only_disposable_cache_and_rejects_invalid_ranges(tmp_path):
    service,output=active(tmp_path,[window(0,mean=8)],1.)
    complete_query(service,window='all')
    original=output.with_suffix('.segments').joinpath('windows.jsonl').read_bytes()
    for start,end,span in [('2','1',''),('nan','3',''),('0','1','-1'),('0','1e100','')]:
        with pytest.raises(HTTPException) as exc:curves(service,'run',KEY,start,end,'test',span)
        assert exc.value.status_code==422
    with pytest.raises(HTTPException):curves(service,'other',KEY,'0','1','test')
    assert output.with_suffix('.segments').joinpath('windows.jsonl').read_bytes()==original


def test_preview_activity_renews_only_readonly_session_and_not_changed_run():
    cache=TelemetryHub();cache.begin('run')
    rt=SimpleNamespace(controller=SimpleNamespace(telemetry_hub=cache))
    hub=SimpleNamespace(_lock=threading.Lock(),_sessions={'owner':rt})
    sessions=PreviewSessions(hub)
    with patch('factory_hmi.gateway.preview.time.monotonic',return_value=0):
        token=sessions.exchange(sessions.issue('owner','run'))
    with patch('factory_hmi.gateway.preview.time.monotonic',return_value=3500):
        assert sessions.runtime(token) is rt
    with patch('factory_hmi.gateway.preview.time.monotonic',return_value=3700):
        assert sessions.runtime(token) is rt
        expiry=sessions.sessions[token][1]
        cache.begin('another')
        with pytest.raises(HTTPException) as exc:sessions.runtime(token)
        assert exc.value.status_code==410 and sessions.sessions[token][1]==expiry
