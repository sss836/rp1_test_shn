import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowUpRight, CheckCircle2, ChevronDown, Clock3, History, Layers, Link2, ListChecks, Plus, Radio, Sparkles, Square, X } from "lucide-react";
import { assistantApi, type PageContext, type AssistantCard, type JobPayload, type JobEvent, type ChatJob } from "../assistantApi";
import { ApiError, requestId } from "../client";
import { locationHref, type View } from "../navigation";
import { Markdown } from "./assistant/Markdown";
import { BusinessProposal } from "./assistant/BusinessProposal";
import { Evidence } from "./assistant/Evidence";
import { Composer, type ComposerSubmission } from "./assistant/Composer";
import { clearAccountDrafts } from "../assistantStorage";
import { DiagnosisPreparation } from "./assistant/DiagnosisPreparation";
import { LocalPermissions } from "./assistant/LocalPermissions";
import { SupportWorkspace } from "./assistant/SupportWorkspace";
import "../assistant.css";

const finished = new Set(["COMPLETED", "FAILED", "TIMED_OUT", "INTERRUPTED", "CANCELLED"]);
const targets = new Set(["asset", "test-case", "planning", "archive", "curves", "benches", "stations", "execution"]);
const viewNames: Record<string, string> = { curves: "曲线分析", archive: "归档", planning: "项目与计划", asset: "样品", benches: "台架", stations: "工位", execution: "测试执行", "test-case": "测试用例", dashboard: "总览" };
const stateNames: Record<string, string> = { QUEUED: "排队中", RUNNING: "正在处理", WAITING_PERMISSION: "等待资料或读取许可", COMPLETED: "对话已结束", FAILED: "未完成", TIMED_OUT: "已超时", INTERRUPTED: "已中断", CANCELLED: "已停止" };
const reasonNames:Record<string,string>={round_limit:"达到交互上限",read_limit:"达到查询上限",deadline:"任务总时限已到",provider_timeout:"单次模型请求超时",provider_network:"模型网络中断",provider_http:"模型服务异常",billing_stopped:"费用预留未成功",billing_unverified:"用量待核对",auth_expired:"登录授权已失效",tool_failed:"工具处理未完成",invalid_reply:"答复无效",bridge_busy:"桥接服务繁忙",cancelled:"已停止"};
const businessNames: Record<string,string> = {saved:"已取得保存回执",needs_input:"待补充，尚未保存",needs_adoption:"待采用，尚未保存",answered:"查询或答复已返回",not_saved:"请核对真实回执"};
const metricNames: Record<string, string> = { pos_rad: "位置", vel_rad_s: "角速度", torque_nm: "力矩", current_a: "电流", temperature_c: "温度" };
const suggestions = [
  { icon: Activity, text: "解释当前选区", hint: "查看原始区间统计" },
  { icon: ListChecks, text: "查看测试进展", hint: "了解计划与执行状态" },
  { icon: Radio, text: "查询工位状态", hint: "查看最新在线快照" },
];

function when(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
}
function amount(value: number) { return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumSignificantDigits: 6 }) : "—"; }
function money(value: unknown) { const n = Number(value); return Number.isFinite(n) ? n.toFixed(n > 0 && n < 0.01 ? 4 : 2) : "—"; }
function selection(context: PageContext) {
  if (context.selection_empty) return "当前视窗没有重叠数据";
  const metric = context.metrics?.map((name) => metricNames[name] ?? name).join("、");
  const range = context.start !== undefined || context.end !== undefined ? `${context.start ?? "起点"}–${context.end ?? "终点"} s` : "";
  return [metric, range].filter(Boolean).join(" · ") || "关联当前页面";
}

