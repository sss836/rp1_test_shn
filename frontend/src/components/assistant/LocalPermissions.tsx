import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { assistantApi, type LocalPermission } from "../../assistantApi";
import { ApiError, requestId } from "../../client";

const terminal = new Set(["COMPLETED","FAILED","TIMED_OUT","INTERRUPTED","CANCELLED"]);
const stateNames:Record<LocalPermission["state"],string>={pending:"等待你决定",approved:"已允许，等候上位机处理",denied:"已拒绝",expired:"申请已过期",completed:"已收到上位机处理结果",cancelled:"申请已取消"};
type Scope="file"|"directory";
function availableScopes(item:LocalPermission):Scope[]{return (["file","directory"] as const).filter(scope=>item.allowed_scopes.includes(scope)&&!!(item.scope_targets?.[scope]??(scope==="file"?item.path:undefined)));}
function scopeTarget(item:LocalPermission,scope:Scope){return item.scope_targets?.[scope]??(scope==="file"?item.path:"");}
function expires(value:string){const date=new Date(value);return Number.isNaN(date.getTime())?"有效时间待核对":date.toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false});}
function expiredOnScreen(item:LocalPermission,now:number){const end=Date.parse(item.expires_at);return item.state==="pending"&&Number.isFinite(end)&&end<=now;}
function PermissionDialog({item,pending,error,onDismiss,onDecide}:{item:LocalPermission;pending:boolean;error:string;onDismiss:()=>void;onDecide:(scope:Scope|"deny")=>void}){
  const choices=availableScopes(item),[scope,setScope]=useState<Scope>(choices[0]??"file"),dialog=useRef<HTMLDivElement>(null);
  useEffect(()=>{const previous=document.activeElement;dialog.current?.querySelector<HTMLElement>("input:not(:disabled),button:not(:disabled)")?.focus();return()=>{if(previous instanceof HTMLElement&&previous.isConnected)previous.focus();};},[]);
  function keys(event:KeyboardEvent){
    if(event.key==="Escape"&&!pending){event.preventDefault();onDismiss();return;}
    if(event.key!=="Tab")return;
    const elements=Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),a[href],summary')??[]),first=elements[0],last=elements.at(-1);
    if(!first)return;
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
  return <div className="assistant-permission-dialog-backdrop"><div ref={dialog} className="assistant-permission-dialog" role="dialog" aria-modal="true" aria-labelledby="assistant-permission-title" aria-describedby="assistant-permission-explanation" onKeyDown={keys}>
    <h3 id="assistant-permission-title">允许助手查看这份资料吗？</h3><p id="assistant-permission-explanation">为了继续回答你的问题，助手需要查看当前上位机上的资料。{item.share_notice||"读取到的内容会发送给平台使用的模型，用于本次诊断。"}</p>
    <p><strong>为什么需要查看</strong><br/>{item.purpose||"继续检查原问题所需的资料。"}</p><p><strong>要查看的位置</strong><code className="assistant-permission-path">{item.path||"服务器未提供具体位置"}</code></p>
    <fieldset disabled={pending}><legend>这次允许查看哪些内容</legend>{choices.map(choice=><label key={choice}><input type="radio" name={`permission-scope-${item.id}`} value={choice} checked={scope===choice} onChange={()=>setScope(choice)}/><span>{choice==="file"?(["read","csv_diagnosis","log_diagnosis"].includes(item.operation)?"只允许这个文件":"只允许本次指定的路径"):"允许这个目录及其内部文件"}<code className="assistant-permission-path">{scopeTarget(item,choice)}</code></span></label>)}</fieldset>
    {!choices.length&&<p className="assistant-permission-warning">还没有可确认的查看范围。请在原上位机核对这次申请，或稍后重新查询。</p>}
    <p>仅用于当前账号的这次诊断。不会修改文件、运行命令或控制设备，也不会绕过操作系统的读取权限。</p><p>请在 {expires(item.expires_at)} 前决定。暂不决定时，已取得的资料和草稿会保留。</p>
    {error&&<p className="assistant-permission-error" role="alert">{error}</p>}
    <div className="assistant-permission-actions"><button type="button" className="assistant-adopt-button" disabled={pending||!choices.includes(scope)} onClick={()=>onDecide(scope)}>允许只读</button><button type="button" className="assistant-adopt-button" disabled={pending} onClick={()=>onDecide("deny")}>拒绝查看</button><button type="button" className="assistant-text-button" disabled={pending} onClick={onDismiss}>稍后决定</button></div>
    <details><summary>查看申请明细</summary><p>操作：{item.operation}</p><p>申请编号：{item.id}</p><p>申请校验：{item.request_sha256}</p></details>
  </div></div>;
}

