import { useEffect, useId, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CheckCircle2, FileUp, UploadCloud, X, ChartNoAxesCombined, AlertCircle } from "lucide-react";
import { PageTitle, Panel, ErrorState } from "../components/Workbench";
import { useAuth } from "../auth";
import { HistoricalIntakeForm } from "./HistoricalIntakeForm";
import { setupApi, type SetupOptions } from "../setupApi";
import { archivesApi, archiveTitle, type Archive, type ArchiveContext, type ArchiveVerdict } from "../archiveApi";
import { type CsvPreflight } from "../imports/csvPreflight";
import { preflightCsv } from "../imports/preflightClient";
import { importApi, importRequestKey, uploadCsv } from "../imports/importApi";
import { ArchiveStatus } from "./ArchivesPage";
import "./csvImport.css";

type Navigate = (view: "archives" | "archive" | "curves", id?: string) => void;
type Metadata = { actualObject: string; station: string; tester: string; verdict: ArchiveVerdict | ""; notes: string; discrepancy: string };
const uploadReceiptNotice = "原文件已接收，等待服务器校验和解析。人工核实尚未提交。";
const metadataLabels: Record<string, string> = { sample: "采样索引", stage: "阶段", stage_code: "阶段", cycle: "循环标识", cycle_no: "循环标识", cycle_index: "循环标识" };
const blank: Metadata = { actualObject: "", station: "", tester: "", verdict: "", notes: "", discrepancy: "" };
const message = (error: unknown) => error instanceof Error ? error.message : "操作失败，请重试。";
const number = (value: number | null) => value === null ? "未记录" : value.toLocaleString("zh-CN", { maximumFractionDigits: 6 });

function MetadataFields({ value, onChange, options, prefix, disabled = false, requiredBasic = false }: { value: Metadata; onChange: (value: Metadata) => void; options?: SetupOptions; prefix: string; disabled?: boolean; requiredBasic?: boolean }) {
  return <div className="import-form-grid"><label htmlFor={`${prefix}-object`}>样品 / 实际对象<input required={requiredBasic} id={`${prefix}-object`} list={`${prefix}-assets`} maxLength={200} disabled={disabled} value={value.actualObject} onChange={(event) => onChange({ ...value, actualObject: event.target.value })} placeholder="选择已有样品或填写实际对象" /></label><datalist id={`${prefix}-assets`}>{options?.assets.map((asset) => <option key={asset.id} value={asset.code}>{asset.name}</option>)}</datalist><label htmlFor={`${prefix}-station`}>有效工位<select required={requiredBasic} id={`${prefix}-station`} disabled={disabled} value={value.station} onChange={(event) => onChange({ ...value, station: event.target.value })}><option value="">选择工位</option>{options?.stations.map((station) => <option key={station.id} value={station.id}>{station.code} · {station.name}</option>)}</select></label></div>;
}

function PreflightDetails({ result }: { result: CsvPreflight }) {
  return <><dl className="import-facts"><div><dt>数据行</dt><dd>{number(result.rows)}</dd></div><div><dt>原始字段</dt><dd>{result.columns.length}</dd></div><div><dt>相对采样范围 / s</dt><dd>{number(result.start)} – {number(result.end)}</dd></div><div><dt>最大采样间隔 / s</dt><dd>{number(result.maxGap)}</dd></div></dl>
    {!!result.errors.length && <ul className="import-errors" role="alert">{result.errors.map((item) => <li key={item}>{item}</li>)}</ul>}
    {!!result.warnings.length && <details className="import-warnings" open><summary>数据完整性提示 · {result.warnings.length}</summary><ul>{result.warnings.map((item) => <li key={item}>{item}</li>)}</ul></details>}
    <details className="import-column-details"><summary>查看只读字段、单位、缺失与峰谷</summary><div className="table-scroll"><table className="data-table"><thead><tr><th>原始字段</th><th>单位</th><th>有效值</th><th>缺失</th><th>最小值</th><th>最大值</th></tr></thead><tbody>{result.columns.map((column) => <tr key={column.name}><td>{column.name}{column.kind === "metadata" && <small> · {metadataLabels[column.name] ?? "元数据"}（不参与参数统计）</small>}</td><td>{column.kind === "metadata" ? "—" : column.unit}</td><td>{column.kind === "metadata" ? "—" : number(column.count)}</td><td>{column.kind === "metadata" ? "—" : column.count === 0 && column.missing > 0 ? `全部缺失（${column.missing}）` : number(column.missing)}</td><td>{column.kind === "metadata" ? "—" : number(column.min)}</td><td>{column.kind === "metadata" ? "—" : number(column.max)}</td></tr>)}</tbody></table></div></details>
    <details className="import-hash"><summary>SHA-256 原文件校验</summary><code>{result.sha256}</code></details>
  </>;
}

