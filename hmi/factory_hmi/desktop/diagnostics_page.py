"""In-window assistant; its private worker pool never runs control/lease I/O."""
from __future__ import annotations

import html
import base64
import hashlib
import re
from urllib.parse import urlparse
import os
import stat
import threading
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot, QByteArray, QBuffer, QIODevice, Qt, QMimeData, QUrl
from PySide6.QtGui import QPixmap, QTextDocument, QDesktopServices
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextBrowser, QPlainTextEdit, QPushButton, QFileDialog, QApplication, QComboBox, QTreeWidget, QTreeWidgetItem, QFrame, QSplitter, QDialog, QDialogButtonBox, QLineEdit, QInputDialog, QMessageBox, QMenu, QCheckBox, QHeaderView


STYLE = """
QWidget#diagnosticsPage { background: #f3f6fa; color: #172b40; }
QWidget#diagnosticsPage * { font-family: 'Noto Sans CJK SC', 'Microsoft YaHei UI', sans-serif; font-size: 13px; }
QLabel#diagnosticTitle { font-size: 24px; font-weight: 700; color: #12334d; }
QLabel#diagnosticSubtitle, QLabel#diagnosticIdentity { color: #687d90; font-size: 12px; }
QLabel#diagnosticLogo { background: #12334d; color: white; border-radius: 14px; font-size: 19px; font-weight: 700; }
QLabel#diagnosticBadge { background: #e4edf4; color: #49677f; border: 1px solid #d6e2ed; border-radius: 12px; padding: 5px 12px; font-size: 12px; }
QLabel#diagnosticBadge[linked='true'] { background: #e4f4ee; color: #12735a; border-color: #c6e7d9; }
QLabel#diagnosticSection { font-size: 14px; font-weight: 700; color: #183b56; }
QFrame#diagnosticCard { background: #fff; border: 1px solid #dce5ee; border-radius: 12px; }
QFrame#diagnosticCard QLabel { background: transparent; border: none; }
QTextBrowser#diagnosticTranscript { background: #fff; border: none; padding: 8px; color: #243e53; font-size: 14px; }
QPlainTextEdit#diagnosticMessage { background: #f8fafc; border: 1px solid #e6edf3; border-radius: 8px; padding: 10px; font-size: 14px; selection-background-color: #d7e9f5; }
QPlainTextEdit#diagnosticMessage:focus { border: 1px solid #9ebbd0; }
QComboBox#diagnosticHistory { min-height: 34px; padding: 0 12px; border: 1px solid #dce5ee; border-radius: 7px; background: white; color: #405e76; }
QPushButton#diagnosticButton { min-height: 34px; padding: 0 13px; border: 1px solid #dce5ee; border-radius: 7px; background: white; color: #36566e; }
QPushButton#diagnosticButton:hover { background: #edf4f9; border-color: #b5cada; }
QPushButton#diagnosticSend { min-height: 36px; min-width: 84px; padding: 0 16px; background: #123f5b; color: white; border: none; border-radius: 7px; font-weight: 700; }
QPushButton#diagnosticSend:hover { background: #1b5577; }
QPushButton#diagnosticButton:disabled, QPushButton#diagnosticSend:disabled { background: #f0f4f7; color: #9dabb8; border-color: #e6edf3; }
QTreeWidget#diagnosticEvidence { border: none; background: white; alternate-background-color: #f7fafc; outline: none; color: #3d596e; }
QTreeWidget#diagnosticEvidence::item { min-height: 28px; padding: 2px; }
QTreeWidget#diagnosticEvidence QHeaderView::section { background: #f1f6fa; color: #60788b; border: none; padding: 8px 5px; }
QSplitter::handle { background: transparent; width: 12px; }
"""


