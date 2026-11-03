import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, ClipboardList, Monitor, PlayCircle, Power, RefreshCw, Server } from "lucide-react";
import { useAuth } from "../auth";
import { factoryApi, benchState, benchRunning, benchLabels, connectionFresh, receiptLabels, runReceipt, receiptForRun,
  type BenchState, type FactoryBench, type FactoryConnection, type FactoryMonitorData, type FactoryReceipt, type FactoryRun } from "../factoryApi";
import { formatDate, formatDuration } from "../navigation";
import { ErrorState, LoadingState } from "./Workbench";
import "./factoryMonitor.css";

type Navigate = (view: "archive" | "curves", id?: string, domain?: "FACTORY") => void;
const parts: Record<string, string> = { left_arm: "左臂", right_arm: "右臂", left_leg: "左腿", right_leg: "右腿", left_short_arm: "左短臂", right_short_arm: "右短臂", left_short_leg: "左短腿", right_short_leg: "右短腿", waist_hip: "腰髋" };
const priorities: Record<BenchState, number> = { fault: 0, running: 1, paused: 2, completed: 3, idle: 4, unknown: 5, offline: 6, unreported: 7 };
const actionStates = new Set(["upload_error", "parse_error", "rejected"]);
const activeStates = new Set(["running", "paused", "stopping", "fault"]);
const runLabels: Record<string, string> = { running: "运行中", paused: "已暂停", stopping: "正在结束", fault: "故障", completed: "已结束", idle: "空闲", connected: "已连接", unknown: "状态待确认" };

function partName(run: FactoryRun) { return parts[run.test_case ?? ""] || run.test_case || ""; }
function meaningful(run: FactoryRun) {
  return !!(run.execution_uuid || run.execution_code || run.test_id || run.serial_number ||
    run.sample_id || activeStates.has(run.state) || run.state === "completed" || run.active_seconds > 0 || run.completed_cycles > 0);
}
function runName(run: FactoryRun) { return run.serial_number || run.sample_id || partName(run) || "测试身份待核对"; }
function shortDate(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date) : "未上报";
}
function runMetrics(run: FactoryRun) {
  // Old placeholder sessions contain default zeroes, not measured run facts.
  if (!(run.execution_uuid || run.execution_code || run.test_id || run.active_seconds > 0 || run.completed_cycles > 0)) return "运行数据未上报";
  const duration = Number.isFinite(run.active_seconds) && run.active_seconds >= 0 ? formatDuration(run.active_seconds) : "时长未上报";
  const cycles = Number.isFinite(run.completed_cycles) && run.completed_cycles >= 0 ? `${run.completed_cycles} 循环` : "循环未上报";
  return `${duration} · ${cycles}`;
}
function operator(connection?: FactoryConnection) { return connection?.operator_display_name || connection?.operator_name || "操作员未上报"; }

function RunDetails({ run, connection, data, navigate }: { run: FactoryRun; connection: FactoryConnection; data: FactoryMonitorData; navigate: Navigate }) {
  const record = receiptForRun(run, data.receipts);
  const receipt = connection.scope_unconfirmed && !record ? null : runReceipt(run, data.receipts);
  return <div className="factory-run-detail">
    <div className="factory-run-detail-title"><strong>{runName(run)}</strong><span>{runLabels[run.state] || "状态待确认"}</span></div>
    <p>{[partName(run), run.case_code].filter(Boolean).join(" · ") || "测试信息待补充"}</p>
    <p>{runMetrics(run)}{receipt ? ` · ${receiptLabels[receipt]}` : ""}</p>
    <dl><div><dt>测试编号</dt><dd>{run.execution_code || run.test_id || "未上报"}</dd></div>
      {run.execution_uuid && <div><dt>执行标识</dt><dd><code>{run.execution_uuid}</code></dd></div>}</dl>
    {record && <button type="button" className="link-button" onClick={() => navigate("archive", record.archive_id, "FACTORY")}>查看记录<ArrowRight aria-hidden="true" /></button>}
  </div>;
}

