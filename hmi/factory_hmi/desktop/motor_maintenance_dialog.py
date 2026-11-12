from factory_hmi.core.motor_ota_transport import selfcheck_accepted
"""Explicit module OTA preview, human power acknowledgement and fresh readback."""
import time

from shiboken6 import isValid
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit,
                              QTableWidget, QTableWidgetItem, QMessageBox, QComboBox, QHeaderView, QHBoxLayout)
from .motor_firmware_dialog import motor_target_key


MODULE_UPGRADE = "module_108_to_109"
SELECTED_UPGRADE = "selected_108_to_109"
VERSION_REFRESH_WAIT_S = 65


class MotorMaintenanceDialog(QDialog):
    def __init__(self, client, run_rest, identity, parent=None, *, selected_motor=None, scope=None):
        super().__init__(parent)
        self.client, self.run_rest, self.identity = client, run_rest, identity
        self.account = identity()
        self.pending = False
        self._version_refresh = None
        self._version_refresh_deadline = 0.0
        self._version_refresh_note = ""
        self._scan_required = False
        self._superseded_readback_job_id = None
        self.local_write_authorized = False
        self.preview_value = None
        self.job = {}
        self.rows = []
        self.configuration_sha256 = None
        self.selected_motor = selected_motor
        self.prompted = set()
        self.setWindowTitle("模块固件维护")
        self.setObjectName("maintenanceDialog")
        self.resize(1120, 622)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        title = QLabel("电机固件", self)
        title.setObjectName("pageHeading")
        layout.addWidget(title)
        intro = QLabel("对照实际与目标版本。更新前先停测并失能，所选电机全部写完后统一上下电，再回读核验。", self)
        intro.setObjectName("pageDescription")
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
        scope_row.addWidget(QLabel("维护方式", self))
        self.source = QComboBox(self)
        self.source.addItem("本机", True)
        self.source.addItem("测试平台", False)
        self.source.setCurrentIndex(1 if self.account else 0)
        scope_row.addWidget(self.source)
        self.reload_button = QPushButton("刷新版本", self)
        self.select_all_button = QPushButton("全选可更新", self)
        self.clear_selection_button = QPushButton("清除选择", self)
        for button in (self.reload_button, self.select_all_button, self.clear_selection_button):
            button.setObjectName("outlineButton")
            scope_row.addWidget(button)
        scope_row.addStretch()
        layout.addLayout(scope_row)
        self.scope_hint = QLabel(self)
        self.scope_hint.setObjectName("maintenanceHint")
        self.scope_hint.setWordWrap(True)
        self.scope_hint.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.scope_hint)
        self.job_summary = QLabel(self)
        self.job_summary.setWordWrap(True)
        self.job_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.job_summary.hide()
        layout.addWidget(self.job_summary)
        self.table = QTableWidget(0, 10, self)
        self.table.setObjectName("maintenanceTable")
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setHorizontalHeaderLabels(["选择", "模块 / 关节", "接口", "ID", "型号", "OTA请求 / 响应", "目标固件", "索引", "实际固件", "符合情况"])
        for column in (5, 7):
            self.table.setColumnHidden(column, True)
        header = self.table.horizontalHeader()
        header.moveSection(header.visualIndex(8), header.visualIndex(6))
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        layout.addWidget(self.table, 1)
        self.table.itemChanged.connect(self.invalidate)
        self.preview_button = QPushButton("更新所选…", self)
        self.preview_button.setObjectName("primaryButton")
        self.ack_button = QPushButton("已统一上下电，开始核验", self)
        self.read_button = QPushButton("单独回读实际版本", self)
        self.cancel_button = QPushButton("取消后续维护", self)
        actions = QHBoxLayout()
        for widget in (self.ack_button, self.read_button, self.cancel_button):
            actions.addWidget(widget)
        detail_button = QPushButton("查看维护详情", self)
        detail_button.setObjectName("outlineButton")
        detail_button.setCheckable(True)
        actions.addWidget(detail_button)
        actions.addStretch()
        actions.addWidget(self.preview_button)
        layout.addLayout(actions)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.hide()
        layout.addWidget(self.details)
        detail_button.toggled.connect(self.details.setVisible)
        self.reload_button.clicked.connect(self.refresh_versions)
        self.select_all_button.clicked.connect(lambda: self.select_rows(True))
        self.clear_selection_button.clicked.connect(lambda: self.select_rows(False))
        self.preview_button.clicked.connect(lambda: self.preview(confirm=True))
        self.ack_button.clicked.connect(self.acknowledge)
        self.read_button.clicked.connect(lambda: self.action("readback", {"job_id": self.job["job_id"]}))
        self.cancel_button.clicked.connect(lambda: self.action("cancel", {"job_id": self.job["job_id"]}))
        self.scope.currentIndexChanged.connect(lambda: self.render_targets())
        self.source.currentIndexChanged.connect(self._source_changed)
        self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(1000)
        self.finished.connect(self.timer.stop)
        self.controls(); self.reload()

    def invalidate(self, *args):
        if self.preview_value is not None:
            self.details.setPlainText(self.describe(self.job) if self.job.get("job_id") else "")
        self.preview_value = None
        self.render_comparison()
        self.controls()

    def _write_allowed(self):
        return self.local_write_authorized if self.source.currentData() else bool(self.identity())

    def controls(self):
        active = self.job.get("active", False)
        reading = self._version_refresh is not None
        busy = self.pending or reading
        mode = self.scope.currentData()
        upgrade_ready = not mode or self.upgrade_ready(mode)
        self.reload_button.setEnabled(not busy and not active)
        selected = self.selected_rows()
        write_allowed = self._write_allowed()
        idle = write_allowed and not busy and not active and not self._version_refresh_note and not self._scan_required
        self.select_all_button.setText("全选 108" if mode == SELECTED_UPGRADE else "全选可更新")
        self.select_all_button.setEnabled(idle and any(i not in selected for i in self.bulk_selection_rows()))
        self.clear_selection_button.setEnabled(idle and any(i in selected for i in self.checkable_rows()))
        self.preview_button.setEnabled(idle and upgrade_ready and bool(selected)
                                       and all(self.table.cellWidget(i, 6).currentData() for i in selected))
        awaiting = self.job.get("status") == "awaiting_power"
        can_manage_job = write_allowed and bool(self.job.get("job_id")) and not busy
        self.ack_button.setEnabled(can_manage_job and awaiting)
        self.read_button.setEnabled(can_manage_job and awaiting and any(r.get("human_acknowledged_at") for r in self.job.get("targets", []) if r["status"] == "awaiting_power"))
        self.cancel_button.setEnabled(can_manage_job and active)
        self.ack_button.setVisible(awaiting)
        self.read_button.setVisible(False)
        self.cancel_button.setVisible(active)
        self.table.setEnabled(not active and not busy)
        self.scope.setEnabled(not active and not busy)
        self.source.model().item(1).setEnabled(bool(self.identity()))
        self.source.setEnabled(not busy and not (active and self.job.get("job_id")))
        if reading:
            receipt = self._version_refresh
            if receipt["state"] == "complete":
                self.scope_hint.setText("正在载入本机版本结果。")
            else:
                self.scope_hint.setText(
                    f'正在读取本机电机版本 · {receipt["completed_motors"]}/{receipt["total_motors"]} 台')
        elif self._version_refresh_note:
            self.scope_hint.setText(self._version_refresh_note)
        elif self._scan_required and not active:
            self.scope_hint.setText("点击刷新版本，重新扫描电机并读取当前固件。")
        elif self.source.currentData() and not write_allowed and self.rows:
            self.scope_hint.setText("本机会话可以查看版本和签名固件，当前没有写入权限。")
        self._render_job_summary()

    def _render_job_summary(self):
        job = self.job
        relevant = (job.get("job_id") and job.get("configuration_sha256") in
                    (None, self.configuration_sha256))
        self.job_summary.setVisible(bool(relevant))
        if not relevant:
            self.job_summary.clear()
            return
        rows = job.get("targets", [])
        verified = sum(bool(r.get("actual_version_verified")) for r in rows)
        written = sum(r.get("bytes_written", 0) for r in rows)
        skipped = sum(r.get("status") == "not_run" for r in rows)
        status = job.get("status")
        failed = status in {"failed", "version_unknown", "interrupted"}
        label = "本次更新失败" if failed else ("更新完成，回读通过" if status == "verified" else "更新进行中")
        completed = sum(bool(r.get("write_finished_at") or r.get("write_receipt")) for r in rows)
        pending = sum(r.get("status") == "pending" for r in rows)
        checked = sum(bool(r.get("readback_at") or r.get("readback")) for r in rows)
        text = f"{label} · 已写完 {completed}/{len(rows)} 台（{written} 字节） · 待写 {pending} 台 · 已核验 {checked}/{len(rows)} 台 · 回读通过 {verified}/{len(rows)} 台 · 未执行 {skipped} 台"
        if job.get("error"):
            text += "\n" + job["error"]
        elif status == "awaiting_power":
            text += "\n所选电机已全部写完，校验应答已按产线脚本接受。统一重新上下电后，点击「已统一上下电，开始核验」。"
        self.job_summary.setText(text)
        self.job_summary.setStyleSheet("color: #b42318;" if failed else "")


    def selected_rows(self):
        return [i for i in range(self.table.rowCount()) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def checkable_rows(self):
        return [i for i in range(self.table.rowCount())
                if self.table.item(i, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable]

    def updatable_rows(self):
        result = []
        for index in self.checkable_rows():
            combo = self.table.cellWidget(index, 6)
            actual = (self.rows[index].get("observation") or {}).get("raw_firmware")
            if actual and combo.currentData() and combo.currentText() != actual:
                result.append(index)
        return result

    def bulk_selection_rows(self):
        if self.scope.currentData() == SELECTED_UPGRADE:
            return self.checkable_rows()
        return self.updatable_rows()

    def select_rows(self, checked):
        selected = set(self.bulk_selection_rows()) if checked else set()
        self.table.blockSignals(True)
        for index in self.checkable_rows():
            state = Qt.CheckState.Checked if index in selected else Qt.CheckState.Unchecked
            self.table.item(index, 0).setCheckState(state)
        self.table.blockSignals(False)
        self.invalidate()

    @staticmethod
    def _locally_confirmed_108(row):
        return (row.get("firmware_generation") is None
                and row.get("confirmed_firmware_generation") == 108
                and row.get("firmware_confirmation_source") == "local_numeric_evidence")

    @classmethod
    def _generation(cls, row):
        return 108 if cls._locally_confirmed_108(row) else row.get("firmware_generation")

    @staticmethod
    def _display_firmware(observation):
        numeric = observation.get("numeric_software") or observation.get("software")
        return observation.get("raw_firmware") or (f"数值 {numeric}" if numeric else "未取得")

    @staticmethod
    def _firmware_read_timed_out(row):
        observation = row.get("observation") or {}
        return not observation.get("raw_firmware") and "超时" in str((observation.get("errors") or {}).get("0x31", ""))

    @staticmethod
    def _version_read_errors(row):
        errors = (row.get("observation") or {}).get("errors") or {}
        return "\n".join(f'{code.upper()} · {detail}' for code, detail in errors.items())

    def render_comparison(self):
        mode = self.scope.currentData()
        current_job = self.job.get("configuration_sha256") in (None, self.configuration_sha256)
        current_job = current_job and self.job.get("job_id") != self._superseded_readback_job_id
        jobs = {motor_target_key(row): row for row in self.job.get("targets", []) if current_job
                if all(key in row for key in ("index", "interface", "motor_id", "model"))}
        self.table.blockSignals(True)
        for i, row in enumerate(self.rows):
            combo = self.table.cellWidget(i, 6)
            release_id = combo.currentData()
            observation = row.get("observation") or {}
            actual = observation.get("raw_firmware")
            target = combo.currentText() if release_id else ""
            generation = self._generation(row)
            local_108 = self._locally_confirmed_108(row)
            job = jobs.get(motor_target_key(row))
            if job and job.get("actual_version_verified"):
                actual = (job.get("readback") or {}).get("raw_firmware") or actual
                target = (job.get("manifest") or {}).get("version") or target
                status = "回读核验通过"
                if actual:
                    self.table.item(i, 8).setText(actual)
            elif job and (self.job.get("active") or job.get("status") in {"failed", "version_unknown", "interrupted", "not_run"}):
                status = {"writing": "正在更新", "written_awaiting_batch_power": "已写完，等待其他电机", "written_unverified": "已写入，尚未核验", "awaiting_power": "待统一上下电及回读",
                          "verifying": "回读中", "pending": "待更新", "failed": "更新失败",
                          "version_unknown": "版本未核验", "interrupted": "已中断", "not_run": "未执行"}.get(job.get("status"), "待核验")
            elif mode and generation == 109:
                status = "已是 109 · 跳过"
            elif generation is None and self._firmware_read_timed_out(row):
                status = "完整版本读取超时"
            elif mode and generation != 108:
                status = "版本待核实"
            elif local_108 and not mode:
                status = "请选择 108 → 109 范围"
            elif mode and not combo.count():
                status = "缺少签名固件或 OTA 映射"
            elif not target:
                status = "缺少目标固件" if not combo.count() else "请选择目标固件"
            elif local_108:
                status = "有差异"
            elif actual is None:
                status = "实际固件未核实"
            else:
                status = "一致" if actual == target else "有差异"
            item = QTableWidgetItem(status)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            details = [(job or {}).get("error"), row.get("eligibility_error"), self._version_read_errors(row)]
            item.setToolTip("\n".join(detail for detail in details if detail) or status)
            if status in {"一致", "回读核验通过", "已是 109 · 跳过"}:
                item.setForeground(QColor("#126747"))
                item.setBackground(QColor("#EAF7F0"))
            elif status == "有差异":
                item.setForeground(QColor("#99510D"))
                item.setBackground(QColor("#FFF2DD"))
            self.table.setItem(i, 9, item)
        self.table.blockSignals(False)

    def upgrade_ready(self, mode):
        selected = self.selected_rows()
        local = [i for i, row in enumerate(self.rows) if self._locally_confirmed_108(row)]
        unverified = [row for row in self.rows if self._generation(row) not in {108, 109}]
        unknown = len(unverified)
        timed_out = sum(self._firmware_read_timed_out(row) for row in unverified)
        skipped = sum(self._generation(row) == 109 for row in self.rows)
        missing = sum(not self.table.cellWidget(i, 6).currentData() for i in selected)
        unavailable = sum(not self.table.cellWidget(i, 6).count() for i in selected)
        if not self.rows:
            hint = "尚无可核对的目标。请扫描电机并刷新目标。"
        elif unknown and (mode == MODULE_UPGRADE or not (selected or local)):
            hint = f"{unknown} 台电机的 108/109 版本身份未核实。"
            if timed_out:
                hint += f"其中 {timed_out} 台完整固件版本读取超时。"
        elif local and not selected:
            hint = f"{len(local)} 台电机已在本机核实为 108，可勾选。"
            if any(not self.table.cellWidget(i, 6).count() for i in local):
                hint += "缺少适用的签名 109 固件或 OTA 映射。"
        elif not selected:
            hint = ("请勾选实际版本已确认为 108 的电机。" if mode == SELECTED_UPGRADE
                    else f"已是 109 的电机有 {skipped} 台，没有需要升级的 108 电机。")
        elif unavailable:
            hint = f"{unavailable} 台已选 108 电机缺少适用的签名 109 固件或 OTA 映射。"
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

    def request(self, label, operation, success, *, after_success=None, status_read=False,
                station_read=False, quiet=False):
        if self.pending or not (self._write_allowed() or status_read): return
        self.pending = True; account = self.identity(); self.controls()
        source = self.source.currentData()
        def same_context():
            return self.source.currentData() == source and (station_read or source or self.identity() == account)
        received_for_account = False
        def received(value):
            nonlocal received_for_account
            if isValid(self) and same_context():
                success(value.get("result", value))
                received_for_account = True
        def finished():
            if isValid(self):
                self.pending = False; self.controls()
                if received_for_account and after_success and same_context() and self.isVisible():
                    after_success()
        options = {"on_finished": finished}
        if quiet:
            options["quiet"] = True
        self.run_rest(label, operation, received, **options)

    def _request_status(self, label, success):
        local = self.source.currentData() is True
        if not local and not self.identity():
            return
        kwargs = {"local": True} if local else {}
        def received(value):
            self.local_write_authorized = local and value.get("write_authorized") is True
            success(value)
        self.request(label, lambda: self.client.motor_maintenance_status(**kwargs), received, status_read=True)

    def _request_action(self, label, name, payload, success, *, after_success=None):
        kwargs = {"local": True} if self.source.currentData() else {}
        self.request(label, lambda: self.client.motor_maintenance_action(name, payload, **kwargs),
                     success, after_success=after_success)

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
            generation = self._generation(row)
            local_108 = self._locally_confirmed_108(row)
            selectable_108 = generation == 108
            checked = (not mode or selectable_108) and (selected is None or motor_target_key(row) in selected)
            check = QTableWidgetItem()
            flags = Qt.ItemFlag.ItemIsEnabled
            if not mode or (mode == SELECTED_UPGRADE and selectable_108):
                flags |= Qt.ItemFlag.ItemIsUserCheckable
            check.setFlags(flags)
            check.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, check)

            observation = row.get("observation") or {}
            actual = self._display_firmware(observation)
            versions = {108: "已核实 108", 109: "已是 109"}
            classification = "108（本地核实）" if local_108 else versions.get(generation, "108/109 未核实")
            version = f"{actual} · {classification}"
            if observation.get("status") == "stale":
                version = f"{actual} · 已过期"
            values = {1: row["module"] + " / " + row["joint_name"], 2: row["interface"], 3: str(row["motor_id"]),
                      4: row["model"], 5: f'{row["ota_request_id"]:#x} / {row["ota_response_id"]:#x}',
                      7: str(row["index"]), 8: version}
            for column, value in values.items():
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                if column == 8:
                    item.setToolTip(self._version_read_errors(row) or row.get("eligibility_error") or version)
                self.table.setItem(i, column, item)

            files = QComboBox(self.table)
            files.setPlaceholderText("请选择目标固件")
            releases = row.get("upgrade_109_releases" if mode else "cached_releases", [])
            if local_108 and not mode:
                releases = []
            for release in releases:
                filename = release.get("filename") or release["version"]
                files.addItem(str(release["version"]), release["release_id"])
                files.setItemData(files.count() - 1, f'{filename}\nSHA256 {release["sha256"]}', Qt.ItemDataRole.ToolTipRole)
            if len(releases) > 1:
                files.setCurrentIndex(-1)
            elif releases:
                files.setCurrentIndex(0)
            if mode:
                if generation == 109:
                    placeholder = "已是 109，跳过"
                elif generation is None and self._firmware_read_timed_out(row):
                    placeholder = "完整版本读取超时"
                elif generation != 108:
                    placeholder = "版本待核实"
                else:
                    placeholder = "请选择固件" if releases else "缺少签名 109 固件或 OTA 映射"
                files.setPlaceholderText(placeholder)
                files.setEnabled(bool(releases) and generation == 108)
            elif local_108:
                files.setPlaceholderText("请选择 108 → 109 范围")
                files.setEnabled(False)
            if row.get("eligibility_error"):
                files.setToolTip(row["eligibility_error"])
            files.currentIndexChanged.connect(self.invalidate)
            self.table.setCellWidget(i, 6, files)
        self.table.blockSignals(False)
        self.invalidate()

    def _apply_status(self, value, *, after_version_refresh=False):
        selected = {motor_target_key(self.rows[i]) for i in self.selected_rows()} if self.rows else None
        if after_version_refresh:
            self._version_refresh = None
            self._version_refresh_note = ""
            self._superseded_readback_job_id = value["job"].get("job_id")
        self._scan_required = value.get("scan_required", False)
        self.rows = value["targets"]
        self.configuration_sha256 = value.get("configuration_sha256")
        self.render_targets(selected)
        self.show_job(value["job"])

    def reload(self, *, after_version_refresh=False):
        self._request_status("读取维护目标", lambda value: self._apply_status(
            value, after_version_refresh=after_version_refresh))

    def refresh_versions(self):
        if self.pending or self._version_refresh is not None or self.job.get("active"):
            return
        self._version_refresh_note = "本次版本读取尚未完成。请重新刷新版本。"
        self.invalidate()
        self._version_refresh_deadline = time.monotonic() + VERSION_REFRESH_WAIT_S
        def accepted(receipt):
            if receipt.get("request_id") and receipt.get("state") in {"queued", "reading", "complete"}:
                self._version_refresh = receipt
            else:
                self._version_refresh_note = "本机版本读取未返回任务状态，请重新刷新。"
        self.request("发起本机版本读取", self.client.refresh_motor_versions, accepted,
                     status_read=True, station_read=True)
        if self.pending:
            self.scope_hint.setText("正在请求本机重新读取电机版本。")

    def _poll_version_refresh(self):
        current = self._version_refresh
        if time.monotonic() >= self._version_refresh_deadline:
            self._version_refresh = None
            self._version_refresh_note = "等待本机版本结果超时，请重新刷新版本。"
            self.controls()
            return
        if current["state"] == "complete":
            self.reload(after_version_refresh=True)
            return
        def received(receipt):
            if receipt.get("request_id") == current["request_id"] and receipt.get("state") in {"queued", "reading", "complete"}:
                self._version_refresh = receipt
            else:
                self._version_refresh = None
                self._version_refresh_note = "本机版本读取已中断或工位已变更，请重新刷新。"
        self.request("读取本机版本进度", self.client.motor_version_refresh_status, received,
                     status_read=True, station_read=True, quiet=True)

    def _source_changed(self):
        self._version_refresh_note = ""
        self._scan_required = False
        self.local_write_authorized = False
        self.preview_value = None
        self.job = {}
        self.rows = []
        self.configuration_sha256 = None
        self.table.setRowCount(0)
        self.details.clear()
        self.controls()
        self.reload()

    def preview(self, *, confirm=False):
        if self._version_refresh is not None or self._version_refresh_note or self._scan_required:
            return
        mode = self.scope.currentData()
        if mode and not self.upgrade_ready(mode):
            return
        selections = [{"index": int(self.table.item(i,7).text()), "release_id": self.table.cellWidget(i,6).currentData()}
                      for i in self.selected_rows()]
        if not selections or any(not entry["release_id"] for entry in selections):
            return
        payload = {"selections": selections}
        if mode:
            payload["mode"] = mode
        if self.configuration_sha256:
            payload["configuration_sha256"] = self.configuration_sha256
        def received(value):
            self.preview_value = value
            self.details.setPlainText("尚未写入。请核对最终接口、ID、型号、当前版本、目标版本、SHA256及兼容来源。\n" + self.describe(value, preview=True))
        self._request_action("核对所选固件", "preview", payload, received,
                             after_success=self.start if confirm else None)

    def start(self):
        preview = self.preview_value
        if (not preview or self.pending or self._version_refresh is not None
                or self._version_refresh_note or not self._write_allowed()):
            return
        account = self.identity()
        local = self.source.currentData()
        dialog = QMessageBox(QMessageBox.Icon.Question, "确认固件更新",
                             "确认设备已停测并失能？逐台写完全部所选电机后，统一重新上下电并回读核验。",
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setDetailedText(self.describe(preview, preview=True))
        summary = [f'{row.get("interface")} ID {row.get("motor_id")}  {row.get("joint_name")}\n'
                   f'{self._display_firmware(row.get("before") or {})} → {(row.get("manifest") or {}).get("version") or "未取得"}'
                   for row in preview.get("targets", [])]
        dialog.setInformativeText("\n".join(summary))
        if (dialog.exec() != QMessageBox.StandardButton.Yes or self.preview_value is not preview
                or self.source.currentData() != local or (not local and self.identity() != account)):
            return
        preview_id = preview["preview_id"]
        self.preview_value = None
        self.action("start", {"preview_id": preview_id, "confirmed": True})

    def acknowledge(self):
        if self.pending or not self.ack_button.isEnabled():
            return
        self.action("confirm-power-and-readback", {"job_id": self.job["job_id"], "confirmed": True})

    def action(self, name, payload):
        self._request_action("模块固件维护", name, payload, self.show_job)

    def poll(self):
        if self.pending:
            return
        if self.identity() != self.account:
            self.account = self.identity()
            if self.source.currentData():
                self.controls()
            elif not self.account:
                self.source.setCurrentIndex(0)
                return
            else:
                self._source_changed()
                return
        if self._version_refresh is not None:
            self._poll_version_refresh()
            return
        if self.job.get("active"):
            def received(value):
                if value["job"].get("active"):
                    self.show_job(value["job"])
                else:
                    self._apply_status(value)
            self._request_status("读取维护进度", received)

    @staticmethod
    def describe(value, preview=False):
        statuses = {"queued": "排队中", "pending": "待执行", "writing": "正在写入",
                    "written_awaiting_batch_power": "已写完，等待批次完成", "written_unverified": "已写入，尚未核验",
                    "awaiting_power": "等待统一重新上下电", "readback_requested": "已确认，正在启动批次回读",
                    "verifying": "正在回读", "verified": "实际版本已匹配", "failed": "失败，后续已停止",
                    "version_unknown": "实际版本未知", "interrupted": "已中断，未完成核验",
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
                          f'写前版本：{MotorMaintenanceDialog._display_firmware(before)}；软件 {before.get("software", "未知")}；硬件 {before.get("hardware", "未知")}；协议 {before.get("protocol", "未知")}',
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
        self.render_comparison()
        if job.get("job_id"):
            self.details.setPlainText(self.describe(job))
            rows = job.get("targets", [])
            key = (job["job_id"], "batch_power")
            if (job.get("status") == "awaiting_power" and rows
                    and all(r["status"] == "awaiting_power" and selfcheck_accepted(r.get("write_receipt", {}).get("selfcheck_response")) for r in rows)
                    and key not in self.prompted):
                self.prompted.add(key)
                QMessageBox.information(self, "请统一重新上下电", "全部所选电机已写完，校验应答已按产线脚本接受。请统一重新上下电，完成后点击「已统一上下电，开始核验」，自动逐台读取实际版本及协议。")

        self.controls()
