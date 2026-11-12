"""Template-only parameter maintenance; expected values are never editable here."""
import time
from shiboken6 import isValid
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QLabel,QTableWidget,QTableWidgetItem,
                              QComboBox,QPushButton,QPlainTextEdit,QMessageBox,QHeaderView,
                              QHBoxLayout,QGroupBox,QWidget,QTabWidget,QAbstractItemView,QFileDialog)
from factory_hmi.client import GatewayError
from factory_hmi.motor_parameter_contract import display
from factory_hmi.core.parameter_reference import describe_reference,reference_text
from .motor_firmware_dialog import motor_target_key

SCRIPT_PROFILE='lro-pn-script-v1'
SCRIPT_ASSOCIATION='脚本兼容：单请求时序关联，回包不含Pn编号'
TEMPLATE_ENTRIES_ROLE = Qt.ItemDataRole.UserRole + 1
REFERENCE_ITEM_STATES={'read':'已记录实测值','reference_match':'与参考一致','reference_difference':'与参考有差异'}
REFERENCE_JOB_STATES={'reference_checked':'只读检查结束','reference_checked_with_findings':'只读检查结束，有差异或未核验项'}

class MotorParametersDialog(QDialog):
    def __init__(self,client,run_rest,identity,parent=None,*,selected_motor=None):
        super().__init__(parent)
        self.client,self.run_rest,self.identity=client,run_rest,identity
        self.account=identity();self.pending=False;self.preview_value=None;self.job={};self.prompted=set()
        self.rows=[];self.selected_motor=selected_motor;self.configuration_sha256=None
        self.reference_dialog=None
        self._closed=False;self._request_label='';self._read_revision=None;self._selection_revision=0
        self._comparison_read_job_id = None
        self.local_write_authorized = False
        self._update_flow = None; self._update_error = ""; self._loading_templates = False
        self._display_value={};self._details_pending=''
        self.setWindowTitle('模块参数');self.resize(1220,680)
        self.setObjectName('maintenanceDialog')
        layout=QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        heading=QHBoxLayout()
        title=QLabel('电机参数',self)
        title.setObjectName('pageHeading')
        heading.addWidget(title,1)
        self.reference_button=QPushButton('Pn参考资料',self)
        self.reference_button.setObjectName('outlineButton');heading.addWidget(self.reference_button)
        layout.addLayout(heading)
        note=QLabel('对照实测参数与模板目标。更新前请停测并失能。',self)
        note.setObjectName('pageDescription')
        note.setWordWrap(True);layout.addWidget(note)
        self.step_hint=QLabel(self);self.step_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.step_hint.setObjectName('maintenanceHint')
        self.step_hint.setWordWrap(True);layout.addWidget(self.step_hint)
        targets=QWidget(self);target_layout=QVBoxLayout(targets)
        target_layout.setContentsMargins(0, 0, 0, 0)
        toolbar=QHBoxLayout()
        self.source=QComboBox(self)
        self.source.addItem('本机','local')
        self.source.addItem('测试平台','platform')
        self.source.setCurrentIndex(1 if self.account else 0)
        toolbar.addWidget(QLabel('模板来源',self))
        toolbar.addWidget(self.source)
        self.reload_button=QPushButton('刷新目标',self)
        self.templates_button=QPushButton('刷新适用模板',self)
        self.select_all_button=QPushButton('全选',self)
        self.clear_selection_button=QPushButton('清除选择',self)
        self.import_button=QPushButton('导入签名模板…',self)
        for button in (self.reload_button,self.select_all_button,self.clear_selection_button,self.templates_button,self.import_button):
            button.setObjectName('outlineButton');toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.target_count=QLabel('尚无目标',self);toolbar.addWidget(self.target_count)
        target_layout.addLayout(toolbar)
        self.table=QTableWidget(0,11,self)
        self.table.setObjectName('maintenanceTable')
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setHorizontalHeaderLabels(['选择','模块 / 关节','接口','ID','型号','适用模板','索引','实际固件','实际参数','目标参数','符合情况'])
        self.table.setColumnHidden(6,True);self.table.verticalHeader().setVisible(False)
        self.table.setColumnHidden(4, True)
        self.table.setColumnHidden(7, True)
        self.table.verticalHeader().setDefaultSectionSize(54)
        header=self.table.horizontalHeader()
        for column in (0,2,3,4,7,10):header.setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        for column in (1,5,8,9):header.setSectionResizeMode(column,QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(180)
        self.table.itemChanged.connect(self.invalidate);target_layout.addWidget(self.table)
        layout.addWidget(targets, 1)
        result_panel=QWidget(self);result_layout=QVBoxLayout(result_panel);result_layout.setContentsMargins(0,0,0,0)
        read_actions=QHBoxLayout()
        self.read_reference_button=QPushButton('只读参考',self);self.read_reference_button.setObjectName('outlineButton')
        self.read_button=QPushButton('读取并比较',self);self.read_button.setObjectName('outlineButton')
        self.preview_button=QPushButton('更新所选…',self);self.preview_button.setObjectName('primaryButton')
        self.cancel_button=QPushButton('取消后续操作',self);self.cancel_button.setObjectName('criticalOutlineButton')
        for button in (self.read_reference_button,self.read_button,self.cancel_button):
            read_actions.addWidget(button)
        read_actions.addStretch(1)
        self.result_status=QLabel('尚未读取',self);self.result_status.setWordWrap(True);read_actions.addWidget(self.result_status)
        details_button = QPushButton('逐项明细', self)
        details_button.setObjectName('outlineButton')
        details_button.setCheckable(True)
        read_actions.addWidget(details_button)
        read_actions.addWidget(self.preview_button)
        result_layout.addLayout(read_actions)
        self.result_tabs=QTabWidget(self)
        self.comparison=QTableWidget(0,7,self)
        self.comparison.setHorizontalHeaderLabels(['目标','参数','读取 / 旧值','模板新值','写后回读','比较状态','写入状态'])
        self.comparison.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.comparison.verticalHeader().setVisible(False)
        self.comparison.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for column in (0,1,5,6):self.comparison.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        for column in (2,3,4):self.comparison.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeMode.Stretch)
        self.result_tabs.addTab(self.comparison,'逐项结果 / 写入预览')
        self.details=QPlainTextEdit(self);self.details.setReadOnly(True)
        self.result_tabs.addTab(self.details,'版本、模板与原始证据')
        self.result_tabs.hide()
        details_button.toggled.connect(self.result_tabs.setVisible)
        self.table.cellDoubleClicked.connect(lambda *_: details_button.setChecked(True))
        result_layout.addWidget(self.result_tabs,1)
        self.write_group=QGroupBox('写入后回读',self);write_layout=QVBoxLayout(self.write_group)
        write_note=QLabel('写入前核对上方目标、实际旧值与模板新值。人工重新上电声明与再次回读分别记录。',self)
        write_note.setWordWrap(True);write_layout.addWidget(write_note)
        write_actions=QHBoxLayout();write_actions.addStretch(1)
        self.ack_button=QPushButton('确认已重新上下电…',self);self.ack_button.setObjectName('outlineButton')
        self.verify_button=QPushButton('再次回读核验',self);self.verify_button.setObjectName('outlineButton')
        for button in (self.ack_button,self.verify_button):write_actions.addWidget(button)
        write_layout.addLayout(write_actions);result_layout.addWidget(self.write_group)
        layout.addWidget(result_panel)
        for button,callback in ((self.reference_button,self.show_reference),(self.reload_button,self.reload),
                (self.select_all_button,lambda:self.select_rows(True)),
                (self.clear_selection_button,lambda:self.select_rows(False)),
                (self.templates_button,self.refresh_templates),(self.import_button,self.import_templates),
                (self.read_reference_button,self.read_reference),
                (self.read_button,self.read_comparison),(self.preview_button,self.update_selected),
                (self.ack_button,self.acknowledge),
                (self.verify_button,lambda:self.action('readback',{'job_id':self.job['job_id']})),
                (self.cancel_button,lambda:self.action('cancel',{'job_id':self.job['job_id']}))):button.clicked.connect(callback)
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(1000)
        self.source.currentIndexChanged.connect(self.source_changed)
        self.finished.connect(self._finish_window)
        for bar in (self.details.verticalScrollBar(),self.details.horizontalScrollBar()):
            bar.sliderReleased.connect(lambda:self._set_details(self._details_pending))
        for bar in (self.comparison.verticalScrollBar(),self.comparison.horizontalScrollBar()):
            bar.sliderReleased.connect(lambda:self._render_comparison(self._display_value))
        self.controls();self.reload()

    def _finish_window(self,*args):
        self._closed=True;self._update_flow=None;self.timer.stop()

    def _selected(self):
        return [i for i in range(self.table.rowCount()) if self.table.item(i,0) and self.table.item(i,0).checkState()==Qt.CheckState.Checked]

    def select_rows(self,checked):
        self.table.blockSignals(True)
        state=Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.table.rowCount()):
            self.table.item(i,0).setCheckState(state)
        self.table.blockSignals(False)
        self.invalidate()

    def _set_details(self,text):
        self._details_pending=text
        if self.details.toPlainText()==text:return
        vertical,horizontal=self.details.verticalScrollBar(),self.details.horizontalScrollBar()
        if vertical.isSliderDown() or horizontal.isSliderDown():return
        vpos,hpos=vertical.value(),horizontal.value()
        self.details.setPlainText(text);vertical.setValue(vpos);horizontal.setValue(hpos)

    @staticmethod
    def _value_text(item,raw):
        if raw is None:return '未取得'
        shown=display(item,raw);values=shown['value']
        value=' ～ '.join(f'{v:g}' for v in values)
        return value+' '+shown['unit']

    def _render_comparison(self,value):
        self._display_value=value
        vertical,horizontal=self.comparison.verticalScrollBar(),self.comparison.horizontalScrollBar()
        if vertical.isSliderDown() or horizontal.isSliderDown():return
        reference=value.get('mode')=='read_reference'
        if self.comparison.isColumnHidden(4)!=reference:
            for column in (4,6):self.comparison.setColumnHidden(column,reference)
            self.comparison.horizontalHeaderItem(2).setText('实测值' if reference else '读取 / 旧值')
            self.comparison.horizontalHeaderItem(3).setText('109 脚本参考值' if reference else '模板新值')
        flat=[(target,item) for target in value.get('targets',[]) for item in target.get('items',[])]
        vpos,hpos=vertical.value(),horizontal.value()
        if self.comparison.rowCount()!=len(flat):self.comparison.setRowCount(len(flat))
        labels={'passed':'一致','mismatch':'存在差异','timeout':'无响应','association_unknown':'归属未核验',
                'not_run':'未执行','unknown':'未核验','pending':'待执行','writing':'正在写入','reading':'正在读取',
                'failed':'失败','not_written':'未写入','written_verified':'即时回读通过',
                'written_unverified':'已写入，未核验','send_uncertain_unverified':'发送结果不确定',
                **REFERENCE_ITEM_STATES}
        for i,(target,item) in enumerate(flat):
            actual=item.get('actual') or {}
            old=item.get('old_raw',actual.get('raw'))
            old_text=self._value_text(item,old) if 'old_raw' in item or actual.get('status')=='read' else ('未核验响应' if old is not None else '未取得')
            back=item.get('post_power') or item.get('immediate') or {}
            back_text=self._value_text(item,back.get('raw')) if back.get('status')=='read' else ('未核验响应' if back.get('raw') is not None else '尚未回读')
            expected=item.get('expected_raw')
            expected_text='—' if reference and expected is None else self._value_text(item,expected)
            write_status=item.get('write_status','')
            cells=[f"{target.get('interface','')} ID {target.get('motor_id','')}",item['key'],old_text,
                   expected_text,back_text,labels.get(item['status'],item['status']),
                   labels.get(write_status,write_status)]
            for col,text in enumerate(cells):
                existing=self.comparison.item(i,col)
                if existing is None or existing.text()!=text:self.comparison.setItem(i,col,QTableWidgetItem(text))
        vertical.setValue(vpos);horizontal.setValue(hpos)

    def show_reference(self):
        if self.reference_dialog is not None and isValid(self.reference_dialog):
            self.reference_dialog.show();self.reference_dialog.raise_();return
        text=reference_text()
        dialog=QDialog(self);dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle('Pn参考资料（适用性待核实）');dialog.resize(900,650)
        layout=QVBoxLayout(dialog);details=QPlainTextEdit(dialog);details.setReadOnly(True)
        details.setPlainText(text);layout.addWidget(details)
        close=QPushButton('关闭',dialog);close.clicked.connect(dialog.close);layout.addWidget(close)
        self.finished.connect(dialog.close)
        self.reference_dialog=dialog;dialog.show()

    def invalidate(self,*args):
        self.preview_value=None;self._selection_revision+=1;self._read_revision=None
        if not self._loading_templates:self._update_flow=None
        self._update_error=''
        self.render_motor_comparison(self.job)
        self.controls()

    def _job_targets(self, value):
        if value.get('configuration_sha256') not in (None, self.configuration_sha256):
            return {}
        return {motor_target_key(row): row for row in value.get('targets', [])
                if all(key in row for key in ('index', 'interface', 'motor_id', 'model'))}

    def _template_matches(self, index, target):
        expected = (target.get('template_record') or {}).get('template') or {}
        selected = self.table.cellWidget(index, 5).currentData()
        return bool(selected and selected == expected.get('template_id'))

    def update_selections(self):
        job = self.job
        if (job.get('mode') != 'read' or job.get('active') or not self._comparison_read_job_id
                or job.get('job_id') != self._comparison_read_job_id
                or job.get('status') not in {'checked', 'checked_with_differences'} or job.get('transport_error')):
            return []
        targets = self._job_targets(job)
        selections = []
        for index in self._selected():
            target = targets.get(motor_target_key(self.rows[index]))
            if not target or not self._template_matches(index, target):
                continue
            items = target.get('items', [])
            if (any(item.get('status') == 'mismatch' for item in items)
                    and all((item.get('actual') or {}).get('status') == 'read' for item in items)):
                selections.append({'index': target['index'], 'template_id': self.table.cellWidget(index, 5).currentData()})
        return selections

    def render_motor_comparison(self, value):
        targets = self._job_targets(value)
        mode = value.get('mode')
        self.table.blockSignals(True)
        for index, row in enumerate(self.rows):
            target = targets.get(motor_target_key(row))
            items = target.get('items', []) if target else []
            actual, expected = [], []
            if mode == 'read' and target and not self._template_matches(index, target):
                status = '模板已变化 · 请重新比较'
            elif not items:
                combo = self.table.cellWidget(index, 5)
                status = '待比较' if combo.currentData() else '无适用模板'
                for item in combo.currentData(TEMPLATE_ENTRIES_ROLE) or []:
                    expected.append(f"{item['key']}  {self._value_text(item, item['expected_raw'])}")
            else:
                for item in items:
                    value_now = item.get('post_power') or item.get('immediate') or item.get('actual') or {}
                    shown = self._value_text(item, value_now.get('raw')) if value_now.get('status') == 'read' else '未核验'
                    actual.append(f"{item['key']}  {shown}")
                    expected.append(f"{item['key']}  {self._value_text(item, item.get('expected_raw')) if item.get('expected_raw') is not None else '—'}")
                if mode == 'write':
                    status = '回读核验通过' if target.get('post_power_verified') else {
                        'awaiting_power': '待重新上下电及回读', 'failed': '更新失败',
                        'writing': '正在更新', 'interrupted': '已中断',
                    }.get(target.get('status'), '待核验')
                elif mode == 'read_reference':
                    differences = sum(item.get('status') == 'reference_difference' for item in items)
                    status = f'参考有差异 {differences} 项' if differences else '仅供参考'
                else:
                    differences = sum(item.get('status') == 'mismatch' for item in items)
                    unknown = sum(item.get('status') not in {'passed', 'mismatch'} for item in items)
                    status = (f'有差异 {differences} 项，未核验 {unknown} 项' if differences and unknown
                              else f'未核验 {unknown} 项' if unknown
                              else f'有差异 {differences} 项' if differences else '符合')
            for column, lines in ((8, actual), (9, expected), (10, [status])):
                text = '\n'.join(lines[:2]) or '—'
                if len(lines) > 2:
                    text += f'\n共 {len(lines)} 项，双击查看'
                cell = QTableWidgetItem(text)
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                cell.setToolTip('\n'.join(lines))
                if column == 10 and (status == '符合' or status == '回读核验通过'):
                    cell.setForeground(QColor('#126747'))
                    cell.setBackground(QColor('#EAF7F0'))
                elif column == 10 and '差异' in status:
                    cell.setForeground(QColor('#99510D'))
                    cell.setBackground(QColor('#FFF2DD'))
                self.table.setItem(index, column, cell)
        self.table.blockSignals(False)

    def controls(self):
        local=self.is_local()
        online=not local and bool(self.identity())
        active=bool(self.job.get('active'))
        write_authorized=self.local_write_authorized if local else online
        preparing = self._update_flow is not None
        idle=not self.pending and not active and not preparing and (local or online)
        self.source.setEnabled(not self.pending and not preparing and not (active and self.job.get('job_id')))
        selected=self._selected()
        missing=sum(not self.table.cellWidget(i,5).currentData() for i in selected)
        unknown=sum('firmware_generation' in self.rows[i] and self.rows[i]['firmware_generation'] not in (108,109)
                    for i in selected)
        local_legacy = sum(self._locally_confirmed_108(self.rows[i].get('observation') or {}) for i in selected)
        self.reload_button.setEnabled(idle)
        self.select_all_button.setEnabled(idle and len(selected)<self.table.rowCount())
        self.clear_selection_button.setEnabled(idle and bool(selected))
        self.templates_button.setEnabled(idle and write_authorized and bool(selected))
        self.import_button.setVisible(local)
        self.import_button.setEnabled(idle)
        self.read_reference_button.setEnabled(idle and bool(selected))
        self.read_button.setEnabled(idle and write_authorized and bool(selected))
        read_current=self._read_revision==self._selection_revision and self._read_revision is not None
        self.preview_button.setEnabled(idle and write_authorized and bool(selected))
        self.preview_button.setToolTip('自动核对版本、按型号匹配参数并读取差异，再确认写入。')
        awaiting=self.job.get('status')=='awaiting_power' and not self.pending
        partial=not active and self.job.get('mode')=='write' and any(r.get('power_cycle_required') and not r.get('human_acknowledged_at') for r in self.job.get('targets',[]))
        self.ack_button.setEnabled(write_authorized and (awaiting or partial) and not self.pending)
        self.verify_button.setEnabled(write_authorized and awaiting and any(r.get('human_acknowledged_at') for r in self.job.get('targets',[]) if r['status']=='awaiting_power'))
        self.cancel_button.setEnabled(active and bool(self.job.get('job_id')) and not self.pending)
        self.table.setEnabled(idle)
        self.target_count.setText(f'{self.table.rowCount()} 个目标 · 已选 {len(selected)} 个')
        self.write_group.setVisible(self.job.get('mode')=='write')
        self.ack_button.setVisible(self.job.get('mode')=='write')
        self.verify_button.setVisible(self.job.get('mode')=='write')
        self.cancel_button.setVisible(active)
        if self.pending:hint='正在'+self._request_label+'，请等待结果。'
        elif not local and not online:hint='登录后可查询平台模板，也可切换到本机。'
        elif awaiting:hint='写入后等待人员重新上下电；人工确认后，再点击“再次回读核验”。'
        elif active and self.job.get('status')=='other_account':hint='当前工位已有另一个维护会话的任务。'
        elif active and self.job.get('mode')=='read_reference':hint='正在只读检查，可取消尚未执行的读取。'
        elif active:hint='参数任务正在进行；可取消后续操作，已经发送的写入不能撤回。'
        elif preparing:hint='正在自动准备所选参数；匹配模板、读取比较和生成清单，无需另点其他按钮。'
        elif self._update_error:hint=self._update_error
        elif self.preview_value is not None:hint='已生成更新清单，请核对实际值与目标值。'
        elif partial:hint='存在部分写入或未核验项。请按实际情况重新上下电并确认，再独立读取核对。'
        elif not self.table.rowCount():hint='尚无有效目标。请先在主界面扫描电机，再刷新目标。'
        elif not selected:hint='勾选需要读取和比较的目标。'
        elif unknown:hint=f'已选 {unknown} 台版本需刷新；点击读取或更新时自动重新扫描并读取版本。'
        elif self.job.get('mode')=='read_reference':hint='当前为参考读取。需要写入时，直接点击“更新所选”，程序自动匹配产线参数并重新比较。'
        elif not write_authorized:hint='本地可只读检查。读取并比较及更新所选需要本机已授权的签名模板。'
        elif missing and local and local_legacy:hint=f'{local_legacy} 台 108 已核实。点击“更新所选”，自动按型号匹配本机产线基础参数。'
        elif missing and local:hint='直接点击“更新所选”，自动按型号匹配本机产线参数；无需登录或导入模板。'
        elif missing:hint=f'已选 {len(selected)} 台。点击“更新所选”，自动查询适用模板并比较；存在多个模板时提示选择。'
        elif self.update_selections():hint='勾选有差异的电机后，点击“更新所选”。'
        elif self.job.get('mode')=='read' and not read_current:hint='新增目标或更换模板后，请重新读取比较。'
        else:hint='直接点击“更新所选”准备写入；“读取并比较”可单独查看差异。'
        self.step_hint.setText(hint)

    def is_local(self):
        return self.source.currentData() == 'local'

    def request_context(self):
        return (True, None) if self.is_local() else (False, self.identity())

    def request(self,label,operation,success,*,after_success=None,local=False):
        if self.pending or self._closed or (not local and not self.identity()):return
        self.pending=True;self._request_label=label;context=self.request_context();self.controls()
        received_for_account = False
        update_flow = self._update_flow
        def received(value):
            nonlocal received_for_account
            if isValid(self) and not self._closed and self.request_context()==context:
                success(value.get('result',value))
                received_for_account = True
        def finished():
            if isValid(self) and not self._closed:
                self.pending=False
                if not received_for_account and update_flow is not None and self._update_flow is update_flow:
                    self._stop_update('本次准备未完成，未自动写入；请处理上方错误后重试。')
                self.controls()
                if received_for_account and after_success and self.request_context()==context and self.isVisible():
                    after_success()
        self.run_rest(label,operation,received,on_finished=finished)

    def request_status(self, label, success, *, after_success=None):
        local = self.is_local()
        operation = (lambda: self.client.motor_parameter_status(local=True)) if local else self.client.motor_parameter_status
        self.request(label, operation, success, local=local, after_success=after_success)

    def request_action(self, label, name, payload, success, *, after_success=None):
        local = self.is_local()
        def operation():
            if local:
                return self.client.motor_parameter_action(name, payload, local=True)
            return self.client.motor_parameter_action(name, payload)
        self.request(label, operation, success, after_success=after_success, local=local)

    def reload(self):
        self._comparison_read_job_id = None
        selected={motor_target_key(self.rows[i]) for i in self._selected()} if self.rows else None
        if selected is None and self.selected_motor is not None:
            selected={self.selected_motor}
        def received(value):
            self.rows=value['targets']
            self.configuration_sha256=value.get('configuration_sha256')
            self.local_write_authorized=self.is_local() and value.get('write_authorized') is True
            self.table.blockSignals(True);self.table.setRowCount(len(self.rows))
            for i,row in enumerate(self.rows):
                checkbox=QTableWidgetItem()
                checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
                checked=selected is None or motor_target_key(row) in selected
                checkbox.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.table.setItem(i,0,checkbox)
                observation=row.get('observation') or {}
                actual=self._actual_version_label(observation)
                for col,text in ((1,row['module']+' / '+row['joint_name']),(2,row['interface']),(3,str(row['motor_id'])),(4,row['model']),(6,str(row['index'])),(7,actual)):
                    item=QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
                    if col==7 and 'firmware_generation' in row and row['firmware_generation'] not in (108,109):
                        item.setToolTip('缺少完整的 108/109 实际版本')
                    self.table.setItem(i,col,item)
                combo=QComboBox(self.table);combo.setPlaceholderText('先获取适用模板');combo.currentIndexChanged.connect(self.invalidate);self.table.setCellWidget(i,5,combo)
            self.table.blockSignals(False);self.invalidate();self.show_job(value['job'])
        self.request_status('读取参数目标', received)

    def import_templates(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入签名参数模板', '', '签名目录 (*.json)')
        if path:
            self.request('导入签名参数模板', lambda: self.client.motor_parameter_import(path),
                         lambda _: self.invalidate(), after_success=self.reload, local=True)

    def templates(self, *, after_success=None, only_missing=False):
        selected=[(i,self.rows[i]) for i in self._selected()
                  if not only_missing or not self.table.cellWidget(i, 5).currentData()]
        if not selected:return
        local=self.is_local()
        def operation():
            catalogs={}
            for _,row in selected:
                index=row['index']
                try:
                    value=(self.client.motor_parameter_templates(index,local=True) if local
                           else self.client.motor_parameter_templates(index))
                    catalogs[index]=(value.get('result',value)['templates'],None)
                except GatewayError as exc:
                    catalogs[index]=([],str(exc.detail or exc))
            return {'catalogs':catalogs}
        def received(value):
            failures=[]
            for i,target in selected:
                records,error=value['catalogs'][target['index']]
                combo=self.table.cellWidget(i,5);previous=combo.currentData();combo.blockSignals(True);combo.clear();combo.setToolTip(error or '')
                for row in records:
                    t=row['template'];profile=' · 脚本兼容Pn' if t.get('protocol_profile')==SCRIPT_PROFILE else ''
                    label=(f"产线参数 · {t['model']} · {'109全量' if t.get('protocol_profile') else '108基础参数'}"
                           if row.get('execution_source')=='bundled-production-script' else f"{t['version']} · {t['model']} · {row['sha256'][:12]}{profile}")
                    combo.addItem(label,t['template_id'])
                    combo.setItemData(combo.count()-1,self.template_details(t),Qt.ItemDataRole.ToolTipRole)
                    combo.setItemData(combo.count()-1,t.get('entries',[]),TEMPLATE_ENTRIES_ROLE)
                if previous and combo.findData(previous)>=0:combo.setCurrentIndex(combo.findData(previous))
                elif records and records[0].get('execution_source')=='bundled-production-script':combo.setCurrentIndex(0)
                elif combo.count()==1:combo.setCurrentIndex(0)
                elif combo.count()>1:
                    combo.setPlaceholderText('选择适用模板')
                    combo.setCurrentIndex(-1)
                elif error:
                    combo.setPlaceholderText('模板查询失败')
                    failures.append(f"{target['interface']} ID {target['motor_id']}：{error}")
                else:combo.setPlaceholderText('无适用的本机产线参数或已导入模板' if local else '暂无已发布适用模板')
                combo.blockSignals(False)
            source=('按电机型号自动匹配内置产线参数；无需登录或联网。' if local
                    else '仅列出管理员已确认完整版本适用性的模板。')
            details=source+'\n108使用脚本的8项基础参数；升级109后使用该型号的完整参数。\n内置产线参数按原脚本读写和比较，写后统一重新上电核验。'
            self._set_details('\n'.join([details,*failures]))
            self._loading_templates=True
            try:self.invalidate()
            finally:self._loading_templates=False
        self.request('查询参数模板',operation,received,after_success=after_success,local=local)

    def refresh_templates(self):
        if self.templates_button.isEnabled():self._prepare_operation('templates')

    def read_comparison(self):
        if self.read_button.isEnabled():self._prepare_operation('compare')

    def read(self, *, for_update=False):
        selections=[{'index':self.rows[i]['index'],'template_id':self.table.cellWidget(i,5).currentData()} for i in self._selected()]
        if not selections or any(not row['template_id'] for row in selections):self.controls();return
        self.preview_value=None;self._read_revision=None;self._comparison_read_job_id=None
        revision=self._selection_revision
        def received(value):
            if revision!=self._selection_revision:return
            self._read_revision=revision;self._comparison_read_job_id=value.get('job_id')
            if for_update and self._flow_current():
                self._update_flow.update(stage='reading',read_job_id=value.get('job_id'))
            self.show_job(value)
        payload={'selections':selections}
        if self.configuration_sha256:payload['configuration_sha256']=self.configuration_sha256
        self.request_action('读取参数并比较','read',payload,received,
                            after_success=self._continue_update if for_update else self._finish_read_preparation)
    def read_reference(self):
        if self.read_reference_button.isEnabled():self._prepare_operation('reference')

    def _read_reference(self):
        if not self._flow_current():return
        self.preview_value=None;self._read_revision=None;self._comparison_read_job_id=None
        payload={'indices':[self.rows[i]['index'] for i in self._selected()]}
        if self.configuration_sha256:payload['configuration_sha256']=self.configuration_sha256
        self.request_action('只读检查电机参数','read-reference',payload,self.show_job,
                            after_success=self._finish_read_preparation)
    def _stop_update(self, message):
        self._update_flow=None;self.preview_value=None;self._update_error=message
        self.result_status.setText(message);self.controls()

    def _flow_current(self):
        flow=self._update_flow
        if not flow or self._closed:return False
        keys=tuple(motor_target_key(self.rows[i]) for i in self._selected())
        if (flow['context']!=self.request_context() or flow['keys']!=keys
                or flow['configuration']!=self.configuration_sha256):
            self._stop_update('账号、来源或所选电机已变化，已停止自动准备；请重新操作。')
            return False
        return True

    def update_selected(self):
        if self.preview_button.isEnabled():self._prepare_operation('update')

    def _prepare_operation(self, intent):
        if self.pending or self._update_flow is not None or self.job.get('active') or not self._selected():return
        self.preview_value=None;self._update_error='';self._comparison_read_job_id=None
        self._update_flow=dict(context=self.request_context(),configuration=self.configuration_sha256,
                              keys=tuple(motor_target_key(self.rows[i]) for i in self._selected()),
                              stage='versions',deadline=time.monotonic()+45,version_request=None,intent=intent)
        # Parameter reads close the scanning backend to own the CAN transport.
        # Every new read/template action must reacquire a fresh version snapshot,
        # just like update, rather than reuse that disconnected cache.
        def accepted(receipt):
            if not self._flow_current():return
            if not receipt.get('request_id') or receipt.get('state') not in {'queued','reading','complete'}:
                self._stop_update('版本读取未受理，请确认 CAN 已连接并已扫描电机。');return
            self._update_flow['version_request']=receipt
        self.request('核对所选电机版本',self.client.refresh_motor_versions,accepted,
                     local=True,after_success=self._poll_update_versions)

    def _poll_update_versions(self):
        if self.pending or not self._flow_current():return
        flow=self._update_flow
        if time.monotonic()>=flow['deadline']:
            self._stop_update('等待版本读取超时，未写入参数；请检查连接后重试。');return
        current=flow.get('version_request')
        if not current:return
        if current['state']=='complete':
            flow['stage']='targets'
            def received(value):
                if not self._flow_current():return
                by_key={motor_target_key(row):row for row in value['targets']}
                if value.get('configuration_sha256')!=flow['configuration'] or any(key not in by_key for key in flow['keys']):
                    self._stop_update('有效配置或电机身份已变化，请刷新目标后重新选择。');return
                for i in self._selected():self.rows[i]=by_key[motor_target_key(self.rows[i])]
                self.local_write_authorized=self.is_local() and value.get('write_authorized') is True
                if value['job'].get('active'):
                    self._stop_update('工位已有参数任务，请等其结束后再操作。');self.show_job(value['job']);return
                if self.is_local() and not self.local_write_authorized:
                    self._stop_update('当前窗口没有本机维护权限，请从当前工位窗口重新打开。');return
            self.request_status('核对参数目标',received,after_success=self._dispatch_prepared_operation)
            return
        def received(receipt):
            if not self._flow_current():return
            if receipt.get('request_id')!=current['request_id'] or receipt.get('state') not in {'queued','reading','complete'}:
                self._stop_update('版本读取已中断或工位变化，未写入参数。');return
            flow['version_request']=receipt
        self.request('读取版本进度',self.client.motor_version_refresh_status,received,local=True)

    def _dispatch_prepared_operation(self):
        if not self._flow_current():return
        missing=[self.rows[i] for i in self._selected()
                 if (self.rows[i].get('observation') or {}).get('status')!='known'
                 or self.rows[i].get('firmware_generation') not in (108,109)]
        if missing:
            names='、'.join(f"{r['interface']} ID {r['motor_id']}" for r in missing)
            self._stop_update(f'{names} 本次版本未读全；未读取参数或写入，请检查该电机的版本读取详情。');return
        if self._update_flow['intent']=='reference':
            self._update_flow['stage']='reference'
            self._read_reference()
        else:self._match_update_templates()

    def _finish_read_preparation(self):
        if self._flow_current():
            self._update_flow=None;self._update_error='';self.controls()

    def _match_update_templates(self):
        if not self._flow_current():return
        self._update_flow['stage']='templates'
        self.templates(after_success=self._read_update_parameters)

    def _read_update_parameters(self):
        if not self._flow_current():return
        missing=[i for i in self._selected() if not self.table.cellWidget(i,5).currentData()]
        if missing:
            targets='、'.join(f"{self.rows[i]['interface']} ID {self.rows[i]['motor_id']}" for i in missing)
            self._stop_update(f'{targets} 没有唯一适用模板，请查看模板列的原因或选择模板后重试。');return
        intent=self._update_flow['intent']
        if intent=='templates':
            self._stop_update('已按本次实际版本匹配所选电机模板，可读取比较或更新。');return
        self._update_flow['stage']='reading'
        self.read(for_update=intent=='update')

    def _continue_update(self):
        if self.pending or not self._flow_current() or self._update_flow['stage']!='reading':return
        job=self.job
        if job.get('job_id')!=self._update_flow.get('read_job_id'):
            self._stop_update('读取任务已变化，未沿用旧结果写入。');return
        if job.get('active'):return
        if job.get('status') not in {'checked','checked_with_differences'} or job.get('transport_error'):
            self._stop_update('参数读取未完整完成，未写入；请查看逐项结果。');return
        targets=self._job_targets(job)
        for index in self._selected():
            row=targets.get(motor_target_key(self.rows[index]))
            if (not row or not self._template_matches(index,row) or not row.get('items')
                    or any(item.get('status') not in {'passed','mismatch'}
                           or (item.get('actual') or {}).get('status')!='read' for item in row['items'])):
                self._stop_update('所选电机存在未读全或未匹配的参数，未写入；请查看逐项结果。');return
        selections=self.update_selections()
        if not selections:
            self._stop_update('所选电机参数均符合目标，无需更新。');return
        self._update_flow['stage']='preview'
        def received(value):
            if not self._flow_current():return
            self.preview_value=value;self._set_details('尚未写入；请核对实际旧值与型号对应的产线参数。\n'+self.describe(value))
            self._render_comparison(value);self.result_status.setText('写入清单已就绪 · 尚未写入')
        self.request_action('准备所选参数更新','preview',
                            {'read_job_id':job['job_id'],'selections':selections},received,after_success=self.start)
    def start(self):
        preview = self.preview_value
        if not preview or self.pending or not (self.local_write_authorized if self.is_local() else self.identity()):return
        if self._update_flow is not None and not self._flow_current():return
        context = self.request_context()
        dialog = QMessageBox(QMessageBox.Icon.Question, '确认参数写入',
                             '确认设备已停测并失能？所选电机将逐项写入和回读，失败可能留下部分写入。',
                             QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, self)
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.setInformativeText('\n'.join(f"{row['interface']} ID {row['motor_id']}  {row['joint_name']} · {len(row['items'])} 项"
                                            for row in preview.get('targets', [])))
        dialog.setDetailedText(self.describe(preview))
        if dialog.exec()!=QMessageBox.StandardButton.Yes or self.preview_value is not preview or self.request_context()!=context:
            self._stop_update('已取消本次参数更新，未发送写入。');return
        if self._update_flow is not None and not self._flow_current():return
        self._update_flow=None;self.preview_value=None
        self.action('start',{'preview_id':preview['preview_id'],'confirmed':True})
    def acknowledge(self):
        if QMessageBox.question(self,'请重新上下电','请重新上下电。已由人员完成了吗？确认只记录人工声明，不是供电检测或参数核验。')==QMessageBox.StandardButton.Yes:self.action('acknowledge',{'job_id':self.job['job_id'],'confirmed':True})
    def action(self,name,payload):self.request_action('模块参数维护',name,payload,self.show_job)

    def source_changed(self):
        self._update_flow=None;self._update_error=''
        self._comparison_read_job_id=None
        self.job={};self.preview_value=None;self.rows=[];self.configuration_sha256=None
        self.table.setRowCount(0);self.details.clear();self._details_pending='';self.comparison.setRowCount(0)
        self._display_value={};self._read_revision=None;self.local_write_authorized=False
        self.controls()
        self.reload()

    def poll(self):
        if self.identity()!=self.account:
            if self.pending and not self.is_local():return
            self.account=self.identity()
            if not self.is_local():
                if self.account:
                    self.source_changed()
                else:
                    self.source.setCurrentIndex(0)
        if self._update_flow and self._update_flow['stage']=='versions':
            self._poll_update_versions();return
        if self.job.get('active'):
            self.request_status('读取参数任务进度',lambda v:self.show_job(v['job']),after_success=self._continue_update)
    @staticmethod
    def template_details(template):
        applicability='；'.join(' / '.join(f"{name}={row.get(name,'未知')}" for name in ('raw_firmware','software','hardware','protocol')) for row in template.get('applicability',[])) or '待核实'
        profile=('产线脚本逐项读写；普通值按原脚本容差比较，Pn按整数比较' if template.get('source','').startswith('bundled:')
                 else SCRIPT_ASSOCIATION if template.get('protocol_profile')==SCRIPT_PROFILE else '普通参数配置')
        return f"模板型号 {template['model']}；适用完整版本：{applicability}\n协议配置：{template.get('protocol_profile','未声明')}；{profile}\n来源 {template['source']}；来源SHA256 {template['source_sha256']}"
    @staticmethod
    def raw32(raw):
        if not isinstance(raw,list) or len(raw)!=1 or type(raw[0]) is not int or not 0<=raw[0]<=0xffffffff:return str(raw)
        return f"{raw[0]} / 0x{raw[0]:08X} / {raw[0].to_bytes(4,'big').hex(' ').upper()}"
    @staticmethod
    def response_details(title,result):
        association=result.get('association_method')
        if result.get('protocol_profile')==SCRIPT_PROFILE and association=='single_flight_time_window' and result.get('parameter_number_echoed') is False:
            method=SCRIPT_ASSOCIATION
        else:
            method='响应关联：'+str(association or '未核验')
        status=result.get('status','未知')
        trust='读取返回' if status=='read' else '未核验，不能作为成功证据'
        return f"{title}证据：{trust}（{status}）；{method}；请求ID {result.get('request_id','未记录')}；时间 {result.get('observed_at','未知')}；错误 {result.get('error') or '无'}；原始报文 {result.get('response_hex') or result.get('candump') or '未记录'}"
    @staticmethod
    def _locally_confirmed_108(observation):
        return (observation.get('confirmed_firmware_generation') == 108
                and observation.get('firmware_confirmation_source') == 'local_numeric_evidence')

    @staticmethod
    def _actual_version_label(observation):
        raw = observation.get('raw_firmware')
        if raw:
            return raw
        if MotorParametersDialog._locally_confirmed_108(observation):
            return f"108（本机核实，数值 {observation.get('software') or '未取得'}）"
        return observation.get('software') or '未取得'

    @staticmethod
    def describe_reference_job(value):
        status=value.get('status','未知')
        lines=['任务状态：'+REFERENCE_JOB_STATES.get(status,status),
               '108 项记录实测值。109 项与脚本参考的差异仅供检查。']
        if value.get('error'):lines.append('停止原因：'+value['error'])
        if value.get('cleanup_quarantined'):lines.append('收尾失败，资源保持隔离：'+value.get('cleanup_error',''))
        for row in value.get('targets',[]):
            version=row.get('before') or {}
            lines.extend(['',f"{row['module']} / {row['joint_name']} · {row['interface']} ID {row['motor_id']} · {row['model']}",
                          f"实际版本 {MotorParametersDialog._actual_version_label(version)}；软件 {version.get('software') or '未知'}；硬件 {version.get('hardware') or '未知'}；协议 {version.get('protocol') or '未知'}"])
            if row.get('reference_source'):
                lines.append(f"109 脚本参考来源 {row['reference_source']} · SHA256 {row.get('reference_sha256') or '未知'}")
            for item in row.get('items',[]):
                actual=item.get('actual') or {}
                status=item.get('status','unknown')
                expected=item.get('expected_raw')
                actual_text=MotorParametersDialog._value_text(item,actual.get('raw')) if actual.get('status')=='read' else '未取得核验值'
                comparison=f"；脚本参考 {MotorParametersDialog._value_text(item,expected)}" if expected is not None else ''
                lines.append(f"{item['key']}：实测 {actual_text}{comparison}；{REFERENCE_ITEM_STATES.get(status,status)}")
                if actual:lines.append(MotorParametersDialog.response_details('独立读取',actual))
        return '\n'.join(lines)
    @staticmethod
    def describe(value):
        if value.get('mode')=='read_reference':
            return MotorParametersDialog.describe_reference_job(value)
        labels={'passed':'比较通过','mismatch':'不匹配','timeout':'无响应','association_unknown':'响应归属未核验','not_run':'未执行','unknown':'未知','pending':'待执行','writing':'正在写入','reading':'正在读取','failed':'失败','interrupted':'中断','verified':'再次回读通过','checked':'读取比较结束','checked_with_differences':'读取结束，存在不符或未核验项','awaiting_power':'等待人工重新上下电','written_unverified':'已写入待核验','written_awaiting_batch_power':'已写入，等待全批完成'}
        lines=['任务状态：'+labels.get(value.get('status'),str(value.get('status','预览')))]
        if value.get('error'):lines.append('停止原因：'+value['error'])
        if value.get('cleanup_quarantined'):lines.append('收尾失败，资源保持隔离：'+value.get('cleanup_error',''))
        for row in value.get('targets',[]):
            record=row['template_record'];t=record['template']
            before = row['before']
            lines.extend(['',f"{row['module']} / {row['joint_name']} · {row['interface']} ID {row['motor_id']} · {row['model']}",f"实际版本 {MotorParametersDialog._actual_version_label(before)}；软件 {before.get('software','未知')}；硬件 {before.get('hardware')}；协议 {before.get('protocol')}",f"模板 {t['version']} · SHA256 {record['sha256']}；发布者 {record['published_by']}",f"来源 {t['source']} · 来源SHA256 {t['source_sha256']}"])
            lines.append(MotorParametersDialog.template_details(t))
            for item in row['items']:
                actual=item.get('old_raw',(item.get('actual') or {}).get('raw'));d=display(item,actual);new=display(item,item['expected_raw'])['value']
                old_label='实际旧值' if 'old_raw' in item or (item.get('actual') or {}).get('status')=='read' else '未核验响应原始值'
                lines.append(f"{item['key']}：{old_label} {d['value']} → 模板新值 {new} {d['unit']}；{item.get('comparison_rule', '容差 ±'+str(d['tolerance']))}；{d['meaning']}；{labels.get(item['status'],item['status'])}；写入 {item['write_status']}")
                if item['key'].startswith('pn_'):
                    lines.append('Pn 32位原始整数（十进制 / 十六进制 / 大端4字节；不套用串口倍率）：'+old_label+' '+MotorParametersDialog.raw32(actual)+' → 模板 '+MotorParametersDialog.raw32(item['expected_raw']))
                if item.get('reference'):
                    if t.get('protocol_profile')==SCRIPT_PROFILE:lines.append('以下为USB/串口独立参考，不替换已发布CAN脚本模板，也不用于发送值换算。')
                    lines.append(describe_reference(item['reference']))
                if item.get('actual'):lines.append(MotorParametersDialog.response_details('独立读取',item['actual']))
                for key,title in (('immediate','即时回读'),('post_power','再次回读')):
                    if item.get(key):
                        lines.append(title+'：'+str(display(item,item[key].get('raw'))['value'])+'；'+item[key].get('status','未知'))
                        if item['key'].startswith('pn_'):lines.append(title+' Pn原始整数：'+MotorParametersDialog.raw32(item[key].get('raw')))
                        lines.append(MotorParametersDialog.response_details(title,item[key]))
            lines.append(f"人工重新上下电声明：{row.get('human_acknowledged_at') or '未确认'}；再次回读核验：{'通过' if row.get('post_power_verified') else '未通过'}")
        if value.get('mode')=='write' and not value.get('active') and value.get('status')!='verified':
            lines.append('部分写入或核验失败：先人工重新上下电并记录声明，再通过独立读取入口重新核对实际值；不会自动恢复或重放写入。')
        return '\n'.join(lines)
    def show_job(self,job):
        self.job=job
        self.render_motor_comparison(job)
        if job.get('job_id'):
            self._set_details(self.describe(job));self._render_comparison(job)
            names={'checked':'读取结束','checked_with_differences':'存在差异或未核验项','reading':'正在读取','writing':'正在写入','awaiting_power':'等待重新上下电','verified':'再次回读通过','failed':'任务失败','interrupted':'已中断',**REFERENCE_JOB_STATES}
            self.result_status.setText(names.get(job.get('status'),str(job.get('status','未知'))))
            for row in job['targets']:
                key=(job['job_id'],row['index'])
                if row.get('power_cycle_required') and job.get('status') in ('awaiting_power','failed','interrupted') and key not in self.prompted:
                    self.prompted.update((job['job_id'],r['index']) for r in job['targets'] if r.get('power_cycle_required'));QMessageBox.information(self,'请重新上下电','请重新上下电。由人员操作；人工确认与再次参数回读分别记录。部分写入或未核验不等于成功。')
        self.controls()