function BenchCard({ bench, state, data, elapsed, live, navigate }: {
  bench: FactoryBench; state: BenchState; data: FactoryMonitorData; elapsed: number; live: boolean; navigate: Navigate;
}) {
  const connections = [...bench.connections].sort((a, b) => (Date.parse(b.last_seen_at) || 0) - (Date.parse(a.last_seen_at) || 0));
  const latest = connections[0];
  const current = connections.flatMap(connection => !connection.scope_conflict && connectionFresh(connection, data, elapsed, live).fresh
    ? connection.runs.filter(meaningful).map(run => ({ connection, run })) : []);
  current.sort((a, b) => Number(activeStates.has(b.run.state)) - Number(activeStates.has(a.run.state)));
  const primary = current[0];
  const conflict = connections.some(c => c.scope_conflict);
  const unconfirmed = connections.some(c => c.scope_unconfirmed);
  return <article className={`factory-bench-card is-${state}`} aria-label={`台架 ${bench.bench_id}`}>
    <header><h3 title={bench.name}>{bench.bench_id.toUpperCase()}</h3><span className={`factory-state ${state}`}>{benchLabels[state]}</span>{!bench.available && <span className="factory-disabled">已停用</span>}</header>
    <div className="factory-bench-main">
      {state === "unreported" ? <><Monitor aria-hidden="true" /><div><strong>等待首次上报</strong><p>{[bench.site_name, bench.lab_name].filter(Boolean).join(" · ")}</p></div></>
        : primary ? <><Server aria-hidden="true" /><div><strong className="factory-run-name">{runName(primary.run)}</strong><p>{[partName(primary.run), primary.run.case_code].filter(Boolean).join(" · ") || "测试信息待核对"}</p><p className="factory-reported-metrics">{runMetrics(primary.run)}</p></div></>
          : <><Server aria-hidden="true" /><div><strong>{state === "offline" ? "当前无在线上报" : state === "idle" ? "当前无运行测试" : "当前测试状态待确认"}</strong><p title={latest ? formatDate(latest.last_seen_at) : undefined}>最近上报 {latest ? shortDate(latest.last_seen_at) : "未上报"}</p><p>{operator(latest)}</p></div></>}
    </div>
    {primary && <p className="factory-card-reporter">{operator(primary.connection)} · {shortDate(primary.connection.last_seen_at)}{current.length > 1 ? ` · 共 ${current.length} 项上报` : ""}</p>}
    {conflict && <p className="factory-card-notice">上报分区冲突，其他分区测试已隐藏</p>}
    {!conflict && unconfirmed && <p className="factory-card-notice">本次上报分区待确认</p>}
    {connections.some(c => c.plc.driver === "mock") && <span className="factory-simulation">模拟 PLC</span>}
    {!!connections.length && <details className="factory-bench-details"><summary>{primary ? "查看上报详情" : "最近上报详情"}</summary>
      <p className="factory-detail-scope">{primary ? "当前上报；现场计数为当前页，不进入正式结果统计。" : "最后上报内容（历史快照），不代表当前运行。"}</p>
      {connections.map(connection => <div className="factory-connection-detail" key={connection.installation_id}>
        <strong>{connection.station_name || "上位机"} · {operator(connection)}</strong>
        <p>最近连接：{formatDate(connection.last_seen_at)}</p><p>采样时间：{formatDate(connection.snapshot_at)}</p>
        {connection.scope_unconfirmed && <p>分区未上报，按已登记工厂台架展示；归档分区待确认。</p>}
        {connection.scope_conflict ? <p>明确属于其他分区的测试信息已隐藏。</p>
          : connection.runs.filter(meaningful).length ? connection.runs.filter(meaningful).map(run => <RunDetails key={run.session_key} run={run} connection={connection} data={data} navigate={navigate} />)
            : <p>没有可识别的测试记录。</p>}
      </div>)}
    </details>}
  </article>;
}

function RecordTask({ record, navigate }: { record: FactoryReceipt; navigate: Navigate }) {
  return <div className="factory-task-row">
    <div className="factory-task-identity"><strong title={record.serial_number || record.test_name}>{record.serial_number || record.test_name || "待核对记录"}</strong><span>{[record.bench_id, record.test_name].filter(Boolean).join(" · ")}</span></div>
    <span className={`factory-receipt ${actionStates.has(record.receipt_state) ? "danger" : ""}`}>{receiptLabels[record.receipt_state]}</span>
    <button type="button" className="link-button factory-task-action" aria-label={`查看 ${record.serial_number || record.test_name || record.archive_code} 的记录`} onClick={() => navigate("archive", record.archive_id, "FACTORY")}>查看记录<ArrowRight aria-hidden="true" /></button>
  </div>;
}

