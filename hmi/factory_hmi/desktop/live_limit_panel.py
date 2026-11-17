"""Desktop controls for the narrow, approved 5-DoF leg pilot."""
from __future__ import annotations

import json
import math

from PySide6.QtCore import QIODevice, QSaveFile, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QVBoxLayout, QWidget,
)

from factory_hmi.core.limit_block_check import Coordinates, nominal_station
from .limit_block_page import add_button


class LiveLimitPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.plan = None
        self.status = {}
        self.generation = 0
        self.pending = False
        self.polling = False
        layout = QVBoxLayout(self)
        title = QLabel("真机限位搜索 · 5 自由度腿试点")
        title.setObjectName("pageHeading")
        layout.addWidget(title)
        info = QLabel("先在「样机配置与连接」应用对应真机配置。本试点使用独立外部供电工装，"
                      "一次检查一个方向，确认缓退后发出失能指令。")
        info.setWordWrap(True)
        layout.addWidget(info)
        row = QHBoxLayout()
        self.limb = QComboBox()
        self.limb.addItem("左腿 5 自由度", "left_short_leg")
        self.limb.addItem("右腿 5 自由度", "right_short_leg")
        self.spec = QComboBox()
        self.serial = QLineEdit()
        self.serial.setPlaceholderText("实际样机编号")
        self.serial.setMaxLength(120)
        for widget in (self.limb, self.spec, self.serial):
            row.addWidget(widget, 1)
        self.preview = add_button(row, "读取核定方案", self._preview, primary=True)
        layout.addLayout(row)
        actions = QHBoxLayout()
        self.connect = add_button(actions, "连接 CAN", window._connect_can)
        self.discover = add_button(actions, "扫描电机", window._discover_motors)
        self.enable = add_button(actions, "使能", window._enable_motors)
        self.reset = add_button(actions, "复位", self._reset)
        self.start = add_button(actions, "开始搜索", self._start, primary=True)
        self.abort = add_button(actions, "中止并失能", self._abort)
        self.report = add_button(actions, "导出报告", self._report)
        layout.addLayout(actions)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.station_digest = QLabel()
        self.station_digest.setWordWrap(True)
        self.station_digest.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.station_digest)
        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setMinimumHeight(320)
        layout.addWidget(self.summary, 1)
        self.poll = QTimer(self)
        self.poll.setInterval(500)
        self.poll.timeout.connect(self._poll)
        self.limb.currentIndexChanged.connect(self._select_limb)
        self.spec.currentIndexChanged.connect(self._invalidated)
        self.serial.textChanged.connect(self._invalidated)
        self._select_limb()

    def showEvent(self, event):
        self.poll.start()
        self._poll()
        super().showEvent(event)

    def hideEvent(self, event):
        self.poll.stop()
        super().hideEvent(event)

    def shutdown(self):
        self.poll.stop()

    def _select_limb(self):
        self.spec.clear()
        for point in Coordinates(nominal_station(self.limb.currentData())).directions:
            self.spec.addItem(f"{point.label} · {point.label_direction}", (point.joint, point.sign))
        self._invalidated()

    def _invalidated(self):
        self.generation += 1
        self.plan = None
        self.summary.setPlainText("选择关节方向后读取工位核定方案。")
        self._refresh()

    def _payload(self):
        joint, direction = self.spec.currentData()
        return {"limb": self.limb.currentData(), "joint": joint, "direction": direction,
                "serial": self.serial.text().strip()}

    def _run(self, label, operation, callback):
        self.pending = True
        self._refresh()
        self.window._run_rest(label, operation, callback, on_finished=self._done)

    def _done(self):
        self.pending = False
        self._refresh()
        self._poll()

    def _preview(self):
        payload, generation = self._payload(), self.generation
        def display(response):
            if generation != self.generation:
                return
            self.plan = response["result"]
            plan, point = self.plan, self.plan["point"]
            lines = [f"工装 {plan['fixture']} · 修订 {plan['revision']} · 核定 {plan['approved_by']}",
                     f"{plan['label']} {plan['direction']} · 参考角 {plan['reference_deg']}°",
                     f"复位起点 {point['staging_pose_deg']}°",
                     f"搜索起点 {point['search_pose_deg']}°",
                     f"最大推进 {point['permitted_travel_deg']}° · 搜索 {point['search_speed_deg_s']}°/s",
                     f"候选力矩 ≥ {point['contact_floor_nm']} N·m · 基线以上 {point['contact_rise_nm']} N·m",
                     f"关节中止力矩 {point['joint_abort_nm']} N·m",
                     f"方案摘要 {plan['digest']}", ""]
            for motor, abort in zip(plan["motors"], point["motor_abort_nm"], strict=True):
                lines.append(f"{motor['bus']}#{motor['id']} · 复位 Kp/Kd {motor['reset_kp']:g}/{motor['reset_kd']:g}"
                             f" · 搜索 {motor['search_kp']:g}/{motor['search_kd']:g} · 电机中止 {abort:g} N·m")
            self.summary.setPlainText("\n".join(lines))
        self._run("读取核定方案", lambda: self.window.client.post("/api/v1/limit-probe/preview", payload), display)

    def _reset(self):
        if not self.plan:
            return
        if QMessageBox.question(self, "确认复位",
                "请核对当前样机、上方核定路径和方案摘要，确认工装独立外部供电、机构支撑及物理急停可用。"
                "点击确认后才开始复位。") != QMessageBox.StandardButton.Yes:
            return
        request = {**self._payload(), "digest": self.plan["digest"], "confirmed": True}
        self._run("限位复位", lambda: self.window.client.post("/api/v1/limit-probe/reset", request),
                  self._take_result)

    def _start(self):
        if not self.plan:
            return
        if QMessageBox.question(self, "确认开始搜索", "请确认实测姿态已停稳，并开始本方向的低速搜索。") != QMessageBox.StandardButton.Yes:
            return
        request = {"digest": self.plan["digest"]}
        self._run("开始限位搜索", lambda: self.window.client.post("/api/v1/limit-probe-start", request),
                  self._take_result)

    def _abort(self):
        self.window._run_rest("中止并失能", self.window.client.disable_motors, lambda _: self._poll())

    def _poll(self):
        if not self.isVisible() or self.polling or self.pending:
            return
        self.polling = True
        self.window._run_rest("查询限位试点状态", lambda: self.window.client.get("/api/v1/limit-probe"),
                              self._take_result, quiet=True, on_finished=self._poll_done)

    def _poll_done(self):
        self.polling = False

    def _take_result(self, response):
        result = response["result"]
        self.status = result
        if self.plan and result.get("station_sha256") and result["station_sha256"] != self.plan["station_sha256"]:
            self._invalidated()
        self._refresh()

    def _refresh(self):
        status = self.status
        active = status.get("active", False)
        preferred = status.get("limb") == self.limb.currentData() and status.get("backend") == "motors_py"
        station_sha = status.get("station_sha256") if preferred else None
        self.station_digest.setVisible(bool(station_sha))
        self.station_digest.setText(f"工位配置摘要 {station_sha}" if station_sha else "")
        idle = not self.pending and not active
        for widget in (self.limb, self.spec, self.serial):
            widget.setEnabled(idle)
        self.preview.setEnabled(idle and preferred and bool(self.serial.text().strip()))
        self.connect.setEnabled(idle and preferred)
        self.discover.setEnabled(idle and preferred)
        self.enable.setEnabled(idle and preferred and self.plan is not None and status.get("discovered", False)
                               and not status.get("motors_enabled", False))
        self.reset.setEnabled(idle and preferred and self.plan is not None and status.get("motors_enabled", False))
        self.start.setEnabled(not self.pending and active and status.get("phase") == "ready" and self.plan is not None)
        self.abort.setEnabled(active or status.get("motors_enabled", False))
        self.report.setEnabled(not active and status.get("phase") in {"finished", "aborted"})
        states = {"idle": "等待配置", "waiting_feedback": "等待新鲜反馈", "reset": "复位中",
                  "ready": "已到达搜索起点", "baseline": "切换搜索增益并采集基线", "search": "低速搜索",
                  "unload": "停止推进并卸载跟踪偏差", "retreat": "沿实测轨迹缓退", "finished": "本方向已结束", "aborted": "本次已中止"}
        text = states.get(status.get("phase"), "应用对应腿部真机配置后可读取核定方案")
        if status.get("positions_rad"):
            text += " · 逻辑角度 " + " / ".join(f"{math.degrees(v):+.2f}°" for v in status["positions_rad"])
        if status.get("fault"):
            text += " · " + status["fault"]
        if status.get("result"):
            result = status["result"]
            labels = {"early": "提前阻挡", "late": "超出参考", "within_reference": "参考范围内",
                      "not_detected": "未检出"}
            text += " · " + labels.get(result["outcome"], result["outcome"])
            if result.get("actual_deg") is not None:
                text += f" · 候选 {result['actual_deg']:+.2f}°"
        disable = status.get("disable", {})
        if disable.get("request") in {"sent", "failed"}:
            text += " · 失能指令" + ("已发出" if disable["request"] == "sent" else "失败")
            if disable.get("feedback") == "unknown":
                text += " · 实际反馈待核对"
        self.message.setText(text)

    def _report(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出本地限位证据", "limit-probe.json", "JSON (*.json)")
        if not path:
            return
        def save(response):
            data = json.dumps(response["result"], ensure_ascii=False, allow_nan=False, indent=2).encode()
            output = QSaveFile(path)
            if (not output.open(QIODevice.OpenModeFlag.WriteOnly)
                    or output.write(data) != len(data) or not output.commit()):
                QMessageBox.warning(self, "保存报告失败", output.errorString())
        self._run("导出限位记录", lambda: self.window.client.get("/api/v1/limit-probe?report=true"), save)
