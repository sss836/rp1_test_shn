"""Explicit module OTA preview, human power acknowledgement and fresh readback."""
import json
from shiboken6 import isValid
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit,
                              QTableWidget, QTableWidgetItem, QMessageBox, QComboBox, QHeaderView)


class MotorMaintenanceDialog(QDialog):
    def __init__(self, client, run_rest, identity, parent=None):
        super().__init__(parent)
        self.client, self.run_rest, self.identity = client, run_rest, identity
        self.account = identity()
        self.pending = False
        self.preview_value = None
        self.job = {}
        self.prompted = set()
        self.setWindowTitle("模块固件维护")
        self.resize(1050, 720)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("先停止测试并失能。选择全部或部分目标，选择已下载且签名兼容的电机固件。每台写完后请人工重新上下电，再单独回读核验。"))
        self.table = QTableWidget(0, 8, self)
        self.table.setHorizontalHeaderLabels(["选择", "模块 / 关节", "接口", "ID", "型号", "OTA请求 / 响应", "已下载兼容固件", "索引"])
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
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1000)
        self.controls(); self.reload()

    def invalidate(self, *args):
        self.preview_value = None
        self.controls()

    def controls(self):
        active = self.job.get("active", False)
        self.reload_button.setEnabled(not self.pending and not active)
        self.preview_button.setEnabled(bool(self.identity()) and not self.pending and not active)
        self.start_button.setEnabled(bool(self.preview_value) and not self.pending and not active)
        awaiting = self.job.get("status") == "awaiting_power"
        self.ack_button.setEnabled(awaiting and not self.pending)
        self.read_button.setEnabled(awaiting and any(r.get("human_acknowledged_at") for r in self.job.get("targets", []) if r["status"] == "awaiting_power") and not self.pending)
        self.cancel_button.setEnabled(active and not self.pending)
        self.table.setEnabled(not active and not self.pending)

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

    def reload(self):
        def received(value):
            self.table.blockSignals(True)
            self.table.setRowCount(len(value["targets"]))
            for i, row in enumerate(value["targets"]):
                check = QTableWidgetItem(); check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked); self.table.setItem(i, 0, check)
                values = [row["module"] + " / " + row["joint_name"], row["interface"], str(row["motor_id"]), row["model"],
                          f'{row["ota_request_id"]:#x} / {row["ota_response_id"]:#x}', "", str(row["index"])]
                for column, text in enumerate(values, 1):
                    if column == 6:
                        files = QComboBox(self.table)
                        for release in row.get("cached_releases", []):
                            files.addItem(f'{release["version"]} · {release["model"]} · SHA256 {release["sha256"][:12]}', release["release_id"])
                        files.currentIndexChanged.connect(self.invalidate)
                        self.table.setCellWidget(i, column, files)
                        continue
                    item = QTableWidgetItem(text)
                    if column != 6: item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.table.setItem(i, column, item)
            self.table.blockSignals(False); self.invalidate(); self.show_job(value["job"])
        self.request("读取维护目标", self.client.motor_maintenance_status, received)

    def preview(self):
        selections = [{"index": int(self.table.item(i,7).text()), "release_id": self.table.cellWidget(i,6).currentData()}
                      for i in range(self.table.rowCount()) if self.table.item(i,0).checkState() == Qt.CheckState.Checked]
        def received(value):
            self.preview_value = value
            self.details.setPlainText("尚未写入。请核对最终接口、ID、型号、当前版本、目标版本、SHA256及兼容来源。\n" + self.describe(value, preview=True))
        self.request("预览模块固件维护", lambda: self.client.motor_maintenance_action("preview", {"selections": selections}), received)

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
        for row in value.get("targets", []):
            before, manifest = row.get("before", {}), row.get("manifest", {})
            lines.extend(["", f'{row.get("module", "")} / {row.get("joint_name", "")} · {row.get("model", "")}',
                          f'接口 {row.get("interface", "")} · 电机ID {row.get("motor_id", "")} · OTA请求 {row.get("ota_request_id", 0):#x} / 响应 {row.get("ota_response_id", 0):#x}',
                          f'写前版本：{before.get("raw_firmware", "未知")}；软件 {before.get("software", "未知")}；硬件 {before.get("hardware", "未知")}；协议 {before.get("protocol", "未知")}',
                          f'目标完整版本：{manifest.get("version", "未知")}；目标软件：{manifest.get("software_version", "未知")}',
                          f'固件SHA256：{manifest.get("sha256", "未知")}',
                          f'固件来源：{manifest.get("source_url", "未知")}；范围：{manifest.get("scope", "未知")}',
                          f'说明：{manifest.get("notes", "")}',
                          f'兼容证据：{(row.get("policy", {}).get("entry") or {}).get("evidence_url", "未知")}'])
            if not preview:
                lines.extend([f'本台状态：{statuses.get(row.get("status"), row.get("status", "未知"))}；已写 {row.get("bytes_written", 0)} 字节',
                              f'人工重新上下电确认：{row.get("human_acknowledged_at") or "未确认"}',
                              f'独立回读：{(row.get("readback") or {}).get("raw_firmware", "尚未读取")}；实际版本核验：{"通过" if row.get("actual_version_verified") else "尚未通过"}'])
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
