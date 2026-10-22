import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowUp, ArrowUpRight, CheckCircle2, ChevronDown, Clock3, History, Layers, Link2, ListChecks, Plus, Radio, Sparkles, Square, X } from "lucide-react";
import { assistantApi, type PageContext, type AssistantCard } from "../assistantApi";
import { requestId } from "../client";
import { locationHref, type View } from "../navigation";
import "../assistant.css";

const finished = new Set(["COMPLETED", "FAILED", "TIMED_OUT", "INTERRUPTED", "CANCELLED"]);
const targets = new Set(["asset", "test-case", "planning", "archive", "curves", "benches", "stations", "execution"]);
const viewNames: Record<string, string> = { curves: "曲线分析", archive: "归档", planning: "项目与计划", asset: "样品", benches: "台架", stations: "工位", execution: "测试执行", "test-case": "测试用例", dashboard: "总览" };
const stateNames: Record<string, string> = { QUEUED: "排队中", RUNNING: "正在处理", COMPLETED: "对话已结束", FAILED: "未完成", TIMED_OUT: "已超时", INTERRUPTED: "已中断", CANCELLED: "已停止" };
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
function ResultCard({ card, context, navigate }: { card: AssistantCard; context: PageContext; navigate: (view: View, id: string | null) => void }) {
  const href = cardHref(card, context);
  const series = Array.isArray(card.data.series) ? card.data.series[0] as Record<string, unknown> | undefined : undefined;
  const range = card.range && typeof card.range === "object" ? card.range as { start?: number; end?: number } : null;
  const rangeText = range && range.start !== undefined && range.end !== undefined ? `${range.start}–${range.end} s` : typeof card.range === "string" ? card.range : "";
  const unit = typeof series?.unit === "string" ? series.unit : card.type === "station_status" ? "工位" : card.unit;
  return <article className="assistant-card">
    <div className="assistant-card-heading"><span className="assistant-card-icon"><CheckCircle2 size={15}/></span><strong>{card.title}</strong></div>
    {rangeText && <p className="assistant-card-range">{rangeText}{unit && <span>单位：{unit}</span>}</p>}
    {series && typeof series.mean === "number" && <div className="assistant-stats">{[["平均值", series.mean], ["最小值", series.min], ["最大值", series.max]].map(([label, value]) => typeof value === "number" && <div key={String(label)}><span>{String(label)}</span><strong>{amount(value)}<small>{unit}</small></strong></div>)}</div>}
    <details className="assistant-evidence"><summary><Link2 size={12}/>来源与明细<ChevronDown size={12}/></summary><div><p>来源：{card.source}</p><p>更新时间：{card.updated_at}</p>{card.scope && <p>{card.scope}</p>}<pre>{JSON.stringify(card.data, null, 2)}</pre></div></details>
    <div className="assistant-card-footer"><span title={card.updated_at}><Clock3 size={11}/>{when(card.updated_at)} 更新</span>{href && <a aria-label="打开对应对象" href={href} onClick={(event) => { event.preventDefault(); navigate(card.target!.view as View, card.target!.id); }}>查看{card.target!.view === "archive" ? "归档" : card.target!.view === "planning" ? "计划" : "对象"}<ArrowUpRight size={13}/></a>}</div>
  </article>;
}

