"""Grouped local business editor; original evidence and revision audit stay visible."""
import uuid

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                              QLineEdit, QMessageBox, QPlainTextEdit, QScrollArea,
                              QTabWidget, QVBoxLayout, QWidget)

from factory_hmi.core.local_corrections import (
    ENUMS, FIELDS, GROUPS, LABELS, MULTILINE_FIELDS, validated_changes,
)


class LocalCorrectionDialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.request_id = uuid.uuid4().hex
        self.payload = None
        self.setWindowTitle('本地更正（不上传平台）')
        self.resize(920, 780)
        layout = QVBoxLayout(self)
        notice = QLabel('更正本机业务显示、筛选和导出。每次保存保留原值与历史；原始CSV和内部身份保持原样，平台真实同步/审批回执单独显示。以后归档仍须核实原始证据。')
        notice.setWordWrap(True); layout.addWidget(notice)
        tabs = QTabWidget(); tabs.setObjectName('local_correction_tabs'); layout.addWidget(tabs, 1)
        self.editors = {}
        for title, rows in GROUPS:
            page = QWidget(); form = QFormLayout(page)
            scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page)
            tabs.addTab(scroll, title)
            for key, label, _ in rows:
                value = str(state['values'].get(key, ''))
                if key in ENUMS:
                    editor = QComboBox()
                    editor.addItem('未记录', '')
                    for code, text in ENUMS[key].items(): editor.addItem(text, code)
                    index = editor.findData(value)
                    if index < 0:
                        editor.addItem('原值：' + value, value); index = editor.count() - 1
                    editor.setCurrentIndex(index)
                elif key in MULTILINE_FIELDS:
                    editor = QPlainTextEdit(value); editor.setMaximumHeight(75)
                else:
                    editor = QLineEdit(value); editor.setMaxLength(FIELDS[key])
                    if key in {'started_at', 'ended_at'}:
                        editor.setPlaceholderText('2026-11-03T17:30:00+08:00；空白表示未记录')
                    elif key.endswith('_seconds') or key.endswith('_count') or key in {'completed_cycles', 'record_rate_hz', 'sample_completeness'}:
                        editor.setPlaceholderText('非负数；空白表示未记录')
                editor.setObjectName('local_' + key)
                editor.setToolTip(label + '；仅本机业务视图，不改写封存证据')
                form.addRow(label, editor); self.editors[key] = editor
        audit = QPlainTextEdit(); audit.setReadOnly(True); audit.setObjectName('local_correction_audit')
        original = '\n'.join(f"{label}：{state['original'].get(key, '')}" for key, label in LABELS.items())
        history = '\n\n'.join(
            f"v{r['revision']} · {r['recorded_at']} · 操作人：{r['actor']}\n原因：{r['reason']}\n"
            + '\n'.join(f"{LABELS.get(k, k)}：{r['before'].get(k, '')} → {value}" for k, value in r['changes'].items())
            for r in state['records'])
        audit.setPlainText('内部记录ID（只读）：' + str(state['test_id']) + '\n\n原始业务信息（只读）\n' + original + '\n\n更正历史\n' + (history or '尚无本地更正'))
        tabs.addTab(audit, '原值与更正历史')
        form = QFormLayout()
        self.reason = QPlainTextEdit(); self.reason.setMaximumHeight(70)
        self.reason.setPlaceholderText('必填：为什么更正；最多500字')
        form.addRow('更正原因', self.reason); layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('仅保存到本机')
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    @staticmethod
    def editor_value(editor):
        if isinstance(editor, QComboBox): return str(editor.currentData() or '')
        if isinstance(editor, QPlainTextEdit): return editor.toPlainText().strip()
        return editor.text().strip()

    def save(self):
        try:
            changes = {key: self.editor_value(editor) for key, editor in self.editors.items()
                       if self.editor_value(editor) != self.state['values'].get(key, '')}
            changes = validated_changes(changes)
            reason = self.reason.toPlainText().strip()
            if not reason or len(reason) > 500:
                raise ValueError('必须填写更正原因，最多500字')
        except ValueError as exc:
            QMessageBox.warning(self, '本地更正未保存', str(exc)); return
        self.payload = dict(changes=changes, reason=reason,
                            expected_revision=self.state['revision'], request_id=self.request_id)
        self.accept()
