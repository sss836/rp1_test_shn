"""Explicit peripheral operations; bounded polling and GUI-owned heartbeat."""
from __future__ import annotations

import json
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest, QNetworkReply
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout, QHeaderView,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget)

from factory_hmi.client import is_loopback_gateway
from factory_hmi.core.dexhand import AXES, arm_busy_for_hand


class DeviceHeartbeat(QObject):
    failed = Signal(str)

    def __init__(self, page):
        super().__init__(page)
        self.page, self.reply = page, None
        self.manager = QNetworkAccessManager(self)
        if is_loopback_gateway(page.client.base_url):
            self.manager.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.pulse)
        self.deadline = QTimer(self)
        self.deadline.setSingleShot(True)
        self.deadline.timeout.connect(self._timeout)
        self.timer.start()

    def pulse(self):
        page = self.page
        if self.reply is not None or not page.owned() or page.closed:
            return
        generation = page.state.get("generation")
        req = QNetworkRequest(QUrl(f"{page.client.base_url}/api/v1/device-tools/{page.kind}/heartbeat"))
        req.setRawHeader(b"Content-Type", b"application/json")
        req.setRawHeader(b"X-RP1-Client-ID", page.client.client_id.encode())
        if page.client.operator_token:
            req.setRawHeader(b"X-RP1-Operator-Token", page.client.operator_token.encode())
        reply = self.reply = self.manager.post(req, json.dumps({"generation": generation}).encode())
        reply.finished.connect(lambda: self._finished(reply, generation))
        self.deadline.start(500)

    def _timeout(self):
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()
            self.failed.emit("设备心跳超时；连续丢失 5 秒将停止设备")

    def _finished(self, reply, generation):
        if reply is not self.reply:
            return
        self.reply = None
        self.deadline.stop()
        try:
            if generation != self.page.state.get("generation") or self.page.closed:
                return
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if reply.error() != QNetworkReply.NoError or not status or int(status) >= 400:
                self.failed.emit("设备心跳被拒绝或断线，请检查状态")
        finally:
            reply.deleteLater()

    def stop(self):
        self.timer.stop()
        self.deadline.stop()
        reply, self.reply = self.reply, None
        if reply is not None:
            reply.abort()
            reply.deleteLater()


