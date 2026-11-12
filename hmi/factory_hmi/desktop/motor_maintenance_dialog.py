"""Explicit module OTA preview, human power acknowledgement and fresh readback."""
from shiboken6 import isValid
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit,
                              QTableWidget, QTableWidgetItem, QMessageBox, QComboBox, QHeaderView, QHBoxLayout)
from .motor_firmware_dialog import motor_target_key


MODULE_UPGRADE = "module_108_to_109"
SELECTED_UPGRADE = "selected_108_to_109"


class MotorMaintenanceDialog(QDialog):
    def __init__(self, client, run_rest, identity, parent=None, *, selected_motor=None, scope=None):
        super().__init__(parent)
        self.client, self.run_rest, self.identity = client, run_rest, identity
        self.account = identity()
        self.pending = False
        self.preview_value = None
        self.job = {}
        self.rows = []
        self.configuration_sha256 = None
        self.selected_motor = selected_motor
        self.prompted = set()
        self.setWindowTitle("模块固件维护")
        self.resize(1120, 720)
        layout = QVBoxLayout(self)
        intro = QLabel("先停止测试并失能。每台写完后请人工重新上下电，再单独回读核验。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("维护范围", self))
        self.scope = QComboBox(self)
        self.scope.addItem("整组 108 → 109", MODULE_UPGRADE)
        self.scope.addItem("所选电机 108 → 109", SELECTED_UPGRADE)
        self.scope.addItem("手动选择签名固件", None)
        self.scope.setCurrentIndex(self.scope.findData(scope))
        scope_row.addWidget(self.scope)
        scope_row.addStretch()
        layout.addLayout(scope_row)
        self.scope_hint = QLabel(self)
        self.scope_hint.setWordWrap(True)
        self.scope_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.scope_hint)
        self.table = QTableWidget(0, 9, self)
        self.table.setHorizontalHeaderLabels(["选择", "模块 / 关节", "接口", "ID", "型号", "OTA请求 / 响应", "已下载兼容固件", "索引", "实际固件 / 状态"])
        self.table.setColumnHidden(7, True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table)
        self.table.itemChanged.connect(self.invalidate)
        self.reload_button = QPushButton("读取当前有效目标", self)
        self.preview_button = QPushButton("预览所选目标与兼容固件", self)
        self.start_button = QPushButton("确认清单并启动串行写入", self)
        self.ack_button = QPushButton("人工确认已重新上下电", self)
        self.read_button = QPushButton("单独回读实际版本", self)
        self.cancel_button = QPushButton("取消后续维护", self)
        for widget in (self.reload_button, self.preview_button, self.start_button, self.ack_button, self.read_button, self.cancel_button):
            layout.addWidget(widget)
        self.details = QPlainTextEdit(self); self.details.setReadOnly(True); layout.addWidget(self.details)
        self.reload_button.clicked.connect(self.reload)
        self.preview_button.clicked.connect(self.preview)
        self.start_button.clicked.connect(self.start)
        self.ack_button.clicked.connect(self.acknowledge)
        self.read_button.clicked.connect(lambda: self.action("readback", {"job_id": self.job["job_id"]}))
        self.cancel_button.clicked.connect(lambda: self.action("cancel", {"job_id": self.job["job_id"]}))
        self.scope.currentIndexChanged.connect(lambda: self.render_targets())
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1000)
        self.finished.connect(self.timer.stop)
        self.controls(); self.reload()

    def invalidate(self, *args):
        if self.preview_value is not None:
            self.details.setPlainText(self.describe(self.job) if self.job.get("job_id") else "")
        self.preview_value = None
        self.controls()

    def controls(self):
        active = self.job.get("active", False)
        mode = self.scope.currentData()
        upgrade_ready = not mode or self.upgrade_ready(mode)
        self.reload_button.setEnabled(not self.pending and not active)
        self.preview_button.setEnabled(bool(self.identity()) and not self.pending and not active and upgrade_ready)
        self.start_button.setEnabled(bool(self.preview_value) and not self.pending and not active)
        awaiting = self.job.get("status") == "awaiting_power"
        self.ack_button.setEnabled(awaiting and not self.pending)
        self.read_button.setEnabled(awaiting and any(r.get("human_acknowledged_at") for r in self.job.get("targets", []) if r["status"] == "awaiting_power") and not self.pending)
        self.cancel_button.setEnabled(active and not self.pending)
        self.table.setEnabled(not active and not self.pending)
        self.scope.setEnabled(not active and not self.pending)

    def selected_rows(self):
        return [i for i in range(self.table.rowCount()) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def upgrade_ready(self, mode):
        selected = self.selected_rows()
        unknown = sum(row.get("firmware_generation") not in {108, 109} for row in self.rows)
        skipped = sum(row.get("firmware_generation") == 109 for row in self.rows)
        missing = sum(not self.table.cellWidget(i, 6).currentData() for i in selected)
        if not self.rows:
            hint = "尚无可核对的目标。请扫描电机并刷新目标。"
        elif mode == MODULE_UPGRADE and unknown:
            hint = f"{unknown} 台电机的 108/109 版本身份未核实。请核对后再预览整组升级。"
        elif not selected:
            hint = ("请勾选实际版本已确认为 108 的电机。" if mode == SELECTED_UPGRADE
                    else f"已是 109 的电机有 {skipped} 台，没有需要升级的 108 电机。")
        elif missing:
            hint = f"有 {missing} 台已选 108 电机尚未选定适用的签名 109 固件。"
        else:
            hint = f"待升级 {len(selected)} 台 108 电机。"
            if mode == MODULE_UPGRADE and skipped:
                hint += f" 已是 109 的 {skipped} 台将跳过。"
            self.scope_hint.setText(hint)
            return True
        self.scope_hint.setText(hint)
        return False

    def request(self, label, operation, success):
        if self.pending or not self.identity(): return
        self.pending = True; account = self.identity(); self.controls()
        def received(value):
            if isValid(self) and self.identity() == account:
                success(value.get("result", value))
        def finished():
            if isValid(self):
                self.pending = False; self.controls()
        self.run_rest(label, operation, received, on_finished=finished)

    def render_targets(self, selected=None):
        mode = self.scope.currentData()
        if not mode:
            self.scope_hint.setText("勾选目标并选择适用的已下载签名固件。")
        elif mode == MODULE_UPGRADE:
            selected = None
        elif selected is None:
            selected = {self.selected_motor}
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            generation = row.get("firmware_generation")
            checked = (not mode or generation == 108) and (selected is None or motor_target_key(row) in selected)
            check = QTableWidgetItem()
            flags = Qt.ItemFlag.ItemIsEnabled
            if not mode or (mode == SELECTED_UPGRADE and generation == 108):
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            check.setFlags(flags)
            check.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, check)

            observation = row.get("observation") or {}
            actual = observation.get("raw_firmware") or observation.get("software") or "未取得"
            versions = {108: "已核实 108", 109: "已是 109"}
            version = f'{actual} · {versions.get(generation, "108/109 未核实")}'
            values = {1: row["module"] + " / " + row["joint_name"], 2: row["interface"], 3: str(row["motor_id"]),
                      4: row["model"], 5: f'{row["ota_request_id"]:#x} / {row["ota_response_id"]:#x}',
                      7: str(row["index"]), 8: version}
            for column, value in values.items():
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 8 and row.get("eligibility_error"):
                    item.setToolTip(row["eligibility_error"])
                self.table.setItem(i, column, item)

            files = QComboBox(self.table)
            releases = row.get("upgrade_109_releases" if mode else "cached_releases", [])
            for release in releases:
                filename = release.get("filename") or release["version"]
                files.addItem(f'{filename} · SHA256 {release["sha256"][:12]}', release["release_id"])
            if mode:
                if generation == 109:
                    placeholder = "已是 109，跳过"
                elif generation != 108:
                    placeholder = "版本待核实"
                else:
                    placeholder = "请选择固件" if releases else "缺少适用的签名 109 固件"
                files.setPlaceholderText(placeholder)
                if len(releases) > 1:
                    files.setCurrentIndex(-1)
                files.setEnabled(bool(releases) and generation == 108)
            if row.get("eligibility_error"):
                files.setToolTip(row["eligibility_error"])
            files.currentIndexChanged.connect(self.invalidate)
            self.table.setCellWidget(i, 6, files)
        self.table.blockSignals(False)
        self.invalidate()

    def reload(self):
        selected = {motor_target_key(self.rows[i]) for i in self.selected_rows()} if self.rows else None
        def received(value):
            self.rows = value["targets"]
            self.configuration_sha256 = value.get("configuration_sha256")
            self.render_targets(selected)
            self.show_job(value["job"])
        self.request("读取维护目标", self.client.motor_maintenance_status, received)

    def preview(self):
        mode = self.scope.currentData()
        if mode and not self.upgrade_ready(mode):
            return
        selections = [{"index": int(self.table.item(i,7).text()), "release_id": self.table.cellWidget(i,6).currentData()}
                      for i in self.selected_rows()]
        payload = {"selections": selections}
        if mode:
            payload["mode"] = mode
            if self.configuration_sha256:
                payload["configuration_sha256"] = self.configuration_sha256
        def received(value):
            self.preview_value = value
            self.details.setPlainText("尚未写入。请核对最终接口、ID、型号、当前版本、目标版本、SHA256及兼容来源。\n" + self.describe(value, preview=True))
        self.request("预览模块固件维护", lambda: self.client.motor_maintenance_action("preview", payload), received)

    def start(self):
        if not self.preview_value: return
        if QMessageBox.question(self, "确认设备维护", "已核对目标清单、设备停止与失能？启动后逐台写入，每台都需人工重新上下电和版本回读。") != QMessageBox.StandardButton.Yes: return
        preview_id = self.preview_value["preview_id"]
        self.preview_value = None
        self.action("start", {"preview_id": preview_id, "confirmed": True})

    def acknowledge(self):
        if QMessageBox.question(self, "请重新上下电", "请重新上下电。已由人员完成了吗？确认仅记录人工声明，不能证明电源状态或版本核验通过。") == QMessageBox.StandardButton.Yes:
            self.action("acknowledge", {"job_id": self.job["job_id"], "confirmed": True})

    def action(self, name, payload):
        self.request("模块固件维护", lambda: self.client.motor_maintenance_action(name, payload), self.show_job)

    def poll(self):
        if self.identity() != self.account:
            self.account = self.identity(); self.preview_value = None; self.job = {}; self.details.clear(); self.controls()
        if self.job.get("active"):
            self.request("读取维护进度", self.client.motor_maintenance_status, lambda v: self.show_job(v["job"]))

    @staticmethod
    def describe(value, preview=False):
        statuses = {"queued": "排队中", "pending": "待执行", "writing": "正在写入",
                    "awaiting_power": "等待人工重新上下电", "readback_requested": "等待版本回读",
                    "verifying": "正在回读", "verified": "实际版本已匹配", "failed": "失败，后续已停止",
                    "version_unknown": "实际版本未知，后续已停止", "interrupted": "已中断，未完成核验",
                    "not_run": "未执行", "interrupted_unverified": "中断后未核验"}
        lines = [] if preview else ["维护状态：" + statuses.get(value.get("status"), str(value.get("status", "未知")))]
        if value.get("cleanup_quarantined"):
            lines.append("收尾未完成，资源保持隔离：" + value.get("cleanup_error", "未知原因"))
        if value.get("error"):
            lines.append("停止原因：" + value["error"])
        for row in value.get("skipped_targets", []):
            before = row.get("before") or {}
            actual = before.get("raw_firmware") or before.get("software") or "109"
            lines.append(f'跳过已是 109 的电机：{row["joint_name"]} · {row["interface"]} ID {row["motor_id"]} · {actual}')
        for row in value.get("targets", []):
            before, manifest = row.get("before", {}), row.get("manifest", {})
            policy_entry = row.get("policy", {}).get("entry") or {}
            destination = policy_entry.get("target") or {}
            lines.extend(["", f'{row.get("module", "")} / {row.get("joint_name", "")} · {row.get("model", "")}',
                          f'接口 {row.get("interface", "")} · 电机ID {row.get("motor_id", "")} · OTA请求 {row.get("ota_request_id", 0):#x} / 响应 {row.get("ota_response_id", 0):#x}',
                          f'写前版本：{before.get("raw_firmware", "未知")}；软件 {before.get("software", "未知")}；硬件 {before.get("hardware", "未知")}；协议 {before.get("protocol", "未知")}',
                          f'目标完整版本：{manifest.get("version", "未知")}；目标软件：{manifest.get("software_version", "未知")}',
                          f'固件文件：{manifest.get("filename", "未知")}',
                          f'固件SHA256：{manifest.get("sha256", "未知")}',
                          f'固件来源：{manifest.get("source_url", "未知")}；范围：{manifest.get("scope", "未知")}',
                          f'说明：{manifest.get("notes", "")}',
                          f'兼容证据：{policy_entry.get("evidence_url", "未知")}'])
            if destination:
                lines.append(f'目标硬件：{destination["hardware"]}；目标协议：{destination["protocol"]}')
            if not preview:
                lines.extend([f'本台状态：{statuses.get(row.get("status"), row.get("status", "未知"))}；已写 {row.get("bytes_written", 0)} 字节',
                              f'人工重新上下电确认：{row.get("human_acknowledged_at") or "未确认"}',
                              f'独立回读：{(row.get("readback") or {}).get("raw_firmware", "尚未读取")}；实际版本核验：{"通过" if row.get("actual_version_verified") else "尚未通过"}'])
        if value.get("mode") in ("module_108_to_109", "selected_108_to_109"):
            lines.append("\n升级后使用 109 控制：核对工位中该电机的 V1.03 配置，并重新连接。")
        return "\n".join(lines)

    def show_job(self, job):
        self.job = job
        if job.get("job_id"):
            self.details.setPlainText(self.describe(job))
            for row in job.get("targets", []):
                key = (job["job_id"], row["index"])
                if row.get("power_cycle_required") and row["status"] != "writing" and key not in self.prompted:
                    self.prompted.add(key)
                    QMessageBox.information(self, "请重新上下电", "请重新上下电。由人员操作；之后点击人工确认，再单独回读实际版本。未核验不得视为升级成功。")
        self.controls()
