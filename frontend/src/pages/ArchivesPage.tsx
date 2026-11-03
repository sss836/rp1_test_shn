import {FactoryHandoff} from "../components/FactoryHandoff";
import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, ChartNoAxesCombined, ChevronDown, FileCheck2, FileUp, Search, Download } from "lucide-react";
import { archivesApi, archiveDateFilters, archiveDomain, archiveStartedAt, dataDomainLabels, dataDomainSourceLabels, archiveTitle, archiveStation, formatArchiveTimestamp, missingArchiveSubmissionInfo, archiveProcessingLabels, archiveUploadLabels, verdictLabels, type Archive, type ArchiveContext, type ArchiveDataDomain, type ArchiveFilters, type ArchiveVerdict } from "../archiveApi";
import { useAuth } from "../auth";
import { setupApi } from "../setupApi";
import { formatDate, formatDuration } from "../navigation";
import { EmptyState, ErrorState, LoadingState, PageTitle, Panel } from "../components/Workbench";
import { StatusMark } from "../components/StatusMark";
import { HistoricalIntakeForm } from "./HistoricalIntakeForm";

type Navigate = (view: "import" | "archives" | "factory" | "archive" | "curves" | "execution" | "submissions", id?: string, domain?: ArchiveDataDomain) => void;
const machineLabels: Record<string, string> = { completed: "执行完成", failed: "故障结束", stopped: "人工停止", interrupted: "执行中断", unknown: "未知" };
const displayValue = (value: unknown) => value === null || value === undefined ? "未记录" : String(value);
const declarationFields = [
  ["active_seconds", "人工申报运行时长（秒）", 315360000, "0.001"],
  ["completed_cycles", "人工申报完成循环", 2147483647, "1"],
  ["exception_count", "人工申报异常次数", 2147483647, "1"],
] as const;
function localDateTimeInput(value: string | null | undefined) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = (part: number, length = 2) => String(part).padStart(length, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}.${pad(date.getMilliseconds(), 3)}`;
}

export function ArchiveStatus({ item }: { item: Archive }) {
  const domain = archiveDomain(item);
  return <div className="archive-statuses"><StatusMark tone={domain === "UNCLASSIFIED" ? "warning" : "muted"} label={dataDomainLabels[domain]} /><StatusMark tone={item.upload_state === "CORRUPT" ? "danger" : item.upload_state === "COMPLETE" ? "normal" : "warning"} label={archiveUploadLabels[item.upload_state]} /><StatusMark tone={item.processing_state === "FAILED" ? "danger" : item.processing_state === "READY" ? "normal" : "muted"} label={archiveProcessingLabels[item.processing_state]} />{<StatusMark tone={item.classification_state === "UNCLASSIFIED" ? "warning" : "muted"} label={item.classification_state === "UNCLASSIFIED" ? "待关联正式用例" : "已关联正式用例"} />}<StatusMark tone={item.submission_status === "APPROVED" ? "normal" : item.submission_status === "REJECTED" ? "danger" : "muted"} label={item.submission_status === "APPROVED" ? "已批准" : item.submission_status === "REJECTED" ? "已驳回" : item.submission_status === "PENDING" ? "待审核" : "未送审"} /></div>;
}

function DomainClassificationForm({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const [domain, setDomain] = useState<"RESEARCH" | "FACTORY">(item.data_domain === "FACTORY" ? "FACTORY" : "RESEARCH");
  const [serialNumber, setSerialNumber] = useState(item.serial_number ?? "");
  const [bench, setBench] = useState(item.bench_id ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const receipt = useRef({ signature: "", key: "" });
  async function save(event: FormEvent) {
    event.preventDefault(); if (!reason.trim() || busy) return;
    const payload = { expected_revision: item.revision, data_domain: domain, reason: reason.trim(), ...(domain === "FACTORY" ? { serial_number: serialNumber, bench_id: bench.trim() } : {}) };
    const signature = JSON.stringify(payload);
    if (signature !== receipt.current.signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.classifyDomain(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <details className="panel archive-more" open={archiveDomain(item) === "UNCLASSIFIED"}><summary>{archiveDomain(item) === "UNCLASSIFIED" ? "补充分区归类" : "追加业务分区修正"}</summary><p className="archive-muted">按真实执行证据选择研发或工厂并记录依据。业务分区与正式用例关联分别保存；分区修正追加版本，原始 CSV 与旧证据保留。</p><form className="archive-form" onSubmit={(event) => void save(event)}><label>业务分区<select value={domain} onChange={(event) => setDomain(event.target.value as "RESEARCH" | "FACTORY")}><option value="RESEARCH">研发</option><option value="FACTORY">工厂</option></select></label>{domain === "FACTORY" && <><label>本次工厂 SN<input type="text" required maxLength={200} value={serialNumber} onChange={(event) => setSerialNumber(event.target.value)} /></label><label>本次台架编号<input required maxLength={100} value={bench} onChange={(event) => setBench(event.target.value)} /></label></>}<label>分区依据说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} /></label><button type="submit" disabled={busy || !reason.trim() || domain === "FACTORY" && (!serialNumber || !bench.trim())}>{busy ? "正在保存…" : "保存分区版本"}</button>{error != null && <ErrorState error={error} retry={() => void onSaved()} />}</form></details>;
}

function FactoryReview({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const receipt = useRef({ signature: "", key: "" });
  async function review(decision: "APPROVE" | "REJECT") {
    if (!item.can_review || busy || !reason.trim() || item.submission_revision == null) return;
    const payload = { expected_submission_revision: item.submission_revision, decision, reason: reason.trim() };
    const signature = JSON.stringify(payload);
    if (signature !== receipt.current.signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.factoryReview(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title="工厂测试数据审批" description="核对本次工厂 SN、台架、原始证据及核实结论。批准后计入工厂正式统计。"><div className="archive-form"><label>审批说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div className="archive-submit-row"><button type="button" disabled={busy || !reason.trim()} onClick={() => void review("APPROVE")}>{busy ? "正在处理…" : "批准工厂记录"}</button><button type="button" disabled={busy || !reason.trim()} onClick={() => void review("REJECT")}>驳回工厂记录</button></div>{error != null && <ErrorState error={error} retry={() => void onSaved()} />}</div></Panel>;
}

const analysisLabels: Record<string, string> = { active_seconds: "运行秒数", completed_cycles: "完成循环", exception_count: "异常次数", row_count: "采样行数", sample_count: "采样点数", max_gap_seconds: "最大采样间隔 / s", completeness: "采样完整率", sample_completeness: "采样完整率", criteria_version: "判据版本", verdict: "分析结论", automatic_verdict: "自动判定", checks: "规则检查", subjects: "对象分析", gaps: "缺失段", metrics: "指标统计", min: "最小值", max: "最大值", mean: "均值", peak_abs: "绝对峰值", rms: "均方根", temperature_rise_c: "温升 / °C", tracking_rms_rad: "跟踪误差 RMS / rad", start: "起点", end: "终点", unit: "单位", key: "参数", status: "状态", configured: "已配置", passed: "通过", failed: "失败", unconfigured: "未配置" };
export function AnalysisValue({ value, label }: { value: unknown; label?: string }) {
  if (value === null || value === undefined) return <span>未配置</span>;
  if (Array.isArray(value)) return <ul className="archive-analysis-list">{value.map((item, index) => <li key={index}><AnalysisValue value={item} /></li>)}</ul>;
  if (typeof value === "object") return <dl className="archive-analysis-grid">{Object.entries(value).map(([key, entry]) => <div key={key}><dt>{analysisLabels[key] ?? key}</dt><dd><AnalysisValue value={entry} label={key} /></dd></div>)}</dl>;
  if (typeof value === "boolean") return <span>{value ? "是" : "否"}</span>;
  if (typeof value === "number") return <span>{value.toLocaleString("zh-CN", { maximumFractionDigits: 6 })}</span>;
  return <span>{label === "verdict" || label === "automatic_verdict" || label === "status" ? analysisLabels[String(value)] ?? String(value) : String(value)}</span>;
}

export function ArchiveEvidence({ item }: { item: Archive }) {
  return <div className="archive-evidence">
    <Panel title="采集事实" eyebrow="ACQUISITION FACTS" description="测得数据与机器终态在归档后冻结。">
      <dl className="fact-grid"><div><dt>执行是否完成</dt><dd>{machineLabels[String(item.facts.machine_status)] ?? displayValue(item.facts.machine_status)}</dd></div><div><dt>自动判定</dt><dd>{item.facts.automatic_verdict === "unconfigured" ? "未配置" : verdictLabels[item.facts.automatic_verdict as ArchiveVerdict] ?? "未记录"}</dd></div><div><dt>运行时长</dt><dd>{formatDuration(typeof item.facts.active_seconds === "number" ? item.facts.active_seconds : null)}</dd></div><div><dt>循环 / 异常</dt><dd>{displayValue(item.facts.completed_cycles)} / {displayValue(item.facts.exception_count)}</dd></div><div><dt>采集开始</dt><dd>{formatDate(item.facts.started_at as string | null)}</dd></div><div><dt>采集结束</dt><dd>{formatDate(item.facts.ended_at as string | null)}</dd></div><div><dt>数据 / 时钟质量</dt><dd>{displayValue(item.facts.data_quality)} / {displayValue(item.facts.clock_quality)}</dd></div></dl>
    </Panel>
    <details className="panel archive-analysis">
      <summary className="panel-head archive-analysis-toggle"><div><span>REPRODUCIBLE ANALYSIS</span><h2>自动结果分析</h2><p>分析与归档判据一起保存；未配置的判据显示为未配置。</p></div><ChevronDown aria-hidden="true" /></summary>
      {item.manifest?.analysis ? <AnalysisValue value={item.manifest.analysis} /> : <p className="archive-muted">{item.processing_state === "FAILED" ? "解析失败，尚未生成分析。" : "等待归档解析完成。"}</p>}
      <details className="archive-rules"><summary>查看本次归档使用的判据</summary><AnalysisValue value={item.acceptance} /></details>
    </details>
    <Panel title="人工核实与测试员记录" eyebrow="VERIFICATION & NOTES">{item.current_verification ? <><dl className="fact-grid"><div><dt>实际测试人</dt><dd>{item.current_verification.tester}</dd></div><div><dt>人工核实结论</dt><dd>{verdictLabels[item.current_verification.verdict]}</dd></div><div><dt>核实时间 / 版本</dt><dd>{formatDate(item.current_verification.created_at)} · 第 {item.current_verification.revision} 版</dd></div><div><dt>与自动结论差异的原因</dt><dd>{item.current_verification.discrepancy_reason || "无"}</dd></div>{item.current_verification.declared_started_at && <div><dt>{item.current_verification.timing_evidence ? "文件名与 CSV 识别时间（北京时间）" : "历史人工补录时间"}</dt><dd>{item.current_verification.timing_evidence ? formatArchiveTimestamp(item.current_verification.declared_started_at) : formatDate(item.current_verification.declared_started_at)} 至 {item.current_verification.timing_evidence && item.current_verification.declared_ended_at ? formatArchiveTimestamp(item.current_verification.declared_ended_at) : formatDate(item.current_verification.declared_ended_at)}</dd></div>}{declarationFields.map(([key, label]) => item.current_verification?.[`declared_${key}`] != null && <div key={key}><dt>{key === "active_seconds" && item.current_verification.timing_evidence ? "CSV 记录时长（秒）" : label}</dt><dd>{item.current_verification[`declared_${key}`]}</dd></div>)}</dl><p className="archive-notes">{item.current_verification.notes}</p></> : <p className="archive-muted">尚未保存核实记录。</p>}</Panel>
  </div>;
}

function VerificationForm({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const detected = item.detected_timing;
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const [tester, setTester] = useState(item.current_verification?.tester ?? "");
  const [station, setStation] = useState(item.current_verification?.station_id ?? item.classification?.station_id ?? "");
  const [verdict, setVerdict] = useState<ArchiveVerdict | "">(item.current_verification?.verdict ?? "");
  const [notes, setNotes] = useState(item.current_verification?.notes ?? "");
  const [discrepancy, setDiscrepancy] = useState(item.current_verification?.discrepancy_reason ?? "");
  const [started, setStarted] = useState(() => localDateTimeInput(item.current_verification?.declared_started_at));
  const [ended, setEnded] = useState(() => localDateTimeInput(item.current_verification?.declared_ended_at));
  const [declarations, setDeclarations] = useState(() => ({
    active_seconds: String(item.current_verification?.declared_active_seconds ?? ""),
    completed_cycles: String(item.current_verification?.declared_completed_cycles ?? ""),
    exception_count: String(item.current_verification?.declared_exception_count ?? ""),
  }));
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const receipt = useRef({ signature: "", key: "" });
  const different = verdict !== "" && verdict !== (item.manifest?.analysis?.verdict ?? item.facts.automatic_verdict);
  async function save(event: FormEvent) {
    event.preventDefault(); if (!verdict || busy) return;
    if (!detected && (started || ended) && (!started || !ended || new Date(started) >= new Date(ended))) { setError(new Error("人工补录开始与结束时间必须同时填写，且结束晚于开始。")); return; }
    const declared = Object.fromEntries(declarationFields.filter(([key]) => item.origin === "HISTORY" && item.facts[key] == null && !(detected && key === "active_seconds") && declarations[key] !== "").map(([key]) => [`declared_${key}`, Number(declarations[key])]));
    if (declared.declared_active_seconds != null && started && ended && declared.declared_active_seconds > (new Date(ended).getTime() - new Date(started).getTime()) / 1000 + .001) { setError(new Error("人工申报运行时长不得超过开始至结束的区间。")); return; }
    const payload = { expected_revision: item.revision, tester: tester.trim(), station_id: station, verdict, notes: notes.trim(), discrepancy_reason: discrepancy.trim(), reason: reason.trim(), ...declared, ...(!detected && started ? { declared_started_at: new Date(started).toISOString(), declared_ended_at: new Date(ended).toISOString() } : {}) };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.verify(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title={item.current_verification ? "追加核实版本" : "保存上传数据核实"} description="核实测试人、工位和人工结论，补充本次备注。">
    <form className="archive-form" onSubmit={(event) => void save(event)}><div className="archive-form-grid">
      <label>实际测试人<input required maxLength={100} value={tester} onChange={(event) => setTester(event.target.value)} /></label>
      <label>核实工位<select required value={station} onChange={(event) => setStation(event.target.value)}><option value="">选择有效工位</option>{options.data?.stations.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
      <label>人工核实结论<select required value={verdict} onChange={(event) => setVerdict(event.target.value as ArchiveVerdict | "")}><option value="">请选择结论</option><option value="passed">通过</option><option value="failed">失败</option><option value="blocked">待判定 / 阻塞</option></select></label>
      {different && <label>与自动结论不同的原因<textarea required maxLength={1000} value={discrepancy} onChange={(event) => setDiscrepancy(event.target.value)} /></label>}
      <label className="archive-form-wide">测试员记录<textarea required maxLength={2000} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="记录本次相关备注，无补充事项填写“无”" /></label>
      {detected ? <><label>自动识别开始时间（北京时间）<input readOnly value={formatArchiveTimestamp(detected.started_at)} /></label><label>CSV 末条采样时间（北京时间）<input readOnly value={formatArchiveTimestamp(detected.ended_at)} /></label><label>CSV 记录时长（秒）<input readOnly value={detected.recorded_seconds} /></label><p className="archive-form-wide archive-muted">后台从上位机文件名读取 UTC 开始时间，再加上 CSV 末条采样秒数，已转换为北京时间，无需手填。结束时间对应最后一条采样；记录时长按采样时间轴计算，不等同设备有效运行计时。</p></> : item.origin === "HISTORY" && !item.facts.started_at && <><label>人工补录开始时间<input required type="datetime-local" step="0.001" value={started} onChange={(event) => setStarted(event.target.value)} /></label><label>人工补录结束时间<input required type="datetime-local" step="0.001" value={ended} onChange={(event) => setEnded(event.target.value)} /></label><p className="archive-form-wide archive-muted">未识别到受支持的上位机文件名或有效采样时间，请按实际试验记录补录起止时间。</p></>}
      {item.origin === "HISTORY" && <>{declarationFields.filter(([key]) => item.facts[key] == null && !(detected && key === "active_seconds")).map(([key, label, max, step]) => <label key={key}>{label}<input required type="number" min={0} max={max} step={step} value={declarations[key]} onChange={(event) => setDeclarations({ ...declarations, [key]: event.target.value })} /></label>)}<p className="archive-form-wide archive-muted">历史记录填写完整后可送审。核实版本保存补充信息及时间来源；原始 CSV、采集事实和自动分析保留原值。</p></>}
      <label className="archive-form-wide">核实说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明此次核实或修改的原因" /></label>
    </div>{options.error && <ErrorState error={options.error} retry={() => void options.refetch()} />}<button type="submit" disabled={busy || options.isLoading || !tester.trim() || !station || !verdict || !notes.trim() || !reason.trim() || different && !discrepancy.trim()}>{busy ? "正在保存…" : "保存核实版本"}</button>{error != null && <ErrorState error={error} retry={() => setError(null)} />}</form>
  </Panel>;
}

function ClassificationForm({ item, onSaved }: { item: Archive; onSaved: () => Promise<void> }) {
  const campaigns = useQuery({ queryKey: ["setup", "campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const [campaign, setCampaign] = useState(item.classification?.campaign_id ?? "");
  const [contextId, setContextId] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const receipt = useRef({ signature: "", key: "" });
  const contexts = useQuery({ queryKey: ["setup", "contexts", campaign], queryFn: ({ signal }) => setupApi.contexts(campaign, signal), enabled: Boolean(campaign) });
  const context = contexts.data?.find((entry) => entry.id === contextId) ?? (!contextId && item.classification ? contexts.data?.find((entry) =>
    ["campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id"].every((key) => entry.context[key] === item.classification?.[key as keyof ArchiveContext])) : undefined);
  const stationMismatch = Boolean(context && item.current_verification && context.context.station_id !== item.current_verification.station_id);
  const plannedStation = options.data?.stations.find((entry) => entry.id === context?.context.station_id);
  const plannedStationLabel = plannedStation ? `${plannedStation.code} · ${plannedStation.name}` : context?.context.station_id;
  const actualStationLabel = item.current_verification?.station_code ? `${item.current_verification.station_code}${item.current_verification.station_name ? ` · ${item.current_verification.station_name}` : ""}` : item.current_verification?.station_id;
  async function save(event: FormEvent) {
    event.preventDefault(); if (!context || !item.current_verification || busy || !reason.trim()) return;
    const payload = { expected_revision: item.revision, context: { ...context.context, station_id: item.current_verification.station_id } as ArchiveContext, reason: reason.trim() };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await archivesApi.classify(item.id, payload, receipt.current.key); await onSaved(); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title={item.classification_state === "CLASSIFIED" ? "追加正式归属修正" : "归入正式测试用例"} description="选择已有计划中的正式用例与样品，使用已核实的实际工位归类。保存追加版本，历史归属、判据和结论继续保留。"><form className="archive-form" onSubmit={(event) => void save(event)}><div className="archive-form-grid">
    <label>测试计划<select required value={campaign} onChange={(event) => { setCampaign(event.target.value); setContextId(""); }}><option value="">选择计划</option>{campaigns.data?.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
    <label>用例与样品<select required value={context?.id ?? ""} disabled={!campaign || contexts.isLoading} onChange={(event) => setContextId(event.target.value)}><option value="">选择已发布用例的计划上下文</option>{contexts.data?.filter((entry) => entry.status !== "CANCELLED").map((entry) => <option key={entry.id} value={entry.id}>{entry.case_code} · {entry.case_name} · {entry.version} · {entry.asset_code}</option>)}</select></label>
    <label className="archive-form-wide">归类说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder={stationMismatch ? "请说明计划工位与实际工位不同的原因" : "说明本次归类或归属修正的原因"} /></label>
  </div>{stationMismatch && <p role="status" className="archive-muted">计划工位：{plannedStationLabel}；已核实实际工位：{actualStationLabel}。本次归类采用实际工位，请在归类说明中记录差异原因；原计划上下文保留。</p>}{campaigns.error && <ErrorState error={campaigns.error} retry={() => void campaigns.refetch()} />}{contexts.error && <ErrorState error={contexts.error} retry={() => void contexts.refetch()} />}<button type="submit" disabled={busy || !context || !reason.trim() || !item.current_verification}>{busy ? "正在归类…" : item.classification_state === "CLASSIFIED" ? "保存归属修正版本" : "保存正式归属"}</button>{!item.current_verification && <p className="archive-muted">请先保存核实记录。</p>}{error != null && <ErrorState error={error} retry={() => setError(null)} />}</form></Panel>;
}

export function ArchiveDetailPage({ id, navigate }: { id: string; navigate: Navigate }) {
  const { session } = useAuth();
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["archives", "detail", id], queryFn: ({ signal }) => archivesApi.detail(id, signal), refetchInterval: (entry) => ["READY", "FAILED"].includes(entry.state.data?.processing_state ?? "") ? false : 5000 });
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  async function saved() { await Promise.all([cache.invalidateQueries({ queryKey: ["archives"] }), cache.invalidateQueries({ queryKey: ["factory-statistics"] })]); }
  async function submit() {
    const item = query.data; if (!item || busy) return;
    const payload = { expected_revision: item.revision, expected_submission_revision: item.submission_revision ?? 0, reason: reason.trim() };
    const signature = JSON.stringify(payload); if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null); setNotice("");
    try { await archivesApi.submit(id, payload, receipt.current.key); setNotice(item.data_domain === "FACTORY" ? "工厂记录已送审。具有审批权限的账号可在本详情页审核。" : "已送审。请在测试数据审批页面核对并审核。"); await saved(); await cache.invalidateQueries({ queryKey: ["submissions"] }); } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  if (query.isLoading) return <LoadingState label="正在加载归档记录" />;
  if (query.error || !query.data) return <ErrorState error={query.error} retry={() => void query.refetch()} />;
  const item = query.data;
  const factory = item.data_domain === "FACTORY";
  const research = item.data_domain === "RESEARCH";
  const editable = item.can_edit && item.submission_status !== "APPROVED" && item.submission_status !== "PENDING";
  const compactHistory = research && item.origin === "HISTORY" && item.is_owner;
  const classificationStationMismatch = Boolean(item.current_verification && item.classification && item.current_verification.station_id !== item.classification.station_id);
  const factoryStationCode = item.current_verification?.station_code ?? item.verified_station_code;
  const factoryStationMismatch = factory && Boolean(item.bench_id && factoryStationCode && item.bench_id !== factoryStationCode);
  const missingHistoricalInfo = item.origin === "HISTORY" && missingArchiveSubmissionInfo(item).length > 0;
  const canSubmit = !factoryStationMismatch && !missingHistoricalInfo && editable && item.is_owner && item.processing_state === "READY" && item.upload_state === "COMPLETE" && (factory ? Boolean(item.serial_number && item.bench_id) : research) && item.classification_state === "CLASSIFIED" && Boolean(item.current_verification) && !classificationStationMismatch;
  return <div className="archives-page"><button type="button" className="back-button" onClick={() => navigate(factory ? "factory" : "archives", undefined, archiveDomain(item))}><ArrowLeft aria-hidden="true" />返回数据归档</button><PageTitle eyebrow="TEST ARCHIVE" title={archiveTitle(item)} description={`${item.test_name} · ${item.actual_object} · ${archiveStation(item)}`} trailing={<button type="button" disabled={item.processing_state !== "READY"} onClick={() => navigate("curves", item.id, archiveDomain(item))}><ChartNoAxesCombined aria-hidden="true" />详细曲线</button>} /><ArchiveStatus item={item} />
    <p className="archive-scope-note">业务分区：{dataDomainLabels[archiveDomain(item)]} · 依据：{item.data_domain_source ? dataDomainSourceLabels[item.data_domain_source] : "未返回分区依据"} · SN：{item.serial_number ?? "未记录"} · 台架：{item.bench_id ?? "未记录"}</p>
    {editable && <DomainClassificationForm key={`domain:${item.revision}`} item={item} onSaved={saved} />}
    {item.processing_error && <p role="alert" className="curve-error">解析失败：{item.processing_error}</p>}
    {notice && <p role="status">{notice}</p>}
    {compactHistory && editable && item.processing_state === "READY" && <HistoricalIntakeForm key={`intake:${item.revision}`} item={item} onSubmitted={async (updated) => { setNotice("已送审，请到测试数据审批页面审核。"); cache.setQueryData(["archives", "detail", id], updated); await saved(); await cache.invalidateQueries({ queryKey: ["submissions"] }); }} />}
    {compactHistory ? <details className="panel archive-more"><summary>查看采集信息、自动分析与更多操作</summary><ArchiveEvidence item={item} />{typeof item.can_analyze === "boolean" && item.processing_state === "READY" && <ReanalysisPanel item={item} />}</details> : <><ArchiveEvidence item={item} />{typeof item.can_analyze === "boolean" && item.processing_state === "READY" && <ReanalysisPanel item={item} />}{item.data_domain==="FACTORY"&&<FactoryHandoff id={item.id} canEdit={session?.user.role!=="VIEWER"&&Boolean(item.is_owner||session?.user.role==="SYSTEM_ADMIN")}/>}</>}
    {editable && !compactHistory && item.processing_state === "READY" && <VerificationForm key={`verify:${item.revision}`} item={item} onSaved={saved} />}
    {editable && item.processing_state !== "READY" && <p className="archive-muted">文件归档校验和自动分析完成后，可以保存核实版本。</p>}
    {editable && (research || factory) && !compactHistory && <ClassificationForm key={`classify:${item.revision}`} item={item} onSaved={saved} />}
    {editable && factoryStationMismatch && <p role="status" className="archive-muted">本次台架 {item.bench_id} 与核实工位 {factoryStationCode} 不一致，请按真实记录修正分区证据或重新核实工位后送审。</p>}
    {editable && (research || factory) && !compactHistory && classificationStationMismatch && <p role="status" className="archive-muted">核实工位与当前归类版本不同，请追加正式归属修正后重新送审。</p>}
    {editable && !compactHistory && <Panel title={item.submission_status === "REJECTED" ? "核实后重新送审" : "提交正式审批"} description={factory ? "工厂记录按真实 SN、台架和工位独立送审。补齐正式用例、执行上下文并核实批准后进入工厂正式统计。" : "归档、核实与正式归属齐全后可送审。审批通过后进入研发正式查询。"}><div className="archive-submit-row"><label>提交说明<input maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} /></label><button type="button" disabled={!canSubmit || !reason.trim() || busy} onClick={() => void submit()}><FileCheck2 aria-hidden="true" />{busy ? "正在送审…" : "送审"}</button></div>{!canSubmit && <p className="archive-muted">{missingHistoricalInfo ? "请保存核实信息；未自动识别的时间、运行时长及完成循环和异常次数需填写完整后送审。" : !item.is_owner ? "管理员可以补齐核实与归类，请由原提交人送审。" : factory ? "请完成原始文件归档校验、人工核实、工厂身份信息及正式用例关联。OTHER 可先查看曲线，不能直接进入正式统计。" : !research ? "请先按真实证据补充业务分区，再完成对应分区的送审信息。" : "请完成文件归档解析、人工核实及正式用例归类。"}</p>}{error != null && <ErrorState error={error} retry={() => void query.refetch()} />}</Panel>}
    {factory && item.submission_status === "PENDING" && item.can_review === true && <FactoryReview key={`review:${item.submission_revision}`} item={item} onSaved={saved} />}
    {factory && item.submission_status === "PENDING" && item.can_review !== true && <p className="archive-scope-note">工厂记录待审核，当前账号没有本记录的审批权限。</p>}
    {!factory && item.submission_id && session?.user.role !== "VIEWER" && <button type="button" className="link-button" onClick={() => navigate("submissions")}>查看测试数据审批</button>}{item.execution_id && session?.user.role !== "FACTORY_REVIEWER" && <button type="button" className="link-button" onClick={() => navigate("execution", item.execution_id!)}>查看正式执行：{item.execution_code}</button>}
    <details className="archive-rules" open={item.origin !== "HISTORY"}><summary>文件与追溯</summary><Panel title="文件与追溯"><dl className="fact-grid"><div><dt>原始 CSV</dt><dd>{item.filename}</dd></div><div><dt>归档大小</dt><dd>{(item.size_bytes / 1024 / 1024).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} MiB</dd></div><div><dt>来源</dt><dd>{item.origin === "HISTORY" ? "历史补录" : item.origin === "OTHER" ? "其他测试" : "上位机测试"}</dd></div><div><dt>稳定执行标识</dt><dd>{item.execution_uuid}</dd></div><div><dt>归档编号</dt><dd>{item.archive_code}</dd></div><div><dt>原始文件校验</dt><dd className="archive-hash">{item.sha256}</dd></div></dl></Panel></details>
    {(item.domain_history?.length || item.factory_review_history?.length) ? <Panel title="分区与工厂审批历史"><div className="archive-history">{item.domain_history?.map((entry) => <details key={`domain:${entry.revision}`}><summary>分区第 {entry.revision} 版 · {dataDomainLabels[entry.data_domain]} · {formatDate(entry.created_at)} · {entry.actor_name ?? "未记录"}</summary><p>{entry.reason}</p><p>{dataDomainSourceLabels[entry.data_domain_source]} · SN：{entry.serial_number ?? "未记录"} · 台架：{entry.bench_id ?? "未记录"}</p><details><summary>分区证据</summary><AnalysisValue value={entry.evidence} /></details></details>)}{item.factory_review_history?.map((entry) => <details key={`review:${entry.revision}`}><summary>工厂审批第 {entry.revision} 版 · {entry.decision === "APPROVED" ? "批准" : "驳回"} · {formatDate(entry.created_at)} · {entry.actor_name ?? "未记录"}</summary><p>{entry.reason}</p></details>)}</div></Panel> : null}
    {(item.verification_history?.length || item.classification_history?.length) ? <Panel title="核实与归类历史"><div className="archive-history">{item.verification_history?.map((entry) => <details key={`v:${entry.revision}`}><summary>核实第 {entry.revision} 版 · {formatDate(entry.created_at)} · {entry.actor_name ?? entry.payload.tester}</summary><p>{entry.reason}</p><dl className="fact-grid"><div><dt>实际测试人</dt><dd>{entry.payload.tester}</dd></div><div><dt>核实工位标识</dt><dd>{entry.payload.station_id}</dd></div><div><dt>人工结论</dt><dd>{verdictLabels[entry.payload.verdict]}</dd></div><div><dt>差异说明</dt><dd>{entry.payload.discrepancy_reason || "无"}</dd></div></dl><p className="archive-notes">{entry.payload.notes}</p></details>)}{item.classification_history?.map((entry) => <details key={`c:${entry.revision}`}><summary>归类第 {entry.revision} 版 · {formatDate(entry.created_at)}{entry.actor_name ? ` · ${entry.actor_name}` : ""}</summary><p>{entry.reason}</p><dl className="submission-evidence">{Object.entries(entry.payload).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details>)}</div></Panel> : null}
  </div>;
}

function ReanalysisPanel({ item }: { item: Archive }) {
  const cache = useQueryClient();
  const reports = useQuery({ queryKey: ["archives", "analyses", item.id], queryFn: ({ signal }) => archivesApi.analyses(item.id, signal) });
  const catalog = useQuery({ queryKey: ["setup", "catalog"], queryFn: ({ signal }) => setupApi.catalog(signal), enabled: item.can_analyze === true });
  const [version, setVersion] = useState(item.classification?.test_case_version_id ?? "");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  async function append(event: FormEvent) {
    event.preventDefault();
    if (!version || !reason.trim() || busy) return;
    const payload = { test_case_version_id: version, reason: reason.trim() };
    const signature = JSON.stringify(payload);
    if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null); setNotice("");
    try {
      await archivesApi.analyze(item.id, payload, receipt.current.key);
      setNotice("已追加分析版本。原始报告与正式审批结果保持原有版本。");
      await cache.invalidateQueries({ queryKey: ["archives", "analyses", item.id] });
    } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <Panel title="按新用例判据重新分析" description="新分析用于回顾比较，另存来源与规则快照；不会覆盖原报告，也不会改变已批准结果或统计。">
    {item.can_analyze && <form className="archive-form archive-form-grid" onSubmit={(event) => void append(event)}>
      <label>分析使用的发布用例<select required value={version} disabled={catalog.isLoading || busy} onChange={(event) => setVersion(event.target.value)}><option value="">选择已发布用例版本</option>{catalog.data?.filter((entry) => entry.enabled && entry.version_id).map((entry) => <option key={entry.version_id} value={entry.version_id!}>{entry.code} · {entry.name} · {entry.version}</option>)}</select></label>
      <label>重新分析说明<input required maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="说明为何使用这个用例版本进行回顾分析" /></label>
      <button type="submit" disabled={!version || !reason.trim() || busy || catalog.isLoading}>{busy ? "正在追加分析…" : "追加分析版本"}</button>
    </form>}
    {catalog.error && <ErrorState error={catalog.error} retry={() => void catalog.refetch()} />}
    {reports.isLoading && <LoadingState label="正在加载分析版本" />}
    {reports.error && <ErrorState error={reports.error} retry={() => void reports.refetch()} />}
    {reports.data?.items.length === 0 && <p className="archive-muted">尚未追加新判据分析。当前原始分析保留在上方。</p>}
    {reports.data?.items.map((entry) => <details className="archive-rules" key={entry.id}><summary>分析第 {entry.version} 版 · {entry.source_case_code} · {entry.source_version} · {formatDate(entry.created_at)}</summary><p>{entry.reason} · {entry.actor_name}</p><p className="archive-muted">{entry.mapping_notes}</p><AnalysisValue value={entry.report} /><details><summary>查看该版本判据快照</summary><AnalysisValue value={entry.acceptance} /></details></details>)}
    {notice && <p role="status">{notice}</p>}
    {error != null && <ErrorState error={error} retry={() => void reports.refetch()} />}
  </Panel>;
}

export default function ArchivesPage({ navigate, mode = "archives", domain: controlledDomain, onDomainChange, embedded = false }: { embedded?: boolean; navigate: Navigate; mode?: "archives" | "curves"; domain?: ArchiveDataDomain; onDomainChange?: (domain: ArchiveDataDomain) => void }) {
  const analysisEntry = mode === "curves";
  const { session } = useAuth();
  const [localDomain, setLocalDomain] = useState<ArchiveDataDomain>("RESEARCH");
  const domain = controlledDomain ?? localDomain;
  const factory = domain === "FACTORY";
  const [draft, setDraft] = useState({ q: "", sn: "", bench: "", from: "", to: "", origin: "", classification_state: "" });
  const [applied, setApplied] = useState<Omit<ArchiveFilters, "data_domain" | "submission_status" | "before">>({});
  const [submission, setSubmission] = useState("");
  const [filterError, setFilterError] = useState<unknown>(null);
  const filters: ArchiveFilters = { ...applied, data_domain: domain, submission_status: submission };
  const physicalFilters = { sn: applied.sn, bench: applied.bench, started_from: applied.started_from, started_to: applied.started_to };
  const [before,setBefore]=useState<number|null>(null);
  const query = useQuery({ queryKey: ["archives", "list", session?.user.id, filters,before], queryFn: ({signal}) => archivesApi.list({ ...filters, before }, signal) });
  const statistics = useQuery({ queryKey: ["factory-statistics", session?.user.id, physicalFilters], queryFn: ({ signal }) => archivesApi.factoryStatistics(physicalFilters, signal), enabled: factory });
  const items = query.data?.items ?? [];
  function changeDomain(next: ArchiveDataDomain) {
    if (factory || next === "FACTORY") return;
    setBefore(null); setSubmission(""); setApplied({}); setDraft({ q: "", sn: "", bench: "", from: "", to: "", origin: "", classification_state: "" });
    if (onDomainChange) onDomainChange(next); else setLocalDomain(next);
  }
  function search(event: FormEvent) {
    event.preventDefault();
    try {
      const dates = archiveDateFilters(draft.from, draft.to);
      // SN is opaque text: do not normalize case, remove leading zeroes, or
      // reinterpret it as a registered research asset identifier.
      setApplied({ sn: draft.sn, bench: draft.bench.trim(), ...dates, ...(!factory ? { q: draft.q.trim(), origin: draft.origin, classification_state: draft.classification_state } : {}) });
      setBefore(null); setFilterError(null);
    } catch (failure) { setFilterError(failure); }
  }
  const ledgerParams=new URLSearchParams();
  for(const [key,value] of Object.entries(filters))if(value!==undefined&&value!==null&&value!=="")ledgerParams.set(key,String(value));
  const ledgerHref="/api/v1/archive-ledger.csv?"+ledgerParams.toString();
  const title = analysisEntry ? `${dataDomainLabels[domain]}曲线分析` : factory ? "工厂测试" : domain === "UNCLASSIFIED" ? "分区待归类台账" : "研发数据归档";
  const ledgerTitle = submission === "PENDING" ? "待审批台账" : submission === "APPROVED" ? "已批准台账" : submission === "REJECTED" ? "已驳回台账" : "归档台账";
  // The factory console has its own compact read view. Shared archive pages,
  // evidence editing and approval actions retain their existing presentation.
  if (embedded && factory && !analysisEntry) {
    return <section className="factory-ledger" aria-labelledby="factory-records-heading">
      <header className="factory-ledger-header">
        <div><h2 id="factory-records-heading">测试记录</h2>{!query.isLoading && !query.error && <span>当前页 {items.length} 条</span>}</div>
        <div className="factory-ledger-tools">
          <label><span>审批范围</span><select aria-label="台账范围" value={submission} onChange={(event) => { setBefore(null); setSubmission(event.target.value); }}>
            <option value="">全部台账</option><option value="PENDING">待审批台账</option><option value="APPROVED">已批准台账</option><option value="REJECTED">已驳回台账</option>
          </select></label>
          <a role="button" className="factory-ledger-export" href={ledgerHref} aria-disabled={query.isLoading || !!query.error || !items.length} onClick={event => { if (query.isLoading || query.error || !items.length) event.preventDefault(); }}><Download aria-hidden="true" />导出台账 CSV</a>
        </div>
      </header>
      <form className="factory-ledger-filters" role="search" aria-label="工厂测试记录查询" onSubmit={search}>
        <label>SN<input type="text" maxLength={200} value={draft.sn} onChange={(event) => setDraft({ ...draft, sn: event.target.value })} placeholder="输入 SN" /></label>
        <label>台架编号<input maxLength={100} value={draft.bench} onChange={(event) => setDraft({ ...draft, bench: event.target.value })} placeholder="输入台架编号" /></label>
        <label>开始日期<input aria-label="开始日期（北京时间）" title="按北京时间查询" type="date" value={draft.from} onChange={(event) => setDraft({ ...draft, from: event.target.value })} /></label>
        <label>结束日期<input aria-label="结束日期（含当天）" title="包含当天，按北京时间查询" type="date" min={draft.from || undefined} value={draft.to} onChange={(event) => setDraft({ ...draft, to: event.target.value })} /></label>
        <button type="submit" className="factory-ledger-query"><Search aria-hidden="true" />查询</button>
      </form>
      {filterError != null && <ErrorState error={filterError} retry={() => setFilterError(null)} />}
      <section className="factory-ledger-statistics" aria-label="工厂执行统计">
        {statistics.isLoading ? <LoadingState label="正在加载工厂统计" /> : statistics.error || !statistics.data ? <ErrorState error={statistics.error} retry={() => void statistics.refetch()} /> : <dl>
          <div><dt>台账记录</dt><dd>{statistics.data.total_count.toLocaleString("zh-CN")}</dd></div>
          <div><dt>待审批</dt><dd><button type="button" className="link-button" onClick={() => { setBefore(null); setSubmission("PENDING"); }}>{statistics.data.pending_count.toLocaleString("zh-CN")}</button></dd></div>
          <div><dt>已批准</dt><dd>{statistics.data.approved_count.toLocaleString("zh-CN")}</dd></div>
          <div><dt>正式通过 / 失败 / 阻塞</dt><dd>{statistics.data.passed_count} / {statistics.data.failed_count} / {statistics.data.blocked_count}</dd></div>
          <div><dt>正式运行时长</dt><dd>{formatDuration(statistics.data.active_seconds)}</dd></div>
        </dl>}
      </section>
      <p className="factory-ledger-statistics-note">统计按 SN、台架与日期查询；审批范围只筛选下方台账。</p>
      {query.isLoading ? <LoadingState label="正在读取工厂测试记录" /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : !items.length ? <div className="factory-ledger-empty"><strong>暂无符合条件的测试记录</strong><p>上传后的工厂记录会显示在这里，也可清空查询条件后重试。</p></div> : <>
        <div className="factory-ledger-table-scroll"><table className="factory-record-table"><thead><tr><th>SN / 测试</th><th>台架</th><th>采集时间</th><th>归档状态</th><th>审批状态</th><th>操作</th></tr></thead><tbody>
          {items.map(item => {
            const name = item.serial_number || item.actual_object || "SN 未记录";
            const uploadLabel = item.upload_state === "CORRUPT" ? "文件校验失败" : item.upload_state !== "COMPLETE" ? "上传未完成" : item.processing_state === "FAILED" ? "解析失败" : item.processing_state === "READY" ? "文件就绪" : "正在解析";
            const uploadTone = item.upload_state === "CORRUPT" || item.processing_state === "FAILED" ? "danger" : item.upload_state === "COMPLETE" && item.processing_state === "READY" ? "normal" : "warning";
            const approvalLabel = item.submission_status === "APPROVED" ? "已批准" : item.submission_status === "PENDING" ? "待审批" : item.submission_status === "REJECTED" ? "已驳回" : "未送审";
            return <tr key={item.id}>
              <td><button type="button" className="factory-ledger-record-name" title={name} aria-label={`查看 ${name} 记录`} onClick={() => navigate("archive", item.id, "FACTORY")}>{name}</button><small className="factory-ledger-test-name" title={item.test_name}>{item.test_name}</small>
                <details className="factory-ledger-identity"><summary>记录编号</summary><dl><div><dt>归档编号</dt><dd>{item.archive_code}</dd></div><div><dt>执行编号</dt><dd>{item.execution_code || "尚未生成"}</dd></div><div><dt>执行标识</dt><dd>{item.execution_uuid}</dd></div></dl></details>
              </td>
              <td><strong>{item.bench_id ?? "未记录"}</strong></td>
              <td><time title={formatDate(archiveStartedAt(item))}>{formatDate(archiveStartedAt(item))}</time><small>{item.origin === "HISTORY" ? "历史补录" : "工位采集"}</small></td>
              <td><StatusMark tone={uploadTone} label={uploadLabel} />{item.processing_error && <p className="factory-ledger-error" title={item.processing_error}>{item.processing_error}</p>}</td>
              <td><StatusMark tone={item.submission_status === "APPROVED" ? "normal" : item.submission_status === "REJECTED" ? "danger" : item.submission_status === "PENDING" ? "warning" : "muted"} label={approvalLabel} /></td>
              <td><div className="factory-ledger-record-actions"><button type="button" className="link-button" onClick={() => navigate("archive", item.id, "FACTORY")}>查看详情<ArrowRight aria-hidden="true" /></button><button type="button" className="factory-ledger-curve" title={item.processing_state === "READY" ? "查看曲线" : `${archiveProcessingLabels[item.processing_state]}，暂不可查看曲线`} aria-label={`查看 ${name} 曲线`} disabled={item.processing_state !== "READY"} onClick={() => navigate("curves", item.id, "FACTORY")}><ChartNoAxesCombined aria-hidden="true" /></button></div></td>
            </tr>;
          })}
        </tbody></table></div>
        {(before !== null || query.data?.next_cursor) && <nav className="factory-ledger-pages" aria-label="工厂记录分页">{before !== null && <button type="button" disabled={query.isFetching} onClick={() => setBefore(null)}>返回第一页</button>}{query.data?.next_cursor && <button type="button" disabled={query.isFetching} onClick={() => setBefore(query.data!.next_cursor)}>{query.isFetching ? "加载中…" : "下一页"}</button>}</nav>}
      </>}
      <p className="factory-ledger-footnote">正式结果仅包含已核实、已批准且原始文件校验通过的记录；工厂不计算 MTBF。台账导出包含当前查询的全部分页。</p>
    </section>;
  }
  return <div className="archives-page">{!embedded && <PageTitle eyebrow={factory ? "FACTORY TESTS" : analysisEntry ? "CURVE ANALYSIS" : "CENTRAL TEST ARCHIVE"} title={title} description={factory ? "按实际 SN、台架及采集日期查询独立工厂记录。相同 SN 的每次复测保留独立执行编号。" : "按分区查询归档、核实记录及曲线；正式数据须经核实和审批。"} trailing={!analysisEntry && domain !== "UNCLASSIFIED" && ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "") ? <button type="button" onClick={() => navigate("import", undefined, domain)}><FileUp aria-hidden="true" />{factory ? "上传测试记录" : "数据补录"}</button> : undefined} />}
    {!factory && <nav className="archive-domain-switch" aria-label="数据业务分区">{(["RESEARCH", "UNCLASSIFIED"] as const).map((entry) => <button type="button" key={entry} aria-pressed={domain === entry} onClick={() => changeDomain(entry)}>{dataDomainLabels[entry]}</button>)}</nav>}
    {session?.user.role === "VIEWER" && <p className="archive-scope-note">显示你有计划权限的已批准记录。</p>}
    <p className="archive-scope-note">{domain === "UNCLASSIFIED" ? "这些历史记录缺少可信台架或显式模式证据，暂不计入研发或工厂正式统计。分区待归类与缺少正式用例关联是两种不同状态。" : factory ? "工厂正式统计只使用已核实、已批准且原始归档校验通过的记录；待审、驳回与未送审记录单列台账。工厂不计算 MTBF。" : "本页只查询研发分区。待审、驳回和待关联正式用例的记录不计入研发正式统计。"}</p>
    {analysisEntry && <p className="archive-scope-note">曲线按当前业务分区查询，不随顶部整机 / 模块切换筛选；查看曲线不改变审批状态。</p>}
    <form className="filter-bar archive-filter archive-domain-filter" role="search" onSubmit={search}>
      <label>SN<input type="text" maxLength={200} value={draft.sn} onChange={(event) => setDraft({ ...draft, sn: event.target.value })} placeholder="保留大小写与前导零" /></label>
      <label>台架编号<input maxLength={100} value={draft.bench} onChange={(event) => setDraft({ ...draft, bench: event.target.value })} /></label>
      <label>开始日期（北京时间）<input type="date" value={draft.from} onChange={(event) => setDraft({ ...draft, from: event.target.value })} /></label>
      <label>结束日期（含当天）<input type="date" min={draft.from || undefined} value={draft.to} onChange={(event) => setDraft({ ...draft, to: event.target.value })} /></label>
      {!factory && <><label>执行编号 / 测试名称<input maxLength={200} value={draft.q} onChange={(event) => setDraft({ ...draft, q: event.target.value })} /></label><label>正式用例关联<select value={draft.classification_state} onChange={(event) => setDraft({ ...draft, classification_state: event.target.value })}><option value="">全部用例关联状态</option><option value="UNCLASSIFIED">待关联正式用例</option><option value="CLASSIFIED">已关联正式用例</option></select></label><label>记录来源<select value={draft.origin} onChange={(event) => setDraft({ ...draft, origin: event.target.value })}><option value="">全部来源</option><option value="HMI">上位机</option><option value="OTHER">其他测试</option><option value="HISTORY">历史补录</option></select></label></>}
      <button type="submit"><Search aria-hidden="true" />查询</button>
    </form>
    {filterError != null && <ErrorState error={filterError} retry={() => setFilterError(null)} />}
    {factory && <Panel title="工厂执行统计" description="以下统计使用上方已查询的 SN、台架与日期条件；审批状态只筛选下方台账。">
      {statistics.isLoading ? <LoadingState label="正在加载工厂统计" /> : statistics.error || !statistics.data ? <ErrorState error={statistics.error} retry={() => void statistics.refetch()} /> : <dl className="factory-statistics"><div><dt>台账记录数</dt><dd>{statistics.data.total_count.toLocaleString("zh-CN")}</dd></div><div><dt>待审批记录数</dt><dd><button type="button" className="link-button" onClick={() => setSubmission("PENDING")}>{statistics.data.pending_count.toLocaleString("zh-CN")}</button></dd></div><div><dt>已批准测试数</dt><dd>{statistics.data.approved_count.toLocaleString("zh-CN")}</dd></div><div><dt>正式通过 / 失败 / 阻塞</dt><dd>{statistics.data.passed_count} / {statistics.data.failed_count} / {statistics.data.blocked_count}</dd></div><div><dt>正式运行时长</dt><dd>{formatDuration(statistics.data.active_seconds)}</dd></div></dl>}
    </Panel>}
    <div className="archive-ledger-controls"><label>台账范围<select value={submission} onChange={(event) => {setBefore(null);setSubmission(event.target.value);}}><option value="">全部台账</option><option value="PENDING">待审批台账</option><option value="APPROVED">已批准台账</option><option value="REJECTED">已驳回台账</option></select></label><a role="button" className="archive-curve-link" href={ledgerHref} aria-disabled={query.isLoading || !!query.error || !items.length} onClick={event=>{if(query.isLoading||query.error||!items.length)event.preventDefault();}}><Download aria-hidden="true" />导出台账 CSV</a><span className="archive-muted">导出当前查询的全部分页；原始采样请进入曲线工作台按区间下载。</span></div>
    {query.isLoading ? <LoadingState /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : !items.length ? <EmptyState title="暂无符合条件的归档" description="测试结束、核实并显式上传后，记录会出现在对应分区。可尝试清空查询条件。" /> : <Panel title={analysisEntry ? "选择记录开始分析" : `${dataDomainLabels[domain]} · ${ledgerTitle}`} trailing={<span className="archive-muted">当前页 {items.length} 条</span>}><div className="table-scroll"><table className="data-table archive-table"><thead><tr><th>执行编号 / 测试</th><th>SN / 对象 / 台架</th><th>归档与审批</th><th>采集时间</th><th>操作</th></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><button type="button" className="link-button" onClick={() => navigate("archive", item.id, archiveDomain(item))}>{archiveTitle(item)}</button><small>{item.test_name}</small></td><td>{item.serial_number ? <><strong>SN：{item.serial_number}</strong><small>{item.actual_object}</small></> : item.actual_object}<small>台架：{item.bench_id ?? "未记录"} · {archiveStation(item)}</small></td><td><ArchiveStatus item={item} />{item.processing_error && <small className="archive-list-error">解析失败：{item.processing_error}</small>}</td><td>{formatDate(archiveStartedAt(item))}<small>{item.origin === "HISTORY" ? "历史补录" : "工位采集"}</small></td><td><button type="button" className="archive-curve-link" disabled={item.processing_state !== "READY"} onClick={() => navigate("curves", item.id, archiveDomain(item))}><ChartNoAxesCombined aria-hidden="true" />{analysisEntry ? "打开曲线分析" : "曲线"}</button>{analysisEntry && item.processing_state !== "READY" && <small>{archiveProcessingLabels[item.processing_state]}，暂不可分析</small>}</td></tr>)}</tbody></table></div>{before!==null&&<button onClick={()=>setBefore(null)}>返回第一页</button>}{query.data?.next_cursor&&<button type="button" disabled={query.isFetching} onClick={() => setBefore(query.data!.next_cursor)}>{query.isFetching ? "加载中…" : "下一页"}</button>}</Panel>}
  </div>;
}