class DeviceToolsPage(QWidget):
    changed = Signal()

    def __init__(self, kind, client, run_rest, arm_snapshot, parent=None):
        super().__init__(parent)
        self.kind, self.client, self.run_rest, self.arm_snapshot = kind, client, run_rest, arm_snapshot
        self.state = {}
        self.poll_pending = self.action_pending = self.closed = False
        self.claim_unknown = False
        self.command_epoch = 0
        self.inflight = set()
        self.closing_requested = False
        self.buttons = {}
        self.action_error = ""
        self.status_label = QLabel()
        self.status_label.setObjectName("toolStatus")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.setStyleSheet("""
            QFrame#toolCard {background: #ffffff; border: 1px solid #d7e1eb; border-radius: 9px;}
            QLabel {background: transparent; color: #173248;}
            QLabel#toolTitle {font-size: 22px; font-weight: 700;}
            QLabel#toolHeading {font-size: 15px; font-weight: 650;}
            QLabel#toolHint {font-size: 13px; color: #526a7a;}
            QLabel#toolValue {font-size: 18px; font-weight: 650; color: #155d69;}
            QLabel#toolStatus {font-size: 13px; color: #455f72;}
            QPushButton {min-height: 26px; max-height: 34px; padding: 2px 15px; border-radius: 5px;}
            QPushButton#secondary {color: #21556d; background: #f3f8fa; border: 1px solid #b8cdd6;}
            QPushButton#secondary:hover {background: #e5f0f4;}
            QPushButton#secondary:disabled {color: #85949b; border-color: #dae2e7;}
            QLineEdit {background: white; border: 1px solid #bdcdd8; border-radius: 5px; padding: 4px 8px;}
            QTableWidget {background: white; alternate-background-color: #f4f8fa; border: none;}
            QHeaderView::section {background: #edf3f6; color: #456074; border: none; padding: 5px;}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        heading.addWidget(self._label("灵巧手测试" if kind == "dexhand" else "HIPNUC IMU", "toolTitle"))
        heading.addStretch()
        if kind == "imu":
            heading.addWidget(self._label("CAN 接口"))
            self.interface = QLineEdit("can2")
            self.interface.setAccessibleName("IMU CAN 接口")
            self.interface.setFixedWidth(88)
            heading.addWidget(self.interface)
        else:
            self.bus_label = self._label("")
            heading.addWidget(self.bus_label)
        layout.addLayout(heading)
        if kind == "imu":
            self._build_imu(layout)
        else:
            self._build_hand(layout)
        layout.addStretch()
        self.heartbeat = DeviceHeartbeat(self)
        self.heartbeat.failed.connect(self.status_label.setText)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._render()

    @staticmethod
    def _label(text, name="toolHint"):
        label = QLabel(text)
        label.setObjectName(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    @staticmethod
    def _card(parent_layout):
        card = QFrame()
        card.setObjectName("toolCard")
        parent_layout.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(9)
        return layout

    def _button(self, action, text, layout, *, secondary=False):
        button = QPushButton(text)
        if secondary:
            button.setObjectName("secondary")
        button.clicked.connect(lambda: self.command(action))
        layout.addWidget(button)
        self.buttons[action] = button
        return button

    def _build_imu(self, layout):
        for action, title, detail, caption in (
            ("initialize", "初始化", "以 500 kbit/s 写入配置，完成后自动切至 1 Mbit/s。", "初始化 IMU"),
            ("check", "回传检查", "以 1 Mbit/s 采集 5 秒，测量三组数据的回传频率。", "检查回传"),
        ):
            row = QHBoxLayout()
            self._card(layout).addLayout(row)
            description = QVBoxLayout()
            description.addWidget(self._label(title, "toolHeading"))
            hint = self._label(detail)
            hint.setWordWrap(True)
            description.addWidget(hint)
            row.addLayout(description, 1)
            self._button(action, caption, row, secondary=action == "initialize").setMinimumWidth(94)
        results = self._card(layout)
        title_row = QHBoxLayout()
        title_row.addWidget(self._label("本次结果", "toolHeading"))
        title_row.addStretch()
        self._button("stop", "停止", title_row, secondary=True)
        self._button("recover", "核对接口恢复", title_row, secondary=True)
        self.export_button = QPushButton("导出报告")
        self.export_button.setObjectName("secondary")
        self.export_button.clicked.connect(self.export_report)
        title_row.addWidget(self.export_button)
        results.addLayout(title_row)
        results.addWidget(self.status_label)
        self.metrics = {}
        self.metrics_panel = QWidget()
        metrics = QGridLayout(self.metrics_panel)
        metrics.setContentsMargins(0, 4, 0, 0)
        for col, (key, title) in enumerate((("quaternion_wxyz", "四元数"), ("acceleration_m_s2", "加速度"),
                                            ("angular_velocity_rad_s", "角速度"))):
            metrics.addWidget(self._label(title), 0, col)
            self.metrics[key] = self._label("—", "toolValue")
            metrics.addWidget(self.metrics[key], 1, col)
            metrics.setColumnStretch(col, 1)
        results.addWidget(self.metrics_panel)

    def _build_hand(self, layout):
        actions = self._card(layout)
        hint = self._label("开始运动后自动回零，拇指依次往返，再由四指做波浪运动。请保持手臂静止、手指行程内无遮挡。")
        hint.setWordWrap(True)
        actions.addWidget(hint)
        row = QHBoxLayout()
        self._button("read-state", "读取状态", row, secondary=True)
        self._button("wave", "开始运动", row)
        row.addStretch()
        actions.addLayout(row)
        results = self._card(layout)
        results.addWidget(self._label("六轴反馈", "toolHeading"))
        results.addWidget(self.status_label)
        self.feedback = QTableWidget(6, 6)
        self.feedback.setHorizontalHeaderLabels(["手指轴", "位置", "速度", "电流", "使能", "故障码"])
        self.feedback.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.feedback.verticalHeader().hide()
        self.feedback.setShowGrid(False)
        self.feedback.setAlternatingRowColors(True)
        self.feedback.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.feedback.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.feedback.verticalHeader().setDefaultSectionSize(27)
        self.feedback.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = self.feedback.horizontalHeader()
        header.setFixedHeight(34)
        self.feedback.setFixedHeight(header.height() + self.feedback.verticalHeader().length() + 2)
        for i, name in enumerate(AXES):
            for j in range(6):
                item = QTableWidgetItem(name if j == 0 else "—")
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.feedback.setItem(i, j, item)
        results.addWidget(self.feedback)

    def owned(self):
        return self.state.get("owner") == self.client.client_id

    def active(self):
        return self.owned() or self.action_pending or self.claim_unknown

    def poll(self):
        if self.closed or self.poll_pending or self.action_pending or not (self.isVisible() or self.active()):
            return
        self.poll_pending = True
        epoch = self.command_epoch
        self.run_rest("读取设备状态", lambda: self.client.device_status(self.kind),
                      lambda state: self.apply(state) if epoch == self.command_epoch else None,
                      quiet=True, on_finished=lambda: setattr(self, "poll_pending", False),
                      on_failure=lambda error: self.status_label.setText(str(error)) if not self.closed else None)

    def apply(self, state):
        if self.closed: return
        previous_report = (self.state.get("last_report") or {}).get("id")
        current_report = (state.get("last_report") or {}).get("id")
        if self.claim_unknown and (state.get("owner") == self.client.client_id or current_report != previous_report):
            self.action_error = ""
        self.state = state
        self.claim_unknown = False
        self._render()
        self.changed.emit()

    def _render(self):
        state = self.state
        own, busy = self.owned(), bool(state.get("busy")) or self.action_pending
        free = not state.get("owner") and bool(state)
        report = state.get("last_report") or {}
        result = report.get("result") or {}
        detail = state.get("detail", "")
        if free and not report and not self.action_error and detail in {"", "未连接"}:
            detail = "等待读取状态" if self.kind == "dexhand" else "等待操作"
        if report and not busy and not state.get("owner"):
            detail = result.get("error") or result.get("note") or detail
        if state.get("owner") and not own:
            detail = "其他窗口正在操作设备"
        self.status_label.setText(self.action_error or detail or "正在读取网关状态…")
        if self.kind == "imu":
            self.interface.setEnabled(free and not busy)
            for action in ("initialize", "check"):
                self.buttons[action].setEnabled(free and not busy)
            failed = state.get("phase") == "cleanup_failed"
            self.buttons["stop"].setVisible(own and busy and not failed)
            self.buttons["recover"].setVisible(own and failed)
            self.export_button.setVisible(bool(report))
            self.metrics_panel.setVisible(report.get("action") == "check")
            for key, label in self.metrics.items():
                hz = (result.get("observed_hz") or {}).get(key)
                label.setText(f"{hz:.1f} Hz" if isinstance(hz, (int, float)) else "—")
            return
        arm = self.arm_snapshot() or {}
        simulated = arm.get("backend") == "fake"
        if state.get("owner"):
            simulated = (state.get("config") or {}).get("backend") == "mock"
        elif report:
            simulated = report.get("source_kind") == "simulation"
        if simulated:
            self.status_label.setText("模拟设备 · " + self.status_label.text())
        buses = sorted({m["bus"] for m in arm.get("configured_motors", []) if m.get("bus")})
        interface = (state.get("config") or {}).get("interface") if state.get("owner") else None
        self.bus_label.setText(interface or " / ".join(buses) or "待配置七自由度手臂")
        self.buttons["read-state"].setEnabled((free and not busy) or (own and state.get("phase") == "waving"))
        wave = self.buttons["wave"]
        wave.setText("重试断开" if own and state.get("phase") == "cleanup_failed" else
                     "停止运动" if own and (state.get("config") or {}).get("operation") == "wave" else "开始运动")
        can_stop = own and ((state.get("config") or {}).get("operation") == "wave" or state.get("phase") == "cleanup_failed")
        arm_busy = arm_busy_for_hand(arm)
        wave.setEnabled((can_stop and state.get("phase") != "stopping") or (free and not busy and not arm_busy))
        feedback = state.get("feedback") if state.get("owner") else result.get("feedback")
        if not state.get("owner") and arm_busy:
            cached = (report.get("owner") == self.client.client_id and report.get("config_revision") is not None
                      and report["config_revision"] == arm.get("config_revision") and report.get("node_id") == 1
                      and buses == [report.get("interface")] and report.get("created_at") and feedback)
            if cached:
                self.status_label.setText(f"手臂当前仅可查看历史反馈 · {report['created_at']}")
            else:
                feedback = None
                self.status_label.setText("手臂当前无法新建共线连接，此工位暂无匹配的历史反馈")
        for i in range(6):
            for j, key in enumerate(("positions", "velocities", "currents", "enabled", "fault_codes"), start=1):
                value = feedback[key][i] if feedback and key in feedback else None
                text = "—" if value is None else ("是" if value else "否") if key == "enabled" else str(value)
                self.feedback.item(i, j).setText(text)

    def command(self, action, *, closing=False):
        if action == "read-state" and ((self.owned() and self.state.get("phase") == "waving")
                                      or arm_busy_for_hand(self.arm_snapshot() or {})):
            self.poll()
            return
        if action == "wave" and self.owned():
            action = "disconnect" if self.state.get("phase") == "cleanup_failed" else "finish-wave"
        if self.closed or action in self.inflight or (self.action_pending and action not in {"stop", "disconnect", "finish-wave"}):
            return
        if action == "initialize" and QMessageBox.question(
                self, "初始化 IMU", f"将向 {self.interface.text().strip()} 上的 IMU 写入配置并复位。继续？") != QMessageBox.Yes:
            return
        settings = {"generation": self.state.get("generation")}
        if action in {"initialize", "check"}:
            settings["interface"] = self.interface.text().strip()
        elif action in {"read-state", "wave"}:
            arm = self.arm_snapshot() or {}
            settings.update(backend="mock" if arm.get("backend") == "fake" else "hardware", follow_arm=True)
        self.action_error = ""
        self.action_pending = True
        self.inflight.add(action)
        self.command_epoch += 1
        epoch = self.command_epoch
        if action in {"read-state", "wave", "initialize"} or (self.kind == "imu" and action == "check"):
            self.claim_unknown = True
        self._render()
        self.run_rest("设备操作", lambda: self.client.device_action(self.kind, action, settings),
                      lambda state: self.apply(state) if epoch == self.command_epoch else None,
                      quiet=True, on_failure=self._failed, on_finished=lambda: self._action_finished(action))

    def _action_finished(self, action):
        self.inflight.discard(action)
        self.action_pending = bool(self.inflight)
        self._render()
        self.changed.emit()
        self.poll()

    def _failed(self, error):
        if not self.closed:
            self.action_error = str(error)
            self.status_label.setText(self.action_error)
            if getattr(error, "status_code", None) in {400, 401, 403, 404, 409, 422}:
                self.claim_unknown = False

    def request_close(self):
        if self.owned() and not self.closing_requested:
            self.closing_requested = True
            self.command("stop" if self.kind == "imu" else "disconnect", closing=True)

    def export_report(self):
        report = self.state.get("last_report")
        if not report: return
        directory = QFileDialog.getExistingDirectory(self, "选择导出目录（将创建报告 ID 子目录）")
        if directory:
            self.run_rest("导出设备报告", lambda: self.client.export_device_report(report["id"], directory),
                          lambda path: self.status_label.setText(f"已导出：{path}"), on_failure=self._failed)

    def shutdown(self):
        self.closed = True
        self.timer.stop()
        self.heartbeat.stop()