export function AssistantPanel({ accountKey, context, navigate }: { accountKey: string; context: PageContext; navigate: (view: View, id: string | null) => void }) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false), [thread, setThread] = useState(""), [jobId, setJob] = useState("");
  const [input, setInput] = useState(""), [error, setError] = useState(""), [sending, setSending] = useState(false), [approved, setApproved] = useState(false), [showHistory, setShowHistory] = useState(false), [canRetry, setCanRetry] = useState(false);
  const pending = useRef<{ request_key: string; thread_id?: string; message: string; context: PageContext } | null>(null);
  const controller = useRef<AbortController | null>(null), textarea = useRef<HTMLTextAreaElement | null>(null), bottom = useRef<HTMLDivElement | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  useEffect(() => { const show = () => setOpen(true); window.addEventListener("rp1-open-assistant", show); return () => window.removeEventListener("rp1-open-assistant", show); }, []);
  const threads = useQuery({ queryKey: ["assistant-threads", accountKey], queryFn: ({ signal }) => assistantApi.threads(signal), enabled: open, retry: false });
  const history = useQuery({ queryKey: ["assistant-history", accountKey, thread], queryFn: ({ signal }) => assistantApi.history(thread, signal), enabled: open && !!thread, retry: false });
  const job = useQuery({ queryKey: ["assistant-job", accountKey, jobId], queryFn: ({ signal }) => assistantApi.job(jobId, signal), enabled: !!jobId, retry: false, refetchInterval: (query) => query.state.data && finished.has(query.state.data.state) ? false : 1000 });
  const budget = useQuery({ queryKey: ["assistant-budget", accountKey], queryFn: ({ signal }) => assistantApi.budget(signal), enabled: open, retry: false, refetchInterval: open ? 5000 : false });
  const active = !!jobId && (!job.data || !finished.has(job.data.state));
  const blocked = budget.data?.blocked || budget.data?.pricing_verified === false;
  useEffect(() => {
    if (!job.data || !finished.has(job.data.state)) return;
    void cache.invalidateQueries({ queryKey: ["assistant-history", accountKey, job.data.thread_id] });
    void cache.invalidateQueries({ queryKey: ["assistant-threads", accountKey] });
    if (job.data.receipt) { void cache.invalidateQueries({ queryKey: ["setup-options"] }); void cache.invalidateQueries({ queryKey: ["setup-campaigns"] }); void cache.invalidateQueries({ queryKey: ["setup-catalog"] }); void cache.invalidateQueries({ queryKey: ["assets"] }); }
  }, [job.data, cache, accountKey]);
  useEffect(() => { if (open) bottom.current?.scrollIntoView?.({ block: "end" }); }, [open, history.data?.messages.length, job.data?.result?.reply]);

  async function send(payload: NonNullable<typeof pending.current>) {
    controller.current?.abort(); const abort = new AbortController(); controller.current = abort; setSending(true); setError(""); setCanRetry(false);
    try { const result = await assistantApi.submitJob(payload, abort.signal); if (abort.signal.aborted) return; setThread(result.thread_id); setJob(result.id); setInput(""); setApproved(false); pending.current = null; await threads.refetch(); }
    catch (e) { if (!abort.signal.aborted) { setError(e instanceof Error ? e.message : "发送失败，可用原请求标识重试并查询回执。"); setCanRetry(pending.current !== null); } }
    finally { if (!abort.signal.aborted) setSending(false); }
  }
  function submit(event: FormEvent) {
    event.preventDefault(); if (!input.trim() || active || sending || blocked) return;
    const payload = { request_key: requestId(), ...(thread ? { thread_id: thread } : {}), message: input.trim(), context: structuredClone(context) };
    pending.current = payload; void send(payload);
  }
  function newThread() { if (active || sending) return; setThread(""); setJob(""); setInput(""); setError(""); setApproved(false); pending.current = null; setShowHistory(false); textarea.current?.focus(); }
  const outcome = job.data?.result?.setup;
  const publish = outcome?.operations?.some((op) => op.kind === "case_publish" || op.data.publish === true);
  const queryError = threads.error ?? history.error ?? job.error ?? budget.error;
  const messages = history.data?.messages ?? [];
  const currentReplyInHistory = messages.some((message) => message.job_id === jobId && message.role === "assistant");
  const hasConversation = messages.length > 0 || !!jobId;
  return <>
    <button className="assistant-entry" type="button" aria-expanded={open} aria-controls="rp1-assistant-panel" onClick={() => setOpen(!open)}><Sparkles size={17}/><span>助手</span>{active && <span className="assistant-live-dot"/>}</button>
    {open && <aside id="rp1-assistant-panel" className="assistant-panel" aria-label="RP1助手">
      <header className="assistant-header"><span className="assistant-brand-mark"><Sparkles size={20}/></span><div className="assistant-heading"><h2>RP1 助手</h2><span>查询、分析与建档</span></div><div className="assistant-header-actions"><button className={`assistant-icon-button${showHistory ? " is-selected" : ""}`} type="button" aria-label="查看历史会话" title="历史会话" aria-expanded={showHistory} onClick={() => setShowHistory(!showHistory)}><History size={17}/></button><button className="assistant-icon-button" type="button" aria-label="新对话" title="新对话" disabled={active || sending} onClick={newThread}><Plus size={19}/></button><span className="assistant-toolbar-divider"/><button className="assistant-icon-button" type="button" aria-label="收起" title="收起" onClick={() => setOpen(false)}><X size={18}/></button></div></header>
      {showHistory && <div className="assistant-history"><label htmlFor="assistant-history-select">历史会话</label><select id="assistant-history-select" value={thread} disabled={active || sending} onChange={(event) => { setThread(event.target.value); setJob(""); setError(""); pending.current = null; }}><option value="">新会话</option>{threads.data?.map((item) => <option value={item.id} key={item.id}>{when(item.updated_at)} · {item.id.slice(0, 8)}</option>)}</select>{history.data?.jobs[0] && !jobId && <button type="button" className="assistant-text-button" onClick={() => setJob(history.data!.jobs[0].id)}>核对最近任务与真实回执</button>}</div>}
      <ContextSummary context={context}/>
      {(error || queryError || blocked) && <div className="assistant-notice" role="alert"><p>{error || (queryError instanceof Error ? queryError.message : blocked ? "计费信息待核对，暂时无法发起新的模型调用。" : "助手连接异常，现有业务仍可使用。")}</p>{error && canRetry && <button type="button" className="assistant-text-button" disabled={sending} onClick={() => { if (pending.current) void send(pending.current); }}>使用原请求标识重试并核对回执</button>}</div>}
      <div className="assistant-conversation" aria-live="polite">
        {!hasConversation && <div className="assistant-welcome"><span className="assistant-welcome-mark"><Sparkles size={24}/></span><h3>今天想了解什么？</h3><p>从当前页面开始，<br/>让数据和测试进展更清楚。</p><div className="assistant-suggestions">{suggestions.map(({ icon: Icon, text, hint }) => <button key={text} type="button" onClick={() => { setInput(text); textarea.current?.focus(); }}><span className="assistant-suggestion-icon"><Icon size={17}/></span><span><strong>{text}</strong><small>{hint}</small></span><ChevronDown className="assistant-suggestion-arrow" size={15}/></button>)}</div></div>}
        {messages.map((message) => <section className={`assistant-message is-${message.role}`} key={message.id}>{message.role === "assistant" && <span className="assistant-message-avatar"><Sparkles size={13}/></span>}<div className="assistant-message-content"><span className="assistant-message-name">{message.role === "user" ? "你" : "RP1 助手"}</span><p>{message.body}</p>{message.cards.map((card, index) => <ResultCard key={index} card={card} context={message.context?.kind ? message.context : context} navigate={navigate}/>)}</div></section>)}
        {job.data && !currentReplyInHistory && job.data.result && <section className="assistant-message is-assistant"><span className="assistant-message-avatar"><Sparkles size={13}/></span><div className="assistant-message-content"><span className="assistant-message-name">RP1 助手</span><p>{job.data.result.reply}</p>{job.data.result.cards.map((card, index) => <ResultCard key={index} card={card} context={job.data!.context} navigate={navigate}/>)}</div></section>}
        {(active || sending) && <div className="assistant-working"><span className="assistant-live-dot"/>{sending ? "正在提交…" : job.data?.state === "QUEUED" ? "排队中，稍后为你处理" : "正在整理结果…"}</div>}
        {job.data && <div className="assistant-task-state" aria-label="当前任务"><span title={`模型交互 ${job.data.rounds}/6`}>{job.data.receipt && <CheckCircle2 size={12}/>}任务：{stateNames[job.data.state] ?? job.data.state}{job.data.result?.business_status && ` · ${businessNames[job.data.result.business_status]}`}</span>{active && <button type="button" className="assistant-text-button" onClick={() => void assistantApi.cancel(jobId).then(() => job.refetch()).catch((e: unknown) => setError(e instanceof Error ? e.message : "停止失败，请核对回执。"))}><Square size={10}/>停止并核对回执</button>}</div>}
        {outcome?.status === "needs_adoption" && <section className="assistant-proposal"><strong>需要你采用参数</strong><p>以下建议尚未保存，请补充真实值或采用后执行。</p><pre>{JSON.stringify(outcome.operations, null, 2)}</pre>{publish && <label><input type="checkbox" checked={approved} onChange={(event) => setApproved(event.target.checked)}/>我明确要求发布所示版本</label>}<button type="button" className="assistant-adopt-button" disabled={sending || (publish && !approved)} onClick={() => { setSending(true); void assistantApi.adoptSetup(outcome.proposal_id!, outcome.proposal_hash!, approved).then(() => { void job.refetch(); void history.refetch(); }).catch((e: unknown) => setError(e instanceof Error ? e.message : "采用失败")).finally(() => setSending(false)); }}>采用所示参数并执行</button></section>}
        <div ref={bottom}/>
      </div>
      <footer className="assistant-footer"><form onSubmit={submit}><div className="assistant-composer"><label htmlFor="assistant-message" className="assistant-sr-only">消息</label><textarea id="assistant-message" ref={textarea} required rows={2} maxLength={8000} placeholder="向助手提问，或描述你要做的事…" value={input} onChange={(event) => setInput(event.target.value)} disabled={active || sending} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}/><div className="assistant-composer-actions"><span><Link2 size={12}/>已关联当前页面</span><button className="assistant-send" type="submit" aria-label="发送" title="发送" disabled={active || sending || !input.trim() || blocked}><ArrowUp size={18}/></button></div></div><p className="assistant-input-hint">Enter 发送<span>Shift + Enter 换行</span></p></form>
        <div className="assistant-meta">{budget.data ? <details className="assistant-budget"><summary><span className={`assistant-budget-dot${blocked ? " is-blocked" : ""}`}/>今日 ¥{money(budget.data.spent_cny)}<span className="assistant-budget-limit"> / ¥100</span><ChevronDown size={11}/></summary><div className="assistant-budget-popover"><strong>站点每日预算</strong><p>费用上界 ¥{money(budget.data.spent_cny)}</p><p>已预留 ¥{money(budget.data.reserved_cny)}</p><p>北京时间零点重置。费用为保守上界，非供应商账单。</p></div></details> : <span/>}<span title="聊天按当前账号保存30天，业务操作证据独立长期保留。">聊天保留30天</span></div>
      </footer>
    </aside>}
  </>;
}
