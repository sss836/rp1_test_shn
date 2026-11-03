"""Receipt-only recovery with inert files and fake platform transport."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest
from uuid import uuid4
from fastapi import HTTPException
from factory_hmi.gateway.diagnostics import DiagnosticBridge


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory(); self.root=Path(self.directory.name); self.actor=str(uuid4())
        self.bridge=DiagnosticBridge(self.root); self.identifier=str(uuid4()); self.posts=[]; self.offline=False; self.logout_after_apply=False
        self.bridge._binding=dict(owner='window',generation='old',operator='fixture',station='S1')
        self.proposal=dict(id=self.identifier,actor_id=self.actor,role='SYSTEM_ADMIN',installation_id=self.bridge.installation,
                           window_id='window',proposal_sha256='a'*64,settlement_token='v1.'+'b'*64)
        self.receipt=dict(status='applied',saved=True,loaded=False,current_process_updated=False,proposal_sha256='a'*64,
                          before_sha256='c'*64,after_sha256='d'*64,backup_path='fixture-backup',message='saved only')
        self.bridge.repairs.apply=Mock(side_effect=self.applied)
        self.bridge._remote=self.remote
    def tearDown(self): self.directory.cleanup()
    def applied(self,*args,**kwargs):
        if self.logout_after_apply: self.bridge.revoke()
        return dict(self.receipt)
    def remote(self,binding,method,path,payload=None,**kw):
        if path=='/api/v1/auth/me': return {'user':{'id':self.actor,'role':'SYSTEM_ADMIN'}}
        if path=='/api/v1/assistant/capabilities': return {'mode':'assistant_runtime'}
        if '/pending?' in path: return {'items':[self.proposal]}
        if path.endswith('/receipt'):
            self.posts.append(payload)
            if self.offline: raise HTTPException(502,'synthetic lost acknowledgement')
            return {'accepted':True,'replayed':len(self.posts)>1,'state':'applied'}
        raise AssertionError(path)
    def apply(self): return self.bridge.call('window','repair-apply',{'generation':'old','id':self.identifier})
    def reconnect(self):
        # Recreate bridge to prove recovery is from disk, not old in-memory state.
        bridge=DiagnosticBridge(self.root); bridge._binding=dict(owner='new-window',generation='new',operator='fixture',station='S1')
        bridge._remote=self.remote; bridge.repairs.apply=Mock(side_effect=AssertionError('receipt retry must never execute'))
        return bridge
    def test_lost_ack_persists_token_and_identical_receipt_and_only_reposts_after_restart(self):
        self.offline=True; result=self.apply()
        self.assertEqual(result['synchronization'],'pending'); self.assertTrue(result['receipt']['saved'])
        path=self.bridge.repairs.state/'settlements'/(self.identifier+'.json'); before=json.loads(path.read_text())
        self.assertEqual(before['result'],self.receipt); self.assertEqual(before['settlement_token'],self.proposal['settlement_token'])
        self.offline=False; bridge=self.reconnect(); status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement']['delivered'],1); bridge.repairs.apply.assert_not_called()
        self.assertEqual(self.posts[0],self.posts[1]); self.assertTrue(json.loads(path.read_text())['delivered'])
        self.assertIsNone(json.loads(path.read_text())['settlement_token'])
    def test_logout_after_disk_save_returns_saved_pending_and_new_session_does_not_apply(self):
        self.logout_after_apply=True
        with self.assertRaises(HTTPException) as caught: self.apply()
        self.assertEqual(caught.exception.status_code,409); self.assertTrue(caught.exception.detail['saved'])
        self.assertIn('已保存',caught.exception.detail['message']); self.assertEqual(self.posts,[])
        bridge=self.reconnect(); status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement']['delivered'],1); bridge.repairs.apply.assert_not_called()
    def test_other_account_cannot_receive_or_settle_old_receipts(self):
        self.offline=True; self.apply(); self.offline=False
        self.actor=str(uuid4()); bridge=self.reconnect(); status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement'],{'pending_saved':0,'pending_other':0,'delivered':0}); self.assertEqual(len(self.posts),1)
    def test_intent_crash_recovers_only_existing_terminal_receipt_without_touching_target(self):
        self.bridge._settlement_intent(self.proposal)
        receipt=self.bridge.repairs.state/(self.identifier+'.json'); receipt.write_text(json.dumps(self.receipt))
        bridge=self.reconnect(); status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement']['delivered'],1); self.assertEqual(self.posts[0]['result'],self.receipt)
        bridge.repairs.apply.assert_not_called()
    def test_unfinished_intent_does_not_execute_or_fabricate_saved_receipt(self):
        self.bridge._settlement_intent(self.proposal); bridge=self.reconnect(); status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement']['pending_other'],1); self.assertEqual(status['repair_settlement']['pending_saved'],0)
        self.assertEqual(self.posts,[]); bridge.repairs.apply.assert_not_called()
    def test_feedback_bridge_explicit_default_false_and_true(self):
        sent=[]; self.bridge._remote=lambda binding,method,path,payload=None,**kw: sent.append(payload) or {}
        data=dict(generation='old',request_key=str(uuid4()),job_id=str(uuid4()),rating='issue',note='fixture')
        self.bridge.call('window','feedback',data); self.bridge.call('window','feedback',dict(data,share_for_faq=True))
        self.assertIs(sent[0]['share_for_faq'],False); self.assertIs(sent[1]['share_for_faq'],True)

    def test_retry_of_completed_outbox_does_not_apply_again_even_without_core_receipt(self):
        self.offline=True; first=self.apply(); self.offline=False
        second=self.apply()
        self.assertEqual(first['receipt'],second['receipt']); self.assertEqual(self.bridge.repairs.apply.call_count,1)
        self.assertEqual(self.posts[0],self.posts[1])

    def test_readonly_hash_recovery_when_both_core_receipt_and_result_outbox_failed(self):
        import hashlib
        code=self.root/'hmi'/'factory_hmi'; code.mkdir(parents=True); target=code/'fixture.py'
        original,replacement=b'VALUE = 1\n',b'VALUE = 2\n'; target.write_bytes(replacement)
        self.bridge.repairs.roots=(self.root/'hmi',); self.bridge.repairs.aliases['$PROJECT']=self.root
        self.proposal.update(path='factory_hmi/fixture.py',kind='code',expected_sha256=hashlib.sha256(original).hexdigest(),replacement=replacement.decode())
        intent=self.bridge._settlement_intent(self.proposal)
        backup=self.bridge.repairs.state/(self.identifier+'.before'); backup.write_bytes(original)
        bridge=self.reconnect(); bridge.repairs.roots=(self.root/'hmi',)
        status=bridge.call('new-window','status')
        self.assertEqual(status['repair_settlement']['delivered'],1); bridge.repairs.apply.assert_not_called()
        receipt=self.posts[0]['result']; self.assertTrue(receipt['saved']); self.assertEqual(receipt['after_sha256'],hashlib.sha256(replacement).hexdigest())
        self.assertEqual(target.read_bytes(),replacement); self.assertIn('只读恢复',receipt['message'])
