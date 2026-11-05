import { useEffect, useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, X } from "lucide-react";
import { useAuth } from "../auth";
import { ApiError, requestId } from "../client";
import { formatDate } from "../navigation";
import { robotFactoryApi, robotResultStatusLabels, validateRobotResultReceipt, type RobotResultReceipt, type RobotResultUpload } from "../robotFactoryApi";
import { ErrorState, LoadingState } from "./Workbench";
import "./robotFactoryResults.css";

async function fileHash(file: File): Promise<string> {
  const bytes = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取所选 CSV，请重新选择。"));
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(file);
  });
  if (!globalThis.crypto?.subtle) throw new Error("当前浏览器无法校验证据哈希，请使用平台 HTTPS 地址。请勿跳过文件校验。");
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), n => n.toString(16).padStart(2, "0")).join("");
}

function UploadForm({ initialRobot, initialSource, onUploaded }: { initialRobot: string; initialSource: string; onUploaded: (record: RobotResultReceipt) => void }) {
  const [file, setFile] = useState<File | null>(null), [source, setSource] = useState(initialSource), [robot, setRobot] = useState(initialRobot), [kind, setKind] = useState<"real" | "simulated">("real");
  const [reason, setReason] = useState(""), [verified, setVerified] = useState(false), [completed, setCompleted] = useState(false);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [frozen, setFrozen] = useState<{ file: File; metadata: RobotResultUpload; key: string } | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !file || !verified || !completed || !source.trim() || !robot.trim() || !reason.trim()) return;
    if (!/\.csv$/i.test(file.name) || file.size < 1 || file.size > 4 * 1024 ** 2) { setError("请选择不超过 4 MiB 的非空标准 CSV。"); return; }
    setBusy(true); setError("");
    const active = new AbortController(); controller.current = active;
    try {
      const evidence = frozen ?? { file, metadata: { source_id: source.trim(), robot_id: robot.trim(), source_kind: kind, filename: file.name, size_bytes: file.size, sha256: await fileHash(file), verified: true as const, completed: true as const, reason: reason.trim() }, key: requestId() };
      if (active.signal.aborted) return;
      setFrozen(evidence);
      const record = validateRobotResultReceipt(await robotFactoryApi.upload(evidence.file, evidence.metadata, evidence.key, active.signal), evidence.metadata);
      if (!active.signal.aborted) onUploaded(record);
    } catch (failure) {
      if (active.signal.aborted) return;
      if (failure instanceof ApiError && failure.status >= 400 && failure.status < 500 && ![408, 409, 429].includes(failure.status)) setFrozen(null);
      setError(failure instanceof Error ? failure.message : "提交失败，请保持原文件与信息重试。");
    } finally { if (!active.signal.aborted) setBusy(false); }
  }
  const locked = busy || Boolean(frozen);
  return <form className="robot-result-upload" onSubmit={event => void submit(event)}>
    <h3>提交整机工厂结果</h3><p>选择原整机上位机导出的标准 17 列结果 CSV。分区固定为工厂；文件经校验保存后进入待审核，批准前不形成正式工厂记录。</p>
    <div className="robot-result-fields">
      <label>原上位机编号<input value={source} maxLength={100} required disabled={locked} onChange={event => setSource(event.target.value)} placeholder="与 CSV 的上位机编号一致" /></label>
      <label>机器人 ID<input value={robot} maxLength={128} required disabled={locked} onChange={event => setRobot(event.target.value)} /></label>
      <label>数据来源<select value={kind} disabled={locked} onChange={event => setKind(event.target.value as "real" | "simulated")}><option value="real">真实测试</option><option value="simulated">模拟测试（不能批准为正式记录）</option></select></label>
      <label>标准结果 CSV<input type="file" accept=".csv,text/csv" disabled={locked} required onChange={event => { setFile(event.target.files?.[0] ?? null); setVerified(false); setCompleted(false); setError(""); }} /></label>
    </div>
    <label className="robot-result-reason">核实及提交说明<textarea value={reason} maxLength={300} required disabled={locked} onChange={event => setReason(event.target.value)} /></label>
    <label className="robot-result-confirm"><input type="checkbox" checked={completed} disabled={locked} onChange={event => setCompleted(event.target.checked)} />已确认文件中的测试已结束</label>
    <label className="robot-result-confirm"><input type="checkbox" checked={verified} disabled={locked} onChange={event => setVerified(event.target.checked)} />已核实 SN、机型、工位、项目、结果与原始证据，并明确提交本文件</label>
    {frozen && !busy && <p role="status">上次请求回执尚未确认。重试将发送同一文件、核实信息与幂等键，避免重复归档。</p>}
    {error && <p role="alert" className="robot-result-error">{error}</p>}
    <button type="submit" className="factory-upload-button" disabled={busy || !file || !verified || !completed || !source.trim() || !robot.trim() || !reason.trim()}><FileUp aria-hidden="true" />{busy ? "正在校验并提交…" : frozen ? "重试同一提交" : "明确提交，送交审核"}</button>
  </form>;
}

function ResultDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { session } = useAuth();
  const cache = useQueryClient();
  const query = useQuery({ queryKey: ["robot-result-detail", session?.user.id, id], queryFn: ({ signal }) => robotFactoryApi.detail(id, signal) });
  const [reason, setReason] = useState(""), [busy, setBusy] = useState(false), [notice, setNotice] = useState(""), [failure, setFailure] = useState("");
  const receipt = useRef({ signature: "", key: "" });
  const item = query.data;
  async function review(decision: "APPROVE" | "REJECT") {
    if (!item || busy || !reason.trim() || item.can_review !== true) return;
    const signature = JSON.stringify([id, item.revision, decision, reason.trim()]);
    if (receipt.current.signature !== signature) receipt.current = { signature, key: requestId() };
    setBusy(true); setNotice(""); setFailure("");
    try {
      const result = await robotFactoryApi.review(id, item.revision, decision, reason.trim(), receipt.current.key);
      cache.setQueryData(["robot-result-detail", session?.user.id, id], result);
      setNotice(decision === "APPROVE" ? "已批准为整机工厂结果汇总。" : "已驳回，请提交人按审核意见核实后重新提交。");
      await cache.invalidateQueries({ queryKey: ["robot-result-archives"] });
      await cache.invalidateQueries({ queryKey: ["factory-robots"] });
    } catch (error) { setFailure(error instanceof Error ? error.message : "审核失败"); await query.refetch(); }
    finally { setBusy(false); }
  }
  const canReview = !query.isError && item?.status === "PENDING" && item.can_review === true && ["SYSTEM_ADMIN", "TEST_EXECUTOR", "FACTORY_REVIEWER"].includes(session?.user.role ?? "");
  return <section className="robot-result-detail" aria-label="整机结果详情">
    <header><h3>整机工厂结果详情</h3><button type="button" aria-label="关闭整机结果详情" disabled={busy} onClick={onClose}><X aria-hidden="true" /></button></header>
    {query.isPending ? <LoadingState label="正在读取整机结果详情" /> : !item ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <>
      <dl className="robot-result-meta"><div><dt>机器人 / SN</dt><dd>{item.robot_id} / {item.serial_number}</dd></div><div><dt>机型 / 上位机</dt><dd>{item.model} / {item.source_id}</dd></div><div><dt>审批状态</dt><dd>{robotResultStatusLabels[item.status]} · 审核状态版本 {item.revision}</dd></div><div><dt>提交人</dt><dd>{item.owner_name || "未提供"}</dd></div><div><dt>提交时间</dt><dd>{formatDate(item.created_at)}</dd></div><div><dt>来源</dt><dd>{item.source_kind === "simulated" ? "模拟测试" : "真实测试"}</dd></div></dl>
      <p className="robot-result-scope">{item.status === "APPROVED" ? "已批准工厂结果，可按批准记录查询。" : "本记录尚未批准，仅供核实与审核。"}结果汇总不替代逐点采集证据，也不形成研发 MTBF 暴露资格。</p>
      <p>核实及提交说明：{item.reason}</p>
      <div className="robot-result-table-scroll"><table className="robot-result-values"><thead><tr>{item.columns.map((column, i) => <th key={`${i}:${column}`} scope="col">{column}</th>)}</tr></thead><tbody>{item.rows.map((row, i) => <tr key={`${row.record_id}:${row.version}:${i}`}>{row.values.map((value, j) => <td key={j}>{value || "—"}</td>)}</tr>)}</tbody></table></div>
      <details className="robot-result-evidence"><summary>原文件与校验信息</summary><p>{item.filename} · {item.size_bytes} 字节</p><code>SHA256 {item.sha256}</code><a href={robotFactoryApi.fileHref(item.id)}>下载原始结果 CSV</a><h4>各记录的内容版本</h4><p>按记录编号追踪内容版本，与审核状态版本分别保留；原始 17 列不作改写。</p><dl>{item.rows.map((row, i) => <div key={`${row.record_id}:${i}`}><dt>{row.record_id}</dt><dd>内容版本 {row.version}</dd></div>)}</dl></details>
      {canReview && <div className="robot-result-review"><label>审核意见<textarea value={reason} maxLength={300} disabled={busy} onChange={event => setReason(event.target.value)} /></label><div><button type="button" disabled={busy || !reason.trim() || item.source_kind === "simulated"} onClick={() => void review("APPROVE")}>批准整机工厂汇总</button><button type="button" disabled={busy || !reason.trim()} onClick={() => void review("REJECT")}>驳回整机工厂汇总</button></div>{item.source_kind === "simulated" && <p>模拟测试不能批准为正式工厂记录。</p>}</div>}
      {item.status === "PENDING" && !canReview && <p>当前账号没有本记录的审核权限。</p>}
      {notice && <p role="status">{notice}</p>}{failure && <p role="alert" className="robot-result-error">{failure}</p>}
      <h4>审批记录</h4>{item.review_history.length ? item.review_history.map((entry, i) => <article className="robot-result-history" key={i}><strong>{entry.decision === "APPROVE" || entry.decision === "APPROVED" ? "批准" : "驳回"}</strong><span>{entry.reviewer_name || "审核人已留档"} · {formatDate(entry.reviewed_at || entry.created_at)}</span><p>{entry.reason}</p></article>) : <p>尚无审核结果。</p>}
    </>}
  </section>;
}

