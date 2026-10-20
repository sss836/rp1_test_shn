"""Small factory floor workflow backed by local gateway quick verification."""
from __future__ import annotations
from factory_hmi.client import GatewayError
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QGroupBox, QPushButton, QComboBox, QLineEdit, QDialog, QFormLayout,
    QDialogButtonBox, QDoubleSpinBox, QCheckBox, QButtonGroup)


class QuickVerificationPage(QWidget):
    def __init__(self, client, runner, render_snapshot, parent=None):
        super().__init__(parent)
        self.client, self.runner, self.render_snapshot = client, runner, render_snapshot
        self.setObjectName('quickVerificationPage')
        self.setStyleSheet("""
            QWidget#quickVerificationPage { background: #f3f5f8; }
            QGroupBox#quickCard { background: #fff; border: 1px solid #d7dfe7;
                border-radius: 6px; margin-top: 12px; padding: 12px 10px 10px;
                font-weight: 700; color: #173a58; }
            QGroupBox#quickCard::title { subcontrol-origin: margin; left: 12px;
                padding: 0 5px; background: #f3f5f8; }
            QPushButton#quickPreset { background: #fff; color: #24435e;
                border: 1px solid #c8d5e1; min-height: 28px; max-height: 36px; }
            QPushButton#quickPreset:hover { background: #edf4fa; }
            QPushButton#quickPreset:checked { background: #e3f0fc;
                color: #165fa5; border: 2px solid #2e73b9; }
            QPushButton#quickPreset:disabled { color: #95a0ac; background: #f0f3f6; }
        """)
        self.profiles = {}
        self.selected = None
        self._suggested_sample = ''
        self._start_error = ''
        self._pending = False
        self._status_pending = False
        self._phase = 'idle'
        self._loading = False
        self._buttons = []
        layout = QVBoxLayout(self)
        title = QLabel('工厂快速验证')
        title.setObjectName('pageHeading')
        layout.addWidget(title)
        note = QLabel('选择部位与供电方式 → 运行并填写小时数 → 回默认位和循环验证\n'
                      '全程使用本机配置与内置轨迹，速度固定 1 倍；记录保存在本机，无需登录云端。')
        note.setObjectName('pageDescription'); note.setWordWrap(True); layout.addWidget(note)
        self.selection_group = QButtonGroup(self)
        self.selection_group.setExclusive(True)
        choices = QGroupBox('选择验证部位'); choices.setObjectName('quickCard')
        self.grid = QGridLayout(choices)
        layout.addWidget(choices)
        settings = QGroupBox('本次验证'); settings.setObjectName('quickCard')
        form = QFormLayout(settings)
        self.sample = QLineEdit()
        self.sample.setPlaceholderText('本地样机标识，可自由填写')
        form.addRow('样机标识', self.sample)
        self.power_source = QComboBox()
        self.power_source.addItem('外部供电', 'external')
        self.power_source.addItem('PLC 供电（自动控制）', 'plc')
        form.addRow('供电方式', self.power_source)
        self.power_note = QLabel()
        self.power_note.setWordWrap(True)
        form.addRow(self.power_note)
        self.power_source.currentIndexChanged.connect(self._power_source_changed)
        self._power_source_changed()
        self.primary_can = QComboBox(); self.secondary_can = QComboBox()
        for widget in [self.primary_can, self.secondary_can]:
            widget.addItems([f'can{i}' for i in range(4)])
            widget.currentTextChanged.connect(self._mapping_changed)
        self.primary_label = QLabel('CAN 接口')
        self.secondary_label = QLabel('右侧 CAN')
        form.addRow(self.primary_label, self.primary_can)
        form.addRow(self.secondary_label, self.secondary_can)
        self.asset = QLabel('等待选择部位'); self.asset.setWordWrap(True)
        form.addRow('内置轨迹', self.asset)
        self.mapping_note = QLabel(); self.mapping_note.setWordWrap(True)
        form.addRow('电机映射', self.mapping_note)
        self.reload_button = QPushButton('重新读取配置与轨迹')
        self.reload_button.setObjectName('outlineButton')
        self.reload_button.clicked.connect(self._prepare)
        form.addRow(self.reload_button)
        layout.addWidget(settings)
        self.detail = QLabel('正在读取本机验证目录…'); self.detail.setWordWrap(True)
        self.detail.setMinimumHeight(48); layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        self.run_button = QPushButton('运行…'); self.run_button.setObjectName('primaryButton')
        self.pause_button = QPushButton('暂停'); self.pause_button.setObjectName('outlineButton')
        self.resume_button = QPushButton('继续'); self.resume_button.setObjectName('outlineButton')
        self.end_button = QPushButton('结束并失能'); self.end_button.setObjectName('criticalOutlineButton')
        for button in [self.run_button, self.pause_button, self.resume_button, self.end_button]:
            button.setMinimumHeight(40); buttons.addWidget(button)
        self.run_button.clicked.connect(self._ask_run)
        self.pause_button.clicked.connect(lambda: self._action('暂停', self.client.playback_pause))
        self.resume_button.clicked.connect(lambda: self._action('继续', self.client.playback_resume))
        self.end_button.clicked.connect(self._end)
        layout.addLayout(buttons)
        self.supply_note = QLabel('结束后电机失能并断开 CAN，外部电源由现场管理。')
        self.supply_note.setWordWrap(True); layout.addWidget(self.supply_note); layout.addStretch(1)
        self.timer = QTimer(self); self.timer.setInterval(500); self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self._sync_buttons()
        self.runner('读取快速验证目录', self.client.quick_profiles, self._catalog, quiet=True,
                    on_failure=lambda exc: self.detail.setText('本机快速验证目录不可用：'+str(exc)))

    def _power_source_changed(self, _index=0):
        external = self.power_source.currentData() == 'external'
        self.power_note.setText(
            '外部电源供电，通过 CAN 扫描核对电机反馈；PLC 在线状态不影响本模式。'
            if external else
            '由网关按默认参数控制 PLC，实际供电许可确认后才扫描和使能。')
        if hasattr(self, 'supply_note'):
            self.supply_note.setText('结束后电机失能并断开 CAN，外部电源由现场管理。' if external else
                                     'PLC 为各窗口共用供电；结束只清理本窗口电机与 CAN。')

    def _catalog(self, payload):
        for i, profile in enumerate(payload.get('items', [])):
            key = profile['id']; self.profiles[key] = profile
            button = QPushButton(profile['label']); button.setObjectName('quickPreset')
            button.setCheckable(True); self.selection_group.addButton(button, i)
            button.clicked.connect(lambda _checked=False, key=key: self.select(key))
            self.grid.addWidget(button, i//2, i%2); self._buttons.append(button)
        self.detail.setText('请选择部位，自动读取网关默认参数和内置轨迹')
        self._sync_buttons()

    def select(self, key):
        if self._pending or self._phase in {'running', 'paused'}:
            return
        self.selected = key; p = self.profiles[key]
        self.selection_group.button(list(self.profiles).index(key)).setChecked(True)
        self._loading = True
        waist = key == 'waist_hip'
        self.primary_label.setText('左侧 CAN' if waist else 'CAN 接口')
        self.secondary_label.setVisible(waist); self.secondary_can.setVisible(waist)
        self.primary_can.setCurrentText(p['interfaces'][1] if waist else p['interfaces'][0])
        self.secondary_can.setCurrentText(p['interfaces'][0] if waist else 'can1')
        if not self.sample.text().strip() or self.sample.text() == self._suggested_sample:
            self.sample.setText('本地验证-'+key)
        self._suggested_sample = '本地验证-'+key
        self.asset.setText(p['trajectory'])
        self._loading = False
        self._mapping_text()
        self._prepare()

    def _mapping_text(self):
        if self.selected == 'waist_hip':
            self.mapping_note.setText(f"左侧 {self.primary_can.currentText()}：ID1 腰 yaw、ID2 左髋 pitch；"
                                      f"右侧 {self.secondary_can.currentText()}：ID1 腰 roll、ID2 右髋 pitch。")
        elif self.selected:
            self.mapping_note.setText(f"{self.primary_can.currentText()} · {self.profiles[self.selected]['motor_count']} 个电机，沿用网关默认配置。")

    def _mapping_changed(self):
        if self._loading:
            return
        self._mapping_text()
        if self.selected:
            self._phase = 'changed'; self.detail.setText('CAN 映射已修改，请重新读取配置与轨迹')
            self._sync_buttons()

    def _interfaces(self):
        if self.selected == 'waist_hip':
            right, left = self.secondary_can.currentText(), self.primary_can.currentText()
            return [right, left, right, left]
        return [self.primary_can.currentText()]

    def _prepare(self):
        if not self.selected or self._pending:
            return
        key, interfaces = self.selected, self._interfaces()
        self._start_error = ''
        self._pending = True; self._phase = 'preparing'; self._sync_buttons()
        self.detail.setText('正在读取部位配置与内置轨迹（此步骤不使能电机）…')
        self.runner('读取快速验证', lambda: self.client.quick_prepare(key, interfaces), self._received,
                    quiet=True, on_failure=self._failed, on_finished=self._finished)

    def _ask_run(self):
        dialog = QDialog(self); dialog.setWindowTitle('快速验证运行')
        form = QFormLayout(dialog)
        hours = QDoubleSpinBox(); hours.setDecimals(3); hours.setRange(.001, 10000); hours.setValue(1)
        hours.setSuffix(' 小时'); form.addRow('循环运行时间', hours)
        external = self.power_source.currentData() == 'external'
        explanation = QLabel(
            ('使用已接通的外部电源，连接 CAN、扫描并使能电机，\n' if external else
             '按默认参数打开所需 PLC 通道，连接 CAN、扫描并使能电机，\n')
            + '平滑回默认位，预检通过后以 1 倍速循环。')
        explanation.setWordWrap(True)
        form.addRow(explanation)
        safety = QCheckBox(('已接通外部电源，并确认支撑、运动范围及物理急停安全' if external else
                            '已确认机器人支撑、运动范围和物理急停安全'))
        form.addRow(safety)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('确认并运行')
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        safety.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.start(hours.value(), safety.isChecked())
        dialog.deleteLater()

    def start(self, hours, confirmed):
        if self._pending or self._phase != 'ready':
            return
        sample = self.sample.text().strip()
        if not sample:
            self.detail.setText('请填写本地样机标识'); return
        self._start_error = ''
        power_source = self.power_source.currentData()
        self._pending = True; self._phase = 'starting'; self._sync_buttons()
        self.detail.setText('正在准备运行，可点击“结束并失能”取消')
        self.runner('快速验证运行', lambda: self.client.quick_start(hours, sample, physical_estop_confirmed=confirmed, power_source=power_source),
                    self._received, quiet=True, on_failure=self._start_failed, on_finished=self._finished)

    @staticmethod
    def _failure_text(exc):
        detail = getattr(exc, 'detail', '')
        if isinstance(detail, dict):
            detail = detail.get('message') or str(exc)
        return str(detail or exc)

    def _start_failed(self, exc):
        self._start_error = self._failure_text(exc)
        self._failed(exc)
        if isinstance(exc, GatewayError) and exc.status_code is not None and 400 <= exc.status_code < 500:
            # A confirmed rejection is already handled by the gateway. Sending
            # end here hides the failure and could stop an earlier active run.
            return
        # A lost response does not establish whether motors started. Cancel
        # uncertain/5xx requests, retaining the original cause after cleanup.
        self.runner('取消未确认的快速验证', self.client.quick_end, self._received, quiet=True,
                    on_failure=self._failed)

    def _action(self, label, operation):
        if self._pending:
            return
        self._pending = True; self._sync_buttons()
        self.runner(label, operation, self._received, quiet=True,
                    on_failure=self._failed, on_finished=self._finished)

    def _end(self):
        self.end_button.setEnabled(False)
        self.detail.setText('正在结束并等待本窗口电机失能…')
        self.runner('结束快速验证', self.client.quick_end, self._received, quiet=True,
                    on_failure=self._failed, on_finished=self._finished)

    def _received(self, payload):
        if isinstance(payload.get('snapshot'), dict):
            self.render_snapshot(payload['snapshot'])
        result = payload.get('result', payload)
        if isinstance(result, dict) and 'phase' in result:
            self._phase = result['phase']
            detail = str(result.get('detail', ''))
            failure = self._start_error or str(result.get('failure') or '')
            if failure and failure not in detail:
                detail = failure + '；' + detail
            self.detail.setText(detail)
        self._sync_buttons()

    def _failed(self, exc):
        self._phase = 'fault'
        self.detail.setText('操作未完成：'+self._failure_text(exc))
        self._sync_buttons()

    def _finished(self):
        self._pending = False; self._sync_buttons()

    def refresh(self):
        if not self.selected or self._status_pending:
            return
        self._status_pending = True
        self.runner('读取快速验证状态', self.client.quick_status, self._status_received, quiet=True,
                    on_finished=lambda: setattr(self, '_status_pending', False))

    def _status_received(self, payload):
        if self._phase == 'changed' and payload.get('phase') == 'ready':
            return
        self._received(payload)

    def _sync_buttons(self):
        active = self._phase not in {'idle', 'ready', 'changed', 'ended', 'completed', 'fault'}
        busy = self._pending or active or self._phase in {'running', 'paused'}
        for button in self._buttons:
            button.setEnabled(not busy)
        for widget in [self.primary_can, self.secondary_can, self.sample, self.power_source]:
            widget.setEnabled(not busy)
        self.reload_button.setEnabled(bool(self.selected) and not busy)
        self.run_button.setEnabled(self._phase == 'ready' and not self._pending)
        self.pause_button.setEnabled(self._phase == 'running' and not self._pending)
        self.resume_button.setEnabled(self._phase == 'paused' and not self._pending)
        self.end_button.setEnabled(bool(self.selected) and (active or self._phase in {'ready', 'fault'}))

    def stop(self):
        self.timer.stop()
