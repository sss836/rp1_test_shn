"""Isolated text fixtures only. No real installation/configuration is modified."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4
from factory_hmi.core.diagnostic_repairs import DiagnosticRepairer, safe_profile_change


def digest(value): return hashlib.sha256(value.encode()).hexdigest()


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(); self.root = Path(self.directory.name)
        self.code = self.root / 'hmi' / 'factory_hmi'; self.code.mkdir(parents=True)
        self.target = self.code / 'fixture.py'; self.target.write_text('VALUE = 1\n')
        self.config = self.code / 'config' / 'reliability_platform.yaml'; self.config.parent.mkdir()
        self.config.write_text('profiles:\n  - key: safe\n    label: Before\n    station_id: unchanged\n')
        self.repairer = DiagnosticRepairer(self.root / 'state', code_roots=[self.root / 'hmi'], profile_path=self.config)
        self.installation, self.window, self.actor = str(uuid4()), 'fixture-window', str(uuid4())

    def tearDown(self): self.directory.cleanup()

    def proposal(self, **changes):
        data = dict(id=str(uuid4()), job_id=str(uuid4()), actor_id=self.actor, role='SYSTEM_ADMIN', installation_id=self.installation,
                    window_id=self.window, kind='code', path='factory_hmi/fixture.py', expected_sha256=digest('VALUE = 1\n'),
                    original='VALUE = 1\n', replacement='VALUE = 2\n', reason='synthetic only')
        data.update(changes)
        data['proposal_sha256'] = digest(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False))
        from datetime import datetime, timezone, timedelta
        data['expires_at'] = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        data['state'] = 'queued'
        return data

    def apply(self, proposal, **kw):
        return self.repairer.apply(proposal, role=kw.pop('role',proposal['role']), installation_id=self.installation, window_id=self.window, **kw)

    def test_admin_atomic_save_backup_and_byte_identical_receipt_retry(self):
        proposal = self.proposal(); result = self.apply(proposal)
        self.assertEqual(result['status'], 'applied'); self.assertEqual(self.target.read_text(), 'VALUE = 2\n')
        self.assertEqual(Path(result['backup_path']).read_text(), 'VALUE = 1\n')
        self.assertEqual(self.apply(proposal), result)
        self.assertFalse(result['loaded']); self.assertFalse(result['current_process_updated'])

    def test_executor_cannot_write_code_or_device_or_identity(self):
        result = self.apply(self.proposal(role='TEST_EXECUTOR'))
        self.assertEqual(result['status'], 'rejected'); self.assertEqual(self.target.read_text(), 'VALUE = 1\n')
        original = self.config.read_text()
        for replacement in [original.replace('unchanged','other-station'), original + 'factory_hmi:\n  safety: {}\n']:
            proposal = self.proposal(role='TEST_EXECUTOR',kind='config',path='factory_hmi/config/reliability_platform.yaml',original=original, replacement=replacement, expected_sha256=digest(original))
            self.assertEqual(self.apply(proposal)['status'], 'rejected')

    def test_executor_real_profile_display_label_and_equivalent_format(self):
        original = self.config.read_text(); replacement = original.replace('Before','After')
        proposal = self.proposal(role='TEST_EXECUTOR',kind='config',path='factory_hmi/config/reliability_platform.yaml',original=original,replacement=replacement,expected_sha256=digest(original))
        self.assertEqual(self.apply(proposal)['status'], 'applied')
        self.assertEqual(self.config.read_text(), replacement)
        self.assertTrue(safe_profile_change(replacement, replacement + '\n# harmless formatting\n'))
        self.assertFalse(safe_profile_change(replacement, replacement.replace('label: After', 'label: ""')))

    def test_changed_baseline_conflict_no_overwrite(self):
        proposal = self.proposal(); self.target.write_text('VALUE = 99\n')
        self.assertEqual(self.apply(proposal)['status'], 'conflict'); self.assertEqual(self.target.read_text(), 'VALUE = 99\n')

    def test_concurrent_edit_during_prepare_is_rejected(self):
        proposal = self.proposal()
        import factory_hmi.core.diagnostic_repairs as module
        original = module.tempfile.mkstemp
        def changed(*args, **kwargs):
            result = original(*args, **kwargs); self.target.write_text('VALUE = 100\n'); return result
        with patch.object(module.tempfile, 'mkstemp', side_effect=changed):
            result = self.apply(proposal)
        self.assertEqual(result['status'], 'conflict'); self.assertEqual(self.target.read_text(), 'VALUE = 100\n')

    def test_revocation_at_commit_never_replaces(self):
        calls=[]
        def current(): calls.append(1); return len(calls) == 1
        self.assertEqual(self.apply(self.proposal(), still_current=current)['status'], 'conflict')
        self.assertEqual(self.target.read_text(), 'VALUE = 1\n')

    def test_tampered_proposal_and_role_window_rejected(self):
        proposal = self.proposal(); proposal['replacement'] = 'VALUE = 3\n'
        self.assertEqual(self.apply(proposal)['status'], 'rejected')
        self.assertEqual(self.apply(self.proposal(window_id='other-window'))['status'], 'rejected')
        self.assertEqual(self.apply(self.proposal(), role='TEST_EXECUTOR')['status'], 'rejected')

    def test_syntax_error_symlink_hardlink_and_outside_root_rejected(self):
        self.assertEqual(self.apply(self.proposal(replacement='def invalid('))['status'], 'rejected')
        alias = self.code / 'alias.py'; alias.symlink_to(self.target)
        self.assertEqual(self.apply(self.proposal(path='factory_hmi/alias.py'))['status'], 'rejected')
        outside = self.root / 'outside.py'; outside.write_text('VALUE = 1\n')
        self.assertEqual(self.apply(self.proposal(path=str(outside)))['status'], 'rejected')
        import os
        os.link(self.target, self.code / 'hard.py')
        self.assertEqual(self.apply(self.proposal())['status'], 'rejected')

    def test_concurrent_controlled_repairs_serialized_second_conflicts(self):
        proposals = [self.proposal(replacement=f'VALUE = {value}\n') for value in (2, 3)]
        results=[]
        workers=[threading.Thread(target=lambda item=item: results.append(self.apply(item))) for item in proposals]
        for worker in workers: worker.start()
        for worker in workers: worker.join(2)
        self.assertEqual(sorted(result['status'] for result in results), ['applied','conflict'])

    def test_crash_after_replace_before_receipt_recovers_without_second_write(self):
        proposal=self.proposal(); self.repairer.state.mkdir(parents=True)
        (self.repairer.state / (proposal['id'] + '.before')).write_text(proposal['original'])
        self.target.write_text(proposal['replacement'])
        result=self.apply(proposal)
        self.assertEqual(result['status'], 'applied'); self.assertIn('恢复', result['message'])

    def test_expired_proposal_and_os_denial_never_modify(self):
        proposal=self.proposal(); proposal['expires_at']='2000-01-01T00:00:00+00:00'
        self.assertEqual(self.apply(proposal)['status'],'rejected')
        with patch('factory_hmi.core.diagnostic_repairs.os.access', return_value=False):
            result=self.apply(self.proposal())
        self.assertEqual(result['status'],'failed'); self.assertIn('操作系统',result['message'])
        self.assertEqual(self.target.read_text(),'VALUE = 1\n')

    def test_config_type_changes_and_yaml_aliases_rejected(self):
        before='profiles:\n  - key: a\n    label: A\nflag: true\n'
        self.assertFalse(safe_profile_change(before,before.replace('true','1')))
        self.assertFalse(safe_profile_change(before,before.replace('label: A','label: &label A')))

    def test_receipt_reused_after_repairer_restart_without_replacing_again(self):
        proposal=self.proposal(); receipt=self.apply(proposal)
        replacement=DiagnosticRepairer(self.root/'state',code_roots=[self.root/'hmi'],profile_path=self.config)
        with patch('factory_hmi.core.diagnostic_repairs.os.replace') as replace:
            result=replacement.apply(proposal,role='SYSTEM_ADMIN',installation_id=self.installation,window_id=self.window)
        self.assertEqual(result,receipt); replace.assert_not_called()


    def test_project_read_alias_matches_repository_root(self):
        proposal=self.proposal(path='$PROJECT/hmi/factory_hmi/fixture.py')
        self.assertEqual(self.apply(proposal)['status'],'applied')

    def test_post_replace_directory_fsync_failure_reports_saved_and_retry_does_not_write(self):
        import os, stat
        proposal=self.proposal(); original=os.fsync
        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode): raise OSError('synthetic directory fsync failure')
            return original(descriptor)
        with patch('factory_hmi.core.diagnostic_repairs.os.fsync',side_effect=fail_directory): result=self.apply(proposal)
        self.assertTrue(result['saved']); self.assertTrue(result['replacement_completed']); self.assertTrue(result['after_hash_verified'])
        self.assertFalse(result['durability_verified']); self.assertEqual(result['status'],'applied')
        self.assertEqual(self.target.read_text(),proposal['replacement']); self.assertEqual(result['after_sha256'],digest(proposal['replacement']))
        self.assertIn('文件已替换',result['message'])
        with patch('factory_hmi.core.diagnostic_repairs.os.replace') as replace: replay=self.apply(proposal)
        self.assertEqual(replay,result); replace.assert_not_called()

    def test_post_replace_directory_close_failure_reports_saved(self):
        import os, stat
        proposal=self.proposal(); original=os.close
        def fail_directory(descriptor):
            directory=stat.S_ISDIR(os.fstat(descriptor).st_mode)
            original(descriptor)
            if directory: raise OSError('synthetic directory close failure')
        with patch('factory_hmi.core.diagnostic_repairs.os.close',side_effect=fail_directory): result=self.apply(proposal)
        self.assertTrue(result['saved']); self.assertTrue(result['after_hash_verified']); self.assertFalse(result['durability_verified'])
        self.assertEqual(self.target.read_text(),proposal['replacement'])

    def test_receipt_file_fsync_failure_returns_saved_truth_for_bridge_outbox(self):
        import os
        proposal=self.proposal(); original=os.fsync
        def fail_receipt(descriptor):
            target=Path(os.readlink(f'/proc/self/fd/{descriptor}'))
            if target.name==proposal['id']+'.tmp': raise OSError('synthetic receipt fsync failure')
            return original(descriptor)
        with patch('factory_hmi.core.diagnostic_repairs.os.fsync',side_effect=fail_receipt): result=self.apply(proposal)
        self.assertTrue(result['saved']); self.assertFalse(result['receipt_durable']); self.assertTrue(result['after_hash_verified'])
        self.assertEqual(self.target.read_text(),proposal['replacement']); self.assertIn('审计回执未持久',result['message'])
        with patch('factory_hmi.core.diagnostic_repairs.os.replace', wraps=os.replace) as replace: replay=self.apply(proposal)
        self.assertTrue(replay['saved']); self.assertTrue(replay['receipt_durable'])
        self.assertFalse(any(Path(call.args[1])==self.target for call in replace.call_args_list))

    def test_receipt_atomic_replace_failure_returns_saved_fact_and_readonly_retry(self):
        import os
        proposal=self.proposal(); original=os.replace
        def fail_receipt(source,destination):
            if Path(destination).name==proposal['id']+'.json': raise OSError('synthetic receipt rename failure')
            return original(source,destination)
        with patch('factory_hmi.core.diagnostic_repairs.os.replace',side_effect=fail_receipt): result=self.apply(proposal)
        self.assertTrue(result['saved']); self.assertFalse(result['receipt_durable']); self.assertEqual(result['after_sha256'],digest(proposal['replacement']))
        self.assertEqual(self.target.read_text(),proposal['replacement'])