export function RobotFactoryResults({ robotId = "", sourceId = "", initialArchiveId = "" }: { robotId?: string; sourceId?: string; initialArchiveId?: string }) {
  const { session } = useAuth();
  const cache = useQueryClient();
  const [status, setStatus] = useState(""), [sn, setSn] = useState(""), [source, setSource] = useState(sourceId);
  const [filters, setFilters] = useState({ serial_number: "", source_id: sourceId }), [before, setBefore] = useState<number | null>(null), [selected, setSelected] = useState(initialArchiveId);
  const [uploadOpen, setUploadOpen] = useState(false), [notice, setNotice] = useState("");
  const canUpload = ["SYSTEM_ADMIN", "TEST_EXECUTOR"].includes(session?.user.role ?? "");
  const query = useQuery({ queryKey: ["robot-result-archives", session?.user.id, robotId, filters, status, before], queryFn: ({ signal }) => robotFactoryApi.list({ ...filters, robot_id: robotId, status, before }, signal), refetchInterval: 15_000 });
  return <section className="robot-factory-results" aria-labelledby="robot-factory-heading">
    <header><div><small>WHOLE-MACHINE FACTORY RESULTS</small><h2 id="robot-factory-heading">整机工厂结果{robotId ? ` · ${robotId}` : ""}</h2><p>标准 17 列结果汇总 · 工厂分区 · 上传和审核分开处理</p></div>{canUpload && <button type="button" className="factory-upload-button" onClick={() => setUploadOpen(value => !value)}><FileUp aria-hidden="true" />{uploadOpen ? "收起提交" : "提交整机结果 CSV"}</button>}</header>
    {uploadOpen && canUpload && <UploadForm key={`${sourceId}:${robotId}`} initialRobot={robotId} initialSource={sourceId} onUploaded={record => { setNotice(record.status === "PENDING" ? "原始文件已接收，结果待审核；尚未成为已批准工厂汇总。" : `已找到同一文件的归档回执：${robotResultStatusLabels[record.status]}。`); setUploadOpen(false); setSelected(record.id); void cache.invalidateQueries({ queryKey: ["robot-result-archives"] }); void cache.invalidateQueries({ queryKey: ["factory-robots"] }); }} />}
    {notice && <p role="status">{notice}</p>}
    <form className="robot-result-filters" onSubmit={event => { event.preventDefault(); setBefore(null); setFilters({ serial_number: sn.trim(), source_id: source.trim() }); }}><label>整机 SN<input value={sn} maxLength={200} onChange={event => setSn(event.target.value)} /></label><label>整机上位机编号<input value={source} maxLength={200} onChange={event => setSource(event.target.value)} /></label><label>整机审批状态<select value={status} onChange={event => { setStatus(event.target.value); setBefore(null); }}><option value="">全部</option><option value="PENDING">待审核</option><option value="APPROVED">已批准</option><option value="REJECTED">已驳回</option></select></label><button type="submit">查询整机结果</button></form>
    {query.isPending ? <LoadingState label="正在读取整机工厂结果" /> : !query.data ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <>
      {query.isError && <p role="alert" className="robot-result-error">刷新失败，下方为上次收到的记录。审核前请重新读取详情。</p>}
      <div className="robot-result-table-scroll"><table className="robot-result-list"><thead><tr><th>机器人 / SN</th><th>机型 / 上位机</th><th>原文件</th><th>审批状态</th><th>提交时间</th><th>操作</th></tr></thead><tbody>{query.data.items.map(item => <tr key={item.id}><td><strong>{item.robot_id}</strong><small>{item.serial_number}</small></td><td>{item.model}<small>{item.source_id}</small></td><td>{item.filename}{item.source_kind === "simulated" && <small>模拟测试</small>}</td><td><span className={`robot-result-status is-${item.status.toLowerCase()}`}>{robotResultStatusLabels[item.status]}</span></td><td>{formatDate(item.created_at)}</td><td><button type="button" className="link-button" onClick={() => { setSelected(item.id); setNotice(""); }}>查看整机结果</button></td></tr>)}</tbody></table></div>
      {!query.data.items.length && <p className="robot-result-empty">暂无符合条件的整机工厂结果。原上位机中的测试用例不会自动变为已批准记录。</p>}
      <div className="robot-result-pages">{before !== null && <button type="button" onClick={() => setBefore(null)}>整机结果第一页</button>}{query.data.next_cursor !== null && <button type="button" disabled={query.isFetching} onClick={() => setBefore(query.data!.next_cursor)}>下一页整机结果</button>}</div>
    </>}
    {selected && <ResultDetail key={selected} id={selected} onClose={() => setSelected("")} />}
  </section>;
}