export function FactoryMonitor({ navigate }: { navigate: Navigate }) {
  const { session } = useAuth();
  const allowed = ["SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"].includes(session?.user.role ?? "");
  const [cursor, setCursor] = useState<string | null>(null);
  const [before, setBefore] = useState<number | null>(null);
  const [automatic, setAutomatic] = useState(true);
  const [filter, setFilter] = useState("all");
  const [now, setNow] = useState(Date.now);
  const query = useQuery({ queryKey: ["factory-monitor", session?.user.id, cursor, before], queryFn: ({ signal }) => factoryApi.monitor(cursor, before, signal), enabled: allowed, refetchInterval: automatic ? 5000 : false });
  useEffect(() => { const timer = window.setInterval(() => setNow(Date.now()), 1000); return () => window.clearInterval(timer); }, []);
  if (!allowed) return <p className="archive-scope-note">现场监控向工厂审批员、执行员和管理员开放；下方可查看有权访问的正式记录。</p>;
  if (query.isPending) return <LoadingState label="正在读取工厂现场" />;
  if (!query.data) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const data = query.data;
  const elapsed = Math.max(0, now - query.dataUpdatedAt);
  const live = automatic && !query.isError && elapsed < 15_000;
  const state = (bench: FactoryBench) => benchState(bench, data, elapsed, live);
  const visible = data.items.filter(b => filter === "all" || (filter === "running" ? benchRunning(b, data, elapsed, live) : state(b) === filter))
    .sort((a, b) => priorities[state(a)] - priorities[state(b)] || a.bench_id.localeCompare(b.bench_id, "zh-CN", { numeric: true }));
  const online = data.items.filter(b => b.connections.some(c => connectionFresh(c, data, elapsed, live).online)).length;
  const offline = data.items.filter(b => state(b) === "offline");
  const completed = data.items.flatMap(bench => bench.connections.flatMap(c => connectionFresh(c, data, elapsed, live).fresh && !c.scope_conflict && !c.scope_unconfirmed
    ? c.runs.filter(r => meaningful(r) && r.state === "completed" && ["awaiting_upload", "identity_unknown"].includes(runReceipt(r, [...data.receipts, ...data.work_items]) ?? "")).map(run => ({ bench, run })) : []));
  const anomalies = data.items.filter(b => state(b) === "fault" || b.connections.some(c => connectionFresh(c, data, elapsed, live).online && (c.scope_conflict || !b.available)));
  const failed = data.work_items.filter(r => actionStates.has(r.receipt_state));
  const work = [...data.work_items].sort((a, b) => Number(actionStates.has(b.receipt_state)) - Number(actionStates.has(a.receipt_state)) || (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
  const summary = [
    { label: "在线台架", count: live ? `${online} / ${data.items.length}` : "—", icon: Server },
    { label: "运行中", count: live ? data.items.filter(b => benchRunning(b, data, elapsed, live)).length : "—", icon: PlayCircle },
    { label: "待处理记录", count: `${completed.length + work.length}${data.work_next_cursor ? "+" : ""}`, icon: ClipboardList },
    { label: "离线台架", count: live ? offline.length : "—", icon: Power },
  ];
  return <div className="factory-monitor">
    {!live && <p className="factory-stale" role="status">当前显示历史快照，实时状态待确认。{query.isError ? "刷新失败，请重试。" : !automatic ? "自动刷新已暂停。" : "状态读取已超时。"}</p>}
    <section className="factory-live-summary" aria-label="本页现场概况">{summary.map(({ label, count, icon: Icon }) => <div key={label}><Icon aria-hidden="true" /><div><span>{label}</span><strong>{count}</strong></div></div>)}</section>
    <section className="factory-benches" aria-labelledby="factory-benches-heading">
      <header className="factory-section-heading"><h2 id="factory-benches-heading">现场台架</h2>
        <label className="factory-state-filter"><span className="factory-sr-only">台架状态</span><select value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部台架 {data.items.length}</option>{Object.entries(benchLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <div className="factory-refresh"><span title={formatDate(data.as_of_at)}>更新于 {shortDate(data.as_of_at)}</span><label><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} />自动刷新</label><button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCw aria-hidden="true" />{query.isFetching ? "读取中" : "刷新"}</button></div>
      </header>
      {visible.length ? <div className="factory-bench-grid">{visible.map(bench => <BenchCard key={bench.station_id} bench={bench} state={state(bench)} data={data} elapsed={elapsed} live={live} navigate={navigate} />)}</div>
        : <p className="factory-quiet-empty">{data.items.length ? "没有符合条件的工厂台架" : "尚无已登记的工厂台架"}</p>}
      <footer className="factory-pages"><span>本页 {data.items.length} 台 · 现场状态不计入正式结果统计</span>{cursor && <button type="button" onClick={() => setCursor(null)}>台架第一页</button>}{data.next_cursor && <button type="button" disabled={query.isFetching} onClick={() => setCursor(data.next_cursor)}>下一页台架</button>}</footer>
    </section>
    <div className="factory-work-panels">
      <section className="factory-work-card" aria-labelledby="factory-work-heading"><header><h2 id="factory-work-heading">待处理 <span className="factory-count">{completed.length + work.length}</span></h2></header>
        {completed.map(({ bench, run }) => <div className="factory-task-row" key={`${bench.station_id}:${run.session_key}`}><div className="factory-task-identity"><strong>{runName(run)}</strong><span>{bench.bench_id} · {partName(run) || "测试身份待核对"}</span></div><span className="factory-receipt">{receiptLabels[runReceipt(run, data.receipts)!]}</span></div>)}
        {work.slice(0, 5).map(record => <RecordTask key={record.archive_id} record={record} navigate={navigate} />)}
        {work.length > 5 && <details className="factory-more-work"><summary>查看其余 {work.length - 5} 条待处理记录</summary>{work.slice(5).map(record => <RecordTask key={record.archive_id} record={record} navigate={navigate} />)}</details>}
        {!completed.length && !work.length ? <p className="factory-quiet-empty">当前没有待处理记录</p> : work.some(r => r.receipt_state === "uploading") || completed.length ? <p className="factory-work-help">请在原上位机继续归档；完整编号可进入记录查看。</p> : null}
        {(before || data.work_next_cursor) && <div className="factory-pages">{before && <button type="button" onClick={() => setBefore(null)}>待处理第一页</button>}{data.work_next_cursor && <button type="button" disabled={query.isFetching} onClick={() => setBefore(data.work_next_cursor)}>更多待处理记录</button>}</div>}
      </section>
      <section className="factory-work-card factory-attention" aria-labelledby="factory-attention-heading"><header><h2 id="factory-attention-heading">状态提醒</h2></header>
        {anomalies.map(bench => <div className="factory-attention-item danger" key={bench.station_id}><AlertTriangle aria-hidden="true" /><div><strong>{bench.bench_id.toUpperCase()} · {state(bench) === "fault" ? "故障" : !bench.available ? "台架已停用" : "上报分区冲突"}</strong><p>请核对现场情况与上报详情。</p></div></div>)}
        {failed.map(record => <div className="factory-attention-item danger" key={record.archive_id}><AlertTriangle aria-hidden="true" /><div><button type="button" className="link-button" onClick={() => navigate("archive", record.archive_id, "FACTORY")}>{record.serial_number || record.test_name} · {receiptLabels[record.receipt_state]}</button><p>{record.processing_error || "进入记录查看原因及处理历史"}</p></div></div>)}
        {!!offline.length && <div className="factory-attention-item"><AlertTriangle aria-hidden="true" /><div><strong>{offline.length} 台台架已离线</strong><p>{offline.map(b => b.bench_id.toUpperCase()).join("、")}</p></div></div>}
        {offline.length ? <p className="factory-work-help">最近上报内容可在台架详情查看。</p> : !anomalies.length && !failed.length ? <p className="factory-quiet-empty">{live ? "本页暂无现场或归档异常" : "最新现场状态待确认"}</p> : null}
      </section>
    </div>
  </div>;
}
