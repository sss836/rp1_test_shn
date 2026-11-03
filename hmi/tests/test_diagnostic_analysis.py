"""Synthetic readonly files, including decisive evidence beyond old 64KiB preview."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from factory_hmi.core.diagnostic_files import DiagnosticFileReader
from factory_hmi.core import diagnostic_analysis as analysis


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory();self.root=Path(self.temp.name)
        self.state=self.root/'state';self.project=self.root/'project'
        self.state.mkdir();self.project.mkdir()
        self.reader=DiagnosticFileReader(self.state,roots=[self.project,self.state]);self.scope=('generation','owner','job')
    def tearDown(self):self.temp.cleanup()
    def run_read(self,operation,path,**kw):
        return self.reader.run(self.scope,dict(operation=operation,path=path,**kw))

    def test_csv_finds_tail_gap_and_full_original_hash_without_rewriting(self):
        path=self.state/'measurements.csv'
        raw=('time_s,current\n'+''.join(f'{i},1\n' for i in range(15000))+ '15002,999\n').encode()
        path.write_bytes(raw);before=path.stat()
        result=self.run_read('csv_diagnosis','$STATE/measurements.csv')
        self.assertEqual(result['status'],'read');self.assertTrue(result['complete'])
        data=result['data'];self.assertEqual(data['rows_analyzed'],15001)
        self.assertEqual(data['gap_count'],1);self.assertEqual(data['gaps'][0]['row'],15001)
        self.assertEqual(data['gaps'][0]['possible_missing_samples'],2)
        self.assertEqual(data['numeric_statistics'][1]['max'],999)
        self.assertEqual(result['source_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['sha256_scope'],'file');self.assertGreater(len(raw),65536)
        self.assertEqual(path.read_bytes(),raw);self.assertEqual(path.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertEqual(result['root_cause_status'],'not_confirmed_by_scan')

    def test_stream_slice_resumes_aggregate_hash_and_detects_version_changes(self):
        path=self.state/'sample.csv';raw=b'time_s,value\n0,4\n1,8\n4,12\n';path.write_bytes(raw)
        # First slice ends after two data rows, not on a synthetic byte boundary.
        ticks=iter([0,0,0,0,2])
        with patch.object(analysis,'SLICE_SECONDS',1),patch.object(analysis.time,'monotonic',side_effect=lambda:next(ticks)):
            first=self.run_read('csv_diagnosis','$STATE/sample.csv')
        self.assertFalse(first['complete']);offset=first['next_offset']
        final=self.run_read('csv_diagnosis','$STATE/sample.csv',offset=offset)
        self.assertTrue(final['complete']);self.assertEqual(final['data']['rows_analyzed'],3)
        self.assertEqual(final['source_sha256'],hashlib.sha256(raw).hexdigest())
        path.write_bytes(raw+b'5,16\n')
        changed=self.run_read('csv_diagnosis','$STATE/sample.csv',offset=offset)
        self.assertEqual(changed['status'],'analysis_restart_required');self.assertFalse(changed['complete'])

    def test_unknown_time_units_and_naive_clock_are_explicit(self):
        (self.state/'unknown.csv').write_text('time,value\n0,1\n1,2\n3,4\n')
        unknown=self.run_read('csv_diagnosis','$STATE/unknown.csv')['data']
        self.assertEqual(unknown['time_basis'],'numeric_unit_unknown');self.assertIsNone(unknown['time_output_unit'])
        (self.state/'naive.csv').write_text('timestamp,value\n2026-11-01 01:00:00,1\n2026-11-01 01:00:01,2\n')
        self.assertEqual(self.run_read('csv_diagnosis','$STATE/naive.csv')['data']['time_basis'],'wall_clock_without_timezone')
        (self.state/'none.csv').write_text('value,note\n1,"two\nlines"\n')
        result=self.run_read('csv_diagnosis','$STATE/none.csv')
        self.assertEqual(result['data']['rows_analyzed'],1);self.assertIsNone(result['data']['time_column'])

    def test_common_delimiters_and_malformed_quoted_rows_do_not_fake_success(self):
        (self.state/'semicolon.csv').write_text('time_s;value\n0;1\n1;3\n2;4\n4;8\n')
        result=self.run_read('csv_diagnosis','$STATE/semicolon.csv')
        self.assertTrue(result['complete']);self.assertEqual(result['data']['delimiter'],';')
        self.assertEqual(result['data']['gap_count'],1)
        (self.state/'broken.csv').write_text('time_s,value\n0,"unclosed field\n')
        result=self.run_read('csv_diagnosis','$STATE/broken.csv')
        self.assertEqual(result['status'],'error');self.assertFalse(result['eof'])

    def test_explicit_period_is_honored_and_missing_column_rejected(self):
        (self.state/'x.csv').write_text('time_ms,value\n0,1\n10,2\n40,3\n')
        result=self.run_read('csv_diagnosis','$STATE/x.csv',query=json.dumps({'sample_period':.01}))
        self.assertEqual(result['data']['period'],.01);self.assertEqual(result['data']['gap_count'],1)
        bad=self.run_read('csv_diagnosis','$STATE/x.csv',query=json.dumps({'time_column':'missing'}))
        self.assertEqual(bad['status'],'error');self.assertFalse(bad['eof']);self.assertIn('不存在',bad['error'])

    def test_logs_read_tail_causal_context_and_redact_secrets(self):
        path=self.state/'communication.log'
        raw=('ordinary context\n'*5000+'2026-11-01T03:02:01Z connect peer=127.0.0.1\n'
            '2026-11-01T03:02:02Z ConnectionRefusedError: connection refused api_key=secret-token\n'
            '2026-11-01T03:02:03Z timeout waiting for heartbeat\n').encode();path.write_bytes(raw)
        result=self.run_read('log_diagnosis','$STATE/communication.log')
        self.assertTrue(result['complete']);self.assertEqual(result['data']['match_count'],2)
        match=result['data']['matches'][0]
        self.assertGreater(match['line'],5000);self.assertIn('connect peer',str(match['previous_lines']))
        self.assertEqual(match['time_basis'],'absolute_with_timezone');self.assertNotIn('secret-token',json.dumps(result))
        self.assertEqual(result['source_sha256'],hashlib.sha256(raw).hexdigest())

    def test_discovery_uses_authorized_roots_and_skips_static_catalog_and_private_cache(self):
        (self.state/'communication.log').write_text('timeout\n');(self.state/'data.csv').write_text('time_s\n0\n')
        imported=self.project/'database/imports';imported.mkdir(parents=True);(imported/'test_cases_one.csv').write_text('catalog')
        private=self.state/'diagnostics';private.mkdir();(private/'other-user.csv').write_text('private')
        outside=self.root/'outside';outside.mkdir();(outside/'private.log').write_text('private')
        (self.state/'escape').symlink_to(outside,target_is_directory=True)
        result=self.run_read('discover','$STATE',limit=16)
        paths=[x['path'] for x in result['candidates']]
        self.assertIn('$STATE/communication.log',paths);self.assertIn('$STATE/data.csv',paths)
        self.assertFalse(any('test_cases' in p or 'other-user' in p or 'private.log' in p for p in paths))

    def test_grant_does_not_bypass_os_denial_or_allow_fifo(self):
        outside=self.root/'outside.csv';outside.write_text('time_s\n0\n')
        self.assertEqual(self.run_read('csv_diagnosis',str(outside))['status'],'permission_required')
        self.reader.grant(self.scope,outside,'file')
        with patch('factory_hmi.core.diagnostic_files.os.open',side_effect=PermissionError('OS denial')):
            self.assertEqual(self.run_read('csv_diagnosis',str(outside))['status'],'os_permission_denied')

    def test_latest_error_and_gap_examples_are_retained_after_large_earlier_history(self):
        (self.state/'many.log').write_text(''.join(f'error early {i}\n' for i in range(40))+'timeout final cause: connection refused\n')
        result=self.run_read('log_diagnosis','$STATE/many.log')['data']
        self.assertEqual(result['match_count'],41);self.assertEqual(len(result['matches']),32)
        self.assertIn('final cause',result['matches'][-1]['text'])
        (self.state/'many.csv').write_text('time_s,value\n'+''.join(f'{i*3},1\n' for i in range(40)))
        result=self.run_read('csv_diagnosis','$STATE/many.csv',query=json.dumps({'sample_period':1}))['data']
        self.assertEqual(result['gap_count'],39);self.assertEqual(len(result['gaps']),24)
        self.assertEqual(result['gaps'][-1]['row'],40)

    def test_numeric_overflow_is_explicit_finite_json_and_zero_budget_does_not_scan(self):
        (self.state/'large.csv').write_text('time_s,value\n0,1e308\n1,-1e308\n2,1e308\n')
        result=self.run_read('csv_diagnosis','$STATE/large.csv')
        json.dumps(result,allow_nan=False)
        metric=result['data']['numeric_statistics'][1]
        self.assertTrue(metric['numeric_overflow']);self.assertIsNone(metric['population_standard_deviation'])
        with patch.object(self.reader,'_open_fd',side_effect=AssertionError('zero budget must not scan')):
            result=self.run_read('csv_diagnosis','$STATE/large.csv',_active_budget_ms=0)
        self.assertEqual(result['status'],'budget_exhausted');self.assertFalse(result['complete'])


if __name__=='__main__':unittest.main()
