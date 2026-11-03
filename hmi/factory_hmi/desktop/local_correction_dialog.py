"""Local metadata editor, with original values and append-only audit visible."""
import uuid

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel,
                              QLineEdit, QMessageBox, QPlainTextEdit)

from factory_hmi.core.local_corrections import FIELDS, validated_changes


LABELS = {'sample_id': '模块编号 / SN', 'operator_id': '实际测试人', 'bench_id': '工位',
          'test_name': '测试名称', 'actual_object': '实际测试对象', 'notes': '本地备注'}


class LocalCorrectionDialog(QDialog):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.request_id = uuid.uuid4().hex
        self.payload = None
        self.setWindowTitle('本地更正（不上传平台）')
        self.resize(760, 720)
        form = QFormLayout(self)
        notice = QLabel('只更正本机列表与台账。保留原值和更正历史；不修改原始测量、自动判定或审批状态。以后提交仍须单独核实。')
        notice.setWordWrap(True); form.addRow(notice)
        self.editors = {}
        for key, label in LABELS.items():
            editor = QLineEdit(state['values'].get(key, ''))
            editor.setMaxLength(FIELDS[key]); editor.setObjectName('local_' + key)
            form.addRow(label, editor); self.editors[key] = editor
        self.reason = QPlainTextEdit(); self.reason.setMaximumHeight(70)
        self.reason.setPlaceholderText('必填：为什么更正；最多500字')
        form.addRow('更正原因', self.reason)
        audit = QPlainTextEdit(); audit.setReadOnly(True)
        original = '\n'.join(f"{label}：{state['original'].get(key, '')}" for key, label in LABELS.items())
        history = '\n\n'.join(
            f"v{r['revision']} · {r['recorded_at']} · 操作人：{r['actor']}\n原因：{r['reason']}\n"
            + '\n'.join(f"{LABELS[k]}：{r['before'][k]} → {value}" for k, value in r['changes'].items())
            for r in state['records'])
        audit.setPlainText('原始记录（只读）\n' + original + '\n\n更正历史\n' + (history or '尚无本地更正'))
        form.addRow('原值与审计', audit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText('仅保存到本机')
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject); form.addRow(buttons)

    def save(self):
        try:
            changes = {key: editor.text().strip() for key, editor in self.editors.items()
                       if editor.text().strip() != self.state['values'][key]}
            validated_changes(changes)
            reason = self.reason.toPlainText().strip()
            if not reason or len(reason) > 500:
                raise ValueError('必须填写更正原因，最多500字')
            if any(not value and key != 'notes' for key, value in changes.items()):
                raise ValueError('不能清空已有的SN、人员、工位、名称或实际对象')
        except ValueError as exc:
            QMessageBox.warning(self, '本地更正未保存', str(exc)); return
        self.payload = dict(changes=changes, reason=reason,
                            expected_revision=self.state['revision'], request_id=self.request_id)
        self.accept()
