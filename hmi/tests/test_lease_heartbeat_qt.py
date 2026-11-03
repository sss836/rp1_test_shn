"""Exercise deadlines and lifecycle without sending HTTP or opening a window."""
from __future__ import annotations

import json
import time
import unittest
from unittest.mock import Mock

from PySide6.QtCore import QCoreApplication, QObject, Signal, QByteArray
from PySide6.QtNetwork import QNetworkReply

from factory_hmi.client import GatewayClient
from factory_hmi.desktop.lease_heartbeat import LeaseHeartbeat


class FakeReply(QObject):
    finished=Signal()
    def __init__(self):
        super().__init__()
        self.aborted=False
        self.status=200
        self.body=b'{"ok":true,"lease":{"generation":7}}'
        self.deleted=False
    def attribute(self,_name):return self.status
    def error(self):return QNetworkReply.OperationCanceledError if self.aborted else QNetworkReply.NoError
    def errorString(self):return 'synthetic request cancelled'
    def readAll(self):return QByteArray(self.body)
    def abort(self):
        self.aborted=True
        self.finished.emit()
    def deleteLater(self):self.deleted=True


class LeaseHeartbeatQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QCoreApplication.instance() or QCoreApplication([])
    def setUp(self):
        self.client=GatewayClient('http://invalid.test',lease_heartbeat_timeout=.04)
        self.client.observe_lease({'lease':{'generation':7}})
        self.parent=QObject()
        self.replies=[]
        self.manager=Mock()
        def post(*_args):
            reply=FakeReply()
            self.replies.append(reply)
            return reply
        self.manager.post.side_effect=post
        self.sender=LeaseHeartbeat(self.client,lambda:True,self.parent,manager=self.manager)
    def tearDown(self):self.sender.stop()

    def test_unexpected_qt_reply_cannot_permanently_latch_pending(self):
        from PySide6.QtGui import QStandardItem
        bad_reply=QStandardItem('synthetic wrapper mismatch')
        self.manager.post.side_effect=[bad_reply,FakeReply()]
        failed=Mock();self.sender.failed.connect(failed)
        self.sender.pulse()
        self.assertIsNone(self.sender.reply)
        self.assertFalse(self.sender.deadline.isActive())
        self.assertTrue(self.sender.retry.isActive())
        failed.assert_called_once()
        self.sender.pulse()
        self.assertEqual(self.manager.post.call_count,2)
        self.assertIsNotNone(self.sender.reply)
        self.sender.reply.finished.emit()
        self.assertIsNone(self.sender.reply)
        self.assertEqual(bad_reply.text(),'synthetic wrapper mismatch')

    def test_post_exception_is_reported_and_gui_can_retry(self):
        self.manager.post.side_effect=[RuntimeError('synthetic post failure'),FakeReply()]
        failed=Mock();self.sender.failed.connect(failed)
        self.sender.pulse()
        failed.assert_called_once()
        self.assertIsNone(self.sender.reply)
        self.assertTrue(self.sender.retry.isActive())
        self.sender.pulse()
        self.assertEqual(self.manager.post.call_count,2)
        self.sender.reply.finished.emit()
        self.assertIsNone(self.sender.reply)

    def test_abort_without_finished_signal_cannot_latch_pending(self):
        self.sender.pulse();reply=self.sender.reply
        reply.abort=lambda:None
        self.sender._timeout()
        self.assertIsNone(self.sender.reply)
        self.assertTrue(self.sender.retry.isActive())
        self.assertTrue(reply.deleted)

    def test_only_one_inflight_request_with_identity_and_generation(self):
        self.sender.pulse()
        self.sender.pulse()
        self.manager.post.assert_called_once()
        req=self.manager.post.call_args.args[0]
        self.assertEqual(bytes(req.rawHeader('X-RP1-Lease-Generation')),b'7')
        self.assertEqual(bytes(req.rawHeader('X-RP1-Client-ID')),self.client.client_id.encode())
        self.replies[0].finished.emit()
        self.assertIsNone(self.sender.reply)
        self.assertTrue(self.replies[0].deleted)

    def test_deadline_aborts_then_schedules_bounded_retry(self):
        self.sender.pulse()
        time.sleep(.06)
        self.app.processEvents()
        self.assertTrue(self.replies[0].aborted)
        self.assertIsNone(self.sender.reply)
        self.assertTrue(self.sender.retry.isActive())
        self.assertEqual(self.sender.retry.interval(),150)
        self.sender.stop()
        self.assertFalse(self.sender.retry.isActive())

    def test_late_cancelled_reply_never_clears_or_renews_a_new_request(self):
        renewed=Mock();self.sender.renewed.connect(renewed)
        self.sender.pulse();old=self.sender.reply
        old.abort=lambda:None
        self.sender._timeout()
        self.sender.pulse();current=self.sender.reply
        self.assertIsNot(current,old)
        old.finished.emit()
        renewed.assert_not_called()
        self.assertIs(self.sender.reply,current)
        current.finished.emit()
        renewed.assert_called_once()
        self.assertIsNone(self.sender.reply)

    def test_old_generation_reply_does_not_update_new_lease(self):
        renewed=Mock()
        self.sender.renewed.connect(renewed)
        self.sender.pulse()
        self.client.observe_lease({'lease':{'generation':8}})
        self.replies[0].finished.emit()
        renewed.assert_not_called()
        self.assertEqual(self.client.lease_generation,8)

    def test_close_aborts_and_never_restarts_request(self):
        self.sender.pulse()
        reply=self.replies[0]
        self.sender.stop()
        self.sender.pulse()
        self.app.processEvents()
        self.assertTrue(reply.aborted)
        self.assertFalse(self.sender.retry.isActive())
        self.manager.post.assert_called_once()

    def test_rejected_lease_is_not_automatically_reclaimed(self):
        failed=Mock()
        self.sender.failed.connect(failed)
        self.sender.pulse()
        self.replies[0].status=409
        self.replies[0].finished.emit()
        failed.assert_called_once()
        self.assertEqual(failed.call_args.args[0].status,409)
        self.assertFalse(self.sender.retry.isActive())

    def test_transient_http_failure_retries_without_reclaiming(self):
        self.sender.pulse()
        self.replies[0].status=503
        self.replies[0].finished.emit()
        self.assertTrue(self.sender.retry.isActive())
        self.assertEqual(self.sender.retry.interval(),150)


if __name__=='__main__':unittest.main()
