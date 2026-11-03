import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuth } from "../auth";
import { factoryApi, benchState, benchRunning, benchLabels, connectionFresh, receiptLabels, runReceipt, type FactoryBench } from "../factoryApi";
import { formatDate, formatDuration } from "../navigation";
import { EmptyState, ErrorState, LoadingState, Panel } from "./Workbench";
import "./factoryMonitor.css";

type Navigate = (view: "archive" | "curves", id?: string, domain?: "FACTORY") => void;
const parts: Record<string, string> = { left_arm: "左臂", right_arm: "右臂", left_leg: "左腿", right_leg: "右腿", left_short_arm: "左短臂", right_short_arm: "右短臂" };
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
  const visible = data.items.filter(b => filter === "all" || (filter === "running" ? benchRunning(b, data, elapsed, live) : state(b) === filter));
  const online = data.items.filter(b => b.connections.some(c => connectionFresh(c, data, elapsed, live).online)).length;
  const anomalies = data.items.filter(b => ["fault", "unknown", "offline"].includes(state(b)) || !b.available && b.connections.some(c => connectionFresh(c, data, elapsed, live).online));
  const completed = data.items.flatMap(b => b.connections.flatMap(c => connectionFresh(c, data, elapsed, live).fresh && !c.scope_conflict && !c.scope_unconfirmed ? c.runs.filter(r => r.state === "completed" && ["awaiting_upload", "identity_unknown"].includes(runReceipt(r, data.receipts) ?? "")).map(r => ({ bench: b, run: r })) : []));
  return <div className="factory-monitor">
    <div className="factory-monitor-toolbar"><h2>工厂现场</h2><label><input type="checkbox" checked={automatic} onChange={e => setAutomatic(e.target.checked)} />每 5 秒刷新</label><button type="button" disabled={query.isFetching} onClick={() => void query.refetch()}>刷新现场</button><span>最近读取：{formatDate(data.as_of_at)}</span></div>
    {!live && <p className="factory-stale" role="status">当前显示历史快照，实时状态待确认。{query.isError ? "刷新失败，请重试。" : !automatic ? "自动刷新已暂停。" : "状态读取已超时。"}</p>}
    <section className="factory-live-summary" aria-label="本页现场概况">{[["在线台架", live ? online : "—"], ["运行中", live ? data.items.filter(b => benchRunning(b, data, elapsed, live)).length : "—"], ["在线空闲", live ? data.items.filter(b => state(b) === "idle").length : "—"], ["故障", live ? data.items.filter(b => state(b) === "fault").length : "—"], ["已完成待上传核对", live ? completed.length : "—"]].map(([label, count]) => <div key={label}><span>{label}</span><strong>{count}</strong></div>)}</section>
    <Panel title="在线台架与当前测试" description="仅显示已登记工厂归属的台架。现场计数为当前页，运行状态来自上位机上报，不进入正式结果统计。">
      <label className="factory-state-filter">台架状态<select value={filter} onChange={e => setFilter(e.target.value)}><option value="all">全部</option>{Object.entries(benchLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      {visible.length ? <div className="table-scroll"><table className="data-table factory-live-table"><thead><tr><th>台架 / 位置</th><th>状态</th><th>当前测试与 SN</th><th>运行与记录</th><th>操作员 / 最新上报</th></tr></thead><tbody>{visible.map(bench => <tr key={bench.station_id}>
        <td><strong>{bench.bench_id} · {bench.name}</strong><small>{bench.site_name} / {bench.lab_name}</small>{!bench.available && <small>台架已停用</small>}</td>
        <td><span className={`factory-state ${state(bench)}`}>{benchLabels[state(bench)]}</span>{bench.connections.some(c => c.scope_conflict) && <small>上报分区冲突，需核对</small>}</td>
        <td colSpan={1}>{bench.connections.length ? bench.connections.map(c => <div className="factory-connection" key={c.installation_id}>{!connectionFresh(c, data, elapsed, live).fresh && <small>最后上报内容 · 当前状态待确认</small>}{c.scope_unconfirmed && <small>分区未上报，按已登记工厂台架展示；归档分区待确认</small>}{c.scope_conflict ? <small>明确属于其他分区的测试信息已隐藏</small> : c.runs.length ? c.runs.map(run => <div key={run.session_key}><strong>{run.execution_code || run.test_id || "测试编号未上报"}</strong><small>SN：{run.serial_number || "未上报"} · {parts[run.test_case ?? ""] || run.test_case || "部位未上报"}</small><small>{run.case_code || "用例未上报"}</small></div>) : <small>暂无工厂测试会话</small>}</div>) : "—"}</td>
        <td>{bench.connections.map(c => <div key={c.installation_id}>{!c.scope_conflict && c.runs.map(run => { const record = data.receipts.find(r => r.execution_uuid === run.execution_uuid); const receipt = c.scope_unconfirmed && !record ? null : runReceipt(run, data.receipts); return <div className="factory-run-progress" key={run.session_key}><strong>{formatDuration(run.active_seconds)} · {run.completed_cycles} 循环</strong><small>目标进度：未上报</small>{receipt && <small>{receiptLabels[receipt]}</small>}{record && <button type="button" className="link-button" onClick={() => navigate("archive", record.archive_id, "FACTORY")}>查看记录</button>}</div>; })}</div>)}</td>
        <td>{bench.connections.map(c => <div key={c.installation_id}><strong>{c.operator_display_name || c.operator_name}</strong><small>{formatDate(c.last_seen_at)}</small>{c.plc.driver === "mock" && <small>模拟 PLC</small>}</div>)}</td>
      </tr>)}</tbody></table></div> : <EmptyState title={data.items.length ? "没有符合条件的工厂台架" : "尚无已登记的工厂台架"} description="工厂归属由管理员登记；没有上报不会显示为在线空闲。" />}
      <div className="factory-pages">{cursor && <button type="button" onClick={() => setCursor(null)}>台架第一页</button>}{data.next_cursor && <button type="button" disabled={query.isFetching} onClick={() => setCursor(data.next_cursor)}>下一页台架</button>}<span>本页 {data.items.length} 台</span></div>
    </Panel>
    <div className="factory-work-panels"><Panel title="完成待处理" description="先核对已结束但平台尚无归档的测试，再处理已上传记录。这里只匹配同一次执行 UUID，不按 SN 合并复测。">
      {completed.map(({ bench, run }) => <div className="factory-work-item" key={`${bench.station_id}:${run.session_key}`}><strong>{bench.bench_id} · {run.serial_number || run.test_id || "身份待核对"}</strong><span>{receiptLabels[runReceipt(run, data.receipts)!]}</span></div>)}
      {data.work_items.map(record => <div className="factory-work-item" key={record.archive_id}><button type="button" className="link-button" onClick={() => navigate("archive", record.archive_id, "FACTORY")}>{record.archive_code} · {record.serial_number || "SN 未记录"}</button><span>{record.bench_id} · {receiptLabels[record.receipt_state]}</span></div>)}
      {!completed.length && !data.work_items.length && <p>当前没有可见的待处理记录。</p>}
      <div className="factory-pages">{before && <button type="button" onClick={() => setBefore(null)}>待处理第一页</button>}{data.work_next_cursor && <button type="button" disabled={query.isFetching} onClick={() => setBefore(data.work_next_cursor)}>更多待处理记录</button>}</div>
    </Panel><Panel title="异常提示" description="显示本页当前故障、离线、分区冲突，以及待处理记录中的上传或解析问题。">
      {anomalies.map(bench => <div className="factory-work-item" key={bench.station_id}><strong>{bench.bench_id} · {benchLabels[state(bench)]}</strong><span>{!bench.available ? "台架已停用；" : ""}{bench.connections.some(c => c.scope_conflict) ? "上报分区冲突；" : ""}最近采样：{formatDate(bench.connections.map(c => c.snapshot_at).sort().at(-1))}</span><small>上报操作员：{[...new Set(bench.connections.map(c => c.operator_display_name || c.operator_name))].join("、") || "未上报"}</small></div>)}
      {data.work_items.filter(r => ["upload_error", "parse_error", "rejected"].includes(r.receipt_state)).map(record => <div className="factory-work-item" key={record.archive_id}><button type="button" className="link-button" onClick={() => navigate("archive", record.archive_id, "FACTORY")}>{record.archive_code} · {receiptLabels[record.receipt_state]}</button><span>{record.processing_error || "进入记录查看原因及处理历史"}</span></div>)}
      {!anomalies.length && !data.work_items.some(r => ["upload_error", "parse_error", "rejected"].includes(r.receipt_state)) && <p>{live ? "本页暂无上述异常。" : "最新现场状态待确认。"}</p>}
    </Panel></div>
  </div>;
}
