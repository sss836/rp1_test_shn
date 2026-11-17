"""Run the selected module's commissioned real limit check."""
from __future__ import annotations

import json

from PySide6.QtCore import QIODevice, QSaveFile, QTimer, Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QMessageBox, QTextEdit, QVBoxLayout, QWidget

from factory_hmi.core.limit_block_check import MODULES, NAMES
from .limit_block_page import add_button

PHASE = {"idle": "等待工位就绪", "waiting_feedback": "等待逐电机反馈", "gain_transition": "平滑切换增益",
         "homing": "自动回到轨迹起点", "positioning": "按核定路径转场", "reset": "接近搜索起点", "baseline": "采集力矩基线",
         "search": "低速搜索", "unload": "停止推进", "retreat": "沿实测路径缓退", "clearance": "返回安全转场起点",
         "returning": "回核定收尾位", "finished": "检查完成", "aborted": "已中止"}
VERDICT = {"within_reference": "参考范围内", "early": "提前阻挡", "late": "超出参考", "not_detected": "未检出"}


class ModuleLimitPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.pending = self.polling = False
        self.online = False
        self.status = {}
        layout = QVBoxLayout(self)
        title = QLabel("真机整模块限位检查")
        title.setObjectName("pageHeading")
        layout.addWidget(title)
        detail = QLabel("工位就绪后，点击「检查限位装置」会启动当前模块的整轮真机动作。"
                        "内置轨迹随工位方案自动选用，上位机从当前姿态回到检查起点。")
        detail.setWordWrap(True)
        layout.addWidget(detail)
        buttons = QHBoxLayout()
        self.start = add_button(buttons, "检查限位装置", self._start, primary=True)
        self.abort = add_button(buttons, "中止并失能", self._abort)
        self.export = add_button(buttons, "导出实测报告", self._export)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.state = QLabel()
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.digest = QLabel()
        self.digest.setWordWrap(True)
        self.digest.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.digest)
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(220)
        layout.addWidget(self.results, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll)
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()
        self._poll()

    def hideEvent(self, event):
        self.shutdown()
        super().hideEvent(event)

    def shutdown(self):
        self.timer.stop()

    def _poll(self):
        if not self.isVisible() or self.pending or self.polling:
            return
        self.polling = True
        self.window._run_rest("读取整模块检查状态", lambda: self.window.client.get("/api/v1/limit-probe"),
                              self._take_result, quiet=True, on_failure=self._poll_failed, on_finished=self._poll_done)

    def _poll_failed(self, _):
        self.online = False
        self._refresh()

    def _poll_done(self):
        self.polling = False

    def _take_result(self, response):
        self.status = response["result"]
        self.online = True
        self._refresh()

    def _start(self):
        serial = self.window.robot_id.text().strip()
        if not serial:
            QMessageBox.warning(self, "缺少样机编号", "请在「样机配置与连接」填写实际样机编号。")
            return
        self.pending = True
        limb = self.status.get("limb")
        self._refresh()

        def consent(response):
            plan = response["result"]
            if (not self.isVisible() or self.window.robot_id.text().strip() != serial
                    or self.status.get("limb") != limb or plan.get("limb") != limb):
                self._start_done()
                return
            if not plan["profile"].get("use_builtin_trajectory"):
                fixture = plan["profile"]["fixture"]
                count = len(plan["windows"])
                module = dict(MODULES)[limb]
                text = (f"{module} · 样机 {serial} · 工装 {fixture}\n"
                        f"确认工装独立供电、机构支撑、回位通道畅通且物理急停可用。"
                        f"确认后从当前姿态自动回到轨迹起点，再检查 {count} 个方向。")
                if (speed := plan.get("transfer_speed_deg_s")) is not None:
                    text += f"\n本工位转场速度上限 {speed:g}°/s。"
                choice = QMessageBox.question(self, "确认启动真机检查", text,
                                              QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                              QMessageBox.StandardButton.No)
                if choice != QMessageBox.StandardButton.Yes:
                    self._start_done()
                    return
            payload = {"serial": serial, "digest": plan["digest"], "confirmed": True}
            self.window._run_rest("启动整模块限位检查", lambda: self.window.client.post("/api/v1/limit-module/start", payload),
                                  self._take_result, on_finished=self._start_done)

        self.window._run_rest("核对工位限位方案", lambda: self.window.client.post("/api/v1/limit-module/preview", {"serial": serial}),
                              consent, on_failure=lambda _: self._start_done())

    def _start_done(self):
        self.pending = False
        self._refresh()
        self._poll()

    def _abort(self):
        self.window._run_rest("中止并失能", self.window.client.disable_motors, lambda _: self._poll())

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出限位实测结果", "module-limit-check.json", "JSON (*.json)")
        if not path:
            return

        def save(response):
            data = json.dumps(response["result"], ensure_ascii=False, indent=2, allow_nan=False).encode()
            file = QSaveFile(path)
            if (not file.open(QIODevice.OpenModeFlag.WriteOnly) or file.write(data) != len(data) or not file.commit()):
                QMessageBox.warning(self, "保存失败", file.errorString())

        self.window._run_rest("读取整模块检查报告", lambda: self.window.client.get("/api/v1/limit-probe?report=true"), save)

    def _refresh(self):
        s = self.status
        active = s.get("active", False)
        current = s.get("mode") == "module"
        ready = self.online and s.get("backend") == "motors_py" and s.get("discovered") and s.get("motors_enabled")
        self.start.setEnabled(bool(ready and not active and not self.pending))
        self.abort.setEnabled(bool(active or s.get("motors_enabled")))
        self.export.setEnabled(current and not active and s.get("phase") in {"finished", "aborted"})
        limb = dict(MODULES).get(s.get("limb"), "未应用支持的真机模块")
        state = PHASE.get(s.get("phase"), "等待工位状态") if current else "等待启动"
        if not self.online:
            state += " · 状态连接待确认"
        if s.get("direction_total"):
            state += f" · 第 {s['direction_index']}/{s['direction_total']} 项"
        if self.pending:
            state += " · 正在核对方案或启动"
        if current and s.get("fault"):
            state += " · " + s["fault"]
        disable = s.get("disable", {})
        if current and disable.get("request") in {"sent", "failed"}:
            state += " · 失能指令" + ("已发出" if disable["request"] == "sent" else "发送失败")
            if disable.get("feedback") == "unknown":
                state += " · 实际反馈待核对"
        self.state.setText(f"{limb} · {state}")
        self.digest.setText(f"工位配置摘要 {s['station_sha256']}" if s.get("station_sha256") else "")
        lines = []
        if current:
            for e in s.get("results", []):
                joint = e['joint'].removesuffix('_joint').removeprefix('left_').removeprefix('right_')
                direction = "上限" if e["sign"] > 0 else "下限"
                angle = f"实测 {e['actual_deg']:+.2f}°" if e.get("actual_deg") is not None else "未取得候选角"
                load = f" · 阻挡力矩 {e['torque_floor_nm']:.2f} N·m" if e.get("torque_floor_nm") is not None else ""
                lines.append(f"{NAMES.get(joint, joint)} {direction} · {angle} · 参考 {e['reference_deg']:+.2f}°"
                             f" · {VERDICT[e['outcome']]}{load}")
            if s.get("report_file"):
                lines.append(f"\n本机报告 {s['report_file']}")
            if s.get("record_error"):
                lines.append(f"\n自动保存失败 {s['record_error']}")
        text = "\n".join(lines) or "实测角度和阻挡力矩将在各方向结束后显示。"
        if self.results.toPlainText() != text:
            self.results.setPlainText(text)
