"""PLC power-cabinet page for the PySide6 desktop."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from factory_hmi.client import GatewayClient
from factory_hmi.desktop.theme import set_tone


def _text(value: Any) -> str:
    if value is None:
        return "INVALID"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, dict, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _at(values: Any, index: int, default: Any = None) -> Any:
    if isinstance(values, (list, tuple)) and len(values) > index:
        return values[index]
    return default


class _TrendChart(QWidget):
    COLORS = (
        QColor("#2375C9"),
        QColor("#1F9D69"),
        QColor("#D9822B"),
        QColor("#8B5CF6"),
    )

    def __init__(self, title: str, key: str, unit: str, maximum: float) -> None:
        super().__init__()
        self.title = title
        self.key = key
        self.unit = unit
        self.maximum = maximum
        self.samples: list[Mapping[str, Any]] = []
        self.setMinimumHeight(190)

    def set_samples(self, samples: list[Mapping[str, Any]]) -> None:
        self.samples = samples[-300:]
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        left, top, right, bottom = 48, 30, 16, 28
        plot_width = max(1, self.width() - left - right)
        plot_height = max(1, self.height() - top - bottom)
        painter.setPen(QColor("#24435E"))
        painter.drawText(left, 20, f"{self.title}  (0–{self.maximum:g} {self.unit})")
        painter.setPen(QPen(QColor("#DFE6EE"), 1))
        for row in range(5):
            y = top + plot_height * row / 4
            painter.drawLine(left, int(y), left + plot_width, int(y))
            value = self.maximum * (4 - row) / 4
            painter.setPen(QColor("#718096"))
            painter.drawText(2, int(y) + 4, f"{value:g}")
            painter.setPen(QPen(QColor("#DFE6EE"), 1))
        if len(self.samples) < 2:
            painter.setPen(QColor("#718096"))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "等待实时样本",
            )
            return
        count = len(self.samples)
        for channel in range(4):
            path = QPainterPath()
            started = False
            for index, sample in enumerate(self.samples):
                mask = int(sample.get("AnalogValidMask") or 0)
                values = sample.get(self.key)
                if not isinstance(values, list) or not mask & (1 << channel):
                    started = False
                    continue
                try:
                    value = max(0.0, min(self.maximum, float(values[channel])))
                except (IndexError, TypeError, ValueError):
                    started = False
                    continue
                x = left + plot_width * index / max(1, count - 1)
                y = top + plot_height * (1.0 - value / self.maximum)
                point = QPointF(x, y)
                if not started:
                    path.moveTo(point)
                    started = True
                else:
                    path.lineTo(point)
            painter.setPen(QPen(self.COLORS[channel], 2))
            painter.drawPath(path)
            painter.drawText(left + channel * 72, self.height() - 7, f"CH{channel + 1}")


class PlcCabinetPage(QWidget):
    def __init__(
        self,
        client: GatewayClient,
        runner: Callable[..., None],
        operator_provider: Callable[[], str],
    ) -> None:
        super().__init__()
        self.client = client
        self.runner = runner
        self.operator_provider = operator_provider
        self.setObjectName("tabPage")
        self._pending = False
        self._snapshot: dict[str, Any] = {}
        self._setpoint_dirty = False
        self._setpoint_edit_revision = 0
        self._submitted_setpoint_revision: int | None = None
        self._last_setpoint_feedback = ""
        self._build()
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        QTimer.singleShot(0, self.refresh)

    def stop(self) -> None:
        self.timer.stop()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(12)
        title = QLabel("PLC 配电柜")
        title.setObjectName("pageHeading")
        description = QLabel(
            "系统总使能严格执行“主回路 → PS1 实际输出 → 通道许可”；"
            "所有操作发送逻辑命令并等待 PLC 序号确认。"
        )
        description.setObjectName("pageDescription")
        self.description = description
        layout.addWidget(title)
        layout.addWidget(description)

        flow = QFrame()
        flow.setObjectName("phasePanel")
        flow_layout = QHBoxLayout(flow)
        flow_layout.setContentsMargins(14, 10, 14, 10)
        flow_layout.setSpacing(8)
        self.flow_labels: list[QLabel] = []
        for index, text in enumerate(
            ("1  PLC通信", "2  主回路", "3  PS1输出", "4  通道投入")
        ):
            label = QLabel(text)
            label.setObjectName("flowStep")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.flow_labels.append(label)
            flow_layout.addWidget(label, 1)
            if index < 3:
                arrow = QLabel("→")
                arrow.setObjectName("flowArrow")
                flow_layout.addWidget(arrow)
        layout.addWidget(flow)

        control_row = QHBoxLayout()
        control_row.setSpacing(12)

        system_group = QGroupBox("系统总控（安全顺序）")
        system_group.setObjectName("workflowCard")
        system_layout = QGridLayout(system_group)
        system_layout.setContentsMargins(14, 18, 14, 14)
        system_layout.setHorizontalSpacing(10)
        system_layout.setVerticalSpacing(10)
        self.communication = QLabel("通信：连接中")
        self.communication.setObjectName("infoPanel")
        self.phase = QLabel("阶段：—")
        self.phase.setObjectName("infoPanel")
        self.main_state = QLabel("主回路：—")
        self.main_state.setObjectName("infoPanel")
        self.main_state.setWordWrap(True)
        self.ps1_state = QLabel("PS1：—")
        self.ps1_state.setObjectName("infoPanel")
        system_layout.addWidget(self.communication, 0, 0)
        system_layout.addWidget(self.phase, 0, 1)
        system_layout.addWidget(self.main_state, 1, 0, 1, 2)
        system_layout.addWidget(self.ps1_state, 2, 0, 1, 2)

        self.connect_button = QPushButton("连接 PLC")
        self.connect_button.setObjectName("outlineButton")
        self.connect_button.clicked.connect(
            lambda: self._command(
                "连接 PLC", lambda: self.client.plc_connect(self._user())
            )
        )
        self.start_button = QPushButton("系统总使能（顺序启动）")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QPushButton("正常总停止")
        self.stop_button.setObjectName("criticalOutlineButton")
        self.stop_button.clicked.connect(
            lambda: self._command(
                "正常停止", lambda: self.client.plc_stop(self._user())
            )
        )
        self.all_stop_button = QPushButton("全部停止")
        self.all_stop_button.setObjectName("dangerButton")
        self.all_stop_button.clicked.connect(
            lambda: self._command(
                "全部停止", lambda: self.client.plc_all_stop(self._user())
            )
        )
        self.reset_button = QPushButton("故障复位脉冲")
        self.reset_button.setObjectName("outlineButton")
        self.reset_button.clicked.connect(
            lambda: self._command(
                "故障复位", lambda: self.client.plc_reset_fault(self._user())
            )
        )
        primary_controls = QHBoxLayout()
        primary_controls.setSpacing(8)
        primary_controls.addWidget(self.connect_button)
        primary_controls.addWidget(self.start_button, 2)
        primary_controls.addWidget(self.stop_button)
        system_layout.addLayout(primary_controls, 3, 0, 1, 2)
        safety_controls = QHBoxLayout()
        safety_controls.setSpacing(8)
        safety_controls.addWidget(self.reset_button)
        safety_controls.addWidget(self.all_stop_button)
        system_layout.addLayout(safety_controls, 4, 0, 1, 2)
        self.sequence_feedback = QLabel("总控反馈：尚未操作")
        self.sequence_feedback.setObjectName("controlHint")
        self.sequence_feedback.setWordWrap(True)
        system_layout.addWidget(self.sequence_feedback, 5, 0, 1, 2)
        control_row.addWidget(system_group, 3)

        setpoint_group = QGroupBox("PS1 电源设定")
        setpoint_group.setObjectName("workflowCard")
        setpoint_layout = QGridLayout(setpoint_group)
        setpoint_layout.setContentsMargins(14, 18, 14, 14)
        setpoint_layout.setHorizontalSpacing(10)
        setpoint_layout.setVerticalSpacing(10)
        voltage_label = QLabel("目标电压")
        voltage_label.setObjectName("fieldLabel")
        self.voltage_setpoint = QDoubleSpinBox()
        self.voltage_setpoint.setDecimals(3)
        self.voltage_setpoint.setSuffix(" V")
        self.voltage_setpoint.setValue(3.0)
        self.voltage_setpoint.setKeyboardTracking(False)
        self.voltage_setpoint.valueChanged.connect(self._mark_setpoint_dirty)
        self.voltage_setpoint.lineEdit().textEdited.connect(self._mark_setpoint_dirty)
        current_label = QLabel("限流值")
        current_label.setObjectName("fieldLabel")
        self.current_setpoint = QDoubleSpinBox()
        self.current_setpoint.setDecimals(3)
        self.current_setpoint.setSuffix(" A")
        self.current_setpoint.setValue(0.5)
        self.current_setpoint.setKeyboardTracking(False)
        self.current_setpoint.valueChanged.connect(self._mark_setpoint_dirty)
        self.current_setpoint.lineEdit().textEdited.connect(self._mark_setpoint_dirty)
        setpoint_layout.addWidget(voltage_label, 0, 0)
        setpoint_layout.addWidget(self.voltage_setpoint, 0, 1)
        setpoint_layout.addWidget(current_label, 1, 0)
        setpoint_layout.addWidget(self.current_setpoint, 1, 1)
        self.apply_setpoints_button = QPushButton("应用设定并顺序启动")
        self.apply_setpoints_button.setObjectName("primaryButton")
        self.apply_setpoints_button.clicked.connect(self._apply_setpoints)
        setpoint_layout.addWidget(self.apply_setpoints_button, 2, 0, 1, 2)
        self.setpoint_summary = QLabel("设定回读：—\nPS1实际输出：—")
        self.setpoint_summary.setObjectName("infoPanel")
        self.setpoint_summary.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_summary, 3, 0, 1, 2)
        self.setpoint_feedback = QLabel("设定反馈：尚未操作")
        self.setpoint_feedback.setObjectName("controlHint")
        self.setpoint_feedback.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_feedback, 4, 0, 1, 2)
        self.setpoint_hint = QLabel("设定值将在主回路与PS1通信就绪后自动下发。")
        self.setpoint_hint.setObjectName("muted")
        self.setpoint_hint.setWordWrap(True)
        setpoint_layout.addWidget(self.setpoint_hint, 5, 0, 1, 2)
        control_row.addWidget(setpoint_group, 2)
        layout.addLayout(control_row)

        self.fault = QLabel("故障：无")
        self.fault.setObjectName("faultBarOk")
        self.fault.setWordWrap(True)
        layout.addWidget(self.fault)

        channel_group = QGroupBox("CH1～CH4 单路使能")
        channel_group.setObjectName("workflowCard")
        channel_layout = QHBoxLayout(channel_group)
        channel_layout.setContentsMargins(12, 18, 12, 12)
        channel_layout.setSpacing(10)
        self.channel_checks: list[QCheckBox] = []
        self.channel_titles: list[QLabel] = []
        self.channel_physical: list[QLabel] = []
        self.channel_permits: list[QLabel] = []
        self.channel_measurements: list[QLabel] = []
        self.channel_hints: list[QLabel] = []
        self.channel_buttons: list[QPushButton] = []
        for row in range(4):
            channel = row + 1
            card = QFrame()
            card.setObjectName("channelCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 10, 12, 12)
            card_layout.setSpacing(7)
            card_title = QLabel(f"CH{channel}  ·  DUT{channel} / can{row}")
            card_title.setObjectName("channelTitle")
            physical_label = QLabel("物理许可：—")
            physical_label.setObjectName("channelState")
            permit_label = QLabel("PLC通道许可：—")
            permit_label.setObjectName("channelState")
            measurement = QLabel("0.00 V   0.00 A\n0.00 W   模拟量 —")
            measurement.setObjectName("channelMetric")
            checkbox = QCheckBox("系统总使能时投入")
            checkbox.setChecked(True)
            button = QPushButton(f"CH{channel} 单路投入")
            button.setObjectName("startButton")
            button.clicked.connect(
                lambda _checked=False, selected=channel: self._toggle_channel(selected)
            )
            hint = QLabel("等待状态")
            hint.setObjectName("controlHint")
            hint.setWordWrap(True)
            card_layout.addWidget(card_title)
            card_layout.addWidget(physical_label)
            card_layout.addWidget(permit_label)
            card_layout.addWidget(measurement)
            card_layout.addWidget(checkbox)
            card_layout.addWidget(button)
            card_layout.addWidget(hint)
            self.channel_checks.append(checkbox)
            self.channel_titles.append(card_title)
            self.channel_physical.append(physical_label)
            self.channel_permits.append(permit_label)
            self.channel_measurements.append(measurement)
            self.channel_hints.append(hint)
            self.channel_buttons.append(button)
            channel_layout.addWidget(card, 1)
        layout.addWidget(channel_group)

        trend_group = QGroupBox("实时趋势")
        trend_group.setObjectName("workflowCard")
        charts = QHBoxLayout(trend_group)
        charts.setContentsMargins(10, 16, 10, 8)
        self.voltage_chart = _TrendChart("电压趋势", "Voltage", "V", 100.0)
        self.current_chart = _TrendChart("电流趋势", "Current", "A", 50.0)
        charts.addWidget(self.voltage_chart, 1)
        charts.addWidget(self.current_chart, 1)
        layout.addWidget(trend_group)

        log_group = QGroupBox(
            "操作日志（UTC时间 / 用户 / 旧值 / 新值 / 序号 / PLC确认）"
        )
        log_group.setObjectName("workflowCard")
        log_layout = QVBoxLayout(log_group)
        log_layout.setContentsMargins(10, 16, 10, 10)
        self.logs = QTableWidget(0, 7)
        self.logs.setHorizontalHeaderLabels(
            ("时间", "用户", "字段", "旧值", "新值", "命令序号", "确认结果")
        )
        self.logs.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.logs.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.logs.verticalHeader().setVisible(False)
        self.logs.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.logs.setMinimumHeight(170)
        log_layout.addWidget(self.logs)
        layout.addWidget(log_group, 1)

    def _user(self) -> str:
        return self.operator_provider().strip() or "local-operator"

    def refresh(self) -> None:
        if self._pending:
            return
        self._pending = True
        self.runner(
            "刷新 PLC 状态",
            self.client.plc_snapshot,
            self._receive,
            quiet=True,
            on_finished=lambda: setattr(self, "_pending", False),
        )

    def _command(self, label: str, operation: Callable[[], Any]) -> None:
        if self._pending:
            return
        self._pending = True
        self.runner(
            label,
            operation,
            self._receive,
            on_finished=lambda: setattr(self, "_pending", False),
        )

    def _start(self) -> None:
        if self._pending:
            return
        # Commit drafts on the GUI thread before the runner queues its worker.
        self.voltage_setpoint.interpretText()
        self.current_setpoint.interpretText()
        voltage = self.voltage_setpoint.value()
        current = self.current_setpoint.value()
        user = self._user()
        channels = [
            index
            for index, checkbox in enumerate(self.channel_checks, start=1)
            if checkbox.isChecked()
        ]
        self._submitted_setpoint_revision = self._setpoint_edit_revision
        self._command(
            "启动 PLC 供电顺序",
            lambda: self.client.plc_start(channels, user, voltage, current),
        )

    def _toggle_channel(self, channel: int) -> None:
        status = self._snapshot.get("status")
        permits = status.get("ChannelPermit", []) if isinstance(status, Mapping) else []
        enabled = not bool(permits[channel - 1]) if len(permits) >= channel else True
        self._command(
            f"CH{channel} {'投入' if enabled else '切除'}",
            lambda: self.client.plc_set_channel(channel, enabled, self._user()),
        )

    def _mark_setpoint_dirty(self, _value: float | str) -> None:
        self._setpoint_dirty = True
        self._setpoint_edit_revision += 1

    def _apply_setpoints(self) -> None:
        self._start()

    def _receive(self, payload: Any) -> None:
        value = payload
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping):
            return
        self._snapshot = dict(value)
        self._render()

    def _render(self) -> None:
        snapshot = self._snapshot
        communication = str(snapshot.get("communication_state") or "DISCONNECTED")
        stale = bool(snapshot.get("stale"))
        fresh = communication == "LIVE" and not stale
        read_only = bool(snapshot.get("read_only"))
        live = fresh and not read_only
        driver = str(snapshot.get("driver") or "未知")
        connection_type = "PLC 模拟" if driver == "mock" else f"PLC 实机 · {driver}"
        access = "只读监控" if read_only else "控制模式"
        self.description.setText(
            "读取实物 PLC 状态；当前未启用心跳、启停、使能和设定值写入。"
            if read_only else "系统总使能严格执行“主回路 → PS1 实际输出 → 通道许可”；操作等待 PLC 序号确认。"
        )
        self.communication.setText(
            f"{connection_type} · {access} · {snapshot.get('endpoint', '')}\n通信：{communication}{' / STALE' if stale else ''}"
        )
        set_tone(
            self.communication,
            "warning" if driver == "mock" else "success" if fresh else "danger",
        )
        phase = str(snapshot.get("phase") or "—")
        self.phase.setText(f"阶段：{phase}")
        set_tone(
            self.phase,
            "success"
            if phase == "RUNNING"
            else "warning"
            if phase.startswith(("START_", "WAIT_", "STOP_"))
            else "danger"
            if phase in {"FAULT", "COMM_LOST", "STALE"}
            else "neutral",
        )
        status = snapshot.get("status")
        status = status if isinstance(status, Mapping) else {}
        command = snapshot.get("command")
        command = command if isinstance(command, Mapping) else {}
        self.main_state.setText(
            "主回路  ·  "
            f"请求 {_text(command.get('MainEnable'))}  /  "
            f"KM0反馈 {_text(status.get('MainContactorFB'))}  /  "
            f"主回路就绪 {_text(status.get('MainReady'))}"
        )
        self.ps1_state.setText(
            "PS1输出  ·  "
            f"通信 {_text(status.get('PS1CommOK'))}  /  "
            f"请求 {_text(command.get('PS1OutputEnable'))}  /  "
            f"实际 {_text(status.get('PS1ActualOutput'))}"
        )
        faulted = bool(status.get("FaultLatched") or status.get("PS1Fault"))
        last_error = str(snapshot.get("last_error") or "")
        self.fault.setText(
            f"故障：{'已锁存' if faulted else '无'}  "
            f"FaultCode={status.get('FaultCode', '—')}"
            + (f"  |  {last_error}" if last_error else "")
        )
        self.fault.setObjectName(
            "faultBarError" if faulted or last_error else "faultBarOk"
        )
        self.fault.style().unpolish(self.fault)
        self.fault.style().polish(self.fault)

        main_ready = bool(status.get("MainReady"))
        ps1_actual = bool(status.get("PS1ActualOutput"))
        permits = status.get("ChannelPermit") or [False] * 4
        any_channel = any(bool(_at(permits, index, False)) for index in range(4))
        flow_states = (
            (f"1  PLC通信  {'LIVE' if live else communication}", live),
            (f"2  主回路  {'已就绪' if main_ready else '未就绪'}", main_ready),
            (f"3  PS1输出  {'已开启' if ps1_actual else '已关闭'}", ps1_actual),
            (
                f"4  通道投入  {'运行中' if any_channel else '未投入'}",
                any_channel,
            ),
        )
        for index, (text, confirmed) in enumerate(flow_states):
            label = self.flow_labels[index]
            label.setText(text)
            waiting = (
                index == 1
                and phase in {"WAIT_MAIN_ACK", "WAIT_MAIN_READY"}
                or index == 2
                and phase
                in {
                    "WAIT_PS1_COMM",
                    "WAIT_PS1_STOP_ACK",
                    "WAIT_PS1_STOP_READY",
                    "WAIT_PS1_ACK",
                    "WAIT_PS1_OUTPUT",
                }
                or index == 3
                and phase in {"WAIT_CHANNEL_ACK", "WAIT_CHANNEL_PERMIT"}
            )
            set_tone(
                label,
                "success"
                if confirmed
                else "warning"
                if waiting
                else "danger"
                if index == 0 and not live
                else "neutral",
            )

        physical = status.get("PhysicalPermit") or [False] * 4
        voltage = status.get("Voltage") or [None] * 4
        current = status.get("Current") or [None] * 4
        power = status.get("Power") or [None] * 4
        valid_mask = int(status.get("AnalogValidMask") or 0)
        can_map = snapshot.get("dut_can_map") or {}
        feedback = snapshot.get("feedback") or {}
        feedback = feedback if isinstance(feedback, Mapping) else {}
        group_feedback = feedback.get("ChannelEnable") or {}
        for row in range(4):
            item_feedback = feedback.get(f"ChannelEnable{row + 1}") or group_feedback
            item_feedback = item_feedback if isinstance(item_feedback, Mapping) else {}
            physical_value = _at(physical, row, None)
            permit_value = bool(_at(permits, row, False))
            self.channel_titles[row].setText(
                f"CH{row + 1}  ·  DUT{row + 1} / "
                f"{can_map.get(f'DUT{row + 1}', f'can{row}')}"
            )
            self.channel_physical[row].setText(f"物理许可：{_text(physical_value)}")
            set_tone(
                self.channel_physical[row],
                "success"
                if physical_value is True
                else "danger"
                if physical_value is False
                else "warning",
            )
            self.channel_permits[row].setText(f"PLC通道许可：{_text(permit_value)}")
            set_tone(
                self.channel_permits[row],
                "success" if permit_value else "neutral",
            )
            analog_state = "有效" if valid_mask & (1 << row) else "无效"
            self.channel_measurements[row].setText(
                f"{_text(_at(voltage, row))} V   {_text(_at(current, row))} A\n"
                f"{_text(_at(power, row))} W   模拟量{analog_state}"
            )
            feedback_state = str(item_feedback.get("state") or "confirmed")
            feedback_detail = str(item_feedback.get("detail") or "")
            if feedback_state == "pending":
                reason = "等待PLC确认命令序号"
            elif permit_value:
                reason = "已投入；可单独切除"
            elif not live:
                reason = "PLC通信不可用"
            elif phase != "RUNNING":
                reason = "请先点击“系统总使能（顺序启动）”"
            elif not ps1_actual:
                reason = "等待PS1实际输出"
            elif physical_value is False:
                reason = "物理许可缺失，禁止投入"
            elif feedback_state in {"rejected", "timeout"}:
                reason = feedback_detail or f"上次命令{feedback_state}"
            elif physical_value is None:
                reason = "原始许可未提供，以PLC最终许可为准"
            else:
                reason = "条件满足，可投入"
            button = self.channel_buttons[row]
            button.setText(
                f"CH{row + 1} 单路切除" if permit_value else f"CH{row + 1} 单路投入"
            )
            can_operate = (
                live
                and feedback_state != "pending"
                and (
                    permit_value
                    or (
                        phase == "RUNNING"
                        and ps1_actual
                        and physical_value is not False
                    )
                )
            )
            button.setEnabled(can_operate)
            button.setObjectName(
                "criticalOutlineButton" if permit_value else "startButton"
            )
            button.style().unpolish(button)
            button.style().polish(button)
            button.setToolTip(reason)
            self.channel_hints[row].setText(reason)
            self.channel_checks[row].setEnabled(live and phase == "OFF")

        start_feedback = feedback.get("Start") or {}
        stop_feedback = feedback.get("Stop") or {}
        start_feedback = start_feedback if isinstance(start_feedback, Mapping) else {}
        stop_feedback = stop_feedback if isinstance(stop_feedback, Mapping) else {}
        start_state = str(start_feedback.get("state") or "confirmed")
        stop_state = str(stop_feedback.get("state") or "confirmed")
        start_detail = str(start_feedback.get("detail") or "")
        stop_detail = str(stop_feedback.get("detail") or "")
        if start_state == "pending":
            sequence_text = f"总启动：等待PLC确认 · {start_detail}"
        elif stop_state == "pending":
            sequence_text = f"正常总停止：等待PLC确认 · {stop_detail}"
        elif start_detail and start_detail != "not requested":
            sequence_text = f"总启动：{start_state} · {start_detail}"
        elif stop_detail and stop_detail != "not requested":
            sequence_text = f"正常总停止：{stop_state} · {stop_detail}"
        else:
            sequence_text = "总控反馈：尚未操作"
        self.sequence_feedback.setText(sequence_text)
        any_pending = any(
            isinstance(value, Mapping) and value.get("state") == "pending"
            for value in feedback.values()
        )
        self.start_button.setEnabled(
            live and phase == "OFF" and not faulted and not any_pending
        )
        self.stop_button.setEnabled(
            live
            and phase not in {"OFF", "DISCONNECTED", "COMM_LOST", "STALE"}
            and not any_pending
        )
        self.reset_button.setEnabled(live and faulted and not any_pending)
        self.all_stop_button.setEnabled(live)
        self.connect_button.setEnabled(communication != "LIVE")

        limits = snapshot.get("setpoint_limits")
        limits = limits if isinstance(limits, Mapping) else {}
        setpoint_enabled = bool(limits.get("enabled"))
        voltage_min = float(limits.get("voltage_min_v") or 0.0)
        voltage_max = float(limits.get("voltage_max_v") or 0.0)
        current_min = float(limits.get("current_min_a") or 0.0)
        current_max = float(limits.get("current_max_a") or 0.0)
        step = float(limits.get("step") or 0.001)
        setpoint_feedback = feedback.get("Setpoints") or {}
        setpoint_feedback = (
            setpoint_feedback if isinstance(setpoint_feedback, Mapping) else {}
        )
        setpoint_state = str(setpoint_feedback.get("state") or "confirmed")
        setpoint_detail = str(setpoint_feedback.get("detail") or "")
        if self._last_setpoint_feedback == "pending" and setpoint_state == "confirmed":
            if self._submitted_setpoint_revision == self._setpoint_edit_revision:
                self._setpoint_dirty = False
            self._submitted_setpoint_revision = None
        self._last_setpoint_feedback = setpoint_state
        ranges_valid = (
            setpoint_enabled
            and voltage_max > voltage_min
            and current_max > current_min
            and step > 0.0
        )
        if ranges_valid:
            for editor, minimum, maximum in (
                (self.voltage_setpoint, voltage_min, voltage_max),
                (self.current_setpoint, current_min, current_max),
            ):
                editor.blockSignals(True)
                # Even an unchanged setRange normalizes uncommitted text.
                if editor.minimum() != minimum or editor.maximum() != maximum:
                    editor.setRange(minimum, maximum)
                if editor.singleStep() != step:
                    editor.setSingleStep(step)
                editor.blockSignals(False)
            readback_available = bool(status.get("PS1CommOK"))
            should_sync = (
                readback_available
                and not self._setpoint_dirty
                and not self.voltage_setpoint.hasFocus()
                and not self.current_setpoint.hasFocus()
            )
            if should_sync:
                self.voltage_setpoint.blockSignals(True)
                self.current_setpoint.blockSignals(True)
                self.voltage_setpoint.setValue(
                    float(status.get("PS1SetVoltage") or 0.0)
                )
                self.current_setpoint.setValue(
                    float(status.get("PS1SetCurrent") or 0.0)
                )
                self.voltage_setpoint.blockSignals(False)
                self.current_setpoint.blockSignals(False)
        setpoint_can_edit = (
            ranges_valid
            and live
            and phase == "OFF"
            and not ps1_actual
            and setpoint_state != "pending"
            and not any_pending
        )
        self.voltage_setpoint.setEnabled(setpoint_can_edit)
        self.current_setpoint.setEnabled(setpoint_can_edit)
        self.apply_setpoints_button.setEnabled(setpoint_can_edit)
        self.setpoint_summary.setText(
            "设定回读："
            f"{float(status.get('PS1SetVoltage') or 0.0):.3f} V / "
            f"{float(status.get('PS1SetCurrent') or 0.0):.3f} A\n"
            "实际输出："
            f"{float(status.get('PS1OutputVoltage') or 0.0):.3f} V / "
            f"{float(status.get('PS1OutputCurrent') or 0.0):.3f} A"
        )
        if setpoint_detail == "not requested" or not setpoint_detail:
            setpoint_feedback_text = "设定反馈：尚未操作"
        else:
            setpoint_feedback_text = f"设定反馈：{setpoint_state} · {setpoint_detail}"
        self.setpoint_feedback.setText(setpoint_feedback_text)
        if not ranges_valid:
            setpoint_reason = "配置未提供有效的PS1设定范围，控制已禁用。"
        elif not live:
            setpoint_reason = "PLC通信不可用，禁止修改设定。"
        elif phase != "OFF" or ps1_actual:
            setpoint_reason = "运行中禁止修改；请先执行正常总停止。"
        elif setpoint_state == "pending" or any_pending:
            setpoint_reason = "等待PLC确认当前命令。"
        else:
            setpoint_reason = (
                f"允许范围：{voltage_min:g}–{voltage_max:g} V，"
                f"{current_min:g}–{current_max:g} A；将在总使能过程中下发并回读。"
            )
        self.setpoint_hint.setText(setpoint_reason)
        self.apply_setpoints_button.setToolTip(setpoint_reason)

        trend = snapshot.get("trend")
        trend_items = (
            [item for item in trend if isinstance(item, Mapping)]
            if isinstance(trend, list)
            else []
        )
        self.voltage_chart.set_samples(trend_items)
        self.current_chart.set_samples(trend_items)

        logs = snapshot.get("operation_log")
        log_items = (
            [item for item in logs if isinstance(item, Mapping)]
            if isinstance(logs, list)
            else []
        )
        self.logs.setRowCount(len(log_items))
        for row, entry in enumerate(reversed(log_items)):
            values = (
                str(entry.get("timestamp") or ""),
                str(entry.get("user") or ""),
                str(entry.get("field") or ""),
                _text(entry.get("old_value")),
                _text(entry.get("new_value")),
                _text(entry.get("command_sequence")),
                str(entry.get("confirmation") or ""),
            )
            for column, value in enumerate(values):
                self.logs.setItem(row, column, QTableWidgetItem(value))


__all__ = ["PlcCabinetPage"]