function ContextSummary({ context }: { context: PageContext }) {
  return <section className="assistant-context" aria-label="发送前页面上下文">
    <details>
      <summary><span className="assistant-context-icon"><Layers size={17}/></span><span className="assistant-context-copy"><span className="assistant-context-title">{viewNames[context.view] ?? "当前页面"}<span>{context.kind === "MODULE" ? "模块" : "整机"}</span></span><span className="assistant-context-selection">{selection(context)}</span></span><ChevronDown className="assistant-chevron" size={15}/></summary>
      <div className="assistant-context-details">
        <p className="assistant-caption">发送时附带以下信息，任务发起后保持不变。</p>
        {context.object_id && <p><span>对象</span><code>{context.object_id}</code></p>}
        {context.archive_id && <p><span>归档</span><code>{context.archive_id}</code></p>}
        {!!context.metrics?.length && <p><span>指标</span>{context.metrics.join("、")}</p>}
        {!!context.subjects?.length && <p><span>测量对象</span>{context.subjects.join("、")}</p>}
        {(context.stage || context.cycle) && <p><span>阶段 / 循环</span>{context.stage ?? "全部"} / {context.cycle ?? "全部"}</p>}
        {context.selection_empty && <p role="note">请重新选择归档或区间，助手不会改用全程统计。</p>}
      </div>
    </details>
  </section>;
}

export function cardHref(card: AssistantCard, context: PageContext) {
  if (!card.target || !targets.has(card.target.view)) return null;
  if (card.target.id && !/^[0-9a-f-]{36}$/i.test(card.target.id)) return null;
  return locationHref({ view: card.target.view as View, id: card.target.id, kind: context.kind, part: null });
}
function ResultCard({ card, context, navigate, onRestore }: { card: AssistantCard; context: PageContext; navigate: (view: View, id: string | null) => void; onRestore?: (message:string)=>void }) {
  if(card.type==="diagnosis_preparation")return <DiagnosisPreparation card={card}/>;
  if(card.type==="unprocessed_input"){
    const items=Array.isArray(card.data.items)?card.data.items as Array<{request_key?:string;message?:string}>:[];
    return <article className="assistant-proposal assistant-unprocessed"><strong>补充未用于本次诊断</strong><p>{typeof card.data.summary==="string"?card.data.summary:"这些补充尚未进入本次诊断，请作为下一条问题发送。"}</p>{items.filter(item=>typeof item.message==="string").map((item,index)=><section key={item.request_key??index}><pre>{item.message}</pre><button type="button" className="assistant-text-button" onClick={()=>onRestore?.(item.message!)}>恢复到草稿</button></section>)}<p className="assistant-caption">恢复会追加到当前草稿；由你检查并发送，不会自动执行。</p></article>;
  }
  const href = cardHref(card, context);
  const series = Array.isArray(card.data.series) ? card.data.series[0] as Record<string, unknown> | undefined : undefined;
  const range = card.range && typeof card.range === "object" ? card.range as { start?: number; end?: number } : null;
  const rangeText = range && range.start !== undefined && range.end !== undefined ? `${range.start}–${range.end} s` : typeof card.range === "string" ? card.range : "";
  const unit = typeof series?.unit === "string" ? series.unit : card.type === "station_status" ? "工位" : card.unit;
  const rawState=String(card.data.status??card.data.state??"").toLowerCase();
  const failed=card.data.success===false||card.data.ok===false||Boolean(card.data.error)||["failed","error","cancelled","unknown","partial","pending","waiting_permission","unavailable"].includes(rawState);
  return <article className="assistant-card" data-result-status={failed?rawState||"failed":"evidence_returned"}>
    <div className="assistant-card-heading"><span className="assistant-card-icon">{failed?<Clock3 size={15}/>:<Link2 size={15}/>}</span><strong>{card.title}</strong></div>
    {rangeText && <p className="assistant-card-range">{rangeText}{unit && <span>单位：{unit}</span>}</p>}
    {series && typeof series.mean === "number" && <div className="assistant-stats">{[["平均值", series.mean], ["最小值", series.min], ["最大值", series.max]].map(([label, value]) => typeof value === "number" && <div key={String(label)}><span>{String(label)}</span><strong>{amount(value)}<small>{unit}</small></strong></div>)}</div>}
    <details className="assistant-evidence"><summary><Link2 size={12}/>检查了哪些资料<ChevronDown size={12}/></summary><Evidence card={card}/></details>
    <div className="assistant-card-footer"><span title={card.updated_at}><Clock3 size={11}/>{when(card.updated_at)} 更新</span>{href && <a aria-label="打开对应对象" href={href} onClick={(event) => { event.preventDefault(); navigate(card.target!.view as View, card.target!.id); }}>查看{card.target!.view === "archive" ? "归档" : card.target!.view === "planning" ? "计划" : "对象"}<ArrowUpRight size={13}/></a>}</div>
  </article>;
}