def safe_markdown(body):
    # Disable model HTML in Qt's Markdown parser; remote/file images are
    # additionally blocked by _Transcript.loadResource(). Links need a click.
    document = QTextDocument()
    document.setMarkdown(str(body), QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
    return document.toHtml()


class _Transcript(QTextBrowser):
    def loadResource(self, resource_type, url):
        return None  # no model-triggered network requests or local file reads


class _Composer(QPlainTextEdit):
    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setAcceptDrops(True)

    def canInsertFromMimeData(self, source):
        return source.hasUrls() or source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        if source.hasUrls():
            self.page._add_attachments([url.toLocalFile() for url in source.urls() if url.isLocalFile()])
        elif source.hasImage():
            self.page._paste()
        else:
            super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            self.insertFromMimeData(event.mimeData()); event.acceptProposedAction()
        else: super().dropEvent(event)


class _Signals(QObject):
    done = Signal(object, object)


class _Work(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.signals = _Signals()
        self.fn = fn

    @Slot()
    def run(self):
        try:
            result, error = self.fn(), None
        except Exception as exc:
            result, error = None, exc
        self.signals.done.emit(result, error)


class _SubmissionCancelled(Exception):
    pass


class DiagnosticsPage(QWidget):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.setObjectName('diagnosticsPage')
        self.setStyleSheet(STYLE)
        self.client = client
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self._workers, self._pending = set(), set()
        self.epoch, self._ticks = 0, 0
        self.binding = self.job = self.thread = self.image = None
        self.attachments = []
        self.image_name = "diagnostic-screenshot.png"
        self.submission = None
        self.link_requested = False
        self._permission_prompted = set()
        self._permission_dialogs = []
        self._has_messages = False
        self._drafts, self.references, self._queued_jobs = {}, [], []
        self._event_seq, self._event_round, self.last_job = 0, None, None
        self._terminal_value = None
        self._events_backlog = False
        self._last_answer, self._draft_restoring = '', False
        self._long_answers, self._steering = {}, None
        self._unprocessed_inputs = []
        self._permission_stage_seen = set()
        self._draft_dirty, self._feedback = False, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        logo = QLabel('AI')
        logo.setObjectName('diagnosticLogo')
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setFixedSize(48, 48)
        header.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel('共享诊断助手')
        title.setObjectName('diagnosticTitle')
        subtitle = QLabel('结合现场信息，找到问题的下一步')
        subtitle.setObjectName('diagnosticSubtitle')
        titles.addWidget(title)
        titles.addWidget(subtitle)
        header.addLayout(titles)
        header.addStretch()
        self.badge = QLabel('等待登录')
        self.badge.setObjectName('diagnosticBadge')
        header.addWidget(self.badge)
        layout.addLayout(header)
        self.identity = QLabel("登录测试平台后自动关联；对话不会自动修复或操作设备。")
        self.identity.setObjectName('diagnosticIdentity')
        self.identity.setTextFormat(Qt.TextFormat.PlainText)
        self.identity.setWordWrap(True)
        layout.addWidget(self.identity)
        self.history_list = QComboBox()
        self.history_list.setObjectName('diagnosticHistory')
        self.history_list.setMaximumWidth(350)
        self.history_list.addItem("新对话", None)
        self.history_list.activated.connect(self._history)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.history_list, 1)
        new_chat = QPushButton('＋ 新对话')
        new_chat.setObjectName('diagnosticButton')
        new_chat.clicked.connect(self._new_chat)
        toolbar.addWidget(new_chat)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText('搜索历史')
        self.history_search.setMaximumWidth(150)
        self.history_search.returnPressed.connect(self._search_threads)
        toolbar.addWidget(self.history_search)
        self.history_menu = QPushButton('管理')
        self.history_menu.setObjectName('diagnosticButton')
        self.history_menu.clicked.connect(self._manage_thread)
        toolbar.addWidget(self.history_menu)
        toolbar.addStretch()
        note = QLabel('聊天/附件保留30天')
        note.setObjectName('diagnosticSubtitle')
        toolbar.addWidget(note)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        conversation = QFrame()
        conversation.setObjectName('diagnosticCard')
        conversation_layout = QVBoxLayout(conversation)
        conversation_layout.setContentsMargins(16, 14, 16, 10)
        conversation_label = QLabel('诊断对话')
        conversation_label.setObjectName('diagnosticSection')
        conversation_layout.addWidget(conversation_label)
        self.transcript = _Transcript()
        self.transcript.setObjectName('diagnosticTranscript')
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self._open_link)
        conversation_layout.addWidget(self.transcript, 1)
        self.live_output = _Transcript()
        self.live_output.setMaximumHeight(170)
        self.live_output.setOpenLinks(False)
        self.live_output.anchorClicked.connect(self._open_link)
        self.live_output.hide()
        conversation_layout.addWidget(self.live_output)
        self.progress = QLabel('')
        self.progress.setTextFormat(Qt.TextFormat.PlainText)
        self.progress.setWordWrap(True)
        conversation_layout.addWidget(self.progress)
        self.permission_list = QTreeWidget()
        self.permission_list.setObjectName('diagnosticPermissionList')
        self.permission_list.setHeaderLabels(['读取资料的申请','状态','操作'])
        self.permission_list.header().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.permission_list.header().setSectionResizeMode(1,QHeaderView.ResizeMode.ResizeToContents)
        self.permission_list.header().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.permission_list.setMaximumHeight(150)
        self.permission_list.hide()
        conversation_layout.addWidget(self.permission_list)
        self.permission_resume = QPushButton('已有资料，继续检查')
        self.permission_resume.setObjectName('diagnosticButton')
        self.permission_resume.clicked.connect(self._resume_local_diagnosis)
        self.permission_resume.hide()
        conversation_layout.addWidget(self.permission_resume)
        actions = QHBoxLayout()
        for title, action in [('复制答复', self._copy_answer), ('反馈问题', self._give_feedback), ('常见问题', self._faq), ('下载报告', self._download_report), ('查看修复建议', self._repair_cards)]:
            button = QPushButton(title); button.setObjectName('diagnosticButton'); button.clicked.connect(action); actions.addWidget(button)
        conversation_layout.addLayout(actions)
        splitter.addWidget(conversation)
        self.evidence_card = QFrame()
        self.evidence_card.setObjectName('diagnosticCard')
        evidence_layout = QVBoxLayout(self.evidence_card)
        evidence_layout.setContentsMargins(12, 14, 12, 10)
        evidence_label = QLabel('检查了哪些资料')
        evidence_label.setObjectName('diagnosticSection')
        evidence_layout.addWidget(evidence_label)
        self.evidence_note = QLabel('这里会列出检查过的资料\n点开可以查看详细结果')
        self.evidence_note.setObjectName('diagnosticSubtitle')
        self.evidence_note.setWordWrap(True)
        evidence_layout.addWidget(self.evidence_note)
        self.evidence = QTreeWidget()
        self.evidence.setObjectName('diagnosticEvidence')
        self.evidence.setAlternatingRowColors(True)
        self.evidence.setHeaderLabels(["资料／检查项", "内容"])
        self.evidence.setColumnWidth(0, 115)
        self.evidence.itemDoubleClicked.connect(self._history_attachment_preview)
        self.evidence.hide()
        evidence_layout.addWidget(self.evidence, 1)
        splitter.addWidget(self.evidence_card)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 280])
        layout.addWidget(splitter, 1)
        composer = QFrame()
        composer.setObjectName('diagnosticCard')
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(14, 12, 14, 12)
        composer_layout.setSpacing(10)
        self.preview = QLabel()
        self.preview.setMaximumHeight(100)
        self.preview.mousePressEvent = lambda event: self._preview_image()
        self.preview.hide()
        composer_layout.addWidget(self.preview)
        self.attachment_list = QTreeWidget()
        self.attachment_list.setObjectName('diagnosticAttachments')
        self.attachment_list.setHeaderLabels(["待发送附件（最多3个，可双击预览）", "大小 / 状态", "操作"])
        self.attachment_list.setMaximumHeight(115)
        self.attachment_list.setColumnWidth(0,380)
        self.attachment_list.setColumnWidth(1,240)
        self.attachment_list.hide()
        self.attachment_list.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.attachment_list.itemDoubleClicked.connect(self._attachment_preview)
        composer_layout.addWidget(self.attachment_list)
        self.reference_label = QLabel()
        self.reference_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reference_label.setWordWrap(True)
        self.reference_label.hide()
        composer_layout.addWidget(self.reference_label)
        self.restore_inputs = QPushButton('将这些补充放回输入框')
        self.restore_inputs.setObjectName('diagnosticButton')
        self.restore_inputs.clicked.connect(self._restore_unprocessed_inputs)
        self.restore_inputs.hide()
        composer_layout.addWidget(self.restore_inputs)
        self.message = _Composer(self)
        self.message.setObjectName('diagnosticMessage')
        self.message.setPlaceholderText("输入问题或仅发送附件；CSV、压缩包及其他文件均在发送时才上传，原文件不会修改。")
        self.message.setFixedHeight(96)
        self.message.textChanged.connect(self._draft_changed)
        composer_layout.addWidget(self.message)
        row = QHBoxLayout()
        self.choose, self.paste, self.remove = QPushButton("选择截图"), QPushButton("粘贴截图"), QPushButton("移除截图")
        self.add_attachment = QPushButton("＋ 附件")
        self.remove_attachment = QPushButton("移除")
        self.send, self.cancel = QPushButton("发送"), QPushButton("取消任务")
        self.choose.clicked.connect(self._choose)
        self.paste.clicked.connect(self._paste)
        self.remove.clicked.connect(lambda: self._set_image(None))
        self.add_attachment.clicked.connect(self._choose_attachments)
        self.remove_attachment.clicked.connect(self._remove_attachments)
        self.send.clicked.connect(self._send)
        self.cancel.clicked.connect(self._cancel)
        for button in (self.choose, self.paste, self.remove, self.add_attachment, self.remove_attachment, self.send, self.cancel):
            button.setObjectName('diagnosticButton')
        self.send.setObjectName('diagnosticSend')
        self.choose.hide(); self.paste.hide(); self.remove.hide()
        row.addWidget(self.add_attachment)
        row.addWidget(self.remove_attachment)
        self.reference_button = QPushButton('@ 引用')
        self.reference_button.setObjectName('diagnosticButton')
        self.reference_button.clicked.connect(self._reference_dialog)
        row.addWidget(self.reference_button)
        self.send_mode = QComboBox()
        self.send_mode.addItem('下一条排队', 'queue')
        self.send_mode.addItem('补充当前诊断', 'steer')
        self.send_mode.setToolTip('补充仅用于诊断，不增加预算或授予业务写入；附件请使用排队。')
        row.addWidget(self.send_mode)
        row.addStretch()
        row.addWidget(self.cancel)
        row.addWidget(self.send)
        composer_layout.addLayout(row)
        layout.addWidget(composer)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.upload_timer = QTimer(self)
        self.upload_timer.setInterval(150)
        self.upload_timer.timeout.connect(self._upload_watch)
        self.upload_timer.start()
        self._welcome()
        self._controls()

    def _welcome(self):
        self._has_messages = False
        self.transcript.setHtml('<p style="color:#7590a5; font-size:13px">现场诊断 · 证据可追溯</p>'
            '<br><p style="color:#193e58; font-size:20px"><b>先从一个具体问题开始</b></p>'
            '<p style="color:#6c8396; line-height:160%">描述报错或遇到的现象，也可以添加截图、CSV或压缩包等文件。<br>助手会结合可用资料回答，并列出依据与尚待核实的部分。附件不作为测试数据正式归档。</p>')

    def _new_chat(self):
        if self.job or self.submission:
            return
        self.history_list.setCurrentIndex(0)
        self._history(0)

    def revoke(self):
        if self.submission:
            self.submission['cancel'].set()
        self.epoch += 1
        for dialog in list(self._permission_dialogs): dialog.reject()
        self._permission_prompted.clear()
        for attribute in ('_reference_window','_faq_window','_repair_window','_image_window','_long_answer_window','_feedback_window','_faq_revision_window'):
            dialog = getattr(self, attribute, None)
            if dialog is not None:
                try: dialog.reject()
                except RuntimeError: pass
                setattr(self, attribute, None)
        self._drafts.clear(); self.references.clear(); self._queued_jobs.clear()
        self._draft_dirty = False
        self.reference_label.clear()
        self.live_output.clear(); self.live_output.hide(); self.progress.clear()
        self.last_job, self._last_answer, self._feedback = None, '', None
        self._long_answers.clear(); self._steering = None
        self._unprocessed_inputs.clear(); self.restore_inputs.hide()
        self._permission_stage_seen.clear(); self.permission_list.clear(); self.permission_list.hide(); self.permission_resume.hide()
        self._event_seq, self._event_round = 0, None
        self._terminal_value = None
        self._events_backlog = False
        self.link_requested = False
        self.binding = self.job = self.thread = None
        self.submission = None
        self._welcome()
        self.evidence.clear()
        self.evidence.hide()
        self.evidence_note.show()
        self.message.clear()
        self.history_list.clear()
        self.history_list.addItem("新对话", None)
        self._set_image(None)
        self.attachments.clear()
        self._refresh_attachments()
        self.identity.setText("诊断关联已撤销，请在当前窗口登录平台。")
        self.badge.setText('未关联')
        self.badge.setProperty('linked', False)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)
        self._controls()

    def activate(self):
        self.revoke()
        self.link_requested = True
        self._status()

    def stop(self):
        self.timer.stop()
        self.upload_timer.stop()
        self.revoke()

    def _controls(self):
        enabled = bool(self.binding)
        self.send.setEnabled(enabled and self.job != 'submitting')
        self.send.setText('重试确认' if self.submission else '排队 / 补充' if self.job else '发送')
        self.message.setReadOnly(not enabled)
        self.history_list.setEnabled(enabled and not self.job and not self.submission)
        self.history_menu.setEnabled(enabled and not self.job and not self.submission)
        self.history_search.setEnabled(enabled)
        self.send_mode.setVisible(bool(self.job) and self.job != 'submitting')
        self.cancel.setEnabled(bool(self.job or self.submission))
        self.attachment_list.setEnabled(enabled and not self.submission)
        for button in (self.choose, self.paste, self.remove, self.add_attachment, self.remove_attachment, self.reference_button):
            button.setEnabled(enabled and not self.submission)

    def _run(self, key, fn, success):
        epoch, marker = self.epoch, (self.epoch, key)
        if marker in self._pending or len(self._workers) >= 2:
            return False
        worker = _Work(fn)
        self._workers.add(worker)
        self._pending.add(marker)
        def done(value, error):
            self._workers.discard(worker)
            self._pending.discard(marker)
            if epoch != self.epoch:
                return
            if error is not None:
                if (isinstance(error, _SubmissionCancelled) or
                        (key == 'submit' and self.submission and self.submission['phase'] == 'upload'
                         and self.submission['cancel'].is_set())):
                    if self.job == "submitting": self.job = None
                    self._restore_cancelled_question(self.submission)
                    self.submission = None
                    self.identity.setText("已取消附件发送；原文件未更改，待发送列表已保留。")
                    self._controls()
                elif getattr(error, "status_code", None) in (401, 403):
                    if key in {"faq", "faq-publish"} and getattr(error,"status_code",None) == 403:
                        self.identity.setText("此账号无共享FAQ发布权限；当前诊断登录与个人对话保留。")
                        return
                    self.revoke()
                    if key == 'job' or key.startswith('local-'):
                        self.identity.setText('原诊断登录已失效或权限已变化，已停止读取资料。请重新登录，再发起新的诊断问题；新的登录不能接管旧任务。')
                else:
                    detail = getattr(error, 'detail', None)
                    if key == 'repair-apply' and isinstance(detail, dict) and detail.get('code') == 'repair_receipt_pending':
                        self.identity.setText('修改已保存，结果还没同步到平台；重新登录原账号后会继续同步，不会再次修改文件。' if detail.get('saved') else '修改没有保存成功；重新登录原账号后会同步检查结果。')
                        self._controls()
                        return
                    if key == "submit":
                        if self.job == 'submitting': self.job = None
                        if self.submission and self.submission['entries']:
                            for entry in self.submission['entries']:
                                if entry.get('state') != '已上传': entry['state'] = '失败，重试确认可继续'
                        self._refresh_attachments()
                    self.identity.setText(("上传或发送未完成，点击“重试确认”将沿用原请求编号及已上传附件。\n" + str(error)[:300]) if key == 'submit' else "诊断暂不可用，请稍后重试；控制通道独立运行。")
                    self._controls()
                return
            success(value.get("result", value) if isinstance(value, dict) else value)
        worker.signals.done.connect(done)
        self.pool.start(worker)
        return True

    def _status(self):
        def received(data):
            if self.binding and self.binding["generation"] != data["generation"]:
                self.revoke()
                return
            first, self.binding = self.binding is None, data
            hmi = data["hmi"]
            self.identity.setText(f"账号 {data['operator']} · 工位 {hmi['station'] or '未设置'} · 实例 {hmi['installation_id']} · 运行代码 {hmi['running_version'] or '未知'}")
            self.identity.setToolTip(self.identity.text())
            self.identity.setText(f"{data['operator']}  /  工位 {hmi['station'] or '未设置'}  /  当前使用版本 {hmi['running_version'] or '未知'}")
            settlement = data.get('repair_settlement') or {}
            if settlement.get('pending_saved'):
                self.identity.setText(self.identity.text() + f" · 修改已保存，结果还没同步到平台（{settlement['pending_saved']}）")
            elif settlement.get('pending_other'):
                self.identity.setText(self.identity.text() + ' · 上次修改可能已保存，正在等待核对；不会再次修改文件')
            elif settlement.get('delivered'):
                self.identity.setText(self.identity.text() + ' · 之前的修改结果已同步，没有重复修改文件')
            self.badge.setText('已关联')
            self.badge.setProperty('linked', True)
            self.badge.style().unpolish(self.badge)
            self.badge.style().polish(self.badge)
            self._controls()
            if first:
                self._load_draft()
                self._run("threads", lambda: self.client.diagnostic("threads", {"generation": data["generation"]}), self._initial_threads)
        self._run("status", lambda: self.client.diagnostic("status"), received)

    def _threads(self, rows):
        for row in rows:
            self.history_list.addItem(str(row.get("title") or "诊断对话") + " · " + str(row["updated_at"])[:16], row["id"])

    def _history(self, _index):
        if not self.binding or self.job or self.submission:
            return
        self._save_draft()
        self.thread = self.history_list.currentData()
        self._restore_draft(load=False)
        self.submission = None
        self.last_job, self._last_answer = None, ""
        self._unprocessed_inputs.clear(); self.restore_inputs.hide()
        self._controls()
        self._welcome()
        self.evidence.clear()
        self.evidence.hide()
        self.evidence_note.show()
        if self.thread:
            data = {"generation": self.binding["generation"], "id": self.thread}
            thread = self.thread
            def loaded(value):
                if thread != self.thread: return
                for message in value['messages']:
                    self._display(message['role'], message['body'], message.get('cards'))
                    if message['role'] == 'assistant': self.last_job = message.get('job_id')
                    for attachment in message.get('attachments', []):
                        node = QTreeWidgetItem(self.evidence, [attachment.get('name','附件'), f"{attachment.get('size',0)} bytes · SHA256 {attachment.get('sha256','未知')}"])
                        node.setData(0, Qt.ItemDataRole.UserRole, attachment)
                        self.evidence.show(); self.evidence_note.hide()
                self._restore_active_jobs(value.get('jobs') or [])
            if not self._run("history", lambda: self.client.diagnostic("history", data), loaded):
                QTimer.singleShot(150, lambda: self._history_retry(thread, data, loaded))
        self._load_draft()

    def _tick(self):
        self._ticks += 1
        if self.submission: self._refresh_attachments()
        if self.link_requested and not self.submission and (not self.binding or self._ticks % 5 == 0):
            self._status()
        if self._draft_dirty and self.binding and not self.submission:
            self._persist_draft()
        if self.job and self.job != "submitting" and self.binding and not (self.submission and self.submission["phase"] == "upload"):
            data = {"generation": self.binding["generation"], "id": self.job}
            job = self.job
            if self._terminal_value is None:
                self._run("poll", lambda: self.client.diagnostic("job", data), lambda value: self._job_status(value) if self.job == job else None)
            self._request_events()

    def _display(self, role, body, cards=None):
        if not self._has_messages:
            self.transcript.clear()
            self._has_messages = True
        color, background = ('#59758c', '#f0f5fa') if role == 'user' else ('#13735d', '#eaf6f0')
        label = '你 · 当前问题' if role == 'user' else '诊断助手 · 分析结果'
        display_body = str(body)
        if len(display_body.splitlines()) > 100:
            identifier = uuid4().hex
            self._long_answers[identifier] = display_body
            display_body = '\n'.join(display_body.splitlines()[:80]) + f'\n\n[展开完整答复（{len(str(body).splitlines())}行）](diagnostic-log://{identifier})'
        content = safe_markdown(display_body)
        if role == "assistant": self._last_answer = str(body)
        self.transcript.append(f'<p style="color:{color};font-size:12px"><b>{label}</b></p>'
                               f'<table width="100%" cellspacing="0" cellpadding="12" bgcolor="{background}"><tr><td style="color:#233f54;font-size:14px">{content}</td></tr></table><br>')
        for card in cards or []:
            if card.get('type') == 'diagnosis_preparation':
                self.transcript.append(safe_markdown(self._preparation_summary(card.get('data') or {})))
            if card.get('type') == 'unprocessed_input':
                known = {item['request_key'] for item in self._unprocessed_inputs}
                for item in (card.get('data') or {}).get('items', card.get('items', [])):
                    if isinstance(item,dict) and item.get('request_key') and isinstance(item.get('message'),str) and item['request_key'] not in known:
                        self._unprocessed_inputs.append(dict(request_key=item['request_key'],message=item['message']))
                        known.add(item['request_key'])
                if self._unprocessed_inputs:
                    self.transcript.append('<p><b>以下补充未用于本次诊断</b>：任务结束前还没处理到这些补充，可以放回输入框，下次再问。</p>' + safe_markdown('\n\n'.join(item['message'] for item in self._unprocessed_inputs)))
                    self.restore_inputs.show()
            title, source = str(card.get('title', '证据')), str(card.get('source', '未提供来源'))
            if card.get('type') == 'diagnosis_preparation': source = '本次附件及已关联的上位机'
            self.transcript.append(f"<b>{html.escape(title)}</b> · 来源 {html.escape(source)} · {html.escape(str(card.get('updated_at', '时间未提供')))}")
            root = QTreeWidgetItem(self.evidence, [title, source])
            self._evidence_fields(root, card)
            root.setExpanded(False)
            self.evidence.show()
            self.evidence_note.hide()
            if self._partial_evidence(card):
                self.evidence_note.setText('目前只查看了部分内容，后面的数据还没检查；完整检查后才能判断整个文件。')
                self.evidence_note.show()

    def _evidence_fields(self, parent, value):
        labels = {'source':'来源','updated_at':'更新时间','unit':'单位','scope':'范围','range':'区间',
                  'data':'查询结果','items':'条目','station_name':'工位','state':'状态','online':'在线',
                  'last_seen_at':'最后在线时间','installation_id':'实例','title':'标题','type':'类型'}
        rows = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
        for key, item in rows:
            branch = isinstance(item, (dict, list))
            node = QTreeWidgetItem(parent, [labels.get(str(key), str(key)), '' if branch else str(item)])
            if branch:
                self._evidence_fields(node, item)

    @staticmethod
    def _preparation_summary(data):
        """Business-facing summary; hashes and complete receipts stay in the collapsed tree."""
        phase = data.get('phase')
        progress = ('已经允许查看，正在等候原上位机返回资料；你不需要重复确认。' if data.get('status')=='waiting_for_data' else
                    '需要你允许读取资料，之后会继续检查。' if phase == 'awaiting_permission' else
                    '正在逐项检查资料，下面是目前的发现。' if data.get('continuation') else
                    '本轮资料检查已结束。')
        lines = ['### 检查进展', progress]
        for evidence in (data.get('evidence') or [])[:12]:
            item = evidence.get('data') or {}
            detail = item.get('data') if isinstance(item.get('data'),dict) else item
            path = str(item.get('requested_path') or item.get('path') or evidence.get('title') or '本次资料')
            if len(path) > 240: path = path[:237] + '…'
            if 'rows_analyzed' in detail or isinstance(detail.get('range'),dict):
                rows = detail.get('rows_analyzed', (detail.get('range') or {}).get('rows_analyzed'))
                complete = item.get('complete') is True or detail.get('full_file_statistics') is True
                text = f'{path}：已检查{rows}行数据' if rows is not None else f'{path}：已检查一部分数据'
                text += '（已到文件末尾）。' if complete else '（本次已读范围，仍有内容待检查）。'
                temporal = detail.get('time_analysis') if isinstance(detail.get('time_analysis'),dict) else detail
                gaps = temporal.get('gap_count')
                if gaps is not None: text += f' 发现{gaps}处采样间隔异常；是否漏采还需结合采集设置核实。'
                metrics = detail.get('numeric_statistics') or detail.get('columns') or []
                for metric in metrics[:4]:
                    if not isinstance(metric,dict): continue
                    name = metric.get('column',metric.get('name'))
                    low, high = metric.get('min',metric.get('minimum')), metric.get('max',metric.get('maximum'))
                    if name is not None and low is not None and high is not None: text += f' {name}范围：{low}～{high}。'
                if temporal.get('time_basis') in {'numeric_unit_unknown','wall_clock_without_timezone'}:
                    text += ' 时间单位或时区尚未确定，暂不能与其他来源直接对齐。'
                elif temporal.get('time_basis') == 'relative_origin_unknown':
                    text += ' 时间起点尚未确定，暂不能与其他来源直接对齐。'
                elif temporal.get('time_basis') == 'mixed_time_bases':
                    text += ' 文件中混有不同的时间口径，不能直接对齐或合并解释。'
                elif 'time_column' in temporal and temporal['time_column'] is None:
                    text += ' 没有识别到时间列，暂不能核对采样时序。'
                unprocessed = detail.get('unprocessed_numeric_column_indices') or detail.get('unprocessed_column_indices') or []
                if unprocessed: text += f' 还有{len(unprocessed)}列未做数值统计，当前统计不代表全部列。'
                if any(isinstance(metric,dict) and metric.get('numeric_overflow') for metric in metrics):
                    text += ' 部分数值过大，均值或波动量未能可靠计算；没有把未知值算成零。'
                lines.append('- ' + text)
            elif 'lines_analyzed' in detail:
                text = f"{path}：已检查{detail['lines_analyzed']}行日志"
                if 'match_count' in detail: text += f"，其中{detail['match_count']}处包含报错或异常关键词"
                text += '。' if item.get('complete') is True else '；后面的日志还没检查。'
                matches = detail.get('matches') or []
                if matches: text += ' 例如：' + str(matches[0].get('text') or '')[:180]
                lines.append('- ' + text)
            elif item.get('operation') == 'discover':
                lines.append(f"- 已在允许读取的目录中找到{item.get('candidate_count',len(item.get('candidates') or []))}份候选资料；会继续核对与当前问题有关的文件。")
        issues = data.get('issues') or []
        if issues or data.get('unselected_file_count') or data.get('evidence_complete') is not True:
            lines += ['', '**还缺什么**']
            for issue in issues[:8]:
                if isinstance(issue,dict):
                    lines.append('- ' + str(issue.get('reason') or '此项资料尚未检查') + ('：' + str(issue['path']) if issue.get('path') else ''))
            if data.get('unselected_file_count'):
                lines.append(f"- 还有{data['unselected_file_count']}份候选文件未检查；目前结论只适用于已读资料。")
            if not issues and not data.get('unselected_file_count'): lines.append('- 资料还没查齐，不能据此判断整个文件或完整问题原因。')
        lines += ['', '这些是资料中的实际观察，暂未确认完整原因。完整资料和检查范围可在“检查了哪些资料”中展开查看。']
        return '\n'.join(lines)

    def _send(self):
        if not self.binding or self.job == 'submitting':
            return
        if self.job and self._queued_jobs and not self.submission:
            self.identity.setText('已有一条排队问题；请保留草稿并等待或停止当前任务。'); return
        if self.job and not self.submission and self.send_mode.currentData() == 'steer':
            self._steer(); return
        message = self.message.toPlainText().strip()
        if not self.submission and not message and not self.image and not self.attachments:
            return
        message = message or "请分析所附文件。"
        if len(message) > 8000:
            self.identity.setText("问题最多8000字，请缩短后发送。")
            return
        if self.submission:
            submission = self.submission
        else:
            data = {"generation": self.binding["generation"], "request_key": str(uuid4()), "message": message,
                    "attachments": []}
            if self.thread:
                data["thread_id"] = self.thread
            if self.job: data['after_job_id'] = self._queued_jobs[-1] if self._queued_jobs else self.job
            if self.references: data['references'] = list(self.references)
            entries = ([{"raw": self.image[0], "mime": self.image[1], "filename": self.image_name}]
                       if self.image else []) + [dict(item) for item in self.attachments]
            if len(entries) > 3:
                self.identity.setText("附件和截图合计最多3个。")
                return
            submission = dict(data=data, entries=entries, cancel=threading.Event(), phase='upload', cancel_job=False)
            self.submission = submission
            self.message.clear()
        message = submission['data']['message']
        data = submission['data']
        def submit():
            for index, entry in enumerate(submission['entries']):
                if submission['cancel'].is_set():
                    raise _SubmissionCancelled()
                if entry.get('uploaded_id') or entry.get('cancelled'):
                    continue
                entry['state'] = '读取 / 上传中'
                raw = self._attachment_bytes(entry)
                if entry.get('cancelled'): continue
                entry['upload_id'] = str(uuid4())
                entry.pop('bytes_sent', None); entry.pop('total_bytes', None)
                try:
                    if callable(getattr(self.client, 'diagnostic_attachment_progress', None)):
                        entry['tracked_upload'] = True
                        uploaded = self.client.diagnostic_attachment_progress(raw, entry['filename'], data['generation'], entry['upload_id'])
                    elif 'mime' in entry:
                        # Older mocked/embedded clients retain their original signature.
                        if entry['filename'] == ('diagnostic-screenshot.png' if entry['mime'] == 'image/png' else 'diagnostic-screenshot.jpg'):
                            uploaded = self.client.diagnostic_screenshot(raw, entry['mime'], data['generation'])
                        else:
                            uploaded = self.client.diagnostic_attachment(raw, entry['filename'], data['generation'])
                    else:
                        uploaded = self.client.diagnostic_attachment(raw, entry['filename'], data['generation'])
                except Exception as exc:
                    if entry.get('cancelled') and getattr(exc, 'status_code', None) not in {401,403}:
                        entry['state'] = '已取消，不绑定本任务'
                        continue
                    raise
                # Keep partial receipts only within this frozen account/request.
                entry['uploaded_id'] = uploaded.get('result', uploaded)['id']
                if not entry.get('cancelled'):
                    data['attachments'].append(entry['uploaded_id'])
                    entry['state'] = '已上传'
                else: entry['state'] = '已取消，不绑定本任务'
            if submission['cancel'].is_set():
                raise _SubmissionCancelled()
            submission['phase'] = 'submit'
            return self.client.diagnostic("submit", data)
        def sent(value):
            self.thread = value['thread_id']
            if data.get('after_job_id'):
                self._queued_jobs.append(value['id'])
                self.progress.setText('下一条问题已记下，当前问题处理完后再继续。')
            else:
                self.job = value['id']
                self._event_seq, self._event_round = 0, None
            self.submission = None
            self._display("user", message)
            self.references.clear(); self.reference_label.clear()
            self._set_image(None)
            self.attachments.clear()
            self._refresh_attachments()
            self._controls()
            if submission['cancel_job']:
                self._cancel()
        if not data.get('after_job_id'): self.job = 'submitting'
        if not self._run("submit", submit, sent):
            if self.job == 'submitting': self.job = None
            self.identity.setText("诊断通道繁忙；问题已冻结，点击重试确认继续原请求。")
        self._controls()

    def _job_status(self, value):
        self._permission_states(value.get("permission_requests") or [], value.get('state'))
        self._local_permissions(value.get('local_requests') or [])
        note = value.get('local_requests_note') or value.get('local_requests_error') or value.get('local_results_note')
        if note: self.identity.setText(str(note))
        if value["state"] in {"QUEUED", "RUNNING", "WAITING_PERMISSION"}:
            if value["state"] == "WAITING_PERMISSION":
                states = {row.get('state') for row in value.get('permission_requests') or []}
                text = ('正在同步读取申请或检查结果，连接恢复后可继续；已查到的资料会保留。' if note else
                        '需要你允许读取下面的资料，确认后会继续检查；等待时不占用检查时间。' if value.get('local_requests') else
                        '已允许查看资料，正在读取和检查；允许不代表已经读到内容。' if 'approved' in states else
                        '已有实际检查结果，可以继续检查；之前的资料会保留。' if 'completed' in states and not states.intersection({'expired','denied'}) else
                        '读取申请已过期或已拒绝，可以重新申请；已查到的资料会保留。' if states.intersection({'expired','denied'}) else
                        '正在等待原上位机读取申请；已查到的资料会保留。')
                self.progress.setText(text)
                stage = value.get('result') or {}
                if stage.get('reply') or stage.get('cards'):
                    marker = (self.job, hashlib.sha256(str(stage).encode()).hexdigest())
                    if marker not in self._permission_stage_seen:
                        self._permission_stage_seen.add(marker)
                        self._display('assistant','### 目前已查到的情况（还会继续）\n\n' + str(stage.get('reply') or '已查到部分资料，等待允许后继续检查。'),stage.get('cards'))
                        self.live_output.hide()
            return
        if ((self.binding or {}).get('capabilities') or {}).get('features', {}).get('stream_events') or self._events_backlog:
            self._terminal_value = value
            self.progress.setText('检查已结束，正在整理完整结果。')
            self._request_events()
            return
        self._finish_job(value)

    def _finish_job(self, value):
        self._terminal_value = None
        self._events_backlog = False
        self.permission_list.clear(); self.permission_list.hide(); self.permission_resume.hide()
        result = value.get("result") or {}
        self._display("assistant", result.get("reply", "任务已结束，请核对回执。"), result.get("cards"))
        self.last_job = self.job
        self._steering = None
        self.job = self._queued_jobs.pop(0) if self._queued_jobs else None
        self._event_seq, self._event_round = 0, None
        self.live_output.clear(); self.live_output.hide()
        for dialog in list(self._permission_dialogs): dialog.reject()
        self._permission_prompted.clear()
        self._controls()

    def _local_permissions(self, requests):
        if not self.binding or not self.job or self.job == 'submitting':
            return
        active_ids = {str(item['id']) for item in requests}
        for dialog in list(self._permission_dialogs):
            identifier = dialog.property('localRequestId')
            if identifier and str(identifier) not in active_ids:
                dialog.setProperty('permissionDismissed',True); dialog.reject()
        for item in requests:
            marker = (self.job, item['id'])
            if marker in self._permission_prompted:
                continue
            self._permission_prompted.add(marker)
            epoch, generation, job = self.epoch, self.binding['generation'], self.job
            dialog = QDialog(self)
            dialog.setWindowTitle('允许助手查看这些资料吗？')
            dialog.setProperty('localRequestId',str(item['id']))
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            layout = QVBoxLayout(dialog)
            notice = QLabel(str(item.get('share_notice') or '助手请求读取以下本机资料，读取的内容会发送到统一平台模型用于本次诊断。只读，不执行文件或命令，不操作设备。'))
            notice.setTextFormat(Qt.TextFormat.PlainText)
            notice.setWordWrap(True); layout.addWidget(notice)
            names = {'read':'读取文件','list':'查看目录','search':'查找相关内容','discover':'查找本机日志和CSV','csv_diagnosis':'检查CSV数据','log_diagnosis':'检查日志','execution_summary':'查看本机执行记录','capabilities':'核对可读取的目录'}
            path = QLabel(f"需要做的检查：{names.get(item.get('operation'),item.get('operation',''))}\n路径：{item.get('display_path') or item.get('path','')}\n用途：{item.get('purpose') or '继续检查当前问题'}")
            path.setTextFormat(Qt.TextFormat.PlainText)
            path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            path.setWordWrap(True); layout.addWidget(path)
            choice = QComboBox()
            for label, scope in [('仅此文件／此路径', 'file'), ('此目录及其下文件', 'directory'), ('本任务已登记的诊断目录', 'all')]:
                if scope in item.get('allowed_scopes',['file','directory','all']): choice.addItem(label, scope)
            layout.addWidget(choice)
            limit = QLabel('只允许当前账号、当前问题读取这些资料，不会修改文件或操作设备。申请30分钟内有效；系统本身没有读取权限时，点允许也不能越过系统限制。')
            limit.setWordWrap(True); layout.addWidget(limit)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText('允许只读')
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('拒绝')
            layout.addWidget(buttons)
            buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
            self._permission_dialogs.append(dialog)
            expected = item.get('request_sha256')
            decision_key = str(uuid4())
            def finished(code, dialog=dialog, choice=choice, marker=marker, epoch=epoch, generation=generation, job=job, expected=expected, decision_key=decision_key):
                if dialog in self._permission_dialogs: self._permission_dialogs.remove(dialog)
                if dialog.property('permissionDismissed'): return
                if epoch != self.epoch or not self.binding or self.binding['generation'] != generation or self.job != job:
                    return
                data = dict(generation=generation, id=job, request_id=marker[1],request_key=decision_key,
                            scope=choice.currentData() if code == QDialog.DialogCode.Accepted else 'deny')
                if expected: data['expected_request_sha256'] = expected
                started = self._run('local-approve:' + marker[1], lambda: self.client.diagnostic('local-approve', data),
                                    self._local_approved)
                if not started:
                    self._permission_prompted.discard(marker)
                    self.identity.setText('诊断通道繁忙，稍后重新显示本机读取授权。')
            dialog.finished.connect(finished)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
            dialog.show()  # GUI, status polling and lease timers keep running.

    def _local_approved(self, value):
        result = value.get('result', value)
        if result.get('status') == 'permission_approved':
            self.identity.setText('已记录你的允许，接下来会读取资料并继续检查。')
        elif result.get('status') == 'permission_denied':
            self.identity.setText('已拒绝读取；助手会说明目前还缺哪些资料。')
        elif result.get('status') in {'os_permission_denied', 'error', 'path_changed', 'unsupported_file'}:
            self.identity.setText(str(result.get('error') or '本机读取未完成') + '\n' + str(result.get('advice') or '请核对证据；未将此路径计为已读取。'))
        else:
            self.identity.setText('资料已发给助手；请查看分析结果和依据。')

    def _cancel(self):
        if self.submission:
            # Cover the boundary between the last upload check and job POST.
            # If that POST is already in flight, its returned job is cancelled.
            self.submission['cancel_job'] = True
            if self.submission['phase'] == 'upload':
                self.submission['cancel'].set()
                for entry in self.submission['entries']:
                    if not entry.get('uploaded_id'):
                        entry['cancelled'] = True
                        self._request_upload_cancel(entry)
                if not self.job:
                    self._restore_cancelled_question(self.submission)
                    self.submission = None
                    self._controls()
                self.identity.setText("正在取消发送；当前上传返回后停止，不提交后续文件或任务。")
            else:
                # A lost job acknowledgement is ambiguous. Resolve the same
                # idempotency key before cancelling; never create a replacement.
                self.submission['cancel_job'] = True
                self.identity.setText("正在确认原任务回执；取得任务编号后取消。")
                if not self.job:
                    self._send()
            return
        if self.binding and self.job and self.job != "submitting":
            data = {"generation": self.binding["generation"], "id": self.job}
            self._run("cancel", lambda: self.client.diagnostic("cancel", data), lambda _: None)

    def _set_image(self, image):
        if image and self.image and image[0] == self.image[0]: return
        if image and (len(self.attachments) + 1 > 3 or len(image[0]) > 4 * 1024**2):
            self.identity.setText("截图最多4MiB，附件和截图合计最多3个。")
            return
        if image:
            fingerprint = hashlib.sha256(image[0]).hexdigest()
            if any(item.get('sha256') == fingerprint for item in self.attachments):
                self.identity.setText('同一图片已在附件队列中，无需重复添加。'); return
        self.image = image
        self.preview.clear()
        self.preview.setVisible(bool(image))
        if image:
            pixmap = QPixmap()
            pixmap.loadFromData(image[0])
            if not pixmap.isNull(): self.preview.setPixmap(pixmap.scaled(240, 100, Qt.AspectRatioMode.KeepAspectRatio))
        self._refresh_attachments()

    def _refresh_attachments(self):
        expanded = {self.attachment_list.topLevelItem(index).data(0,Qt.ItemDataRole.UserRole) for index in range(self.attachment_list.topLevelItemCount()) if self.attachment_list.topLevelItem(index).isExpanded()}
        selected = {self.attachment_list.topLevelItem(index).data(0,Qt.ItemDataRole.UserRole) for index in range(self.attachment_list.topLevelItemCount()) if self.attachment_list.topLevelItem(index).isSelected()}
        self.attachment_list.clear()
        entries = self.submission['entries'] if self.submission else ([dict(filename=self.image_name, size=len(self.image[0]), image=True)] if self.image else []) + self.attachments
        self.attachment_list.setVisible(bool(entries))
        for index, entry in enumerate(entries):
            item = QTreeWidgetItem(self.attachment_list, [entry['filename'], f"{entry.get('size', len(entry.get('raw', b''))) / 1024:.1f} KiB · {entry.get('state', '待发送')}"])
            item.setData(0, Qt.ItemDataRole.UserRole, index)
            item.setToolTip(0, entry['filename'])
            item.setToolTip(1, entry.get('state','待发送') + '\n' + entry.get('transport_detail',''))
            if entry.get('transport_detail'):
                QTreeWidgetItem(item,['上传明细',entry['transport_detail'],''])
                item.setExpanded(index in expanded)
            item.setSelected(index in selected)
            action = QPushButton('重试' if '失败' in entry.get('state','') else '取消' if self.submission else '移除')
            action.setObjectName('diagnosticButton')
            if self.submission:
                if '失败' in entry.get('state',''): action.clicked.connect(self._send)
                else: action.clicked.connect(lambda _=False, entry=entry: self._cancel_attachment(entry))
            else:
                action.clicked.connect(lambda _=False, item=item: self._remove_attachment_row(item))
            self.attachment_list.setItemWidget(item, 2, action)

    def _choose_attachments(self):
        epoch = self.epoch
        paths, _ = QFileDialog.getOpenFileNames(self, "添加诊断附件（单个最多32MiB，合计最多3个）", "", "所有文件 (*)")
        if epoch == self.epoch:
            self._add_attachments(paths)

    def _add_attachments(self, paths):
        if self.submission:
            return
        existing = {str(item['path'].resolve()) for item in self.attachments if 'path' in item}
        paths = list(dict.fromkeys(str(Path(value).expanduser().absolute()) for value in paths))
        paths = [value for value in paths if str(Path(value).resolve()) not in existing]
        if len(paths) + len(self.attachments) + bool(self.image) > 3:
            self.identity.setText("附件和截图合计最多3个，请减少选择。")
            return
        incoming = []
        try:
            for value in paths:
                path = Path(value)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024**2:
                    raise ValueError(f"{path.name}：须为非空普通文件，单个最多32MiB。")
                incoming.append(dict(path=path, filename=path.name, size=info.st_size,
                                     identity=(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)))
        except (OSError, ValueError) as exc:
            self.identity.setText(str(exc))
            return
        self.attachments.extend(incoming)
        self._refresh_attachments()
        self._fingerprint_attachments()

    def _remove_attachments(self):
        if self.submission:
            return
        selected = {item.data(0, Qt.ItemDataRole.UserRole) for item in self.attachment_list.selectedItems()}
        offset = bool(self.image)
        self.attachments = [item for index, item in enumerate(self.attachments) if index + offset not in selected]
        if self.image and 0 in selected:
            self._set_image(None)
        self._refresh_attachments()

    @staticmethod
    def _attachment_bytes(entry):
        if 'raw' in entry:
            return entry['raw']
        # This runs in the diagnostic pool, never on the Qt/control heartbeat thread.
        descriptor = os.open(entry['path'], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if not stat.S_ISREG(info.st_mode) or identity != entry['identity']:
                raise ValueError(f"{entry['filename']}已变化，请取消本次发送并重新选择。")
            raw = stream.read(32 * 1024**2 + 1)
            after = os.fstat(stream.fileno())
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity or len(raw) != entry['size']:
                raise ValueError(f"{entry['filename']}读取期间发生变化，请重新选择。")
        return raw

    def _choose(self):
        epoch = self.epoch
        path, _ = QFileDialog.getOpenFileName(self, "选择截图", "", "截图 (*.png *.jpg *.jpeg)")
        if path and epoch == self.epoch:
            source = Path(path)
            if source.stat().st_size <= 4 * 1024**2:
                self.image_name = source.name
                self._set_image((source.read_bytes(), "image/png" if source.suffix.lower() == ".png" else "image/jpeg"))
            else:
                self.identity.setText("截图最大4MB。")

    def _paste(self):
        image = QApplication.clipboard().image()
        if not image.isNull():
            raw = QByteArray()
            buffer = QBuffer(raw)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            buffer.close()
            if raw.size() <= 4 * 1024**2:
                value = bytes(raw)
                fingerprint = hashlib.sha256(value).hexdigest()
                if (self.image and self.image[0] == value) or any(entry.get('sha256') == fingerprint for entry in self.attachments):
                    self.identity.setText('同一图片已在附件队列中。'); return
                if self.image:
                    if len(self.attachments) + 1 >= 3:
                        self.identity.setText('附件和截图合计最多3个。'); return
                    self.attachments.append(dict(raw=value, mime='image/png', filename=f'diagnostic-screenshot-{len(self.attachments)+2}.png', size=len(value), sha256=fingerprint))
                    self._refresh_attachments()
                else:
                    self.image_name = 'diagnostic-screenshot.png'
                    self._set_image((value, "image/png"))
            else:
                self.identity.setText("截图最大4MB。")

    def _open_link(self, url):
        parsed = urlparse(url.toString())
        if parsed.scheme == 'diagnostic-log' and parsed.netloc in self._long_answers:
            dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('完整答复 / 日志')
            layout = QVBoxLayout(dialog); output = _Transcript(); output.setOpenLinks(False); output.anchorClicked.connect(self._open_link)
            output.setHtml(safe_markdown(self._long_answers[parsed.netloc])); layout.addWidget(output)
            copy = QPushButton('复制完整内容'); copy.clicked.connect(lambda: QApplication.clipboard().setText(self._long_answers.get(parsed.netloc,''))); layout.addWidget(copy)
            dialog.resize(800,600); dialog.show(); self._long_answer_window = dialog
            return
        if parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password:
            QDesktopServices.openUrl(url)
        else:
            self.identity.setText('仅允许明确点击公开HTTP/HTTPS来源链接；本机路径请使用授权读取。')

    def _copy_answer(self):
        if not self._last_answer: return
        blocks = re.findall(r'```[^\n]*\n(.*?)```', self._last_answer, re.S)
        if not blocks:
            QApplication.clipboard().setText(self._last_answer); return
        menu = QMenu(self)
        menu.addAction('复制完整答复', lambda: QApplication.clipboard().setText(self._last_answer))
        for index, block in enumerate(blocks):
            menu.addAction(f'复制代码块 {index + 1}', lambda value=block: QApplication.clipboard().setText(value))
        menu.popup(self.transcript.mapToGlobal(self.transcript.rect().center()))
        self._copy_menu = menu

    def _draft_changed(self):
        if not self._draft_restoring: self._draft_dirty = True

    def _save_draft(self):
        self._drafts[self.thread] = dict(message=self.message.toPlainText(), image=self.image,
                                        image_name=self.image_name, attachments=list(self.attachments), references=list(self.references))
        self._persist_draft()

    def _persist_draft(self):
        if not self.binding or not self._draft_dirty: return
        body, thread = self.message.toPlainText(), self.thread
        data = dict(generation=self.binding['generation'], thread_id=thread, message=body)
        def saved(_):
            if self.thread == thread and self.message.toPlainText() == body: self._draft_dirty = False
        self._run('draft-save', lambda: self.client.diagnostic('draft-save', data), saved)

    def _restore_draft(self, load=True):
        self._draft_restoring = True
        draft = self._drafts.get(self.thread, {})
        self.message.setPlainText(draft.get('message', ''))
        self.attachments = list(draft.get('attachments', []))
        self.references = list(draft.get('references', []))
        self.image_name = draft.get('image_name', 'diagnostic-screenshot.png')
        self._set_image(draft.get('image'))
        self.reference_label.setText('引用：' + '、'.join(item['label'] for item in self.references) if self.references else '')
        self.reference_label.setVisible(bool(self.references))
        self._draft_restoring = False
        self._draft_dirty = False
        if not draft and load: self._load_draft()

    def _load_draft(self):
        if not self.binding or self.thread in self._drafts: return
        thread, original = self.thread, self.message.toPlainText()
        data = dict(generation=self.binding['generation'], thread_id=thread)
        def loaded(value):
            if self.thread == thread and self.message.toPlainText() == original and not self._draft_dirty:
                self._draft_restoring = True
                self.message.setPlainText(value.get('message', ''))
                self._draft_restoring = False
        if not self._run('draft-load', lambda: self.client.diagnostic('draft-load', data), loaded):
            epoch=self.epoch
            QTimer.singleShot(150, lambda: self._load_draft() if self.epoch == epoch and self.thread == thread else None)

    def _search_threads(self):
        if not self.binding: return
        data = dict(generation=self.binding['generation'], q=self.history_search.text())
        def loaded(rows):
            current = self.thread
            self.history_list.clear(); self.history_list.addItem('新对话', None); self._threads(rows)
            for index in range(self.history_list.count()):
                if self.history_list.itemData(index) == current: self.history_list.setCurrentIndex(index); break
        self._run('thread-search', lambda: self.client.diagnostic('threads', data), loaded)

    def _manage_thread(self):
        if not self.binding or not self.thread or self.job: return
        menu = QMenu(self)
        epoch, thread = self.epoch, self.thread
        def rename():
            title, accepted = QInputDialog.getText(self, '对话标题', '输入易读标题（最多120字）')
            if accepted and title.strip() and epoch == self.epoch and thread == self.thread:
                self._action('thread-rename', dict(id=self.thread, title=title.strip()[:120]), lambda _: self._search_threads())
        def delete():
            if QMessageBox.question(self, '删除对话', '隐藏此对话及其个人聊天记录？正式业务记录和审计仍保留。') == QMessageBox.StandardButton.Yes and epoch == self.epoch and thread == self.thread:
                def removed(_):
                    self._drafts.pop(self.thread, None)
                    self.thread = None; self._restore_draft(); self._welcome(); self._search_threads()
                self._action('thread-delete', dict(id=self.thread), removed)
        menu.addAction('重命名', rename); menu.addAction('删除', delete)
        menu.popup(self.history_menu.mapToGlobal(self.history_menu.rect().bottomLeft()))
        self._history_popup = menu

    def _action(self, action, data, callback):
        if not self.binding: return False
        payload = dict(data, generation=self.binding['generation'])
        return self._run(action, lambda: self.client.diagnostic(action, payload), callback)

    def _events(self, value):
        self._events_backlog = bool(value.get('has_more'))
        for event in value.get('items', []):
            sequence = int(event.get('seq', 0))
            if sequence <= self._event_seq: continue
            self._event_seq = sequence
            data, kind = event.get('data') or {}, event.get('kind')
            if kind == 'text':
                self._event_round = data.get('round')
                self.live_output.setHtml(safe_markdown(data.get('text', '')))
                self.live_output.show()
            elif kind == 'tool':
                self.progress.setText(f"正在诊断 · {data.get('tool','读取资料')} · {data.get('status','处理中')}")
            elif kind == 'input':
                self.progress.setText('已收到补充，继续检查当前问题；不会因此自动修改数据。')
        self._event_seq = max(self._event_seq, int(value.get('next_seq', self._event_seq)))
        if value.get('has_more'):
            self._request_events()
        elif self._terminal_value is not None:
            if value.get('state') in {'COMPLETED','FAILED','CANCELLED','TIMED_OUT','INTERRUPTED','REJECTED'}:
                self._finish_job(self._terminal_value)
            else:
                self._request_events(delay=100)


    def _request_events(self, delay=0):
        if not self.binding or not self.job or self.job == 'submitting': return
        epoch, job = self.epoch, self.job
        if delay:
            QTimer.singleShot(delay, lambda: self._request_events() if self.epoch == epoch and self.job == job else None)
            return
        data = dict(generation=self.binding['generation'], id=job, after=self._event_seq)
        if not self._run('events', lambda: self.client.diagnostic('events', data), lambda value: self._events(value) if self.job == job else None):
            if self._terminal_value is not None:
                QTimer.singleShot(100, lambda: self._request_events() if self.epoch == epoch and self.job == job else None)

    def _steer(self):
        message = self.message.toPlainText().strip()
        if not message or not self.job: return
        if self.image or self.attachments or self.references:
            self.identity.setText('补充当前诊断只发送文字；有附件或引用请选“下一条排队”。'); return
        if len(message) > 8000:
            self.identity.setText('问题最多8000字。'); return
        job = self.job
        if self._steering is None or self._steering['id'] != job:
            self._steering = dict(id=job, expected_job_id=job, request_key=str(uuid4()), message=message)
        payload = self._steering
        message = payload['message']
        def accepted(_):
            self._steering = None
            self._display('user', '补充当前诊断：' + message)
            if self.message.toPlainText().strip() == message: self.message.clear()
        self._action('steer', payload, accepted)

    def _reference_dialog(self):
        if not self.binding: return
        epoch = self.epoch
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('选择本人可见的业务对象')
        layout = QVBoxLayout(dialog)
        kind = QComboBox()
        for title, value in [('样品','assets'),('工位','stations'),('执行','executions')]: kind.addItem(title, value)
        search = QLineEdit(); search.setPlaceholderText('名称或编号，回车查询')
        table = QTreeWidget(); table.setHeaderLabels(['对象','读取范围'])
        layout.addWidget(kind); layout.addWidget(search); layout.addWidget(table)
        note = QLabel('只附带选中对象引用，发送时服务端重新核对可见性。@文件请直接添加附件；原始CSV不会因此正式归档。'); note.setWordWrap(True); layout.addWidget(note)
        def lookup():
            def result(value):
                if epoch != self.epoch: dialog.reject(); return
                table.clear()
                for row in value.get('items', []):
                    item = QTreeWidgetItem(table, [row['label'], str(row.get('detail',''))]); item.setData(0, Qt.ItemDataRole.UserRole, row)
            self._action('references', dict(kind=kind.currentData(), q=search.text()), result)
        search.returnPressed.connect(lookup); kind.currentIndexChanged.connect(lookup)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons)
        def select():
            item = table.currentItem()
            if item and epoch == self.epoch:
                row = item.data(0, Qt.ItemDataRole.UserRole)
                reference = {key: row[key] for key in ('kind','id','label')}
                if reference not in self.references:
                    if len(self.references) >= 8:
                        self.identity.setText('一条问题最多引用8个对象，请先清空部分引用。'); return
                    self.references.append(reference)
                self.reference_label.setText('引用：' + '、'.join(item['label'] for item in self.references))
                self.reference_label.show()
                dialog.accept()
        clear = QPushButton('清空待发送引用')
        def clear_references():
            if epoch != self.epoch: return
            self.references.clear(); self.reference_label.clear(); self.reference_label.hide(); dialog.accept()
        clear.clicked.connect(clear_references); layout.addWidget(clear)
        buttons.accepted.connect(select); buttons.rejected.connect(dialog.reject)
        dialog.resize(540,380); dialog.show(); self._reference_window = dialog; lookup()

    def _give_feedback(self):
        if not self.last_job:
            self.identity.setText('请在诊断结束后反馈。'); return
        epoch, job = self.epoch, self.last_job
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('反馈问题')
        layout = QVBoxLayout(dialog)
        rating = QComboBox(); rating.addItem('有帮助','helpful'); rating.addItem('存在问题','issue'); layout.addWidget(rating)
        note = QPlainTextEdit(); note.setPlaceholderText('描述有帮助的部分或具体问题，最多4000字。'); layout.addWidget(note)
        share = QCheckBox('允许管理员整理这条反馈，发布到常见问题')
        share.setObjectName('diagnosticShareForFaq'); share.setChecked(False); layout.addWidget(share)
        privacy = QLabel('默认仅自己可见。勾选后，管理员可以去掉个人信息，再发布到常见问题；这条反馈不会自动公开。'); privacy.setWordWrap(True); layout.addWidget(privacy)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel); layout.addWidget(buttons)
        request_key = str(uuid4())
        def submit():
            if epoch != self.epoch: dialog.reject(); return
            text = note.toPlainText()
            if len(text) > 4000:
                self.identity.setText('反馈最多4000字，请缩短后保存。'); return
            payload = dict(request_key=request_key, job_id=job, rating=rating.currentData(), note=text, share_for_faq=share.isChecked())
            def saved(value):
                self._feedback = value
                self.identity.setText('反馈已记录；已授权管理员脱敏后另行发布FAQ。' if payload['share_for_faq'] else '个人反馈已记录，未授权共享FAQ。')
                dialog.accept()
            self._action('feedback', payload, saved)
        buttons.accepted.connect(submit); buttons.rejected.connect(dialog.reject)
        dialog.resize(560,350); dialog.show(); self._feedback_window = dialog

    def _faq(self):
        if not self.binding: return
        epoch = self.epoch
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('常见问题')
        layout = QVBoxLayout(dialog); search = QLineEdit(); search.setPlaceholderText('搜索问题或解决办法，回车查询'); layout.addWidget(search)
        choice = QComboBox(); choice.setObjectName('diagnosticFaqChoice'); layout.addWidget(choice)
        output = _Transcript(); output.setOpenLinks(False); output.anchorClicked.connect(self._open_link); layout.addWidget(output)
        def lookup():
            def loaded(value):
                if epoch != self.epoch: dialog.reject(); return
                rows = value.get('items', []) if isinstance(value, dict) else value
                choice.clear()
                for row in rows: choice.addItem(row.get('question','FAQ') + ' · 版本 ' + str(row.get('revision','')), row)
                output.setHtml(''.join(safe_markdown('### ' + row.get('question', '') + '\n' + row.get('answer', '') + '\n版本 ' + str(row.get('revision', ''))) for row in rows) or '暂无已发布匹配项。')
            self._action('faq', dict(q=search.text()), loaded)
        search.returnPressed.connect(lookup)
        admin = bool(((self.binding or {}).get('capabilities') or {}).get('features',{}).get('faq_publish'))
        publish = QPushButton('管理员：发布这条问题'); publish.clicked.connect(self._publish_faq); publish.setVisible(admin); layout.addWidget(publish)
        revise = QPushButton('管理员：修改选中的常见问题'); revise.setObjectName('diagnosticFaqRevise'); revise.clicked.connect(lambda: self._revise_faq(choice.currentData())); revise.setVisible(admin); layout.addWidget(revise)
        dialog.resize(650,470); dialog.show(); self._faq_window = dialog; self._faq_refresh = lookup; lookup()

    def _publish_faq(self):
        if not bool(((self.binding or {}).get('capabilities') or {}).get('features',{}).get('faq_publish')):
            self.identity.setText('当前账号可以查看常见问题；发布和修改由管理员处理。'); return
        if not self._feedback:
            self.identity.setText('先反馈问题，再由管理员整理到常见问题。'); return
        if self._feedback.get('share_for_faq') is not True:
            self.identity.setText('这条反馈仅自己可见。若允许发布到常见问题，请重新提交时勾选分享许可。'); return
        epoch = self.epoch
        question, accepted = QInputDialog.getText(self, '发布FAQ', '共享问题（请核对脱敏内容）')
        if not accepted or not question.strip() or epoch != self.epoch: return
        answer, accepted = QInputDialog.getMultiLineText(self, '发布FAQ', '共享答案（管理员确认发布，追加版本）', self._last_answer)
        if not accepted or not answer.strip() or epoch != self.epoch: return
        identifier = self._feedback.get('id') or (self._feedback.get('feedback') or {}).get('id')
        if not identifier: return
        self._action('faq-publish', dict(request_key=str(uuid4()), feedback_id=identifier, question=question, answer=answer), lambda value: self.identity.setText('共享FAQ已发布，版本 ' + str(value.get('revision', '未知')) + '；发布人由平台审计保存。'))

    def _download_report(self):
        if not self.last_job:
            self.identity.setText('请在诊断结束后下载报告。'); return
        epoch, job = self.epoch, self.last_job
        fmt, accepted = QInputDialog.getItem(self, '下载派生诊断报告', '不属于正式测试归档，原始CSV不会修改。', ['markdown','csv','json'], 0, False)
        if not accepted or epoch != self.epoch: return
        path, _ = QFileDialog.getSaveFileName(self, '保存派生诊断报告', f'diagnostic-{job[:8]}.{"md" if fmt == "markdown" else fmt}', '所有文件 (*)')
        if not path or epoch != self.epoch: return
        target = Path(path)
        # Never overwrite an existing file or original CSV through an export.
        if target.exists():
            self.identity.setText('请选择新文件名；报告下载不会覆盖已有文件。'); return
        def received(value):
            raw = base64.b64decode(value['content_base64'], validate=True)
            if hashlib.sha256(raw).hexdigest() != value['sha256']: raise ValueError('下载报告哈希不符。')
            def save():
                with target.open('xb') as stream: stream.write(raw)
                return {}
            self._run('report-save', save, lambda _: self.identity.setText('派生诊断报告已保存，未进入正式归档统计。'))
        self._action('report', dict(id=job, format=fmt), received)

    def _repair_cards(self):
        if not self.binding: return
        job = self.last_job or self.job
        if not job or job == 'submitting':
            self.identity.setText('请先让助手读取真实文件并生成修复提议。'); return
        self._action('repairs', dict(id=job), self._show_repairs)

    def _show_repairs(self, value):
        rows = value.get('items', [])
        if not rows:
            self.identity.setText('此任务尚无修复提议；请先描述具体问题，助手须读取真实文件及哈希后提议。'); return
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('审阅本机修复差异')
        layout = QVBoxLayout(dialog)
        choice = QComboBox()
        for row in rows: choice.addItem(row['path'] + ' · ' + row['state'], row)
        layout.addWidget(choice)
        notice = QLabel('仅保存到当前配对本机，先核对原始哈希、角色、操作系统权限和并发修改并备份。不会自动加载、重启或操作PLC/CAN。'); notice.setWordWrap(True); layout.addWidget(notice)
        diff = QPlainTextEdit(); diff.setReadOnly(True); layout.addWidget(diff)
        button = QPushButton('确认差异并保存到本机'); layout.addWidget(button)
        def selected():
            row = choice.currentData(); diff.setPlainText(row.get('diff', '') + '\n\n原因：' + row.get('reason','')); button.setEnabled(row['state'] in {'proposed','queued'})
        choice.currentIndexChanged.connect(selected); selected()
        epoch = self.epoch
        def apply():
            row = choice.currentData()
            if epoch != self.epoch: dialog.reject(); return
            button.setEnabled(False)
            def confirmed(_):
                if epoch != self.epoch: return
                def received(receipt):
                    actual = receipt.get('receipt', {})
                    state = {'applied':'已保存','conflict':'基线冲突，未覆盖','failed':'保存失败','rejected':'已拒绝'}.get(actual.get('status'), receipt.get('state','未知'))
                    if actual.get('saved') and actual.get('status') != 'applied': state = '文件已替换，后续核验未完成'
                    diff.appendPlainText('\n保存结果：' + state + '\n' + actual.get('message','请重新查询原提议的实际回执。') + '\n备份：' + actual.get('backup_path','未创建') + '\n原SHA256：' + actual.get('before_sha256','未核对') + '\n保存后SHA256：' + actual.get('after_sha256','未保存') + '\n加载：未执行；当前进程：未更新')
                    self.identity.setText('修改已保存，结果还没同步到平台；重新登录原账号后会继续同步，不会再次修改文件。' if receipt.get('synchronization') == 'pending' and actual.get('saved') else '请先查看保存结果；保存文件不会让当前运行的软件自动更新。')
                self._action('repair-apply', dict(id=row['id']), received)
            if row['state'] == 'queued': confirmed({})
            else: self._action('repair-confirm', dict(id=row['id'], proposal_sha256=row['proposal_sha256']), confirmed)
        button.clicked.connect(apply)
        dialog.resize(760,500); dialog.show(); self._repair_window = dialog

    def _preview_image(self, raw=None):
        raw = raw or (self.image[0] if self.image else None)
        if not raw: return
        pixmap = QPixmap(); pixmap.loadFromData(raw)
        if pixmap.isNull():
            self.identity.setText('无法识别图片预览；原字节仍保留，可作为文件分析。'); return
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('图片预览')
        layout = QVBoxLayout(dialog); label = QLabel(); label.setPixmap(pixmap.scaled(1000,700, Qt.AspectRatioMode.KeepAspectRatio)); layout.addWidget(label)
        dialog.show(); self._image_window = dialog

    def _attachment_preview(self, item, _column):
        index = item.data(0, Qt.ItemDataRole.UserRole)
        if self.image and index == 0:
            self._preview_image(); return
        entries = self.attachments
        index -= bool(self.image)
        if not 0 <= index < len(entries): return
        entry = dict(entries[index])
        if Path(entry['filename']).suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp'}:
            self._run('image-preview', lambda: self._attachment_bytes(entry), self._preview_image)

    def _remove_attachment_row(self, item):
        self.attachment_list.clearSelection(); item.setSelected(True); self._remove_attachments()

    def _cancel_attachment(self, entry):
        if not self.submission or self.submission['phase'] != 'upload': return
        entry['cancelled'] = True
        self._request_upload_cancel(entry)
        entry['state'] = '正在取消，等待当前网络请求返回' if entry.get('upload_id') and not entry.get('uploaded_id') else '已取消'
        identifier = entry.get('uploaded_id')
        if identifier in self.submission['data']['attachments']: self.submission['data']['attachments'].remove(identifier)
        self._refresh_attachments()

    def _history_attachment_preview(self, item, _column):
        value = item.data(0, Qt.ItemDataRole.UserRole)
        if not value or not isinstance(value, dict) or value.get('kind') != 'image': return
        self._action('attachment-get', dict(id=value['id']), lambda result: self._preview_image(base64.b64decode(result['content_base64'], validate=True)))

    def _history_retry(self, thread, data, callback):
        if self.thread != thread or not self.binding or self.binding['generation'] != data['generation']: return
        if not self._run('history', lambda: self.client.diagnostic('history', data), callback):
            QTimer.singleShot(150, lambda: self._history_retry(thread, data, callback))

    def _fingerprint_attachments(self):
        # Hash only image candidates for cross-entry clipboard/file de-duplication.
        # File I/O always stays in the private diagnostic pool.
        if self.submission: return
        candidates = [item for item in self.attachments if 'sha256' not in item and Path(item['filename']).suffix.lower() in {'.png','.jpg','.jpeg','.webp','.bmp'}]
        if not candidates: return
        def calculate():
            result=[]
            for item in candidates:
                try: result.append((item, hashlib.sha256(self._attachment_bytes(item)).hexdigest()))
                except (OSError, ValueError): pass  # send still performs strict baseline checks
            return result
        def hashed(values):
            if self.submission: return
            seen={hashlib.sha256(self.image[0]).hexdigest()} if self.image else set()
            for item,digest in values:
                if item not in self.attachments: continue
                if digest in seen: self.attachments.remove(item)
                else: item['sha256']=digest; seen.add(digest)
            self._refresh_attachments()
        if not self._run('image-fingerprints', calculate, hashed):
            epoch=self.epoch
            QTimer.singleShot(150, lambda: self._fingerprint_attachments() if self.epoch == epoch else None)

    def _restore_cancelled_question(self, submission):
        if not submission: return
        original = submission['data']['message']
        draft = self.message.toPlainText()
        if not draft:
            self.message.setPlainText(original)
        elif draft != original:
            self.message.setPlainText('【已取消的原问题】\n' + original + '\n\n【下一条草稿】\n' + draft)

    @staticmethod
    def _partial_evidence(value):
        if isinstance(value, dict):
            if any(value.get(key) is False for key in ('eof','complete','model_content_complete','read_complete')): return True
            unread = value.get('unread')
            if isinstance(unread,str) and unread.strip() and unread.strip() not in {'0','none','None'}: return True
            if isinstance(unread,(list,dict)) and unread: return True
            if any(isinstance(value.get(key), (int,float)) and value[key] > 0 for key in ('unread','unread_bytes')): return True
            return any(DiagnosticsPage._partial_evidence(item) for item in value.values())
        if isinstance(value,list): return any(DiagnosticsPage._partial_evidence(item) for item in value)
        return False

    def _restore_unprocessed_inputs(self):
        if not self.binding or not self._unprocessed_inputs: return
        restored = '\n\n'.join(item['message'] for item in self._unprocessed_inputs)
        existing = self.message.toPlainText()
        self.message.setPlainText((existing + '\n\n' if existing else '') + '【未用于本次诊断的补充】\n' + restored)
        self._unprocessed_inputs.clear(); self.restore_inputs.hide()
        self.identity.setText('已把这些补充放回输入框，请确认后下次再问。')

    def _initial_threads(self, rows):
        self._threads(rows)
        if rows and self.thread is None and not self.job and not self.submission:
            self.history_list.setCurrentIndex(1)
            self._history(1)

    def _restore_active_jobs(self, jobs):
        active = [row for row in jobs if row.get('state') in {'RUNNING','QUEUED','WAITING_PERMISSION'}]
        if not active: return
        active.sort(key=lambda row: ({'RUNNING':0,'WAITING_PERMISSION':1,'QUEUED':2}.get(row['state'],3), str(row.get('created_at','')), row['id']))
        self.job = active[0]['id']
        self._queued_jobs = [row['id'] for row in active[1:]]
        self._event_seq, self._event_round = 0, None
        self._terminal_value, self._events_backlog = None, False
        self.progress.setText('已恢复服务器上的运行任务，可停止、补充当前诊断或保留下一条草稿。' + (f' 后续排队{len(self._queued_jobs)}条。' if self._queued_jobs else ''))
        self._controls()

    def _revise_faq(self, row):
        if not self.binding or not isinstance(row,dict): return
        if not bool((self.binding.get('capabilities') or {}).get('features',{}).get('faq_publish')):
            self.identity.setText('当前账号可以查看常见问题；发布和修改由管理员处理。'); return
        if not row.get('source_feedback_id') or not row.get('id') or not isinstance(row.get('revision'),int):
            self.identity.setText('这条常见问题的来源或版本不完整，请刷新后再修改。'); return
        epoch = self.epoch
        dialog = QDialog(self); dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose); dialog.setWindowTitle('修改常见问题')
        layout = QVBoxLayout(dialog)
        label = QLabel(f"当前第{row['revision']}版；确认后会发布新版本，旧版本仍然保留。\n来源：用户已允许分享的问题反馈")
        label.setToolTip('来源反馈编号：' + str(row['source_feedback_id']))
        label.setTextFormat(Qt.TextFormat.PlainText); label.setWordWrap(True); layout.addWidget(label)
        question = QLineEdit(row.get('question','')); question.setObjectName('diagnosticFaqQuestion'); layout.addWidget(question)
        answer = QPlainTextEdit(row.get('answer','')); answer.setObjectName('diagnosticFaqAnswer'); layout.addWidget(answer)
        confirm = QPushButton('确认修改并发布新版本'); confirm.setObjectName('diagnosticFaqConfirm'); layout.addWidget(confirm)
        request_key = str(uuid4())
        def publish():
            if epoch != self.epoch: dialog.reject(); return
            if not question.text().strip() or not answer.toPlainText().strip():
                self.identity.setText('共享问题和答案不能为空。'); return
            payload = dict(request_key=request_key, feedback_id=row['source_feedback_id'], faq_id=row['id'], expected_revision=row['revision'], question=question.text().strip(), answer=answer.toPlainText().strip())
            def saved(value):
                self.identity.setText('共享FAQ修订已发布，版本 ' + str(value.get('revision','未知')))
                dialog.accept()
                try:
                    if self._faq_window.isVisible(): self._faq_refresh()
                except (AttributeError, RuntimeError): pass
            self._action('faq-publish', payload, saved)
        confirm.clicked.connect(publish)
        dialog.resize(650,460); dialog.show(); self._faq_revision_window = dialog

    def _request_upload_cancel(self, entry):
        if not self.binding or not entry.get('upload_id') or not entry.get('tracked_upload'): return
        epoch, generation = self.epoch, self.binding['generation']
        identifier = entry['upload_id']
        data = dict(generation=generation, upload_id=identifier)
        self._run('upload-cancel:' + identifier, lambda: self.client.diagnostic('upload-cancel', data),
                  lambda value: self._upload_progress(entry, identifier, value) if self.epoch == epoch else None)

    def _upload_watch(self):
        submission = self.submission
        if not self.binding or not submission or submission['phase'] != 'upload': return
        for entry in submission['entries']:
            if not entry.get('tracked_upload') or not entry.get('upload_id') or entry.get('uploaded_id'): continue
            if entry.get('state') in {'已取消，不绑定本任务','已上传'} or '失败' in entry.get('state',''): continue
            identifier = entry['upload_id']
            if entry.get('cancelled'):
                self._request_upload_cancel(entry)
            else:
                data = dict(generation=self.binding['generation'], upload_id=identifier)
                self._run('upload-progress:' + identifier, lambda data=data: self.client.diagnostic('upload-progress', data),
                    lambda value, entry=entry, identifier=identifier: self._upload_progress(entry, identifier, value) if self.submission is submission else None)
            break

    def _upload_progress(self, entry, identifier, value):
        if entry.get('upload_id') != identifier or not self.submission or entry not in self.submission['entries']: return
        total, sent = int(value.get('total_bytes',0)), int(value.get('bytes_sent',0))
        if total <= 0 or sent < 0 or sent > total: return
        entry['bytes_sent'], entry['total_bytes'] = sent,total
        percent = sent * 100 / total
        phase = value.get('phase')
        entry['transport_detail'] = f"实际交给网络传输：{sent}/{total}字节。这个计数不表示平台已经保存文件。上传编号：{identifier}"
        if phase == 'uploaded': entry['state'] = '文件上传完成'
        elif phase == 'waiting_response': entry['state'] = '100% 已发送，等待保存确认'
        elif phase in {'cancelling','cancelled'} or entry.get('cancelled'):
            entry['state'] = f'{percent:.1f}% · ' + ('已取消' if phase == 'cancelled' else '正在取消，等待当前网络请求返回')
        elif phase == 'uploading': entry['state'] = f'{percent:.1f}% · 正在上传'
        elif phase == 'prepared': entry['state'] = '上位机正在准备上传'
        elif phase == 'failed': entry['state'] = '上传失败，点击重试可继续'
        self._refresh_attachments()

    def _permission_states(self, rows, job_state=None):
        self.permission_list.clear()
        labels = {'pending':'等待你决定','approved':'已允许，准备检查','denied':'已拒绝','expired':'申请已过期','completed':'已有检查结果','cancelled':'任务已停止'}
        completed = False
        for row in rows:
            state = row.get('state')
            path = str(row.get('display_path') or row.get('path') or row.get('purpose') or '核对可读取的目录')
            item = QTreeWidgetItem(self.permission_list,[path,labels.get(state,str(state or '未知')),''])
            item.setToolTip(0,str(row.get('purpose') or '') + '\n' + path)
            if state in {'expired','denied'}:
                button = QPushButton('重新申请'); button.setObjectName('diagnosticButton')
                button.clicked.connect(lambda _=False,row=dict(row): self._renew_local_permission(row))
                self.permission_list.setItemWidget(item,2,button)
            if state == 'completed': completed = True
        self.permission_list.setVisible(bool(rows))
        self.permission_resume.setVisible(completed and bool(self.job) and job_state == 'WAITING_PERMISSION')

    def _renew_local_permission(self, row):
        if not self.binding or not self.job: return
        data = dict(id=self.job,request_id=row['id'],expected_request_sha256=row['request_sha256'],request_key=str(uuid4()))
        self._action('local-renew',data,lambda _: self.identity.setText('已重新发起读取申请，请核对路径后决定是否允许。'))

    def _resume_local_diagnosis(self):
        if not self.binding or not self.job: return
        self._action('local-resume',dict(id=self.job,request_key=str(uuid4())),lambda _: self.identity.setText('正在继续检查，之前已经查到的资料会保留。'))
