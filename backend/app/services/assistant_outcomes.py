"""Bounded public termination reasons; never expose upstream error payloads."""
REASONS = {
    "round_limit": "已达到6轮交互上限，本次仅整理已有结果；未核实部分仍待确认。",
    "read_limit": "已达到本次查询次数上限，尚未完成的部分没有继续查询。",
    "deadline": "任务达到总执行时限，已停止新增调用。",
    "provider_timeout": "单次模型请求超时，已停止后续调用。",
    "provider_network": "服务器与模型供应商通信中断，未取得完整回答。",
    "provider_http": "模型供应商返回异常状态，未取得完整回答。",
    "billing_stopped": "费用预留未成功，已停止新增模型调用，请核对计费状态。",
    "billing_unverified": "模型用量无法可靠结算，已停止新增调用并保留费用预留。",
    "auth_expired": "原登录会话或任务授权已失效，已停止新增调用。",
    "tool_failed": "助手工具处理未完成，请核对下列已取得的证据。",
    "invalid_reply": "未取得有效的模型答复，已保留实际查询及业务回执。",
    "bridge_busy": "模型桥接服务暂时繁忙，本次未完成。",
    "cancelled": "任务已停止，已提交结果保留。",
}


def failure_reason(status, body):
    code=body.get("error") if isinstance(body,dict) else None
    if isinstance(code,str) and code in REASONS:return code
    return {401:"auth_expired",403:"auth_expired",429:"bridge_busy",504:"deadline"}.get(status,"tool_failed")


def partial_reply(reason,cards):
    facts=[]
    for c in cards:
        data=c.get("data") or {}
        if c.get("type")=="query_issue":continue
        if c.get("type")=="business_search" and isinstance(data.get("items"),list):
            part=(data.get("applied_filters") or {}).get("target_part_code")
            number=data.get("total",len(data["items"]))
            fact=f"{c.get('source','业务目录')}：匹配{number}条，本页{len(data['items'])}条"+(f"，部位{part}" if part else "")+"。"
        elif c.get("type")=="source_evidence" and data.get("path"):
            fact=f"已读取源码 {data['path']}，第{data.get('start')}—{data.get('end')}行，版本{data.get('version','未提供')}。"
        else:fact="已保留证据："+str(c.get("title","查询结果"))+"。"
        if fact not in facts:facts.append(fact)
    return REASONS[reason]+("\n已取得的资料：\n"+"\n".join(facts[:8])+"\n这些资料不代表尚未完成的诊断已经确定；详情见结果卡片。" if facts else " 尚无可用于回答的查询证据。")+" 已核对实际提交回执，不会自动撤销已保存对象。"
