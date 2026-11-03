"""Proactive, resumable evidence collection, using only existing read grants.

No direct server access to the HMI filesystem. Each local operation is a durable
request executed by the original paired desktop; approval is never evidence.
"""
from datetime import datetime, timezone
import json
import re

from app.core.errors import ApiError
from app.services import assistant_local_diagnostics as local
from app.services.assistant_attachment_queries import read_diagnostic_attachment

FAULT = re.compile(r'CSV|缺失|漏采|超时|timeout|断连|连接|报错|异常|故障|排查|日志|diagnos|error|fail',re.I)


def _waiting(card):
    data=card.get('data') or {}
    return data.get('phase')=='awaiting_permission' and data.get('status') in {'permission_required','pending','approved'}


def _summary(item,state,*,waiting=None,continuation=False):
    cards=state.get('evidence',[])
    pending=[waiting['data']['permission_request']] if waiting and waiting.get('data',{}).get('permission_request') else []
    waiting_status='waiting_for_data' if pending and pending[0].get('state')=='approved' else 'permission_required'
    issues=state.get('issues',[])
    incomplete=bool(issues or state.get('unselected_files') or any(
        c.get('data',{}).get('status') in {'partial','unavailable','denied','expired','timeout','os_denied','analysis_restart_required'}
        for c in cards if c.get('type')!='diagnostic_discovery'))
    return {'type':'diagnosis_preparation','title':'诊断已查证范围','source':'task_frozen_attachments_and_original_paired_hmi',
        'updated_at':datetime.now(timezone.utc).isoformat(),'data':{
            'status':waiting_status if waiting else 'collecting' if continuation else 'partial' if incomplete else 'ready',
            'phase':'awaiting_permission' if waiting else 'collecting' if continuation else 'evidence_prepared',
            'continuation':continuation,'pending_permissions':pending,'evidence':cards,
            'issues':issues,'selected_files':state.get('selected_files',[]),'unselected_file_count':state.get('unselected_files',0),
            'evidence_complete':not(waiting or continuation or incomplete),
            'root_cause_confirmed':False,
            'instruction':'先回答实际查证结果，再按调用链解释原因；区分原始观察、可验证原因和仍需核实的假设。不能把同一时间出现当成因果。未知时间单位/原点/时区不能直接对齐。未读范围应说明实际原因和可操作的下一步，已可读取的资料不能只让用户自行检查。权限批准不等于已读；这些证据不增加写入或设备权限。'}}


def _record(state,card):
    # Keep the most recent aggregate for a file, not every growing prefix.
    key=(card.get('attachment_id'),card.get('data',{}).get('operation'),card.get('data',{}).get('requested_path'))
    state.setdefault('evidence',[])[:]=[old for old in state.get('evidence',[]) if
        (old.get('attachment_id'),old.get('data',{}).get('operation'),old.get('data',{}).get('requested_path'))!=key]
    state['evidence'].append(card)


