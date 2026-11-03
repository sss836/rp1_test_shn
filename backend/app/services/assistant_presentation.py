"""Readable result state derives from actual receipts, never a tool-return checkmark."""
def presentation(reply,*,state,completion,outcome,cards,termination):
 saved=bool(outcome and outcome.get('receipt'))
 if completion=='partial' or (saved and state!='COMPLETED'):status='partial'
 elif saved:status='completed'
 elif state=='WAITING_PERMISSION':status='awaiting_permission'
 elif outcome and outcome.get('status')=='needs_adoption':status='awaiting_confirmation'
 elif state=='CANCELLED':status='cancelled'
 elif state in {'TIMED_OUT','INTERRUPTED'}:status='unknown'
 elif state=='FAILED':status='failed'
 elif completion=='partial':status='partial'
 else:status='answered'
 evidence=[str(card.get('title') or '已取得资料') for card in cards if card.get('type') not in {'query_issue','unprocessed_input'}][:8]
 actions={'awaiting_permission':['在资料授权卡处理待授权范围，随后继续原任务。'],'awaiting_confirmation':['核对所示对象、当前值、修改后值和影响范围，再采用参数。'],'failed':['查看失败原因；修正后重试，并保留原请求标识。'],'unknown':['先查询原任务与业务回执，核对结果后再决定重试。'],'partial':['补充尚缺证据后继续核对。'],'cancelled':['已有回执保留；需要继续时重新发起任务。']}.get(status,[])
 unknown=[]
 if status in {'partial','unknown','failed','awaiting_permission'}:unknown.append('本次尚未完成全部核对；未取得真实回执的操作不能认定成功。')
 if not saved and outcome:unknown.append('尚未取得业务保存回执。')
 return dict(status=status,conclusion=str(reply or '尚无结论').split('\n\n')[0][:1000],evidence=evidence,next_actions=actions,unknown=unknown)
