import { useRef, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { assistantApi, type SetupOutcome } from "../assistantApi";
import { apiRequest, requestId } from "../client";
import { Panel } from "../components/Workbench";

export function SetupDialogue() {
  const cache=useQueryClient();
  const [input,setInput]=useState("");
  const [messages,setMessages]=useState<string[]>([]);
  const [outcome,setOutcome]=useState<SetupOutcome | null>(null);
  const [error,setError]=useState("");
  const [pending,setPending]=useState(false);
  const [publishApproved,setPublishApproved]=useState(false);
  const request=useRef<{messages:string[];key:string}|null>(null);
  async function execute(action:()=>Promise<SetupOutcome>) {
    setPending(true);setError("");
    try {
      const result=await action();setOutcome(result);
      if(result.status==="created") {await cache.invalidateQueries();request.current=null;}
    } catch(e) {setError(e instanceof Error?e.message:"建档请求失败，请先核对回执。");}
    finally {setPending(false);}
  }
  function submit(event:FormEvent) {
    event.preventDefault(); if(pending||!input.trim())return;
    const next=outcome?.status==="created"?[input.trim()]:[...messages,input.trim()];
    request.current={messages:next,key:requestId()};setMessages(next);setInput("");setPublishApproved(false);setOutcome(null);
    void execute(()=>assistantApi.setupCommand(next,request.current!.key));
  }
  const hasPublish=outcome?.operations?.some((op)=>op.kind==="case_publish"||op.data.publish===true);
  return <Panel title="对话建档" description="明确指令且字段齐全时直接保存；缺项或冲突会追问。建议参数须由你采用。此处仅处理建档，不启动设备。">
    <p>例如：创建项目，编号 RP-QA-001，名称“模块可靠性”；复杂配置可直接附JSON。修改已发布用例会追加版本。</p>
    <ol aria-label="本页建档输入">{messages.map((message,index)=><li key={index}>{message}</li>)}</ol>
    <form className="setup-form" onSubmit={submit}>
      <label>建档指令<textarea value={input} onChange={(e)=>setInput(e.target.value)} required maxLength={8000} rows={4} disabled={pending}/></label>
      <button className="primary-button" disabled={pending||!input.trim()}>{pending?"正在处理…":"发送建档指令"}</button>
    </form>
    {error&&<p role="alert">{error}</p>}
    {error&&<div>
      <button type="button" disabled={pending} onClick={()=>void execute(()=>assistantApi.setupCommand(request.current!.messages,request.current!.key))}>使用原请求标识重试</button>
      <button type="button" disabled={pending} onClick={()=>void execute(async()=>({status:"created",question:"已取得实际回执。",receipt:await apiRequest<NonNullable<SetupOutcome["receipt"]>>(`/api/v1/setup/receipts/${request.current!.key}`)}))}>查询实际保存回执</button>
    </div>}
    {outcome&&<div aria-live="polite"><p role="status">{outcome.question}</p>
      {outcome.details!==undefined&&<pre>{JSON.stringify(outcome.details,null,2)}</pre>}
      {outcome.status==="needs_adoption"&&<>
        <p>尚未保存；需要采用的字段：{outcome.unconfirmed_fields?.join("、")}</p>
        <pre>{JSON.stringify(outcome.operations,null,2)}</pre>
        {hasPublish&&<label><input type="checkbox" checked={publishApproved} onChange={(e)=>setPublishApproved(e.target.checked)}/>我明确要求发布所示用例版本</label>}
        <button type="button" className="primary-button" disabled={pending||(hasPublish&&!publishApproved)} onClick={()=>void execute(()=>assistantApi.adoptSetup(outcome.proposal_id!,outcome.proposal_hash!,publishApproved))}>采用所示参数并执行整组建档</button>
      </>}
      {outcome.receipt&&<><p>实际请求：{outcome.receipt.request_key}{outcome.receipt.replayed?"（前次回执，未重复创建）":""}</p><pre>{JSON.stringify(outcome.receipt.objects,null,2)}</pre></>}
    </div>}
    <button type="button" disabled={pending} onClick={()=>{setMessages([]);setOutcome(null);setError("");request.current=null;}}>开始新的建档</button>
  </Panel>;
}
