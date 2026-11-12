"""Version observations and independent motor downloads; no write operation."""
from datetime import datetime
from shiboken6 import isValid
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QComboBox, QPushButton, QPlainTextEdit, QCheckBox, QHBoxLayout, QTabWidget, QWidget, QFormLayout, QGroupBox, QFileDialog


def motor_target_key(row):
    return tuple(row.get(field) for field in ("index", "interface", "motor_id", "model"))


class MotorFirmwareDialog(QDialog):
    def __init__(self, client, run_rest, observations, identity, parent=None):
        super().__init__(parent)
        self.setWindowTitle("电机实际版本与固件下载")
        self.resize(850, 640)
        self.client, self.run_rest = client, run_rest
        self.observations, self.identity = observations, identity
        self.rows, self.releases = [], []
        self.pending = False
        self.resource_dialog = None
        self.account = self.identity()
        layout = QVBoxLayout(self)
        intro = QLabel("扫描后自动读取实际版本。查询本机固件无需登录，从平台获取固件时请登录测试平台。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.targets = QComboBox(self)
        self.versions = QPlainTextEdit(self)
        self.versions.setReadOnly(True)
        version_header = QHBoxLayout()
        version_header.addWidget(QLabel("已扫描目标的实际版本", self), 1)
        self.version_details = QCheckBox("显示原始响应", self)
        version_header.addWidget(self.version_details)
        layout.addLayout(version_header)
        self.version_details.toggled.connect(self.refresh)
        for scrollbar in (self.versions.verticalScrollBar(), self.versions.horizontalScrollBar()):
            scrollbar.sliderReleased.connect(self.refresh)
        layout.addWidget(self.versions, 2)
        downloads = QGroupBox("按所选电机查询固件", self)
        download_layout = QVBoxLayout(downloads)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("当前电机", self))
        target_row.addWidget(self.targets, 1)
        self.cached_check_button = QPushButton("查询本机固件", self)
        self.check_button = QPushButton("查询平台固件", self)
        for button in (self.cached_check_button, self.check_button):
            button.setObjectName("outlineButton")
            target_row.addWidget(button)
        download_layout.addLayout(target_row)
        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("固件", self))
        self.files = QComboBox(self)
        self.files.setPlaceholderText("先选择目标并查询")
        file_row.addWidget(self.files, 1)
        self.download_button = QPushButton("从平台下载", self)
        self.download_button.setObjectName("primaryButton")
        file_row.addWidget(self.download_button)
        self.import_button = QPushButton("导入签名固件…", self)
        self.import_button.setObjectName("outlineButton")
        self.import_button.setToolTip(
            "选择签名 release.json。固件文件及可选的签名 motor-ota-policy.json 放在同一目录。")
        file_row.addWidget(self.import_button)
        download_layout.addLayout(file_row)
        self.receipt = QPlainTextEdit(self)
        self.receipt.setReadOnly(True)
        self.receipt.setMaximumHeight(96)
        self.receipt.setPlaceholderText("下载只保存文件，实际设备版本以上方读取结果为准。")
        download_layout.addWidget(self.receipt)
        layout.addWidget(downloads)
        more = QHBoxLayout()
        self.resource_library_button = QPushButton("平台固件文件下载", self)
        self.maintenance_button = QPushButton("108 → 109 升级 / 固件维护…", self)
        self.parameters_button = QPushButton("参数读取与维护…", self)
        self.maintenance_button.setToolTip("带入当前电机，可在维护窗口切换为整组升级。")
        self.parameters_button.setToolTip("带入当前电机，可在参数窗口选择整组或其他电机。")
        for widget, callback in ((self.resource_library_button, self.resource_library),
                                 (self.maintenance_button, self.maintenance),
                                 (self.parameters_button, self.parameters)):
            widget.setObjectName("outlineButton")
            widget.clicked.connect(callback)
            more.addWidget(widget)
        more.addStretch(1)
        layout.addLayout(more)
        self.targets.currentIndexChanged.connect(self._target_changed)
        self.cached_check_button.clicked.connect(self.check_cached)
        self.check_button.clicked.connect(self.check)
        self.download_button.clicked.connect(self.download)
        self.import_button.clicked.connect(self.import_firmware)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.finished.connect(self.timer.stop)
        self.timer.start(1000)
        self.refresh()

    def refresh(self):
        account = self.identity()
        if account != self.account:
            self.account = account
            self._target_changed()
        selected = self._selected_motor()
        rows = self.observations()
        keys = [motor_target_key(row) for row in rows]
        old_keys = [motor_target_key(row) for row in self.rows]
        self.rows = rows
        if keys != old_keys:
            self.targets.clear()
            for row in rows:
                self.targets.addItem(f'{row["joint_name"]} · {row["interface"]} ID {row["motor_id"]}', row["index"])
            if selected in keys:
                self.targets.setCurrentIndex(keys.index(selected))
        self._render_versions(rows)
        self.cached_check_button.setEnabled(bool(rows) and not self.pending)
        self.import_button.setEnabled(bool(rows) and not self.pending)
        self.check_button.setEnabled(bool(account) and bool(rows) and not self.pending)
        self.download_button.setEnabled(bool(account) and bool(self.releases) and not self.pending)

    @staticmethod
    def _version_text(row, details):
        statuses = {"known": "已读取", "timeout": "查询超时", "unknown": "未知", "unsupported": "不支持", "stale": "已过期"}
        status = statuses.get(row.get("status"), row.get("status", "未知"))
        errors = row.get("errors") or {}
        if row.get("status") == "known" and (errors or not row.get("raw_firmware")):
            status = "已读取（部分）"
        lines = [f'{row["interface"]} ID {row["motor_id"]} · {status}',
                 f'软件：{row.get("software") or "未取得"}；硬件：{row.get("hardware") or "未取得"}；协议：{row.get("protocol") or "未取得"}',
                 f'原始固件字符串：{row.get("raw_firmware") or "未取得"}']
        if row.get("configured_profile"):
            lines.append(f'驱动配置：{row["configured_profile"]}；当前协议配置：{row.get("active_profile") or "未应用"}')
        if row.get("profile_error"):
            lines.append("驱动配置提示：" + row["profile_error"])
        observed = row.get("observed_at")
        try:
            stamp = datetime.fromisoformat(observed).astimezone().strftime("%Y-%m-%d %H:%M:%S %z") if observed else "尚未读取"
        except (ValueError, TypeError):
            stamp = observed or "尚未读取"
        lines.append("读取时间：" + stamp)
        names = {"0x31": "原始固件字符串（0x31）", "0x1e": "数值版本（0x1E）"}
        for code, error in errors.items():
            lines.append(f'读取提示：{names.get(str(code).lower(), str(code))}：{error}')
        if row.get("version_relation") == "same_generation":
            lines.append("版本对应：完整固件与数值软件版本属于同一 108/109 版本。")
        elif row.get("version_relation") == "conflict":
            lines.append("读取提示：完整固件与数值软件版本的 108/109 代际冲突，请重新读取。")
        elif row.get("version_relation") == "unverified":
            lines.append("读取提示：原始字符串与数值软件版本不同，二者关系尚未核实。")
        if details:
            lines.append("原始读取时间：" + str(observed or "尚未读取"))
            for code, payload in (row.get("raw_responses") or {}).items():
                lines.append(f'原始响应 {code}：{payload}')
        return "\n".join(lines)

    def _render_versions(self, rows):
        text = "\n\n".join(self._version_text(row, self.version_details.isChecked()) for row in rows)
        text = text or "尚未扫描电机；没有实际版本证据。"
        if text == self.versions.toPlainText():
            return
        vertical, horizontal = self.versions.verticalScrollBar(), self.versions.horizontalScrollBar()
        # Do not change scrollbar range/document while Qt owns a slider drag.
        # The release signal renders the newest cached observations immediately.
        if vertical.isSliderDown() or horizontal.isSliderDown():
            return
        vpos, hpos = vertical.value(), horizontal.value()
        cursor = self.versions.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        self.versions.setPlainText(text)
        last = self.versions.document().characterCount() - 1
        cursor = self.versions.textCursor()
        cursor.setPosition(min(anchor, last))
        cursor.setPosition(min(position, last), QTextCursor.MoveMode.KeepAnchor)
        self.versions.setTextCursor(cursor)
        vertical.setValue(vpos)
        horizontal.setValue(hpos)

    def _target_changed(self):
        self.releases = []
        self.files.clear()
        self.receipt.clear()
        self.download_button.setEnabled(False)

    def _target_key(self):
        index = self.targets.currentData()
        row = next((r for r in self.observations() if r.get("index") == index), {})
        return tuple(row.get(k) for k in ("index", "interface", "motor_id", "model", "raw_firmware", "software", "hardware", "protocol", "status"))

    def _selected_motor(self):
        index = self.targets.currentData()
        return next((motor_target_key(row) for row in self.rows if row["index"] == index), None)

    def _request(self, label, operation, success):
        if self.pending:
            return
        self.pending = True
        identity = self.identity()
        target = self._target_key()
        def received(value):
            if isValid(self) and self.identity() == identity and self._target_key() == target:
                success(value.get("result", value))
        def finished():
            if not isValid(self):
                return
            self.pending = False
            self.refresh()
        self.refresh()
        self.run_rest(label, operation, received, on_finished=finished)

    def check(self):
        index = self.targets.currentData()
        if index is None or not self.identity() or self.pending:
            return
        self._target_changed()
        self._request("查询平台固件", lambda: self.client.motor_firmware_releases(index),
                      lambda value: self._show_releases(value, local=False))

    def check_cached(self):
        index = self.targets.currentData()
        if index is None or self.pending:
            return
        self._target_changed()
        self._request("查询本机固件", lambda: self.client.motor_firmware_cached_releases(index),
                      lambda value: self._show_releases(value, local=True))

    def _show_releases(self, value, *, local):
        releases = value.get("releases", [])
        self.releases = [] if local else releases
        self.files.clear()
        for release in releases:
            manifest = release["manifest"]
            label = f'{manifest["version"]} · {manifest["model"]} · {manifest["notes"]}'
            if local:
                self.files.addItem(f"本机 · {label}")
            else:
                self.files.addItem(label, manifest["release_id"])
        if releases:
            self.files.setCurrentIndex(0)
        if local:
            message = (f"本机有 {len(releases)} 个适用于当前电机的已验签固件。"
                       if releases else "本机没有适用于当前电机的已验签固件。可导入签名固件或登录平台下载。")
        else:
            message = (f"平台有 {len(releases)} 个兼容固件，请选择版本下载。"
                       if releases else "平台没有已发布且兼容的新固件。")
        self.receipt.setPlainText(message)

    def download(self):
        index, release_id = self.targets.currentData(), self.files.currentData()
        if index is None or not release_id or not self.identity():
            return
        self._request("下载电机固件", lambda: self.client.motor_firmware_download(index, release_id),
            lambda value: self.receipt.setPlainText("已下载，尚未刷写；实际版本以上方读取结果为准。\n保存位置：" + str(value.get("firmware_path") or "回执未返回保存位置")))

    def import_firmware(self):
        index = self.targets.currentData()
        if index is None or self.pending:
            return
        target = self._target_key()
        path, _ = QFileDialog.getOpenFileName(self, "选择已签名的固件清单", "", "固件清单 (*.json)")
        if not path or self._target_key() != target or self.pending:
            return
        self._target_changed()
        def show_receipt(value):
            location = value.get("firmware_path") or "回执未返回保存位置"
            policy = "\n签名兼容规则已保存。" if value.get("policy_imported") else ""
            self.receipt.setPlainText(f"已验签并保存到本机，可在固件维护窗口刷新版本。\n保存位置：{location}{policy}")
        self._request("导入签名固件", lambda: self.client.motor_firmware_import(index, path), show_receipt)

    def maintenance(self):
        from .motor_maintenance_dialog import MODULE_UPGRADE, SELECTED_UPGRADE, MotorMaintenanceDialog
        from PySide6.QtCore import Qt
        selected = self._selected_motor()
        dialog = MotorMaintenanceDialog(self.client, self.run_rest, self.identity, self,
                                        selected_motor=selected, scope=SELECTED_UPGRADE if selected else MODULE_UPGRADE)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.finished.connect(dialog.close)
        dialog.show()

    def resource_library(self):
        if self.resource_dialog is not None and isValid(self.resource_dialog) and not self.resource_dialog._closed:
            self.resource_dialog.show();self.resource_dialog.raise_();return
        dialog = MotorFirmwareResourcesDialog(self.client, self.run_rest, self.identity, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.finished.connect(dialog.close)
        self.resource_dialog = dialog
        dialog.show()

    def parameters(self):
        from .motor_parameters_dialog import MotorParametersDialog
        from PySide6.QtCore import Qt
        dialog = MotorParametersDialog(self.client, self.run_rest, self.identity, self,
                                       selected_motor=self._selected_motor())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.finished.connect(dialog.close)
        dialog.show()


class MotorFirmwareResourcesDialog(QDialog):
    """Signed file downloads, kept separate from device observations and OTA."""
    def __init__(self, client, run_rest, identity, parent=None):
        super().__init__(parent)
        self.client, self.run_rest, self.identity = client, run_rest, identity
        self.pending = False
        self.releases = []
        self.account = self.identity()
        self._closed = False
        self.setWindowTitle("固件文件下载")
        self.resize(880, 610)
        layout = QVBoxLayout(self)
        intro = QLabel("选择已发布的固件文件并保存到本机。设备当前版本请在“电机实际版本”中查看。", self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("资源版本", self))
        self.files = QComboBox(self)
        self.files.setPlaceholderText("正在获取资源列表…")
        toolbar.addWidget(self.files, 1)
        self.check_button = QPushButton("刷新列表", self)
        self.check_button.setObjectName("outlineButton")
        toolbar.addWidget(self.check_button)
        layout.addLayout(toolbar)
        self.tabs = QTabWidget(self)
        page = QWidget(self)
        content = QVBoxLayout(page)
        form = QFormLayout()
        self.metadata = {}
        for key, label in (("version", "文件版本标签"), ("filename", "文件名"),
                           ("models", "资源登记型号"), ("size", "文件大小"),
                           ("mapping", "设备版本映射"), ("path", "保存位置")):
            value = QLabel("—", page)
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.metadata[key] = value
            form.addRow(label + "：", value)
        content.addLayout(form)
        self.summary = QLabel("尚未查询资源。", page)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        content.addWidget(self.summary)
        content.addStretch(1)
        self.tabs.addTab(page, "文件信息")
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.tabs.addTab(self.details, "校验详情")
        layout.addWidget(self.tabs, 1)
        footer = QHBoxLayout()
        footer.addWidget(QLabel("仅保存文件；不会安装或刷写设备。", self))
        footer.addStretch(1)
        self.download_button = QPushButton("下载所选文件", self)
        self.download_button.setObjectName("primaryButton")
        footer.addWidget(self.download_button)
        close = QPushButton("关闭", self)
        close.setObjectName("outlineButton")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.check_button.clicked.connect(self.check)
        self.download_button.clicked.connect(self.download)
        self.files.currentIndexChanged.connect(self.show_selection)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1000)
        self.finished.connect(self.finished_window)
        self.refresh()
        self.check()

    def refresh(self):
        account = self.identity()
        if account != self.account:
            self.account = account
            self.releases = []
            self.files.clear()
            self.details.clear()
            self.summary.clear()
            for value in self.metadata.values():
                value.setText("—")
        self.check_button.setEnabled(bool(account) and not self.pending)
        self.files.setEnabled(bool(account) and bool(self.releases) and not self.pending)
        self.download_button.setEnabled(bool(account) and bool(self.releases) and not self.pending)

    def request(self, label, operation, success):
        if self.pending or self._closed or not self.identity():
            return
        self.pending = True
        account = self.identity()
        self.refresh()
        def received(value):
            if isValid(self) and not self._closed and self.identity() == account:
                success(value.get("result", value))
        def finished():
            if isValid(self) and not self._closed:
                self.pending = False
                self.refresh()
        self.run_rest(label, operation, received, on_finished=finished)

    def check(self):
        def received(value):
            selected = self.files.currentData()
            self.releases = value.get("releases", [])
            self.files.clear()
            for release in self.releases:
                manifest = release["manifest"]
                self.files.addItem(manifest["version_label"] + " · " + ", ".join(manifest["models"]), manifest["release_id"])
            found = self.files.findData(selected)
            if found >= 0:
                self.files.setCurrentIndex(found)
            elif self.files.count():
                self.files.setCurrentIndex(0)
            if self.releases:
                self.show_selection()
            else:
                self.files.setPlaceholderText("暂无已发布资源")
                self.summary.setText("尚无已发布的固件文件，请先在平台发布资源。")
                self.details.clear()
                for field in self.metadata.values():
                    field.setText("—")
        self.request("查询固件下载资源", self.client.motor_firmware_resource_releases, received)

    def show_selection(self, *args):
        release = next((row for row in self.releases if row["manifest"]["release_id"] == self.files.currentData()), None)
        if not release:
            return
        manifest = release["manifest"]
        self.metadata["version"].setText(manifest["version_label"] + "（不是实际电机版本）")
        self.metadata["filename"].setText(manifest.get("filename") or "未登记")
        self.metadata["models"].setText("、".join(manifest["models"]))
        size = manifest.get("size_bytes")
        self.metadata["size"].setText(f"{size / 1024:.1f} KiB（{size:,} 字节）" if isinstance(size, int) else "未登记")
        self.metadata["mapping"].setText("未登记完整的软件 / 硬件 / 协议对应关系；此入口不提供刷写。")
        self.metadata["path"].setText("尚未下载")
        self.summary.setText("选择所需文件，点击右下角“下载所选文件”保存到本机。")
        self.details.setPlainText("版本标签来自文件名，不是实际电机版本。\n"
                                  "软件/硬件/协议及来源版本映射未核实；flash_eligible=false，不进入刷写列表。\n"
                                  f"发布记录：{manifest['release_id']}\n"
                                  f"文件SHA256：{manifest.get('sha256') or '未登记'}\n"
                                  f"发布时间：{release.get('published_at') or manifest.get('published_at') or '未登记'}\n"
                                  "下载过程沿用签名与文件哈希校验，完整来源证据保存在发布记录中。")

    def download(self):
        release_id = self.files.currentData()
        if not release_id:
            return
        def received(value):
            path = value.get("resource_path") or "未返回保存位置"
            self.metadata["path"].setText(path)
            title = "下载完成" if value.get("status") == "downloaded" else "下载结果已返回"
            self.summary.setText(title + "。未安装、未刷写、未核验实际版本。")
            self.details.setPlainText(self.details.toPlainText() + "\n\n" + title +
                                      "；未安装、未刷写、未核验实际版本。\n保存位置：" + path)
        self.request("下载固件资源（不刷写）", lambda: self.client.motor_firmware_resource_download(release_id), received)

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)

    def finished_window(self, *args):
        self._closed = True
        self.timer.stop()
