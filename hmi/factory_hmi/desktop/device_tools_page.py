"""Explicit peripheral operations; bounded polling and GUI-owned heartbeat."""
from __future__ import annotations

import json
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit,
    QComboBox, QSpinBox, QPushButton, QTableWidget, QTableWidgetItem, QPlainTextEdit, QMessageBox,
    QFileDialog, QHeaderView, QTabWidget)

from factory_hmi.client import is_loopback_gateway
from factory_hmi.core.dexhand import AXES


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
        layout = QVBoxLayout(self)
        title = QLabel("HIPNUC 初始化与检查" if kind == "imu" else "七自由度末端 · 灵巧手测试")
        title.setStyleSheet("font-size:22px; font-weight:600")
        layout.addWidget(title)
        if kind == "dexhand":
            note = QLabel("灵巧手与当前七自由度手臂共用 CAN。先使手臂静止保持，再连接手；手的停止只停止并失能手指。\n往返时只检查受测轴是否发生运动，不考核目标位置误差。")
            note.setWordWrap(True)
            layout.addWidget(note)
        form = QFormLayout()
        layout.addLayout(form)
        self.interface = QLineEdit("can2")
        self.bus_label = QLabel("跟随当前手臂配置")
        self.backend = QComboBox()
        self.backend.addItem("真实设备", "hardware")
        self.backend.addItem("离线模拟（手臂也须模拟）", "mock")
        self.node = self._spin(1, 127, 1)
        self.speed = self._spin(10, 20000, 500)
        self.speed.setSingleStep(10)
        self.current = self._spin(1, 1000, 200)
        self.amplitude = self._spin(1, 2000, 300)
        self.cycles = self._spin(1, 10, 1)
        self.axis = QComboBox()
        for i, label in enumerate(AXES): self.axis.addItem(label, i)
        self.axis.addItem("六轴依次测试", -1)
        if kind == "imu":
            form.addRow("独立 CAN 接口", self.interface)
        else:
            for name, widget in (("共享接口", self.bus_label), ("设备", self.backend), ("节点 ID", self.node),
                                 ("速度（行程单位/秒）", self.speed), ("电流限幅（额定电流 ‰）", self.current),
                                 ("测试轴", self.axis), ("往返指令幅度（满行程 10000）", self.amplitude), ("循环次数", self.cycles)):
                form.addRow(name, widget)
        controls = QHBoxLayout()
        if kind == "imu":
            tabs = QTabWidget()
            layout.addWidget(tabs)
            for action, label, help_text in (
                ("initialize", "初始化", "以 500 kbit/s 识别 IMU 并写入配置，保存、复位后自动将接口切至 1 Mbit/s。"),
                ("check", "检查回传", "自动将接口设为 1 Mbit/s，监听 5 秒，检查姿态与惯性数据并统计各路回传频率。"),
            ):
                tab = QWidget()
                panel = QVBoxLayout(tab)
                description = QLabel(help_text)
                description.setWordWrap(True)
                panel.addWidget(description)
                button = QPushButton("开始" + label)
                button.clicked.connect(lambda checked=False, a=action: self.command(a))
                panel.addWidget(button)
                self.buttons[action] = button
                tabs.addTab(tab, label)
        layout.addLayout(controls)
        actions = [("stop", "停止"), ("recover", "核对接口恢复")]
        if kind == "dexhand":
            actions = [("connect", "连接"), ("enable", "设置参数并使能"), ("home", "回零"), ("check", "检查反馈 5 秒"),
                       ("run", "开始运动检查"), ("stop", "停止并失能手"), ("disconnect", "断开手")]
        for action, label in actions:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, a=action: self.command(a))
            controls.addWidget(button)
            self.buttons[action] = button
        self.status_label = QLabel("尚未读取设备状态")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.feedback = QTableWidget(6, 6)
        self.feedback.setHorizontalHeaderLabels(["轴", "位置", "速度", "电流（协议值）", "使能 / 回零", "故障"])
        self.feedback.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.feedback.setEditTriggers(QTableWidget.NoEditTriggers)
        if kind == "dexhand": layout.addWidget(self.feedback)
        else: self.feedback.hide()
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("检查与测试记录保存在本网关的 device-tools 目录，含样本 CSV、结果及 SHA256；不会自动进入平台正式统计。")
        layout.addWidget(self.report)
        self.export_button = QPushButton("导出本次报告和样本")
        self.export_button.clicked.connect(self.export_report)
        self.export_button.setEnabled(False)
        layout.addWidget(self.export_button)
        self.heartbeat = DeviceHeartbeat(self)
        self.heartbeat.failed.connect(self.status_label.setText)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self._render()

    @staticmethod
    def _spin(low, high, value):
        widget = QSpinBox()
        widget.setRange(low, high)
        widget.setValue(value)
        return widget

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
                      on_failure=self._failed)

    def apply(self, state):
        if self.closed: return
        self.state = state
        self.claim_unknown = False
        self._render()
        self.changed.emit()

    def _render(self):
        state = self.state
        own, busy = self.owned(), bool(state.get("busy")) or self.action_pending
        free = not state.get("owner") and bool(state)
        for action, button in self.buttons.items():
            allowed = own if action in {"stop", "disconnect"} else free if action in {"connect", "initialize"} or self.kind == "imu" else own and state.get("connected")
            if action == "recover": allowed = own and state.get("phase") == "cleanup_failed"
            button.setEnabled(bool(allowed) and (not busy or action in {"stop", "disconnect"}))
        for widget in (self.interface, self.backend, self.node): widget.setEnabled(free and not busy)
        cfg = state.get("config") or {}
        buses = {m.get("bus") for m in (self.arm_snapshot() or {}).get("configured_motors", [])}
        current_bus = " / ".join(sorted(str(x) for x in buses)) or "请先配置七自由度手臂"
        self.bus_label.setText(cfg.get("interface", current_bus) if state.get("owner") else current_bus)
        mode = "【离线模拟】" if cfg.get("backend") == "mock" else ""
        self.status_label.setText(f"{mode}{state.get('detail', '尚未读取设备状态')}" + ("（其他窗口占用）" if state.get("owner") and not own else ""))
        feedback = state.get("feedback") or {}
        for widget in (self.speed, self.current):
            widget.setEnabled(not busy and (free or (own and feedback.get("feedback_valid") and not any(feedback.get("enabled", [])))))
        for i, name in enumerate(AXES):
            values = [name] + [str((feedback.get(k) or ["—"]*6)[i]) for k in ("positions", "velocities", "currents")]
            values += [f"{(feedback.get('enabled') or ['—']*6)[i]} / {(feedback.get('homed') or ['—']*6)[i]}", str((feedback.get('fault_codes') or ['—']*6)[i])]
            for j, value in enumerate(values): self.feedback.setItem(i, j, QTableWidgetItem(value))
        if feedback and not feedback.get("feedback_valid"):
            self.status_label.setText(self.status_label.text() + " · 反馈已失效")
        report = state.get("last_report")
        self.export_button.setEnabled(bool(report))
        if report and self.report.property("report_id") != report["id"]:
            self.report.setProperty("report_id", report["id"])
            self.report.setPlainText(json.dumps(report, ensure_ascii=False, indent=2))
        elif not report:
            self.report.setProperty("report_id", None)
            self.report.clear()

    def command(self, action, *, closing=False):
        if self.closed or action in self.inflight or (self.action_pending and action not in {"stop", "disconnect"}): return
        if action in {"initialize", "enable", "home", "run"}:
            message = ("将按官方脚本切换 IMU 独立 CAN 速率、保存配置并复位。继续？" if self.kind == "imu"
                       else "请确认手臂保持静止、手指活动范围已清空。回零和测试可能带动全部手指。继续？")
            if QMessageBox.question(self, "执行设备操作", message) != QMessageBox.Yes: return
        settings = {"generation": self.state.get("generation")}
        if action not in {"stop", "disconnect", "recover"}:
            if self.kind == "imu": settings["interface"] = self.interface.text().strip()
            else:
                settings.update(backend=self.backend.currentData(), follow_arm=True, node_id=self.node.value(),
                                speed=self.speed.value(), current_limit=self.current.value(), amplitude=self.amplitude.value(),
                                cycles=self.cycles.value(), axis=self.axis.currentData())
        self.action_pending = True
        self.inflight.add(action)
        self.command_epoch += 1
        epoch = self.command_epoch
        if action in {"connect", "initialize"} or (self.kind == "imu" and action == "check"):
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
            self.status_label.setText(str(error))
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
