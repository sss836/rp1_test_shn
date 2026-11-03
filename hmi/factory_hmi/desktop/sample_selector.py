"""Registered choices online, unrestricted sample text when offline."""
from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal
from PySide6.QtWidgets import QComboBox
from factory_hmi.desktop.case_filter import LIMB_PARTS


def sample_choices(samples: list[dict], limb: str) -> list[dict]:
    parts = {'WAISTHIP'} if limb == 'waist_hip' else set(LIMB_PARTS.get(limb, (set(), set()))[1])
    return [item for item in samples if item.get('target_part_code') in parts]


class SampleChoiceModel(QAbstractListModel):
    """Python-owned row values; never borrow native QStandardItem wrappers.

    The bundled Qt/PySide runtime can retain wrappers for items deleted by
    QComboBox.clear(). Reusing those addresses may then mis-wrap a network
    reply as a model item. A value-only model avoids that lifecycle entirely.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        label, code, _enabled = self._rows[index.row()]
        if role in {Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole}:
            return label
        if role == Qt.ItemDataRole.UserRole:
            return code
        return None

    def flags(self, index):
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return Qt.ItemFlag.NoItemFlags
        flags = Qt.ItemFlag.ItemIsSelectable
        return flags | Qt.ItemFlag.ItemIsEnabled if self._rows[index.row()][2] else flags

    def replace(self, rows):
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()


class SampleSelector(QComboBox):
    textChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._choice_model = SampleChoiceModel(self)
        self.setModel(self._choice_model)
        self.online = False
        self.factory_mode = False
        self._choices_signature = None
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.currentTextChanged.connect(lambda _text: self.textChanged.emit(self.text()))
        self.setPlaceholderText('离线可自由填写样机编号')

    def text(self) -> str:
        return str(self.currentData() or '') if self.online and not self.factory_mode else self.currentText()

    def setText(self, text: str) -> None:
        if self.online and not self.factory_mode:
            index = self.findData(text)
            if index >= 0 and bool(self._choice_model.flags(self._choice_model.index(index, 0)) & Qt.ItemFlag.ItemIsEnabled):
                self.setCurrentIndex(index)
        else:
            self.setEditText(text)

    def setPlaceholderText(self, text: str) -> None:
        if self.lineEdit() is not None:
            self.lineEdit().setPlaceholderText(text)

    def setReadOnly(self, _value: bool) -> None:
        # Platform profiles cannot lock the offline free-text field.
        pass

    def update_choices(self, samples: list[dict], limb: str, online: bool, factory_mode: bool = False) -> None:
        choices = sample_choices(samples, limb) if online and not factory_mode else []
        signature = (online, factory_mode, limb, tuple((item.get('code'), bool(item.get('running'))) for item in choices)) if online else (False, factory_mode)
        if signature == self._choices_signature:
            return
        self._choices_signature = signature
        previous = self.text()
        previous_online = self.online
        self.blockSignals(True)
        self.online = online
        self.factory_mode = factory_mode
        self.setToolTip('工厂模式：可填写未登记 SN，保留大小写及前导零；测试先保存在本机。' if factory_mode else '平台在线：仅可选择当前部位已建档且未运行的样机，每5秒刷新。' if online else '离线模式：可自由填写样机编号，测试先保存在本机。')
        self.setEditable(not online or factory_mode)
        if self.lineEdit() is not None:
            self.lineEdit().setMaxLength(200)
        if online and not factory_mode:
            rows = [('请选择对应部位的样机编号', '', True)]
            for item in choices:
                code = str(item.get('code') or '')
                rows.append((code + (' 在运行' if item.get('running') else ''), code, not bool(item.get('running'))))
            self._choice_model.replace(rows)
            index = self.findData(previous)
            self.setCurrentIndex(max(0, index))
        else:
            self._choice_model.replace([])
            self.setEditText(previous)
            self.setPlaceholderText('工厂 SN（保留大小写及前导零）' if factory_mode else '离线可自由填写样机编号')
        self.blockSignals(False)
        if self.text() != previous or previous_online != online:
            self.textChanged.emit(self.text())

    def selection_allowed(self) -> bool:
        return bool(self.text().strip()) if self.factory_mode else not self.online or (bool(self.text()) and bool(self._choice_model.flags(self._choice_model.index(self.currentIndex(), 0)) & Qt.ItemFlag.ItemIsEnabled))
