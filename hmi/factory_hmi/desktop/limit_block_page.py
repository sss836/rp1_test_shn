"""Module limit block commissioning page. Live motor commands are not enabled here."""
from __future__ import annotations

import json
import math

from PySide6.QtCore import QIODevice, QSaveFile, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from factory_hmi.core.limit_block_check import Coordinates, LimitCheckSimulation, MODULES, nominal_station

PHASES = {
    "prepared": "方案已锁定，等待模拟连接", "connected": "反馈已核对，等待模拟使能",
    "enabled": "已模拟使能，等待复位", "resetting": "正在平滑复位并等待到位反馈",
    "ready": "已回到检查起点，等待开始搜索", "approach": "接近参考限位，收集反馈",
    "baseline": "取得接近位置的力矩基线", "search": "低速搜索候选阻挡点",
    "unload": "停止推进，核对可用退路", "retreat": "沿实测姿态缓退",
    "review": "本项缓退已核对，等待人工确认", "returning": "返回起点并等待停稳",
    "between": "回到检查起点，准备下一项",
    "completed": "模拟检查结束 · 已失能", "aborted": "模拟检查已中止 · 已失能",
}
OUTCOMES = {
    "within_reference": ("参考范围内", "#117752"), "early": ("提前阻挡", "#a95500"),
    "late": ("超出参考", "#b42318"), "not_detected": ("未检出", "#a95500"),
    "uncovered": ("未覆盖", "#62768e"), "aborted": ("中止", "#b42318"),
}


def add_button(row, label, action, *, primary=False):
    button = QPushButton(label)
    button.setObjectName("primaryButton" if primary else "outlineButton")
    button.clicked.connect(action)
    row.addWidget(button)
    return button


class LimitBlockPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tabPage")
        self.session: LimitCheckSimulation | None = None
        self.table_stamp = None
        self.rows_editable = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 5, 12, 10)
        layout.setSpacing(12)
        title = QLabel("限位块检查")
        title.setObjectName("pageHeading")
        layout.addWidget(title)
        intro = QLabel("模拟验收 · 按关节逐项检查限位块位置。实机入口待核定工装搜索范围和力矩参数。")
        intro.setObjectName("pageDescription")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        inputs = QHBoxLayout()
        self.limb = QComboBox()
        for key, label in MODULES:
            self.limb.addItem(label, key)
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("输入样机编号")
        self.serial.setMaxLength(120)
        self.scenario = QComboBox()
        for key, label in LimitCheckSimulation.SCENARIOS.items():
            self.scenario.addItem(label, key)
        for label, widget in (("模块", self.limb), ("样机", self.serial), ("模拟工况", self.scenario)):
            inputs.addWidget(QLabel(label))
            inputs.addWidget(widget, 1)
        self.apply_button = add_button(inputs, "应用检查方案", self._prepare, primary=True)
        layout.addLayout(inputs)

        state = QHBoxLayout()
        self.state = QLabel()
        self.state.setWordWrap(True)
        state.addWidget(self.state, 1)
        self.digest = QLabel("未应用")
        state.addWidget(self.digest)
        layout.addLayout(state)

        actions = QHBoxLayout()
        self.connect_button = add_button(actions, "连接", lambda: self._act("connect"))
        self.enable_button = add_button(actions, "使能", lambda: self._act("enable"))
        self.reset_button = add_button(actions, "复位", lambda: self._act("reset"))
        self.start_button = add_button(actions, "开始搜索", self._start, primary=True)
        self.abort_button = add_button(actions, "中止", self._abort)
        actions.addStretch(1)
        self.export_button = add_button(actions, "导出 JSON", self._export)
        layout.addLayout(actions)

        self.position = QLabel()
        self.position.setObjectName("pageDescription")
        self.position.setWordWrap(True)
        layout.addWidget(self.position)

        selection = QHBoxLayout()
        self.select_all = add_button(selection, "全选", lambda: self._select(True))
        self.select_none = add_button(selection, "清除选择", lambda: self._select(False))
        selection.addStretch(1)
        layout.addLayout(selection)

        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["选择", "关节 / 方向", "电机映射", "参考角 (°)", "候选角 (°)", "偏差 (°)", "实退 (°)", "结果"]
        )
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for col in (1, 2, 7):
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(330)
        layout.addWidget(self.table, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.limb.currentIndexChanged.connect(self._load_rows)
        self._load_rows()

    def _load_rows(self):
        self.table_stamp = None
        self.rows_editable = None
        self.session = None
        self.coordinates = Coordinates(nominal_station(self.limb.currentData()))
        self.table.setRowCount(len(self.coordinates.directions))
        for row, spec in enumerate(self.coordinates.directions):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, check)
            values = [f"{spec.label} · {spec.label_direction}", ", ".join(spec.motor_labels),
                      f"{spec.reference_deg:+.3f}".rstrip("0").rstrip("."), "—", "—", "—", "待检查"]
            for col, value in enumerate(values, 1):
                item = QTableWidgetItem(value)
                if col == 1:
                    item.setToolTip(spec.joint)
                if 3 <= col <= 6:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
        self._refresh()

    def _select(self, checked):
        for row in range(self.table.rowCount()):
            self.table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def _prepare(self):
        selected = [i for i in range(self.table.rowCount())
                    if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]
        try:
            session = LimitCheckSimulation(self.limb.currentData(), self.serial.text(), selected,
                                           self.scenario.currentData(), station=self.coordinates.station)
        except ValueError as exc:
            QMessageBox.warning(self, "方案无法应用", str(exc))
            return
        self.session = session
        self.table_stamp = None
        self._refresh()

    def _act(self, action):
        if self.session is None:
            return
        try:
            getattr(self.session, action)(self.session.digest)
        except (ValueError, RuntimeError) as exc:
            QMessageBox.warning(self, "操作未执行", str(exc))
        self._refresh()

    def _start(self):
        if not self.session:
            return
        if self.session.phase == "review":
            text = "已复核本项模拟缓退与候选结果，可以继续？"
        else:
            text = "已核对模拟方案摘要、模块映射及当前起点角差，可以开始？"
        if QMessageBox.question(self, "确认开始", text) == QMessageBox.StandardButton.Yes:
            self._act("start")

    def _abort(self):
        if self.session is not None:
            self.session.abort()
            self._refresh()

    def _tick(self):
        if self.session is not None:
            for _ in range(8):  # 8× simulated clock, unchanged 20 Hz evidence samples
                self.session.tick()
            self._refresh()

    def shutdown(self):
        self.timer.stop()
        if self.session is not None:
            self.session.abort("窗口关闭")

    @staticmethod
    def _angle(value) -> str:
        return "—" if value is None else f"{value:+.2f}"

    def _refresh(self):
        session = self.session
        phase = session.phase if session else "idle"
        editable = phase in {"idle", "completed", "aborted"}
        for widget in (self.limb, self.serial, self.scenario, self.apply_button, self.select_all, self.select_none):
            widget.setEnabled(editable)
        if editable != self.rows_editable:
            self.rows_editable = editable
            for row in range(self.table.rowCount()):
                flags = Qt.ItemFlag.ItemIsEnabled
                if editable:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                self.table.item(row, 0).setFlags(flags)
        self.connect_button.setEnabled(phase == "prepared")
        self.enable_button.setEnabled(phase == "connected")
        self.reset_button.setEnabled(phase in {"enabled", "ready"})
        self.start_button.setEnabled(phase in {"ready", "review"})
        last = phase == "review" and session is not None and session.index == len(session.plan) - 1
        self.start_button.setText("确认并收尾" if last else "确认并继续" if phase == "review" else "开始搜索")
        self.abort_button.setEnabled(not editable)
        self.export_button.setEnabled(session is not None and phase in {"completed", "aborted", "review"})
        self.state.setText(PHASES.get(phase, "选择检查方向，然后应用方案") +
                           (f" · {session.reason}" if session and session.reason else ""))
        if not session:
            self.digest.setText("未应用")
            self.digest.setToolTip("")
            self.position.setText("复位目标时程 4 秒。每个方向缓退后须确认，模拟时钟为 8 倍速。")
            return
        self.digest.setText(f"方案 {session.digest[:12]}")
        self.digest.setToolTip(session.digest)
        task = session.task
        start_gap = max(abs(math.degrees(a - b)) for a, b in zip(session.actual, session.coords.home))
        measured = math.degrees(session.actual[task.index])
        detail = (f"第 {min(session.index + 1, len(session.plan))}/{len(session.plan)} 项 · "
                  f"{task.label} {task.label_direction} · 实测 {measured:+.2f}° · "
                  f"逻辑力矩 {session.torque[task.index]:.2f} N·m")
        if phase in {"connected", "enabled", "resetting", "ready"}:
            detail += f" · 最大起点角差 {start_gap:.2f}°"
        self.position.setText(detail)
        if phase in {"resetting", "approach", "baseline", "search", "unload", "retreat", "returning", "between"}:
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        stamp = (session.digest, phase in {"completed", "aborted"}, len(session.results))
        if stamp == self.table_stamp:
            return
        self.table_stamp = stamp
        report = {(r["joint"], r["sign"]): r for r in session.report()["results"]}
        for row, spec in enumerate(self.coordinates.directions):
            result = report.get((spec.joint, spec.sign))
            text, color = "未选择", "#62768e"
            values = ("—", "—", "—")
            if result is not None:
                text, color = OUTCOMES[result["outcome"]]
                if result["outcome"] == "uncovered" and not editable:
                    text = "待检查"
                values = tuple(self._angle(result.get(key)) for key in ("actual_deg", "deviation_deg", "retreat_deg"))
            for col, value in enumerate((*values, text), 4):
                self.table.item(row, col).setText(value)
            self.table.item(row, 7).setForeground(QColor(color))

    def _export(self):
        if self.session is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "保存模拟限位检查报告", "limit-check-simulation.json", "JSON (*.json)")
        if not path:
            return
        payload = json.dumps(self.session.report(), allow_nan=False, ensure_ascii=False, indent=2).encode()
        saver = QSaveFile(path)
        if (not saver.open(QIODevice.OpenModeFlag.WriteOnly)
                or saver.write(payload) != len(payload) or not saver.commit()):
            QMessageBox.warning(self, "报告保存失败", saver.errorString())