def prepare(item,arguments):
    if arguments: raise ApiError(422,'diagnosis_arguments','自动查证仅使用本任务冻结问题与资料，不接受模型指定其他身份。')
    state=item.setdefault('diagnosis_state',{})
    metadata=item.get('attachment_metadata',[])
    csvs=[m for m in metadata if str(m.get('name','')).lower().endswith('.csv') and m.get('kind')!='archive']
    index=state.get('attachment_index',0)
    if index<len(csvs):
        meta=csvs[index];offset=state.get('attachment_row',0)
        card=read_diagnostic_attachment(item['actor'],metadata,{'attachment_id':str(meta['id']),'operation':'csv_statistics','rows_start':offset},
            read_state=item.setdefault('attachment_read_state',{}))
        # Distinct chunks are retained because their numerical aggregates differ.
        state.setdefault('evidence',[]).append(card)
        data=card['data'];next_row=data.get('next_rows_start')
        if not data.get('has_measurements'):
            state.setdefault('issues',[]).append({'source':str(meta['id']),'reason':'该CSV范围没有实际测量行，不能判定通过'})
        if not data.get('end_of_file_reached') and next_row is not None and next_row>offset:
            state['attachment_row']=next_row
        else:
            state['attachment_index']=index+1;state['attachment_row']=0
            if not data.get('full_file_statistics'):
                state.setdefault('issues',[]).append({'source':str(meta['id']),'reason':data.get('stop_reason') or '部分列或范围尚未处理','unprocessed_columns':data.get('unprocessed_column_indices',[])})
        return _summary(item,state,continuation=True)
    if not (item.get('context',{}).get('hmi') or {}):
        needs_local=not csvs or bool(re.search(r'超时|连接|断连|报错|故障|日志|timeout|connection|error',item.get('diagnostic_question',''),re.I))
        if needs_local and not state.get('no_pair_recorded'):
            state.setdefault('issues',[]).append({'reason':'本次问题未关联上位机窗口，服务端不能读取用户电脑上的日志；请从已登录上位机诊断页发起，或选择相关日志附件。'})
            state['no_pair_recorded']=True
        return _summary(item,state)
    # A capability receipt establishes the actual default roots; connection
    # presence, browser paths or model text cannot create an authorization.
    if not state.get('capabilities_done'):
        card=local.evidence(item,{'operation':'capabilities'})
        if _waiting(card):return _summary(item,state,waiting=card)
        _record(state,card);state['capabilities_done']=True
        if card.get('data',{}).get('status') not in {'ready','read','ok'}:
            state.setdefault('issues',[]).append({'reason':'本机能力核对未成功','receipt':card.get('data')})
            state['local_done']=True
    if state.get('local_done'):return _summary(item,state)
    if not state.get('discovery_done'):
        card=local.evidence(item,{'operation':'discover','path':'$STATE','limit':16})
        if _waiting(card):return _summary(item,state,waiting=card)
        data=card.get('data',{});card['type']='diagnostic_discovery';_record(state,card)
        candidates=data.get('candidates',[])
        question=item.get('diagnostic_question','')
        need_csv=bool(re.search(r'csv|缺失|漏采|采样',question,re.I)) or not csvs
        selected=[]
        # Keep space for both data and communication logs. Selection stays
        # explicit and bounded by the same task's remaining read budget.
        cap=max(0,min(6,10-int(item.get('reads',0))))
        for kind in (['csv','log'] if need_csv else ['log']):
            group=[c for c in candidates if c.get('kind')==kind and c.get('path')]
            selected.extend(group[:max(1,cap//2)] if cap else [])
        selected=selected[:cap]
        state['selected_files']=selected;state['unselected_files']=max(0,len(candidates)-len(selected))
        if not data.get('complete'):
            state.setdefault('issues',[]).append({'reason':'本机目录检索尚有未覆盖范围','unread':data.get('unread'),'errors':data.get('errors',[])})
        if not any(c.get('kind')=='log' for c in selected):
            state.setdefault('issues',[]).append({'reason':'在本次已检索且可读的实例目录中未找到可核对的日志','searched_roots':data.get('roots',[]),'search_status':data.get('status')})
        if not candidates:state.setdefault('issues',[]).append({'reason':'本机未返回可读候选文件','receipt':data})
        state['discovery_done']=True;state['local_index']=0
    files=state.get('selected_files',[]);position=state.get('local_index',0)
    if position>=len(files):state['local_done']=True;return _summary(item,state)
    target=files[position];operation='csv_diagnosis' if target['kind']=='csv' else 'log_diagnosis'
    args={'operation':operation,'path':target['path'],'offset':state.get('local_offset',0)}
    if state.get('local_attempt'):
        args['query']=json.dumps({'analysis_attempt':state['local_attempt']},separators=(',',':'))
    card=local.evidence(item,args)
    if _waiting(card):return _summary(item,state,waiting=card)
    _record(state,card);data=card.get('data',{})
    next_offset=data.get('next_offset')
    if data.get('status')=='partial' and isinstance(next_offset,int) and next_offset>args['offset']:
        state['local_offset']=next_offset
    elif data.get('status')=='analysis_restart_required' and not state.get('restarted_'+str(position)):
        state['restarted_'+str(position)]=True;state['local_offset']=0;state['local_attempt']=1
    else:
        state['local_index']=position+1;state['local_offset']=0;state['local_attempt']=0
        if not data.get('complete'):
            state.setdefault('issues',[]).append({'path':target['path'],'reason':'文件未完整分析','receipt_status':data.get('status'),'unread':data.get('unread')})
        if (data.get('data') or {}).get('unprocessed_numeric_column_indices'):
            state.setdefault('issues',[]).append({'path':target['path'],'reason':'已遍历文件，但超过64列的数值统计尚未计算，不能把这些列计为已完成分析',
                'unprocessed_numeric_column_indices':data['data']['unprocessed_numeric_column_indices']})
        if operation=='csv_diagnosis' and (data.get('data') or {}).get('has_measurements') is False:
            state.setdefault('issues',[]).append({'path':target['path'],'reason':'CSV没有实际测量行，不能判定数据正常'})
    # Cached completed local receipts cost no new local I/O. Continue to the
    # next actual request within this tool call instead of wasting a model or
    # read-budget turn just to advance an already-persisted cursor.
    return prepare(item,{})