type PermissionProps={accountKey:string;jobId:string;jobState:string;enabled:boolean;onChanged:()=>void};
export function LocalPermissions(props:PermissionProps){return <LocalPermissionsSession key={`${props.accountKey}:${props.jobId}`} {...props}/>;}
function LocalPermissionsSession({accountKey,jobId,jobState,enabled,onChanged}:PermissionProps){
  const [modal,setModal]=useState<string|null>(null),[busy,setBusy]=useState(""),[error,setError]=useState(""),[notice,setNotice]=useState(""),[now,setNow]=useState(()=>Date.now());
  const alive=useRef(true),controller=useRef<AbortController|null>(null),prompted=useRef(new Set<string>()),keys=useRef(new Map<string,string>());
  const ended=terminal.has(jobState);
  const requests=useQuery({queryKey:["assistant-permissions",accountKey,jobId],queryFn:({signal})=>assistantApi.localPermissions(jobId,signal),enabled:enabled&&!!jobId,retry:false,refetchInterval:ended?false:2000});
  useEffect(()=>{alive.current=true;return()=>{alive.current=false;controller.current?.abort();};},[]);
  useEffect(()=>{if(!requests.data?.items.some(item=>item.state==="pending"))return;const timer=window.setInterval(()=>setNow(Date.now()),1000);return()=>window.clearInterval(timer);},[requests.data?.items]);
  useEffect(()=>{
    if(!enabled||ended||requests.data?.paired!==true){setModal(null);return;}
    if(modal){const item=requests.data.items.find(row=>row.id===modal);if(!item||item.state!=="pending"||expiredOnScreen(item,now))setModal(null);return;}
    const next=requests.data.items.find(item=>item.state==="pending"&&!expiredOnScreen(item,now)&&!prompted.current.has(item.id));
    if(next){prompted.current.add(next.id);setModal(next.id);}
  },[enabled,ended,requests.data,modal,now]);
  function key(value:string){const existing=keys.current.get(value);if(existing)return existing;const next=requestId();keys.current.set(value,next);return next;}
  async function action(name:string,operation:(signal:AbortSignal)=>Promise<unknown>,success:string){
    if(busy)return;
    const abort=new AbortController();controller.current?.abort();controller.current=abort;setBusy(name);setError("");setNotice("");
    try{await operation(abort.signal);if(!alive.current||abort.signal.aborted)return;setModal(null);setNotice(success);await requests.refetch();if(alive.current&&!abort.signal.aborted)onChanged();}
    catch(e){if(!alive.current||abort.signal.aborted)return;const suffix=e instanceof ApiError&&[409,410].includes(e.status)?" 这次决定未被确认，请核对申请是否已经过期或被处理。":"";setError((e instanceof Error?e.message:"没有确认成功，请重试并核对状态。")+suffix);await requests.refetch();if(alive.current&&!abort.signal.aborted)onChanged();}
    finally{if(alive.current&&!abort.signal.aborted)setBusy("");}
  }
  function decide(item:LocalPermission,scope:Scope|"deny"){
    void action(`decision:${item.id}`,signal=>assistantApi.decideLocalPermission(jobId,item.id,{request_key:key(`decision:${item.id}:${item.request_sha256}:${scope}`),expected_request_sha256:item.request_sha256,scope},signal),scope==="deny"?"已提交拒绝决定。尚未因此读取这份资料。":"已提交允许决定。还需上位机实际读取并返回结果，才能继续诊断。");
  }
  if(!enabled||!jobId)return null;
  const waiting=jobState==="WAITING_PERMISSION",data=requests.data;
  if(!data){return waiting?<section className="assistant-permissions" aria-label="本机资料申请"><strong>正在核对本机资料申请</strong><p>原诊断暂时暂停，已取得的内容和草稿会保留。</p>{requests.error&&<><p role="alert">{requests.error instanceof Error?requests.error.message:"无法取得申请状态。"}</p><button type="button" className="assistant-text-button" onClick={()=>void requests.refetch()}>重新查询申请</button></>}</section>:null;}
  if(data.paired!==true)return waiting?<section className="assistant-permissions" aria-label="本机资料申请"><strong>还没有可用的上位机关联</strong><p>请从原上位机登录并关联后核对这次诊断。网页不能替上位机读取文件或确认已经读到资料。</p></section>:null;
  if(!data.items.length)return waiting?<section className="assistant-permissions" aria-label="本机资料申请"><strong>尚未取得可确认的资料申请</strong><p>请核对原上位机连接，或稍后重新查询；网页不会据此扩大读取范围。</p><button type="button" className="assistant-text-button" onClick={()=>void requests.refetch()}>重新查询申请</button></section>:null;
  const superseded=new Set(data.items.map(item=>item.previous_request_id).filter(Boolean));
  const latest=data.items.filter(item=>!superseded.has(item.id));
  const selected=data.items.find(item=>item.id===modal),unresolved=latest.some(item=>["pending","approved","expired","denied"].includes(item.state));
  return <section className="assistant-permissions" aria-label="本机资料申请"><strong>继续诊断前，需要查看本机资料</strong><p>{ended?"这次诊断已经结束。下面的申请仅供核对，不会继续读取。":waiting?"正在等待查看资料。等待时暂停分析，之前的资料和草稿会保留。":"下面是这次诊断的资料申请及处理状态。"}</p>
    {data.items.map(item=><article className="assistant-permission-item" key={item.id}><span className="assistant-permission-status">{ended&&["pending","approved"].includes(item.state)?"任务已结束，不再等待资料":expiredOnScreen(item,now)?"显示已到期，正在核对":stateNames[item.state]}</span><p>{item.purpose||"查看与原问题相关的资料"}</p>{superseded.has(item.id)&&<small>这是之前的申请，已另行重新申请。</small>}<code className="assistant-permission-path">{item.path||"具体位置请查看申请明细"}</code>
      {item.state==="pending"&&!expiredOnScreen(item,now)&&<p>{item.share_notice||"读取到的内容会发送给平台使用的模型，用于本次诊断。"}</p>}
      {item.state==="approved"&&<p>{ended?"任务已经结束，允许记录仍保留。是否取得资料，请以实际返回的内容为准。":"已允许查看，正在等候原上位机处理。批准不代表文件已读取；请以实际返回的资料为准。"}</p>}
      {item.state==="completed"&&<p>上位机已返回这次处理结果。是否读到资料、哪些内容仍未查看，请核对资料卡。</p>}
      {item.state==="denied"&&<p>这次没有允许查看。可以重新申请，也可以停止本次诊断。</p>}
      {(item.state==="expired"||expiredOnScreen(item,now))&&<p className="assistant-permission-warning">这次申请已到期或需要核对有效期。重新申请后再决定，不会把迟到批准当成已经读到资料。</p>}
      {!ended&&!superseded.has(item.id)&&<div className="assistant-permission-actions">{item.state==="pending"&&!expiredOnScreen(item,now)&&<button type="button" className="assistant-adopt-button" disabled={!!busy} onClick={()=>{setError("");setModal(item.id);}}>决定是否允许查看</button>}{(["expired","denied"].includes(item.state)||expiredOnScreen(item,now))&&<button type="button" className="assistant-adopt-button" disabled={!!busy} onClick={()=>void action(`retry:${item.id}`,signal=>assistantApi.retryLocalPermission(jobId,item.id,{request_key:key(`retry:${item.id}:${item.request_sha256}`),expected_request_sha256:item.request_sha256},signal),"已提交重新申请，请核对新的路径和有效期后再决定。")}>重新申请查看</button>}</div>}
      <details><summary>申请与范围明细</summary><p>操作：{item.operation}</p><p>有效至：{expires(item.expires_at)}</p>{item.approved_scope&&<p>已批准范围：{item.approved_scope==="file"?"仅指定文件或路径":item.approved_scope==="directory"?"指定目录及其内部文件":"本次已登记的诊断资料"}</p>}{item.scope_root&&<p>批准的位置：{item.scope_root}</p>}<p>申请编号：{item.id}</p><p>申请校验：{item.request_sha256}</p>{item.previous_request_id&&<p>上次申请编号：{item.previous_request_id}</p>}</details>
    </article>)}
    {notice&&<p role="status">{notice}</p>}{error&&<p className="assistant-permission-error" role="alert">{error}</p>}
    {requests.error&&<p role="alert">申请状态暂时无法更新；已有允许决定不等于读取完成。<button className="assistant-text-button" type="button" onClick={()=>void requests.refetch()}>重新查询申请</button></p>}
    {!ended&&<div className="assistant-permission-actions">{waiting&&!unresolved&&latest.some(item=>item.state==="completed")&&<button type="button" className="assistant-adopt-button" disabled={!!busy} onClick={()=>void action("resume",signal=>assistantApi.resumeJob(jobId,key(`resume:${jobId}:${data.items.filter(item=>item.state==="completed").map(item=>item.id).join(":")}`),signal),"已请求继续原诊断，请等待服务器核对原登录和上位机身份。")}>继续原来的诊断</button>}<button type="button" className="assistant-text-button" disabled={!!busy} onClick={()=>void action("cancel",signal=>assistantApi.cancel(jobId,signal),"已请求停止这次诊断，请以任务状态和已有结果为准。")}>停止这次诊断</button></div>}
    <details><summary>本次诊断用量</summary><p>已进行 {data.rounds}/6 轮分析，已调用 {data.read_count}/12 次只读查询。累计执行 {(data.active_elapsed_ms/1000).toFixed(1)} 秒，上限120秒。</p><p>等待允许查看资料的时限为 {Math.round(data.waiting_timeout_seconds/60)} 分钟；等待时间不计入执行用时。继续原任务时用量累计，不重新开始计数。</p></details>
    {selected&&selected.state==="pending"&&!expiredOnScreen(selected,now)&&!ended&&<PermissionDialog key={selected.id} item={selected} pending={!!busy} error={error} onDismiss={()=>setModal(null)} onDecide={scope=>decide(selected,scope)}/>}
  </section>;
}
