"""Registered choices online, unrestricted sample text when offline."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox
from factory_hmi.desktop.case_filter import LIMB_PARTS


def sample_choices(samples: list[dict], limb: str) -> list[dict]:
    parts = {'WAISTHIP'} if limb == 'waist_hip' else set(LIMB_PARTS.get(limb, (set(), set()))[1])
    return [item for item in samples if item.get('target_part_code') in parts]


class SampleSelector(QComboBox):
    textChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.online = False
        self._choices_signature = None
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.currentTextChanged.connect(lambda _text: self.textChanged.emit(self.text()))
        self.setPlaceholderText('离线可自由填写样机编号')

    def text(self) -> str:
        return str(self.currentData() or '') if self.online else self.currentText()

    def setText(self, text: str) -> None:
        if self.online:
            index = self.findData(text)
            if index >= 0 and self.model().item(index).isEnabled():
                self.setCurrentIndex(index)
        else:
            self.setEditText(text)

    def setPlaceholderText(self, text: str) -> None:
        if self.lineEdit() is not None:
            self.lineEdit().setPlaceholderText(text)

    def setReadOnly(self, _value: bool) -> None:
        # Platform profiles cannot lock the offline free-text field.
        pass

    def update_choices(self, samples: list[dict], limb: str, online: bool) -> None:
        choices = sample_choices(samples, limb) if online else []
        signature = (True, limb, tuple((item.get('code'), bool(item.get('running'))) for item in choices)) if online else (False,)
        if signature == self._choices_signature:
            return
        self._choices_signature = signature
        previous = self.text()
        previous_online = self.online
        self.blockSignals(True)
        self.online = online
        self.setToolTip('平台在线：仅可选择当前部位已建档且未运行的样机，每5秒刷新。' if online else '离线模式：可自由填写样机编号，测试先保存在本机。')
        self.clear()
        self.setEditable(not online)
        if online:
            self.addItem('请选择对应部位的样机编号', '')
            for item in choices:
                code = str(item.get('code') or '')
                self.addItem(code + (' 在运行' if item.get('running') else ''), code)
                if item.get('running'):
                    self.model().item(self.count()-1).setEnabled(False)
            index = self.findData(previous)
            self.setCurrentIndex(max(0, index))
        else:
            self.setEditText(previous)
            self.setPlaceholderText('离线可自由填写样机编号')
        self.blockSignals(False)
        if self.text() != previous or previous_online != online:
            self.textChanged.emit(self.text())

    def selection_allowed(self) -> bool:
        return not self.online or (bool(self.text()) and self.model().item(self.currentIndex()).isEnabled())