function ImportFile({ file, initial, bulk, options, owner, navigate, remove }: { file: File; initial: Metadata; bulk: { value: Metadata; revision: number }; options?: SetupOptions; owner: string; navigate: Navigate; remove: () => void }) {
  const cache = useQueryClient();
  const fieldId = useId();
  const [meta, setMeta] = useState(initial);
  const [name, setName] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [preflight, setPreflight] = useState<CsvPreflight | null>(null);
  const [percent, setPercent] = useState(0);
  const [preflightError, setPreflightError] = useState("");
  const [archive, setArchive] = useState<Archive | null>(null);
  const [reused, setReused] = useState(false);
  const [lookupDone, setLookupDone] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [notice, setNotice] = useState("");
  const [campaign, setCampaign] = useState("");
  const [contextId, setContextId] = useState("");
  const abort = useRef<AbortController | null>(null);
  const active = useRef(true);
  const previousBulk = useRef(bulk.revision);
  const campaigns = useQuery({ queryKey: ["setup", "campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const contexts = useQuery({ queryKey: ["setup", "contexts", campaign], queryFn: ({ signal }) => setupApi.contexts(campaign, signal), enabled: Boolean(campaign) });
  const context = contexts.data?.find((item) => item.id === contextId);
  const detail = useQuery({ queryKey: ["archives", "detail", archive?.id], queryFn: ({ signal }) => archivesApi.detail(archive!.id, signal), enabled: Boolean(archive), refetchInterval: (query) => ["READY", "FAILED"].includes(query.state.data?.processing_state ?? "") ? false : 3000 });
  const current = detail.data ?? archive;
  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    void preflightCsv(file, setPercent, controller.signal).then(async (result) => {
      if (!active.current) return;
      setPreflight(result);
      if (result.errors.length) return;
      try { const match = await importApi.lookup(result.sha256, file.size, controller.signal); if (active.current) { setArchive(match.archive); setReused(Boolean(match.archive)); setLookupDone(true); } }
      catch (failure) { if (!controller.signal.aborted) setError(`查重失败：${message(failure)}`); }
    }).catch((failure) => { if (!controller.signal.aborted) setPreflightError(message(failure)); });
    return () => { active.current = false; controller.abort(); abort.current?.abort(); };
  }, [file]);
  useEffect(() => {
    if (bulk.revision !== previousBulk.current) {
      previousBulk.current = bulk.revision;
      if (!archive && !busy) setMeta(bulk.value);
    }
  }, [bulk, archive, busy]);
  async function retryLookup() {
    if (!preflight) return;
    setError("");
    try { const match = await importApi.lookup(preflight.sha256, file.size); setArchive(match.archive); setReused(Boolean(match.archive)); setLookupDone(true); }
    catch (failure) { setError(`查重失败：${message(failure)}`); }
  }
  async function upload() {
    if (!preflight || busy) return;
    const controller = new AbortController(); abort.current = controller;
    setBusy(true); setUploading(true); setError(""); setNotice("");
    try {
      let target = current;
      if (!target) {
        const classification = context ? { ...context.context, station_id: meta.station } as ArchiveContext : null;
        const result = await importApi.create({ filename: file.name, size_bytes: file.size, sha256: preflight.sha256, test_name: name.trim(), actual_object: meta.actualObject.trim(), station_id: meta.station, classification }, importRequestKey(owner, preflight.sha256), controller.signal);
        target = result.archive; setArchive(target); setReused(result.reused);
        if (result.reused) { setNotice("发现相同文件，已复用原归档。请核对原信息后再选择续传。"); return; }
      }
      const updated = await uploadCsv(file, target, controller.signal, (value) => { if (active.current) { setArchive(value); cache.setQueryData(["archives", "detail", value.id], value); } });
      setArchive(updated); setNotice(uploadReceiptNotice);
      await cache.invalidateQueries({ queryKey: ["archives"] });
    } catch (failure) { if (active.current) setError(controller.signal.aborted ? "已暂停。重新选择同一文件或点击续传，将按服务器回执继续。" : message(failure)); }
    finally { abort.current = null; if (active.current) { setBusy(false); setUploading(false); } }
  }
  const validBasic = Boolean(name.trim() && meta.actualObject.trim() && meta.station && options?.stations.some((item) => item.id === meta.station));
  const contextMismatch = Boolean(context && (context.asset_code !== meta.actualObject.trim() || context.context.station_id !== meta.station));
  const canUpload = preflight && !preflight.errors.length && lookupDone && confirmed && validBasic && (!campaign || context) && !contextMismatch;
  const canResume = current?.upload_state === "UPLOADING" && current.is_owner && current.can_edit;
  const canConfirm = current?.processing_state === "READY" && current.origin === "HISTORY" && current.is_owner && current.can_edit;
  const displayNotice = notice === uploadReceiptNotice && current
    ? current.processing_state === "READY"
      ? "原文件校验和解析已完成，可查看曲线。人工核实尚未提交。"
      : current.processing_state === "FAILED" || current.upload_state === "CORRUPT"
        ? "原文件已接收，但服务器校验或解析失败。请查看归档错误详情。"
        : current.processing_state === "PROCESSING"
          ? "原文件已接收，服务器正在校验和解析。人工核实尚未提交。"
          : notice
    : notice;
  const uploadMissing = [
    !name.trim() && "测试名称", !meta.actualObject.trim() && "样品 / 实际对象",
    (!meta.station || !options?.stations.some((item) => item.id === meta.station)) && "有效工位",
    !confirmed && "独立执行确认", Boolean(campaign && !context) && "完整计划上下文",
    contextMismatch && "样品 / 工位与计划一致", !lookupDone && "完成文件查重"
  ].filter(Boolean);
  return <article className="import-file" aria-label={`补录文件 ${file.name}`}>
    <header className="import-file-heading"><FileUp aria-hidden="true" /><div><h3>{file.name}</h3><p>{(file.size / 1024 / 1024).toFixed(2)} MiB · 原文件保持不变</p></div><button className="import-icon" type="button" aria-label={`移除 ${file.name}`} disabled={busy} onClick={remove}><X aria-hidden="true" /></button></header>
    {!preflight && !preflightError && <div role="status" aria-live="polite" aria-atomic="true" className="import-progress"><span>正在本地预检与计算校验值 · {percent}%</span><progress aria-label={`${file.name} 本地预检与校验进度`} value={percent} max={100} /></div>}
    {preflightError && <p className="import-errors" role="alert">{preflightError}</p>}
    {preflight && <PreflightDetails result={preflight} />}
    {!current && preflight && !preflight.errors.length && <div className="import-file-form"><h4>执行信息</h4><p className="import-hint">填写测试名称、样品和工位。解析完成后确认结论，一次送审。</p><label>测试名称<input required maxLength={200} value={name} disabled={busy} onChange={(event) => setName(event.target.value)} placeholder="填写这一次测试的名称" /></label><MetadataFields value={meta} onChange={setMeta} options={options} prefix={`file-${fieldId}`} disabled={busy} requiredBasic />
      <details><summary>可选：关联正式计划与用例</summary><div className="import-form-grid"><label>测试计划<select value={campaign} disabled={busy} onChange={(event) => { setCampaign(event.target.value); setContextId(""); }}><option value="">暂不关联，作为待归类记录</option>{campaigns.data?.filter((entry) => entry.can_edit).map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label><label>已发布用例与样品<select value={contextId} disabled={!campaign || busy || contexts.isLoading} onChange={(event) => setContextId(event.target.value)}><option value="">选择完整计划上下文</option>{contexts.data?.filter((entry) => entry.status !== "CANCELLED").map((entry) => <option key={entry.id} value={entry.id}>{entry.case_code} · {entry.case_name} · {entry.version} · {entry.asset_code}</option>)}</select></label></div>{contextMismatch && <p className="import-errors">所选计划中的样品或工位与本次填写不一致，请核对后再上传。</p>}{(campaigns.error || contexts.error) && <p role="alert" className="import-errors">正式上下文读取失败：{message(campaigns.error || contexts.error)}</p>}<p className="import-hint">仅保存已有计划的完整归属；解析后在同一表单确认并送审。</p></details>
      <label className="import-confirm"><input type="checkbox" required checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />我确认此文件对应一次独立执行，不与其他所选文件合并</label>
      <p className="import-hint">上传为待核实记录。实际测试人、人工结论和备注可在解析后确认；不会自动批准或计入正式统计 / MTBF。</p>
      {uploadMissing.length > 0 && <p className="import-hint" id={`${fieldId}-upload-missing`}>上传前还需：{uploadMissing.join("、")}。</p>}
      <button type="button" className="import-primary" aria-describedby={uploadMissing.length ? `${fieldId}-upload-missing` : undefined} disabled={!canUpload || busy} onClick={() => void upload()}><UploadCloud aria-hidden="true" />{busy ? "正在上传…" : "上传并解析待核实记录"}</button>
    </div>}
    {current && <div className="import-receipt"><div className="import-receipt-title"><CheckCircle2 aria-hidden="true" /><strong>{reused ? "相同文件已存在" : "归档记录已建立"} · {archiveTitle(current)}</strong></div><p>{current.test_name} · {current.actual_object} · {current.station_label}</p><ArchiveStatus item={current} /><p className="import-hint">执行 UUID：{current.execution_uuid}</p>
      {reused && <p className="import-hint">显示原记录。批量填写的信息未写入此归档，修改需进入原归档追加核实版本。</p>}
      {current.upload_state === "UPLOADING" && <div role="status" aria-live="polite" aria-atomic="true" className="import-progress"><span>服务器已接收 {(current.received_bytes / 1024 / 1024).toFixed(2)} / {(current.size_bytes / 1024 / 1024).toFixed(2)} MiB</span><progress aria-label={`${file.name} 原文件上传进度`} max={current.size_bytes} value={current.received_bytes} /></div>}
      {current.processing_error && <p role="alert" className="import-errors">服务器校验 / 解析失败：{current.processing_error}</p>}
      <div className="import-actions">{canResume && <button type="button" disabled={busy} onClick={() => void upload()}>继续上传同一原文件</button>}{uploading && <button type="button" onClick={() => abort.current?.abort()}>暂停上传</button>}<button type="button" onClick={() => navigate("archive", current.id)}>查看归档 / 核实详情</button><button type="button" disabled={current.processing_state !== "READY"} onClick={() => navigate("curves", current.id)}><ChartNoAxesCombined aria-hidden="true" />查看曲线</button></div>
      {canConfirm && <HistoricalIntakeForm key={`intake:${current.revision}`} item={current} onSubmitted={async (updated) => { setArchive(updated); cache.setQueryData(["archives", "detail", current.id], updated); setNotice("已送审，请到测试数据审批页面审核。"); await cache.invalidateQueries({ queryKey: ["archives"] }); await cache.invalidateQueries({ queryKey: ["submissions"] }); }} />}

    </div>}
    {displayNotice && <p role="status" className="import-notice">{displayNotice}</p>}{error && <div role="alert" className="import-errors">{error}{!lookupDone && preflight && <button type="button" onClick={() => void retryLookup()}>重新查重</button>}{current && <button type="button" onClick={() => void detail.refetch()}>刷新归档状态</button>}</div>}
  </article>;
}

export default function CsvImportPage({ navigate }: { navigate: Navigate }) {
  const { session } = useAuth();
  const [files, setFiles] = useState<{ id: string; file: File; initial: Metadata }[]>([]);
  const [meta, setMeta] = useState<Metadata>(blank);
  const [bulk, setBulk] = useState({ value: blank, revision: 0 });
  const [dragging, setDragging] = useState(false);
  const [notice, setNotice] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const allowed = Boolean(session && session.user.role !== "VIEWER");
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal), enabled: allowed });
  function selectFiles(selected: File[]) {
    const next: typeof files = [], issues: string[] = [];
    for (const file of selected) {
      if (!/\.csv$/i.test(file.name)) { issues.push(`${file.name}：请选择 CSV 文件。`); continue; }
      if (!file.size) { issues.push(`${file.name}：文件为空。`); continue; }
      if ([...files, ...next].some((item) => item.file === file)) { issues.push(`${file.name}：已在当前队列。`); continue; }
      if (files.length + next.length >= 20) { issues.push("单批最多 20 份，请分批补录。"); break; }
      next.push({ id: crypto.randomUUID(), file, initial: { ...meta } });
    }
    setFiles((previous) => [...previous, ...next]); setNotice(issues.join(" "));
  }
  if (!allowed) return <Panel title="数据补录"><p>仅执行员和管理员可以补录测试数据，请联系有权限的人员。</p></Panel>;
  return <div className="csv-import-page"><button type="button" className="back-button" onClick={() => navigate("archives")}><ArrowLeft aria-hidden="true" />返回数据归档</button><PageTitle eyebrow="LOCAL CSV IMPORT" title="数据补录" description="选择本地 CSV，核对每次测试的信息，再归档查看曲线。" />
    <ol className="import-steps"><li><span>1</span>选择 CSV</li><li><span>2</span>核对信息并上传</li><li><span>3</span>确认并送审</li></ol>
    <div className={`import-workbench${files.length > 1 ? "" : " single"}`}>{files.length > 1 && <aside><Panel title="批量填写" description="应用到尚未上传的文件，每份文件仍独立确认。"><MetadataFields value={meta} onChange={setMeta} options={options.data} prefix="bulk" /><button type="button" className="import-bulk-button" disabled={!files.length} onClick={() => { setBulk({ value: { ...meta }, revision: bulk.revision + 1 }); setNotice("已应用到尚未建立归档的文件；现有归档信息保持原样。"); }}>应用到未上传文件</button><p className="import-hint">登录提交人：{session?.user.display_name ?? "当前账户"}。测试人会在确认表单中自动带入，可修改。</p>{options.error && <ErrorState error={options.error} retry={() => void options.refetch()} />}</Panel></aside>}
    <div className="import-queue"><div className={`import-dropzone${dragging ? " dragging" : ""}`} onDragOver={(event) => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); selectFiles(Array.from(event.dataTransfer.files)); }}><UploadCloud aria-hidden="true" /><h2>拖入 CSV 文件</h2><p>支持多份文件 · 先在本机预检，不自动上传</p><button type="button" onClick={() => input.current?.click()}>选择本地 CSV</button><input ref={input} tabIndex={-1} type="file" accept=".csv,text/csv" multiple aria-label="选择本地 CSV 文件" onChange={(event) => { selectFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} /></div>
      <p className="import-execution-note"><AlertCircle aria-hidden="true" />一份文件对应一次独立执行。属于同一次执行的分片请保留原件，勿作为多次测试补录。</p>
      {notice && <p role="status" className="import-notice">{notice}</p>}
      {files.length > 0 && <div className="import-queue-heading"><h2>待处理文件</h2><span>{files.length} 份</span></div>}
      {files.map(({ id, file, initial }) => <ImportFile key={id} file={file} initial={initial} bulk={bulk} options={options.data} owner={session!.user.id} navigate={navigate} remove={() => setFiles((previous) => previous.filter((item) => item.id !== id))} />)}
    </div></div>
  </div>;
}
