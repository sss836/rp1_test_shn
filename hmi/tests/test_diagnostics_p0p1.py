"""P0/P1 native interactions with fake transport and inert local files."""
import base64
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtWidgets import QApplication, QDialog
from factory_hmi.desktop.diagnostics_page import DiagnosticsPage, safe_markdown
from factory_hmi.gateway.diagnostics import DiagnosticBridge
from test_diagnostic_attachments import AttachmentClient


class EnhancedClient(AttachmentClient):
    def __init__(self):
        super().__init__(); self.drafts={}; self.events=[]
    def diagnostic(self, action, payload=None):
        if action == 'draft-save':
            self.drafts[payload.get('thread_id')] = payload['message']; return {'result': {'saved':True}}
        if action == 'draft-load': return {'result': {'message': self.drafts.get(payload.get('thread_id'), '')}}
        if action == 'steer': self.calls.append((action,payload)); return {'result': {'accepted':True}}
        if action == 'events': return {'result': {'items':self.events,'next_seq':len(self.events)}}
        return super().diagnostic(action,payload)


class NativeInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.directory=TemporaryDirectory(); self.root=Path(self.directory.name)
        self.client=EnhancedClient(); self.page=DiagnosticsPage(self.client); self.page.timer.stop()
        self.page.binding=self.client.status; self.page._controls(); self.releases=[]
    def tearDown(self):
        for event in self.releases: event.set()
        self.page.stop(); self.page.pool.waitForDone(3000); self.app.processEvents(); self.page.deleteLater(); self.directory.cleanup()
    def pump(self, predicate):
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            self.app.processEvents()
            if predicate(): return
            time.sleep(.003)
        self.fail('Qt condition not reached')
    def file(self, name):
        path=self.root/name; path.write_text('raw,fixture\n'); return path

    def test_choose_drop_and_clipboard_file_urls_share_deduped_queue(self):
        path=self.file('sample.csv')
        self.page._add_attachments([path,path])
        source=QMimeData(); source.setUrls([QUrl.fromLocalFile(str(path))])
        self.page.message.insertFromMimeData(source)
        self.assertEqual(len(self.page.attachments),1)
        self.assertEqual(self.client.uploads,[])
        self.assertTrue(self.page.choose.isHidden()); self.assertTrue(self.page.paste.isHidden())

    def test_next_draft_editable_during_upload_and_never_sent_as_current(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        original=self.client.diagnostic_attachment
        def blocked(*args): entered.set(); release.wait(2); return original(*args)
        self.client.diagnostic_attachment=blocked
        self.page.message.setPlainText('first question'); self.page._add_attachments([self.file('a.csv')]); self.page._send(); self.pump(entered.is_set)
        self.assertFalse(self.page.message.isReadOnly()); self.page.message.setPlainText('next private draft')
        release.set(); self.pump(lambda:not self.page._workers)
        self.assertEqual(self.client.submits[0]['message'],'first question')
        self.assertEqual(self.page.message.toPlainText(),'next private draft')

    def test_running_followup_and_queued_question_bind_correct_job(self):
        current=str(uuid4()); self.page.job=current; self.page.thread=str(uuid4()); self.page._controls()
        self.page.send_mode.setCurrentIndex(1); self.page.message.setPlainText('diagnostic addition'); self.page._send(); self.pump(lambda:not self.page._workers)
        data=[data for action,data in self.client.calls if action=='steer'][0]
        self.assertEqual(data['expected_job_id'],current)
        self.page.send_mode.setCurrentIndex(0); self.page.message.setPlainText('separate next question'); self.page._send(); self.pump(lambda:not self.page._workers)
        self.assertEqual(self.client.submits[0]['after_job_id'],current); self.assertEqual(self.page.job,current)
        queued=self.page._queued_jobs[0]; self.page._job_status({'state':'COMPLETED','result':{'reply':'first result'}})
        self.assertEqual(self.page.job,queued); self.assertEqual(self.page.last_job,current)

    def test_event_duplicates_do_not_duplicate_content_and_round_replaces_prefix(self):
        self.page._events({'items':[{'seq':1,'kind':'text','data':{'round':1,'text':'first'}},{'seq':2,'kind':'tool','data':{'tool':'read_local','status':'running'}}], 'next_seq':2})
        self.page._events({'items':[{'seq':1,'kind':'text','data':{'round':1,'text':'OLD'}}], 'next_seq':2})
        self.assertIn('first',self.page.live_output.toPlainText()); self.assertNotIn('OLD',self.page.live_output.toPlainText())
        self.page._events({'items':[{'seq':3,'kind':'text','data':{'round':2,'text':'final prefix'}}], 'next_seq':3})
        self.assertEqual(self.page.live_output.toPlainText().strip(),'final prefix')

    def test_markdown_html_and_resource_access_are_inert_copy_preserves_text(self):
        self.page._display('assistant','# Result\n\n| A | B |\n|---|---|\n| 1 | 2 |\n<script>alert(1)</script>\n![image](file:///etc/passwd)')
        content=self.page.transcript.toPlainText()
        self.assertIn('Result',content); self.assertIn('<script>',content)
        self.assertIsNone(self.page.transcript.loadResource(2,QUrl('file:///etc/passwd')))
        self.page._last_answer='plain answer'; self.page._copy_answer()
        self.assertEqual(self.app.clipboard().text(),'plain answer')

    def test_per_thread_draft_recovery_and_revoke_clear_private_state(self):
        a,b=str(uuid4()),str(uuid4())
        self.page.thread=a; self.page.message.setPlainText('draft A'); self.page._save_draft(); self.pump(lambda:not self.page._workers)
        self.page.thread=b; self.page._restore_draft(); self.pump(lambda:not self.page._workers)
        self.page.message.setPlainText('draft B'); self.page._save_draft(); self.pump(lambda:not self.page._workers)
        self.page.thread=a; self.page._restore_draft(); self.assertEqual(self.page.message.toPlainText(),'draft A')
        self.page._reference_window=QDialog(self.page); self.page._reference_window.show()
        self.page.revoke(); self.assertEqual(self.page.message.toPlainText(),''); self.assertEqual(self.page._drafts,{})
        self.assertIsNone(self.page._reference_window)

    def test_per_file_cancel_skips_next_upload_and_preserves_successful_file(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        original=self.client.diagnostic_attachment
        def blocked(*args): entered.set(); release.wait(2); return original(*args)
        self.client.diagnostic_attachment=blocked
        self.page._add_attachments([self.file('a.csv'),self.file('b.csv')]); self.page._send(); self.pump(entered.is_set)
        self.page._cancel_attachment(self.page.submission['entries'][1]); release.set(); self.pump(lambda:not self.page._workers)
        self.assertEqual(len(self.client.uploads),1); self.assertEqual(len(self.client.submits[0]['attachments']),1)

    def test_history_callback_for_previous_thread_cannot_repopulate_current(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        original=self.client.diagnostic
        a,b=str(uuid4()),str(uuid4())
        def blocked(action,payload=None):
            if action=='history': entered.set(); release.wait(2); return {'result':{'messages':[{'role':'assistant','body':'old private history'}]}}
            return original(action,payload)
        self.client.diagnostic=blocked
        self.page.history_list.addItem('A',a); self.page.history_list.setCurrentIndex(1); self.page._history(1); self.pump(entered.is_set)
        self.page.thread=b; self.page._welcome(); release.set(); self.pump(lambda:not self.page._workers)
        self.assertNotIn('old private history',self.page.transcript.toPlainText())


class BridgeEnhancedTests(unittest.TestCase):
    def bridge(self,root):
        bridge=DiagnosticBridge(root); bridge._binding=dict(owner='window',generation='gen',operator='fixture',station='fixture')
        return bridge
    def test_bridge_keeps_frozen_queue_and_server_verified_references(self):
        with TemporaryDirectory() as directory:
            bridge=self.bridge(Path(directory)); calls=[]
            bridge._remote=lambda *args,**kwargs: calls.append((args,kwargs)) or {}
            parent,thread=str(uuid4()),str(uuid4())
            refs=[dict(kind='asset',id=str(uuid4()),label='fixture')]
            bridge.call('window','submit',dict(generation='gen',message='next',request_key=str(uuid4()),thread_id=thread,after_job_id=parent,references=refs))
            payload=calls[0][0][3]
            self.assertEqual(payload['after_job_id'],parent); self.assertEqual(payload['context']['references'],refs)
            self.assertEqual(payload['context']['hmi']['window_id'],'window')
    def test_drafts_are_persistent_account_separated_and_generation_guarded(self):
        with TemporaryDirectory() as directory:
            bridge=self.bridge(Path(directory)); thread=str(uuid4())
            bridge.call('window','draft-save',dict(generation='gen',thread_id=thread,message='private A'))
            self.assertEqual(bridge.call('window','draft-load',dict(generation='gen',thread_id=thread))['message'],'private A')
            bridge._binding['operator']='other-user'
            self.assertEqual(bridge.call('window','draft-load',dict(generation='gen',thread_id=thread))['message'],'')
            from fastapi import HTTPException
            with self.assertRaises(HTTPException): bridge.call('window','draft-load',dict(generation='old',thread_id=thread))
    def test_bridge_repair_claim_uses_real_actor_role_and_exact_server_proposal(self):
        with TemporaryDirectory() as directory:
            bridge=self.bridge(Path(directory)); identifier=str(uuid4()); actor=str(uuid4()); seen=[]
            proposal=dict(id=identifier,actor_id=actor,role='SYSTEM_ADMIN',installation_id=bridge.installation,window_id='window',proposal_sha256='a'*64,settlement_token='v1.'+'b'*64)
            def remote(binding,method,path,payload=None,**kwargs):
                seen.append((method,path,payload))
                if '/pending?' in path: return {'items':[proposal]}
                if path=='/api/v1/auth/me': return {'user':{'id':actor,'role':'SYSTEM_ADMIN'}}
                return {'accepted':True}
            bridge._remote=remote
            with patch.object(bridge.repairs,'apply',return_value={'status':'applied','saved':True,'loaded':False}) as apply:
                bridge.call('window','repair-apply',dict(generation='gen',id=identifier,role='TEST_EXECUTOR',replacement='ATTACK'))
            self.assertIs(apply.call_args.args[0],proposal); self.assertEqual(apply.call_args.kwargs['role'],'SYSTEM_ADMIN')
            self.assertEqual(seen[-1][0],'POST'); self.assertIn('/receipt',seen[-1][1])


class DraftCancellationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    setUp=NativeInteractionTests.setUp
    tearDown=NativeInteractionTests.tearDown
    pump=NativeInteractionTests.pump
    file=NativeInteractionTests.file
    def test_cancel_keeps_original_question_and_next_draft(self):
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        original=self.client.diagnostic_attachment
        def blocked(*args): entered.set(); release.wait(2); return original(*args)
        self.client.diagnostic_attachment=blocked
        self.page.message.setPlainText('original unsent question'); self.page._add_attachments([self.file('data.csv')]); self.page._send(); self.pump(entered.is_set)
        self.page.message.setPlainText('next unsent draft'); self.page._cancel(); release.set(); self.pump(lambda:not self.page._workers)
        self.assertIn('original unsent question',self.page.message.toPlainText())
        self.assertIn('next unsent draft',self.page.message.toPlainText())
        self.assertEqual(self.client.submits,[])


class AdditionalNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    setUp=NativeInteractionTests.setUp
    tearDown=NativeInteractionTests.tearDown
    pump=NativeInteractionTests.pump
    file=NativeInteractionTests.file

    def test_lost_steering_ack_reuses_request_and_frozen_text(self):
        self.page.job=str(uuid4()); self.page._controls(); self.page.message.setPlainText('original addition')
        original=self.client.diagnostic; attempts=[]
        def call(action,payload=None):
            if action=='steer':
                attempts.append(dict(payload))
                if len(attempts)==1: raise OSError('lost acknowledgement')
            return original(action,payload)
        self.client.diagnostic=call
        self.page._steer(); self.pump(lambda:not self.page._workers)
        self.page.message.setPlainText('different next draft'); self.page._steer(); self.pump(lambda:not self.page._workers)
        self.assertEqual(attempts[0]['request_key'],attempts[1]['request_key']); self.assertEqual(attempts[1]['message'],'original addition')
        self.assertEqual(self.page.message.toPlainText(),'different next draft')

    def test_evidence_unread_range_is_visible_and_long_logs_fold(self):
        body='\n'.join(f'line {number}' for number in range(130))
        self.page._display('assistant',body,[dict(title='coverage',source='fixture',data=dict(eof=False,unread=123,sha256='fixture'))])
        self.assertIn('展开完整答复',self.page.transcript.toPlainText())
        self.assertNotIn('line 129',self.page.transcript.toPlainText()); self.assertIn('line 129',self.page._last_answer)
        self.assertFalse(self.page.evidence_note.isHidden()); self.assertIn('后面的数据还没检查',self.page.evidence_note.text())
        self.assertTrue(self.page._long_answers)
        self.assertTrue(self.page._partial_evidence({'model_content_complete':False,'unread':'后面的数据还没检查'}))
        self.page.revoke(); self.assertEqual(self.page._long_answers,{})


    def test_multiple_clipboard_images_join_one_visible_queue_and_deduplicate(self):
        from PySide6.QtGui import QImage
        for color in (0x22aabb,0x33bbcc,0x33bbcc):
            image=QImage(40,30,QImage.Format.Format_RGB32); image.fill(color); self.app.clipboard().setImage(image); self.page._paste()
        self.assertIsNotNone(self.page.image); self.assertEqual(len(self.page.attachments),1)
        self.assertEqual(self.page.attachment_list.topLevelItemCount(),2)
        self.assertEqual(self.client.uploads,[])


class FeedbackAndUnprocessedNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    setUp=NativeInteractionTests.setUp
    tearDown=NativeInteractionTests.tearDown
    pump=NativeInteractionTests.pump
    file=NativeInteractionTests.file

    def test_feedback_requires_explicit_share_checkbox_default_false(self):
        from PySide6.QtWidgets import QCheckBox, QDialogButtonBox
        self.page.last_job=str(uuid4()); self.page._give_feedback()
        dialog=self.page._feedback_window; checkbox=dialog.findChild(QCheckBox)
        self.assertFalse(checkbox.isChecked())
        dialog.findChild(QDialogButtonBox).accepted.emit(); self.pump(lambda:not self.page._workers)
        payload=[data for action,data in self.client.calls if action=='feedback'][0]
        self.assertIs(payload['share_for_faq'],False)
        self.page._give_feedback(); dialog=self.page._feedback_window; dialog.findChild(QCheckBox).setChecked(True)
        dialog.findChild(QDialogButtonBox).accepted.emit(); self.pump(lambda:not self.page._workers)
        payload=[data for action,data in self.client.calls if action=='feedback'][-1]
        self.assertIs(payload['share_for_faq'],True)

    def test_faq_uses_actual_revision_in_list_and_publish(self):
        self.page.binding['capabilities']={'features':{'faq_publish':True}}
        from PySide6.QtWidgets import QTextBrowser
        original=self.client.diagnostic
        def call(action,payload=None):
            if action=='faq': return {'result':{'items':[dict(question='Q',answer='A',revision=7)]}}
            if action=='faq-publish': return {'result':{'revision':8}}
            return original(action,payload)
        self.client.diagnostic=call; self.page._faq(); self.pump(lambda:not self.page._workers)
        self.assertIn('版本 7',self.page._faq_window.findChild(QTextBrowser).toPlainText())
        self.page._feedback={'id':str(uuid4()),'share_for_faq':True}
        with patch('factory_hmi.desktop.diagnostics_page.QInputDialog.getText',return_value=('Question',True)), patch('factory_hmi.desktop.diagnostics_page.QInputDialog.getMultiLineText',return_value=('Answer',True)):
            self.page._publish_faq()
        self.pump(lambda:not self.page._workers); self.assertIn('版本 8',self.page.identity.text())

    def test_unprocessed_supplement_is_explicit_and_restores_without_losing_next_draft(self):
        request=str(uuid4()); card=dict(type='unprocessed_input',title='未使用补充',source='assistant_job_inputs',updated_at='fixture',data={'items':[dict(request_key=request,message='not consumed')],'summary':'未用于本次诊断'})
        self.page._display('assistant','done',[card,card])
        self.assertIn('未用于本次诊断',self.page.transcript.toPlainText()); self.assertFalse(self.page.restore_inputs.isHidden())
        self.page.message.setPlainText('next draft'); self.page._restore_unprocessed_inputs()
        self.assertIn('next draft',self.page.message.toPlainText()); self.assertEqual(self.page.message.toPlainText().count('not consumed'),1)
        self.assertTrue(self.page.restore_inputs.isHidden())

    def test_pending_receipt_error_displays_saved_and_never_claims_not_written(self):
        from factory_hmi.client import GatewayHTTPError
        def pending(): raise GatewayHTTPError('pending',status_code=409,detail={'code':'repair_receipt_pending','saved':True,'message':'已保存，云端回执待同步'})
        self.page._run('repair-apply',pending,lambda _: self.fail('unexpected success')); self.pump(lambda:not self.page._workers)
        self.assertIn('修改已保存，结果还没同步到平台',self.page.identity.text())


class FinalNativeBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    setUp=NativeInteractionTests.setUp
    tearDown=NativeInteractionTests.tearDown
    pump=NativeInteractionTests.pump
    file=NativeInteractionTests.file

    def tracker(self, fail_first=False):
        from factory_hmi.client import GatewayHTTPError
        entered,release=threading.Event(),threading.Event(); self.releases.append(release)
        attempts=[]; cancelled=set(); phases={}; original=self.client.diagnostic
        def upload(raw,name,generation,identifier):
            attempts.append(identifier); phases[identifier]=dict(upload_id=identifier,total_bytes=len(raw),bytes_sent=min(65536,len(raw)),phase='uploading')
            entered.set()
            if fail_first and len(attempts)==1: raise OSError('synthetic upload failure')
            release.wait(2)
            if identifier in cancelled: raise GatewayHTTPError('cancelled',status_code=409,detail={'code':'upload_cancelled'})
            return {'result':{'id':str(uuid4())}}
        def call(action,payload=None):
            if action=='upload-progress': return {'result':dict(phases.get(payload['upload_id'],{}))}
            if action=='upload-cancel':
                identifier=payload['upload_id']; cancelled.add(identifier); release.set()
                phases[identifier]['phase']='cancelling'; return {'result':dict(phases[identifier])}
            return original(action,payload)
        self.client.diagnostic_attachment_progress=upload; self.client.diagnostic=call
        return entered,release,attempts,phases

    def test_native_bytes_100_waits_confirmation_and_stale_upload_response_is_ignored(self):
        entered,release,attempts,phases=self.tracker(); path=self.file('progress.csv'); path.write_bytes(b'x'*131072)
        self.page._add_attachments([path]); self.page._send(); self.pump(entered.is_set)
        entry=self.page.submission['entries'][0]; self.page._upload_watch(); self.pump(lambda:entry.get('bytes_sent')==65536)
        self.assertIn('50.0%',entry['state']); self.assertEqual(self.client.submits,[])
        phases[attempts[0]].update(bytes_sent=131072,phase='waiting_response'); self.page._upload_watch(); self.pump(lambda:'等待保存确认' in entry['state'])
        self.assertIsNone(entry.get('uploaded_id')); self.assertEqual(self.client.submits,[])
        before=entry['state']; self.page._upload_progress(entry,str(uuid4()),dict(bytes_sent=0,total_bytes=131072,phase='failed'))
        self.assertEqual(entry['state'],before)
        release.set(); self.pump(lambda:not self.page._workers); self.assertEqual(len(self.client.submits[0]['attachments']),1)

    def test_per_file_cancel_reaches_transport_and_never_binds_cancelled_id(self):
        entered,release,attempts,phases=self.tracker()
        self.page._add_attachments([self.file('cancel.csv'),self.file('keep.csv')]); self.page._send(); self.pump(entered.is_set)
        self.page._cancel_attachment(self.page.submission['entries'][0]); self.pump(lambda:not self.page._workers)
        self.assertEqual(len(attempts),2); self.assertEqual(len(self.client.submits[0]['attachments']),1)

    def test_upload_retry_uses_new_transport_id_and_same_business_request(self):
        entered,release,attempts,phases=self.tracker(fail_first=True)
        self.page._add_attachments([self.file('retry.csv')]); self.page._send(); self.pump(lambda:not self.page._workers)
        key=self.page.submission['data']['request_key']; release.set(); self.page._send(); self.pump(lambda:not self.page._workers)
        self.assertEqual(len(attempts),2); self.assertNotEqual(attempts[0],attempts[1])
        self.assertEqual(self.client.submits[0]['request_key'],key)

    def test_terminal_event_pages_drained_before_clearing_job_and_timeout_finishes(self):
        self.page.binding['capabilities']={'features':{'stream_events':True}}; job=str(uuid4()); self.page.job=job
        original=self.client.diagnostic; cursors=[]
        def call(action,payload=None):
            if action=='events':
                after=payload['after']; cursors.append(after)
                items=[dict(seq=i,kind='tool',data={'tool':'read','status':'completed'}) for i in range(1,201)] if after==0 else [dict(seq=201,kind='text',data={'text':'last prefix'})]
                return {'result':dict(items=items,next_seq=200 if after==0 else 201,has_more=after==0,state='TIMED_OUT')}
            return original(action,payload)
        self.client.diagnostic=call
        self.page._job_status(dict(state='TIMED_OUT',result={'reply':'final timeout reply'})); self.assertEqual(self.page.job,job)
        self.pump(lambda:self.page.job is None and not self.page._workers)
        self.assertIn(200,cursors); self.assertEqual(self.page.transcript.toPlainText().count('final timeout reply'),1)

    def test_history_restores_running_and_queue_and_stop_action(self):
        job,queued,thread=str(uuid4()),str(uuid4()),str(uuid4()); original=self.client.diagnostic
        def call(action,payload=None):
            if action=='history': return {'result':{'messages':[],'jobs':[dict(id=queued,state='QUEUED',created_at='2026-01-02'),dict(id=job,state='RUNNING',created_at='2026-01-01')]}}
            return original(action,payload)
        self.client.diagnostic=call; self.page.history_list.addItem('active',thread); self.page.history_list.setCurrentIndex(1); self.page._history(1)
        self.pump(lambda:not self.page._workers); self.assertEqual(self.page.job,job); self.assertEqual(self.page._queued_jobs,[queued]); self.assertTrue(self.page.cancel.isEnabled())
        self.page._cancel(); self.pump(lambda:not self.page._workers)
        self.assertEqual([data['id'] for action,data in self.client.calls if action=='cancel'],[job])

    def test_admin_revision_editor_prefills_and_sends_exact_baseline(self):
        from PySide6.QtWidgets import QLineEdit,QPlainTextEdit,QPushButton
        self.page.binding['capabilities']={'features':{'faq_publish':True}}
        row=dict(id=str(uuid4()),source_feedback_id=str(uuid4()),revision=2,question='Before Q',answer='Before A')
        self.page._revise_faq(row); dialog=self.page._faq_revision_window
        self.assertEqual(dialog.findChild(QLineEdit,'diagnosticFaqQuestion').text(),'Before Q')
        dialog.findChild(QPlainTextEdit,'diagnosticFaqAnswer').setPlainText('After A')
        dialog.findChild(QPushButton,'diagnosticFaqConfirm').click(); self.pump(lambda:not self.page._workers)
        payload=[data for action,data in self.client.calls if action=='faq-publish'][0]
        self.assertEqual(payload['faq_id'],row['id']); self.assertEqual(payload['expected_revision'],2); self.assertEqual(payload['feedback_id'],row['source_feedback_id'])
        self.assertEqual(payload['answer'],'After A')

    def test_executor_faq_admin_actions_hidden_and_403_keeps_diagnostic_login(self):
        from PySide6.QtWidgets import QPushButton
        from factory_hmi.client import GatewayHTTPError
        self.page._faq(); self.pump(lambda:not self.page._workers)
        self.assertTrue(self.page._faq_window.findChild(QPushButton,'diagnosticFaqRevise').isHidden())
        def forbidden(): raise GatewayHTTPError('permission denied',status_code=403)
        self.page._run('faq-publish',forbidden,lambda _:None); self.pump(lambda:not self.page._workers)
        self.assertIsNotNone(self.page.binding); self.assertIn('当前诊断登录',self.page.identity.text())