type PanelProps={accountKey:string;context:PageContext;navigate:(view:View,id:string|null)=>void};
// Account changes discard the old identity's browser drafts and abort all
// component-scoped requests, including late uploads and incremental event reads.
export function AssistantPanel(props:PanelProps) {
  useEffect(() => {
    try { const previous=sessionStorage.getItem("rp1-assistant-active-account"); if(previous&&previous!==props.accountKey)clearAccountDrafts(previous); sessionStorage.setItem("rp1-assistant-active-account",props.accountKey); } catch { /* Private browsing may disallow storage. */ }
  },[props.accountKey]);
  return <AssistantPanelSession key={props.accountKey} {...props}/>;
}
function restored(account: string, field: string) { try { return sessionStorage.getItem(`rp1-assistant-${field}:${encodeURIComponent(account)}`) ?? ""; } catch { return ""; } }
function restoredQueue(account:string){try{const value=JSON.parse(restored(account,"queued")||"[]");return Array.isArray(value)?value.filter((id:unknown):id is string=>typeof id==="string"&&/^[0-9a-f-]{36}$/i.test(id)):[];}catch{return [];}}
function AssistantPanelSession({ accountKey, context, navigate }: PanelProps) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false), [thread, setThread] = useState(() => restored(accountKey,"thread")), [jobId, setJob] = useState(() => restored(accountKey,"job"));
  const [error, setError] = useState(""), [sending, setSending] = useState(false), [approved, setApproved] = useState(false), [showHistory, setShowHistory] = useState(false), [canRetry, setCanRetry] = useState(false);
  const [search, setSearch] = useState(""), [rename, setRename] = useState<string|null>(null), [deleteConfirmed, setDeleteConfirmed] = useState(false), [suggestion, setSuggestion] = useState<{text:string;key:number;append?:boolean}|null>(null), [queued, setQueued] = useState<string[]>(()=>restoredQueue(accountKey));
  const [composerEpoch,setComposerEpoch]=useState(0),[recentCompleted,setRecentCompleted]=useState<ChatJob[]>([]),[pausedEvidence,setPausedEvidence]=useState<Record<string,NonNullable<ChatJob["result"]>>>({});
  const pendingSteer=useRef<{id:string;message:string;key:string;clear:ComposerSubmission["clear"]}|null>(null);
  const pending = useRef<{payload:JobPayload;clear:ComposerSubmission["clear"];mode:ComposerSubmission["mode"]}|null>(null);
  const controller = useRef<AbortController|null>(null), bottom = useRef<HTMLDivElement|null>(null), alive = useRef(true);
  const unavailableJobs=useRef(new Set<string>());
  const eventCache = useRef(new Map<string,{items:JobEvent[];next_seq:number;state:string;has_more:boolean}>());
  useEffect(() => { alive.current = true; return () => {alive.current = false;controller.current?.abort();}; }, []);
  useEffect(() => { const show = () => setOpen(true); window.addEventListener("rp1-open-assistant",show); return () => window.removeEventListener("rp1-open-assistant",show); }, []);
  useEffect(() => { try { sessionStorage.setItem(`rp1-assistant-thread:${encodeURIComponent(accountKey)}`,thread); sessionStorage.setItem(`rp1-assistant-job:${encodeURIComponent(accountKey)}`,jobId);sessionStorage.setItem(`rp1-assistant-queued:${encodeURIComponent(accountKey)}`,JSON.stringify(queued)); } catch { /* Storage unavailable; normal conversation still works. */ } },[accountKey,thread,jobId,queued]);
  const threads = useQuery({ queryKey:["assistant-threads",accountKey,search],queryFn:({signal})=>assistantApi.threads(signal,search),enabled:open,retry:false });
  const history = useQuery({ queryKey:["assistant-history",accountKey,thread],queryFn:({signal})=>assistantApi.history(thread,signal),enabled:open&&!!thread,retry:false });
  const job = useQuery({ queryKey:["assistant-job",accountKey,jobId],queryFn:({signal})=>assistantApi.job(jobId,signal),enabled:!!jobId,retry:false,refetchInterval:query=>query.state.data&&finished.has(query.state.data.state)?false:1000 });
  const budget = useQuery({ queryKey:["assistant-budget",accountKey],queryFn:({signal})=>assistantApi.budget(signal),enabled:open,retry:false,refetchInterval:open?5000:false });
  const capabilities = useQuery({queryKey:["assistant-capabilities",accountKey],queryFn:({signal})=>assistantApi.capabilities(signal),enabled:open,retry:false});
  const events = useQuery({queryKey:["assistant-events",accountKey,jobId],queryFn:async({signal})=>{
    const previous=eventCache.current.get(jobId), result=await assistantApi.events(jobId,previous?.next_seq??0,signal);
    if(signal.aborted)throw new DOMException("已取消","AbortError");
    const items=new Map((previous?.items??[]).map(item=>[item.seq,item])); for(const item of result.items)items.set(item.seq,item);
    const merged={...result,has_more:result.has_more??result.items.length>=200,items:Array.from(items.values()).sort((a,b)=>a.seq-b.seq)};eventCache.current.set(jobId,merged);return merged;
  },enabled:open&&!!jobId&&capabilities.data?.features?.stream_events===true,retry:false,refetchInterval:query=>query.state.data?.has_more?50:query.state.data&&finished.has(query.state.data.state)?false:query.state.data?.state==="WAITING_PERMISSION"?2000:400});
  const unavailableJob=job.error instanceof ApiError&&[404,410].includes(job.error.status);
  const active=!!jobId&&!unavailableJob&&(!job.data||!finished.has(job.data.state)),blocked=!!(budget.data?.blocked||budget.data?.pricing_verified===false);
  useEffect(()=>{
    if(job.data?.state!=="WAITING_PERMISSION"||!job.data.result)return;
    const result=job.data.result,id=job.data.id;
    setPausedEvidence(previous=>previous[id]===result?previous:{...previous,[id]:result});
  },[job.data]);
  useEffect(()=>{
    if(!job.data||!finished.has(job.data.state))return;
    const completed=job.data;if(completed.result)setRecentCompleted(previous=>previous.some(item=>item.id===completed.id)?previous:[...previous,completed].slice(-20));
    void cache.invalidateQueries({queryKey:["assistant-history",accountKey,job.data.thread_id]});void cache.invalidateQueries({queryKey:["assistant-threads",accountKey]});
    if(job.data.receipt)for(const key of ["setup-options","setup-campaigns","setup-catalog","assets"])void cache.invalidateQueries({queryKey:[key]});
  },[job.data,cache,accountKey]);
  useEffect(()=>{if(job.data&&finished.has(job.data.state)&&queued.length){setJob(queued[0]);setQueued(previous=>previous.slice(1));}},[job.data,queued]);
  useEffect(()=>{if(open)bottom.current?.scrollIntoView?.({block:"end"});},[open,history.data?.messages.length,job.data?.result?.reply,events.data?.next_seq]);
  useEffect(()=>{
    if(!history.data||unavailableJob||(jobId&&(!job.data||!finished.has(job.data.state))))return;
    const unfinished=history.data.jobs.filter(item=>!finished.has(item.state)&&!unavailableJobs.current.has(item.id));
    const next=unfinished.find(item=>item.state==="RUNNING")??unfinished.at(-1);
    if(next&&next.id!==jobId){setJob(next.id);setQueued(previous=>previous.filter(id=>id!==next.id&&unfinished.some(item=>item.id===id)));}
  },[history.data,jobId,job.data,unavailableJob]);
  useEffect(()=>{
    if(!unavailableJob)return;
    unavailableJobs.current.add(jobId);
    setError(job.error instanceof Error?job.error.message:"原任务已隐藏或过期，可开始新对话。");
    setJob("");setQueued([]);pending.current=null;pendingSteer.current=null;setCanRetry(false);
  },[unavailableJob,job.error,jobId]);
  async function send(snapshot:NonNullable<typeof pending.current>){
    controller.current?.abort();const abort=new AbortController();controller.current=abort;setSending(true);setError("");setCanRetry(false);
    try{const result=await assistantApi.submitJob(snapshot.payload,abort.signal);if(abort.signal.aborted||!alive.current)return;snapshot.clear(result.thread_id);setThread(result.thread_id);if(snapshot.mode==="queue"&&active)setQueued(previous=>Array.from(new Set([...previous,result.id])));else setJob(result.id);setApproved(false);pending.current=null;await threads.refetch();}
    catch(e){if(!abort.signal.aborted&&alive.current){setError(e instanceof Error?e.message:"发送失败，可用原请求标识重试并查询回执。");setCanRetry(pending.current!==null);}}
    finally{if(!abort.signal.aborted&&alive.current)setSending(false);}
  }
  async function sendSteer(snapshot:NonNullable<typeof pendingSteer.current>){
    const abort=new AbortController();controller.current?.abort();controller.current=abort;setSending(true);setError("");setCanRetry(false);
    try{await assistantApi.steer(snapshot.id,snapshot.message,snapshot.key,abort.signal);if(!abort.signal.aborted&&alive.current){snapshot.clear();pendingSteer.current=null;void events.refetch();}}
    catch(e){if(!abort.signal.aborted&&alive.current){setError(e instanceof Error?e.message:"补充失败，请核对原运行任务。");setCanRetry(true);}}
    finally{if(!abort.signal.aborted&&alive.current)setSending(false);}
  }
  function submit(submission:ComposerSubmission){
    setSuggestion(null);
    if(submission.mode==="steer"){
      if(!active||!submission.text.trim())return;
      pending.current=null;const snapshot={id:jobId,message:submission.text,key:requestId(),clear:submission.clear};pendingSteer.current=snapshot;void sendSteer(snapshot);return;
    }
    pendingSteer.current=null;
    const frozen=structuredClone(context);if(submission.references.length)frozen.references=submission.references.map(({kind,id,label})=>({kind,id,label}));
    const snapshot={payload:{request_key:requestId(),...(thread?{thread_id:thread}:{}),message:submission.text||"请分析所附诊断附件，说明已读范围、未读内容及可核对的原因与依据。",context:frozen,attachments:submission.attachments,...(submission.mode==="queue"?{after_job_id:queued.at(-1)??jobId}:{})},clear:submission.clear,mode:submission.mode};pending.current=snapshot;void send(snapshot);
  }
  function selectThread(id:string){if(active||sending)return;setComposerEpoch(value=>value+1);setRecentCompleted([]);setPausedEvidence({});setQueued([]);setThread(id);setJob("");setError("");setSuggestion(null);setRename(null);setDeleteConfirmed(false);setCanRetry(false);pending.current=null;pendingSteer.current=null;}
  function restoreSupplement(message:string){setSuggestion({text:message,key:Date.now(),append:true});}
  function newThread(){if(active||sending)return;selectThread("");setShowHistory(false);}
  const outcome=job.data?.result?.setup,publish=outcome?.operations?.some(op=>op.kind==="case_publish"||op.data.publish===true);
  const queryError=threads.error??history.error??job.error??budget.error;
  const messages=history.data?.messages??[],currentReplyInHistory=messages.some(message=>message.job_id===jobId&&message.role==="assistant"),hasConversation=messages.length>0||!!jobId;
  const streamText=[...(events.data?.items??[])].reverse().find(item=>item.kind==="text")?.data.text??"";
  const toolNames:Record<string,string>={read_diagnostic_attachment:"读取诊断附件",search_business_objects:"查询业务对象",source_query:"读取版本化源码",read_local_diagnostic:"读取配对本机证据",search_shared_faq:"检索共享FAQ"};
  const lastTool=[...(events.data?.items??[])].reverse().find(item=>item.kind==="tool");
  return <><button className="assistant-entry" type="button" aria-expanded={open} aria-controls="rp1-assistant-panel" onClick={()=>setOpen(!open)}><Sparkles size={17}/><span>助手</span>{active&&<span className="assistant-live-dot"/>}</button>
    {open&&<aside id="rp1-assistant-panel" className="assistant-panel" aria-label="RP1助手"><header className="assistant-header"><span className="assistant-brand-mark"><Sparkles size={20}/></span><div className="assistant-heading"><h2>RP1 助手</h2><span>查询、分析与建档</span></div><div className="assistant-header-actions"><button className={`assistant-icon-button${showHistory?" is-selected":""}`} type="button" aria-label="查看历史会话" title="历史会话" aria-expanded={showHistory} onClick={()=>setShowHistory(!showHistory)}><History size={17}/></button><button className="assistant-icon-button" type="button" aria-label="新对话" title="新对话" disabled={active||sending} onClick={newThread}><Plus size={19}/></button><span className="assistant-toolbar-divider"/><button className="assistant-icon-button" type="button" aria-label="收起" title="收起" onClick={()=>setOpen(false)}><X size={18}/></button></div></header>
      {showHistory&&<div className="assistant-history"><input aria-label="搜索会话" maxLength={150} placeholder="搜索标题或聊天内容" value={search} onChange={e=>setSearch(e.target.value)}/><label htmlFor="assistant-history-select">历史会话</label><select id="assistant-history-select" value={thread} disabled={active||sending} onChange={e=>selectThread(e.target.value)}><option value="">新会话</option>{threads.data?.map(item=><option value={item.id} key={item.id}>{item.title||`会话 ${item.id.slice(0,8)}`} · {when(item.updated_at)}</option>)}</select>{thread&&<div className="assistant-history-actions"><button type="button" className="assistant-text-button" disabled={active||sending} onClick={()=>setRename(threads.data?.find(item=>item.id===thread)?.title??"")}>重命名</button><button type="button" className="assistant-text-button" disabled={active||sending} onClick={()=>setDeleteConfirmed(true)}>删除会话</button></div>}{rename!==null&&<form onSubmit={e=>{e.preventDefault();void assistantApi.renameThread(thread,rename.trim()).then(()=>{setRename(null);void threads.refetch();}).catch((e:unknown)=>setError(e instanceof Error?e.message:"重命名失败"));}}><input aria-label="会话名称" value={rename} maxLength={120} onChange={e=>setRename(e.target.value)}/><button type="submit" className="assistant-text-button" disabled={!rename.trim()}>保存名称</button><button type="button" className="assistant-text-button" onClick={()=>setRename(null)}>取消</button></form>}{deleteConfirmed&&<div className="assistant-delete-confirm" role="alert"><p>确认删除本账号会话？聊天和报告将不可再查看，独立业务审计保留。</p><button type="button" className="assistant-text-button" onClick={()=>void assistantApi.deleteThread(thread).then(()=>{try{sessionStorage.removeItem(`rp1-assistant-draft:${encodeURIComponent(accountKey)}:${thread}`);}catch{/* Storage unavailable. */}selectThread("");void threads.refetch();}).catch((e:unknown)=>setError(e instanceof Error?e.message:"删除失败"))}>确认删除</button><button type="button" className="assistant-text-button" onClick={()=>setDeleteConfirmed(false)}>保留会话</button></div>}{history.data?.jobs[0]&&!jobId&&<button type="button" className="assistant-text-button" onClick={()=>setJob(history.data!.jobs[0].id)}>核对最近任务与真实回执</button>}<p className="assistant-caption">个人聊天与上传附件按账号保留30天；业务审计独立保留。</p></div>}
      <ContextSummary context={context}/>{(error||queryError||blocked)&&<div className="assistant-notice" role="alert"><p>{error||(queryError instanceof Error?queryError.message:blocked?"计费信息待核对，暂时无法发起新的模型调用。":"助手连接异常，现有业务仍可使用。")}</p>{error&&canRetry&&<button type="button" className="assistant-text-button" disabled={sending} onClick={()=>{if(pendingSteer.current)void sendSteer(pendingSteer.current);else if(pending.current)void send(pending.current);}}>使用原请求标识重试并核对回执</button>}</div>}
      <div className="assistant-conversation" aria-live="polite">{!hasConversation&&<div className="assistant-welcome"><span className="assistant-welcome-mark"><Sparkles size={24}/></span><h3>今天想了解什么？</h3><p>从当前页面开始，<br/>让数据和测试进展更清楚。</p><div className="assistant-suggestions">{suggestions.map(({icon:Icon,text,hint})=><button key={text} type="button" onClick={()=>setSuggestion({text,key:Date.now()})}><span className="assistant-suggestion-icon"><Icon size={17}/></span><span><strong>{text}</strong><small>{hint}</small></span><ChevronDown className="assistant-suggestion-arrow" size={15}/></button>)}</div></div>}
        {messages.map(message=><section className={`assistant-message is-${message.role}`} key={message.id}>{message.role==="assistant"&&<span className="assistant-message-avatar"><Sparkles size={13}/></span>}<div className="assistant-message-content"><span className="assistant-message-name">{message.role==="user"?"你":"RP1 助手"}</span>{message.role==="assistant"?<Markdown text={message.body}/>:<p>{message.body}</p>}{message.attachments?.map(a=><span className="assistant-file-label" key={a.id}>附件：{a.name}</span>)}{message.cards.map((card,index)=><ResultCard key={index} card={card} context={message.context?.kind?message.context:context} navigate={navigate} onRestore={restoreSupplement}/>)}</div></section>)}
        {recentCompleted.filter(completed=>completed.id!==jobId&&!messages.some(message=>message.job_id===completed.id&&message.role==="assistant")).map(completed=><section className="assistant-message is-assistant" key={completed.id}><span className="assistant-message-avatar"><Sparkles size={13}/></span><div className="assistant-message-content"><span className="assistant-message-name">RP1 助手</span><Markdown text={completed.result!.reply}/>{completed.result!.cards.map((card,index)=><ResultCard key={index} card={card} context={completed.context} navigate={navigate} onRestore={restoreSupplement}/>)}</div></section>)}
        {!currentReplyInHistory&&(job.data?.result||streamText)&&<section className="assistant-message is-assistant"><span className="assistant-message-avatar"><Sparkles size={13}/></span><div className="assistant-message-content"><span className="assistant-message-name">RP1 助手{active?" · 正在回答":""}</span><Markdown text={job.data?.result?.reply||streamText}/>{job.data?.result?.cards.map((card,index)=><ResultCard key={index} card={card} context={job.data!.context} navigate={navigate} onRestore={restoreSupplement}/>)}</div></section>}
        {job.data&&!currentReplyInHistory&&!job.data.result&&!!pausedEvidence[jobId]?.cards.length&&<section className="assistant-message is-assistant"><div className="assistant-message-content"><span className="assistant-message-name">之前已取得的资料</span>{pausedEvidence[jobId].cards.map((card,index)=><ResultCard key={index} card={card} context={job.data.context} navigate={navigate} onRestore={restoreSupplement}/>)}</div></section>}
        {(active||sending)&&<div className="assistant-working"><span className="assistant-live-dot"/>{sending?"正在提交…":job.data?.state==="WAITING_PERMISSION"?"正在等待资料，需要你决定时会显示申请。之前的内容会保留。":job.data?.state==="QUEUED"?"排队中，稍后为你处理":lastTool?`${toolNames[lastTool.data.tool??""]??"正在查询证据"}${lastTool.data.status==="completed"?" · 已取得回执":"…"}`:"正在整理结果…"}</div>}{events.error&&active&&<p className="assistant-caption">进展连接中断，将继续核对任务回执；恢复后补读，不会重新执行任务。<button className="assistant-text-button" type="button" onClick={()=>void events.refetch()}>重新连接进展</button></p>}{queued.length>0&&<p className="assistant-caption">已排队 {queued.length} 条，将在前序任务结束后依次处理。</p>}
        {job.data?.result?.presentation&&<section className="assistant-result-summary" aria-label="诊断结论与下一步"><h3>结论</h3><p>{job.data.result.presentation.conclusion}</p>{job.data.result.presentation.evidence.length>0&&<><h4>依据</h4><ul>{job.data.result.presentation.evidence.map((v,i)=><li key={i}>{v}</li>)}</ul></>}{job.data.result.presentation.next_actions.length>0&&<><h4>现在可以做什么</h4>{job.data.result.presentation.next_actions.map((v,i)=><p key={i}>{v}</p>)}</>}{job.data.result.presentation.unknown.length>0&&<><h4>还不能确定什么</h4>{job.data.result.presentation.unknown.map((v,i)=><p key={i}>{v}</p>)}</>}</section>}
        {job.data&&<div className="assistant-task-state" aria-label="当前任务"><span title={`模型交互 ${job.data.rounds}/6`}>{job.data.receipt&&<CheckCircle2 size={12}/>}任务：{job.data.result?.completion==="partial"?"部分答复":stateNames[job.data.state]??job.data.state}{job.data.result?.termination_reason&&reasonNames[job.data.result.termination_reason]&&` · ${reasonNames[job.data.result.termination_reason]}`}{job.data.result?.business_status&&` · ${businessNames[job.data.result.business_status]}`}</span>{active&&<button type="button" className="assistant-text-button" onClick={()=>void assistantApi.cancel(jobId).then(()=>job.refetch()).catch((e:unknown)=>setError(e instanceof Error?e.message:"停止失败，请核对回执。"))}><Square size={10}/>停止并核对回执</button>}</div>}
        {outcome?.status==="needs_adoption"&&<section className="assistant-proposal"><strong>需要你采用参数</strong><p>以下建议尚未保存，请补充真实值或采用后执行。</p><BusinessProposal operations={outcome.operations??[]}/>{publish&&<label><input type="checkbox" checked={approved} onChange={e=>setApproved(e.target.checked)}/>我明确要求发布所示版本</label>}<button type="button" className="assistant-adopt-button" disabled={sending||(publish&&!approved)} onClick={()=>{setSending(true);void assistantApi.adoptSetup(outcome.proposal_id!,outcome.proposal_hash!,approved).then(()=>{if(alive.current){void job.refetch();void history.refetch();}}).catch((e:unknown)=>{if(alive.current)setError(e instanceof Error?e.message:"采用失败");}).finally(()=>{if(alive.current)setSending(false);});}}>采用所示参数并执行</button></section>}
        <LocalPermissions key={`permissions:${jobId}`} accountKey={accountKey} jobId={jobId} jobState={job.data?.state??"QUEUED"} enabled={capabilities.data?.features?.local_permissions===true||job.data?.state==="WAITING_PERMISSION"} onChanged={()=>{void job.refetch();if(thread)void history.refetch();if(capabilities.data?.features?.stream_events===true)void events.refetch();}}/>
        <SupportWorkspace key={jobId} accountKey={accountKey} jobId={jobId} terminal={!!job.data&&finished.has(job.data.state)} canPublish={capabilities.data?.features?.faq_publish===true} enabled={capabilities.data?.features?.support_workspace===true} onError={setError}/><div ref={bottom}/>
      </div><footer className="assistant-footer"><Composer key={composerEpoch} accountKey={accountKey} thread={thread} active={active} sending={sending} blocked={blocked} waitingPermission={job.data?.state==="WAITING_PERMISSION"} suggestion={suggestion} onSubmit={submit} onError={setError}/><div className="assistant-meta">{budget.data?<details className="assistant-budget"><summary><span className={`assistant-budget-dot${blocked?" is-blocked":""}`}/>今日 ¥{money(budget.data.spent_cny)}<span className="assistant-budget-limit"> / ¥100</span><ChevronDown size={11}/></summary><div className="assistant-budget-popover"><strong>站点每日预算</strong><p>费用上界 ¥{money(budget.data.spent_cny)}</p><p>已预留 ¥{money(budget.data.reserved_cny)}</p><p>北京时间零点重置。费用为保守上界，非供应商账单。</p></div></details>:<span/>}<span title="聊天按当前账号保存30天，业务操作证据独立长期保留。">聊天保留30天</span></div></footer>
    </aside>}
  </>;
}
