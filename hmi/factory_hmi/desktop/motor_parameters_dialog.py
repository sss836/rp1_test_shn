"""Template-only parameter maintenance; expected values are never editable here."""
from shiboken6 import isValid
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QLabel,QTableWidget,QTableWidgetItem,
                              QComboBox,QPushButton,QPlainTextEdit,QMessageBox,QHeaderView,
                              QHBoxLayout,QGroupBox,QSplitter,QWidget,QTabWidget,QAbstractItemView)
from factory_hmi.client import GatewayError
from factory_hmi.motor_parameter_contract import display
from factory_hmi.core.parameter_reference import describe_reference,reference_text
from .motor_firmware_dialog import motor_target_key

SCRIPT_PROFILE='lro-pn-script-v1'
SCRIPT_ASSOCIATION='脚本兼容：单请求时序关联，回包不含Pn编号'
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
        self._display_value={};self._details_pending=''
        self.setWindowTitle('模块参数');self.resize(1080,820)
        layout=QVBoxLayout(self)
        heading=QHBoxLayout()
        title=QLabel('选择电机后可直接只读检查，或使用模板维护参数。',self)
        heading.addWidget(title,1)
        self.reference_button=QPushButton('Pn参考资料',self)
        self.reference_button.setObjectName('outlineButton');heading.addWidget(self.reference_button)
        layout.addLayout(heading)
        note=QLabel('请先停止测试并失能。读取和比较不会写入设备；写入可能部分完成。',self)
        note.setWordWrap(True);layout.addWidget(note)
        self.step_hint=QLabel(self);self.step_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.step_hint.setWordWrap(True);layout.addWidget(self.step_hint)
        splitter=QSplitter(Qt.Orientation.Vertical,self);layout.addWidget(splitter,1)
        targets=QGroupBox('1  目标与适用模板',self);target_layout=QVBoxLayout(targets)
        toolbar=QHBoxLayout()
        self.reload_button=QPushButton('刷新目标',self)
        self.templates_button=QPushButton('获取所选电机模板',self)
        self.select_all_button=QPushButton('选择整组',self)
        self.clear_selection_button=QPushButton('清除选择',self)
        for button in (self.reload_button,self.select_all_button,self.clear_selection_button,self.templates_button):
            button.setObjectName('outlineButton');toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.target_count=QLabel('尚无目标',self);toolbar.addWidget(self.target_count)
        target_layout.addLayout(toolbar)
        self.table=QTableWidget(0,8,self)
        self.table.setHorizontalHeaderLabels(['选择','模块 / 关节','接口','ID','型号','适用模板','索引','实际固件'])
        self.table.setColumnHidden(6,True);self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        header=self.table.horizontalHeader()
        for column in (0,2,3,4,7):header.setSectionResizeMode(column,QHeaderView.ResizeMode.ResizeToContents)
        for column in (1,5):header.setSectionResizeMode(column,QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(180)
        self.table.itemChanged.connect(self.invalidate);target_layout.addWidget(self.table)
        splitter.addWidget(targets)
        result_panel=QWidget(self);result_layout=QVBoxLayout(result_panel);result_layout.setContentsMargins(0,0,0,0)
        read_actions=QHBoxLayout()
        self.read_reference_button=QPushButton('只读检查选中电机',self);self.read_reference_button.setObjectName('primaryButton')
        self.read_button=QPushButton('按模板读取并比较',self);self.read_button.setObjectName('outlineButton')
        self.preview_button=QPushButton('生成写入预览',self);self.preview_button.setObjectName('outlineButton')
        self.cancel_button=QPushButton('取消后续操作',self);self.cancel_button.setObjectName('criticalOutlineButton')
        for button in (self.read_reference_button,self.read_button,self.preview_button,self.cancel_button):
            read_actions.addWidget(button)
        read_actions.addStretch(1)
        self.result_status=QLabel('尚未读取',self);self.result_status.setWordWrap(True);read_actions.addWidget(self.result_status)
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
        result_layout.addWidget(self.result_tabs,1)
        self.write_group=QGroupBox('3  写入与回读',self);write_layout=QVBoxLayout(self.write_group)
        write_note=QLabel('写入前核对上方目标、实际旧值与模板新值。人工重新上电声明与再次回读分别记录。',self)
        write_note.setWordWrap(True);write_layout.addWidget(write_note)
        write_actions=QHBoxLayout();write_actions.addStretch(1)
        self.start_button=QPushButton('确认写入…',self);self.start_button.setObjectName('criticalOutlineButton')
        self.ack_button=QPushButton('确认已重新上下电…',self);self.ack_button.setObjectName('outlineButton')
        self.verify_button=QPushButton('再次回读核验',self);self.verify_button.setObjectName('outlineButton')
        for button in (self.start_button,self.ack_button,self.verify_button):write_actions.addWidget(button)
        write_layout.addLayout(write_actions);result_layout.addWidget(self.write_group)
        splitter.addWidget(result_panel);splitter.setStretchFactor(0,2);splitter.setStretchFactor(1,3)
        splitter.setSizes([365,335])
        for button,callback in ((self.reference_button,self.show_reference),(self.reload_button,self.reload),
                (self.select_all_button,lambda:self.select_rows(True)),
                (self.clear_selection_button,lambda:self.select_rows(False)),
                (self.templates_button,self.templates),(self.read_reference_button,self.read_reference),
                (self.read_button,self.read),(self.preview_button,self.preview),
                (self.start_button,self.start),(self.ack_button,self.acknowledge),
                (self.verify_button,lambda:self.action('readback',{'job_id':self.job['job_id']})),
                (self.cancel_button,lambda:self.action('cancel',{'job_id':self.job['job_id']}))):button.clicked.connect(callback)
        self.timer=QTimer(self);self.timer.timeout.connect(self.poll);self.timer.start(1000)
        self.finished.connect(self._finish_window)
        for bar in (self.details.verticalScrollBar(),self.details.horizontalScrollBar()):
            bar.sliderReleased.connect(lambda:self._set_details(self._details_pending))
        for bar in (self.comparison.verticalScrollBar(),self.comparison.horizontalScrollBar()):
            bar.sliderReleased.connect(lambda:self._render_comparison(self._display_value))
        self.controls();self.reload()

    def _finish_window(self,*args):
        self._closed=True;self.timer.stop()

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
        self.preview_value=None;self._selection_revision+=1;self._read_revision=None;self.controls()

    def controls(self):
        authorized=bool(self.identity());active=bool(self.job.get('active'))
        idle=not self.pending and not active and authorized
        selected=self._selected()
        missing=sum(not self.table.cellWidget(i,5).currentData() for i in selected)
        unknown=sum('firmware_generation' in self.rows[i] and self.rows[i]['firmware_generation'] not in (108,109)
                    for i in selected)
        self.reload_button.setEnabled(idle)
        self.select_all_button.setEnabled(idle and len(selected)<self.table.rowCount())
        self.clear_selection_button.setEnabled(idle and bool(selected))
        self.templates_button.setEnabled(idle and bool(selected))
        self.read_reference_button.setEnabled(idle and bool(selected) and not unknown)
        self.read_button.setEnabled(idle and bool(selected) and not missing)
        read_current=self._read_revision==self._selection_revision and self._read_revision is not None
        self.preview_button.setEnabled(idle and read_current and self.job.get('mode')=='read' and bool(self.job.get('job_id')))
        self.start_button.setEnabled(idle and self.preview_value is not None)
        awaiting=self.job.get('status')=='awaiting_power' and not self.pending
        partial=not active and self.job.get('mode')=='write' and any(r.get('power_cycle_required') and not r.get('human_acknowledged_at') for r in self.job.get('targets',[]))
        self.ack_button.setEnabled(authorized and (awaiting or partial) and not self.pending)
        self.verify_button.setEnabled(authorized and awaiting and any(r.get('human_acknowledged_at') for r in self.job.get('targets',[]) if r['status']=='awaiting_power'))
        self.cancel_button.setEnabled(authorized and active and not self.pending)
        self.table.setEnabled(idle)
        self.target_count.setText(f'{self.table.rowCount()} 个目标 · 已选 {len(selected)} 个')
        self.write_group.setVisible(self.preview_value is not None or self.job.get('mode')=='write')
        self.start_button.setVisible(self.preview_value is not None)
        self.ack_button.setVisible(self.job.get('mode')=='write')
        self.verify_button.setVisible(self.job.get('mode')=='write')
        self.cancel_button.setVisible(active)
        if not authorized:hint='请先登录平台账号。'
        elif self.pending:hint='正在'+self._request_label+'，请等待结果。'
        elif awaiting:hint='写入后等待人员重新上下电；人工确认后，再点击“再次回读核验”。'
        elif active and self.job.get('mode')=='read_reference':hint='正在只读检查，可取消尚未执行的读取。'
        elif active:hint='参数任务正在进行；可取消后续操作，已经发送的写入不能撤回。'
        elif self.preview_value is not None:hint='尚未写入。请核对下方旧值与模板新值，再点击“确认写入…”。'
        elif partial:hint='存在部分写入或未核验项。请按实际情况重新上下电并确认，再独立读取核对。'
        elif not self.table.rowCount():hint='尚无有效目标。请先在主界面扫描电机，再刷新目标。'
        elif not selected:hint='勾选需要读取和比较的目标。'
        elif unknown:hint=f'只读检查需要完整的 108/109 实际版本。已选电机中有 {unknown} 台待核实，可扫描后刷新或取消勾选。'
        elif self.job.get('mode')=='read_reference':hint='108 项记录实测值。109 项可与脚本参考比较，差异仅供检查。'
        elif missing:hint=f'已选 {len(selected)} 个目标，其中 {missing} 个尚无适用模板。可直接只读检查，或先获取模板。'
        elif self.job.get('mode')=='read' and self.job.get('job_id') and not read_current:hint='目标或模板已改变，请重新读取并比较；旧结果不能直接用于写入。'
        elif read_current and self.job.get('job_id'):hint='读取已返回。请查看逐项结果；需要写入时，先生成写入预览。'
        else:hint='目标和模板已选择。点击“读取并比较”，此步骤不会写入设备。'
        self.step_hint.setText(hint)

    def request(self,label,operation,success):
        if self.pending or self._closed or not self.identity():return
        self.pending=True;self._request_label=label;account=self.identity();self.controls()
        def received(value):
            if isValid(self) and not self._closed and self.identity()==account:success(value.get('result',value))
        def finished():
            if isValid(self) and not self._closed:self.pending=False;self.controls()
        self.run_rest(label,operation,received,on_finished=finished)
    def reload(self):
        selected={motor_target_key(self.rows[i]) for i in self._selected()} if self.rows else None
        if selected is None and self.selected_motor is not None:
            selected={self.selected_motor}
        def received(value):
            self.rows=value['targets']
            self.configuration_sha256=value.get('configuration_sha256')
            self.table.blockSignals(True);self.table.setRowCount(len(self.rows))
            for i,row in enumerate(self.rows):
                checkbox=QTableWidgetItem()
                checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
                checked=selected is None or motor_target_key(row) in selected
                checkbox.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.table.setItem(i,0,checkbox)
                observation=row.get('observation') or {}
                actual=observation.get('raw_firmware') or observation.get('software') or '未取得'
                for col,text in ((1,row['module']+' / '+row['joint_name']),(2,row['interface']),(3,str(row['motor_id'])),(4,row['model']),(6,str(row['index'])),(7,actual)):
                    item=QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
                    if col==7 and 'firmware_generation' in row and row['firmware_generation'] not in (108,109):
                        item.setToolTip('缺少完整的 108/109 实际版本')
                    self.table.setItem(i,col,item)
                combo=QComboBox(self.table);combo.setPlaceholderText('先获取适用模板');combo.currentIndexChanged.connect(self.invalidate);self.table.setCellWidget(i,5,combo)
            self.table.blockSignals(False);self.invalidate();self.show_job(value['job'])
        self.request('读取参数目标',self.client.motor_parameter_status,received)
    def templates(self):
        selected=[(i,self.rows[i]) for i in self._selected()]
        if not selected:return
        def operation():
            catalogs={}
            for _,row in selected:
                index=row['index']
                try:
                    value=self.client.motor_parameter_templates(index)
                    catalogs[index]=(value.get('result',value)['templates'],None)
                except GatewayError as exc:
                    catalogs[index]=([],str(exc.detail or exc))
            return {'catalogs':catalogs}
        def received(value):
            failures=[]
            for i,target in selected:
                records,error=value['catalogs'][target['index']]
                combo=self.table.cellWidget(i,5);combo.clear();combo.setToolTip(error or '')
                for row in records:
                    t=row['template'];profile=' · 脚本兼容Pn' if t.get('protocol_profile')==SCRIPT_PROFILE else ''
                    combo.addItem(f"{t['version']} · {t['model']} · {row['sha256'][:12]}{profile}",t['template_id'])
                    combo.setItemData(combo.count()-1,self.template_details(t),Qt.ItemDataRole.ToolTipRole)
                if combo.count():combo.setCurrentIndex(0)
                elif error:
                    combo.setPlaceholderText('模板查询失败')
                    failures.append(f"{target['interface']} ID {target['motor_id']}：{error}")
                else:combo.setPlaceholderText('暂无已发布适用模板')
            details='仅列出管理员已确认完整版本适用性的模板。无模板时保持待核实；不能使用脚本候选值直接写入。\n脚本兼容Pn按32位CAN原始整数处理；Pn参考资料的显示倍率不用于换算发送值。\n'+SCRIPT_ASSOCIATION+'。'
            self._set_details('\n'.join([details,*failures]));self.invalidate()
        self.request('查询参数模板',operation,received)
    def read(self):
        selections=[{'index':self.rows[i]['index'],'template_id':self.table.cellWidget(i,5).currentData()} for i in self._selected()]
        if not selections or any(not row['template_id'] for row in selections):self.controls();return
        self.preview_value=None;self._read_revision=None
        revision=self._selection_revision
        def received(value):
            if revision!=self._selection_revision:return
            self._read_revision=revision;self.show_job(value)
        payload={'selections':selections}
        if self.configuration_sha256:payload['configuration_sha256']=self.configuration_sha256
        self.request('读取参数并比较',lambda:self.client.motor_parameter_action('read',payload),received)
    def read_reference(self):
        if not self.read_reference_button.isEnabled():return
        self.preview_value=None;self._read_revision=None
        payload={'indices':[self.rows[i]['index'] for i in self._selected()]}
        if self.configuration_sha256:payload['configuration_sha256']=self.configuration_sha256
        self.request('只读检查电机参数',lambda:self.client.motor_parameter_action('read-reference',payload),self.show_job)
    def preview(self):
        if not self.preview_button.isEnabled():return
        def received(value):
            self.preview_value=value;self._set_details('尚未写入；请核对实际旧值、拟写新值、模板版本和目标。\n'+self.describe(value))
            self._render_comparison(value);self.result_status.setText('写入预览 · 尚未写入');self.result_tabs.setCurrentIndex(0)
        self.request('预览参数写入',lambda:self.client.motor_parameter_action('preview',{'read_job_id':self.job['job_id']}),received)
    def start(self):
        if not self.preview_value:return
        if QMessageBox.question(self,'确认参数写入','已核对实际旧值、模板新值、目标、版本及停止失能状态？逐项写入并回读，失败会保留部分结果，不能整体回滚。')!=QMessageBox.StandardButton.Yes:return
        preview=self.preview_value;self.preview_value=None;self.action('start',{'preview_id':preview['preview_id'],'confirmed':True})
    def acknowledge(self):
        if QMessageBox.question(self,'请重新上下电','请重新上下电。已由人员完成了吗？确认只记录人工声明，不是供电检测或参数核验。')==QMessageBox.StandardButton.Yes:self.action('acknowledge',{'job_id':self.job['job_id'],'confirmed':True})
    def action(self,name,payload):self.request('模块参数维护',lambda:self.client.motor_parameter_action(name,payload),self.show_job)
    def poll(self):
        if self.identity()!=self.account:
            self.account=self.identity();self.job={};self.preview_value=None;self.rows=[];self.configuration_sha256=None;self.table.setRowCount(0);self.details.clear();self._details_pending='';self.comparison.setRowCount(0);self._display_value={};self._read_revision=None;self.controls()
        if self.job.get('active'):self.request('读取参数任务进度',self.client.motor_parameter_status,lambda v:self.show_job(v['job']))
    @staticmethod
    def template_details(template):
        applicability='；'.join(' / '.join(f"{name}={row.get(name,'未知')}" for name in ('raw_firmware','software','hardware','protocol')) for row in template.get('applicability',[])) or '待核实'
        profile=SCRIPT_ASSOCIATION if template.get('protocol_profile')==SCRIPT_PROFILE else '普通参数配置'
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
        return f"{title}证据：{trust}（{status}）；{method}；请求ID {result.get('request_id','未记录')}；时间 {result.get('observed_at','未知')}；错误 {result.get('error') or '无'}；原始报文 {result.get('response_hex') or '未记录'}"
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
                          f"实际版本 {version.get('raw_firmware') or '未知'}；软件 {version.get('software') or '未知'}；硬件 {version.get('hardware') or '未知'}；协议 {version.get('protocol') or '未知'}"])
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
        labels={'passed':'比较通过','mismatch':'不匹配','timeout':'无响应','association_unknown':'响应归属未核验','not_run':'未执行','unknown':'未知','pending':'待执行','writing':'正在写入','reading':'正在读取','failed':'失败','interrupted':'中断','verified':'再次回读通过','checked':'读取比较结束','checked_with_differences':'读取结束，存在不符或未核验项','awaiting_power':'等待人工重新上下电'}
        lines=['任务状态：'+labels.get(value.get('status'),str(value.get('status','预览')))]
        if value.get('error'):lines.append('停止原因：'+value['error'])
        if value.get('cleanup_quarantined'):lines.append('收尾失败，资源保持隔离：'+value.get('cleanup_error',''))
        for row in value.get('targets',[]):
            record=row['template_record'];t=record['template']
            lines.extend(['',f"{row['module']} / {row['joint_name']} · {row['interface']} ID {row['motor_id']} · {row['model']}",f"实际版本 {row['before'].get('raw_firmware')}；软件 {row['before'].get('software','未知')}；硬件 {row['before'].get('hardware')}；协议 {row['before'].get('protocol')}",f"模板 {t['version']} · SHA256 {record['sha256']}；发布者 {record['published_by']}",f"来源 {t['source']} · 来源SHA256 {t['source_sha256']}"])
            lines.append(MotorParametersDialog.template_details(t))
            for item in row['items']:
                actual=item.get('old_raw',(item.get('actual') or {}).get('raw'));d=display(item,actual);new=display(item,item['expected_raw'])['value']
                old_label='实际旧值' if 'old_raw' in item or (item.get('actual') or {}).get('status')=='read' else '未核验响应原始值'
                lines.append(f"{item['key']}：{old_label} {d['value']} → 模板新值 {new} {d['unit']}；容差 ±{d['tolerance']}；{d['meaning']}；{labels.get(item['status'],item['status'])}；写入 {item['write_status']}")
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
        if job.get('job_id'):
            self._set_details(self.describe(job));self._render_comparison(job)
            names={'checked':'读取结束','checked_with_differences':'存在差异或未核验项','reading':'正在读取','writing':'正在写入','awaiting_power':'等待重新上下电','verified':'再次回读通过','failed':'任务失败','interrupted':'已中断',**REFERENCE_JOB_STATES}
            self.result_status.setText(names.get(job.get('status'),str(job.get('status','未知'))))
            for row in job['targets']:
                key=(job['job_id'],row['index'])
                if row.get('power_cycle_required') and row['status']!='writing' and key not in self.prompted:
                    self.prompted.add(key);QMessageBox.information(self,'请重新上下电','请重新上下电。由人员操作；人工确认与再次参数回读分别记录。部分写入或未核验不等于成功。')
        self.controls()
