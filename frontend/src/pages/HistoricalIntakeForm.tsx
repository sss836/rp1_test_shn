import { useRef, useState, type FormEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { archivesApi, formatArchiveTimestamp, type Archive, type ArchiveContext, type ArchiveIntakeSubmit, type ArchiveVerdict } from "../archiveApi";
import { useAuth } from "../auth";
import { setupApi } from "../setupApi";
import { ErrorState, Panel } from "../components/Workbench";

function localInput(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, -1);
}

export function HistoricalIntakeForm({ item, onSubmitted }: { item: Archive; onSubmitted: (item: Archive) => Promise<void> }) {
  const { session } = useAuth();
  const cache = useQueryClient();
  const options = useQuery({ queryKey: ["setup", "options"], queryFn: ({ signal }) => setupApi.options(signal) });
  const campaigns = useQuery({ queryKey: ["setup", "campaigns"], queryFn: ({ signal }) => setupApi.campaigns(signal) });
  const [tester, setTester] = useState(item.current_verification?.tester ?? session?.user.username ?? "");
  const [stationChoice, setStation] = useState(item.current_verification?.station_id ?? item.classification?.station_id ?? "");
  const station = stationChoice || options.data?.stations.find((entry) => entry.code === item.station_label)?.id || "";
  const [verdict, setVerdict] = useState<ArchiveVerdict | "">(item.current_verification?.verdict ?? "");
  const [campaignChoice, setCampaign] = useState(item.classification?.campaign_id ?? "");
  const editableCampaigns = campaigns.data?.filter((entry) => entry.can_edit) ?? [];
  const campaign = campaignChoice || (editableCampaigns.length === 1 ? editableCampaigns[0].id : "");
  const [contextId, setContextId] = useState("");
  const contexts = useQuery({ queryKey: ["setup", "contexts", campaign], queryFn: ({ signal }) => setupApi.contexts(campaign, signal), enabled: Boolean(campaign) });
  const available = contexts.data?.filter((entry) => entry.status !== "CANCELLED") ?? [];
  const matches = available.filter((entry) => item.classification
    ? ["campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id"].every((key) => entry.context[key] === item.classification?.[key as keyof ArchiveContext])
    : entry.asset_code === item.actual_object);
  const context = available.find((entry) => entry.id === contextId) ?? (!contextId ? (matches.length === 1 ? matches[0] : available.length === 1 ? available[0] : undefined) : undefined);
  const [notes, setNotes] = useState(item.current_verification?.discrepancy_reason || (item.current_verification?.notes === "无" ? "" : item.current_verification?.notes) || "");
  const [counts, setCounts] = useState({ completed_cycles: String(item.current_verification?.declared_completed_cycles ?? ""), exception_count: String(item.current_verification?.declared_exception_count ?? "") });
  const [started, setStarted] = useState(() => localInput(item.current_verification?.declared_started_at));
  const [ended, setEnded] = useState(() => localInput(item.current_verification?.declared_ended_at));
  const [seconds, setSeconds] = useState(String(item.current_verification?.declared_active_seconds ?? ""));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const receipt = useRef({ signature: "", key: "" });
  const detected = item.detected_timing;
  const different = Boolean(verdict && verdict !== (item.manifest?.analysis?.verdict ?? item.facts.automatic_verdict));
  const stationMismatch = Boolean(context && station && context.context.station_id !== station);
  const notesRequired = different || stationMismatch;
  const missingCounts = (["completed_cycles", "exception_count"] as const).filter((key) => item.facts[key] == null);
  const complete = tester.trim() && station && verdict && context && (!notesRequired || notes.trim())
    && missingCounts.every((key) => counts[key] !== "")
    && (detected || item.facts.started_at || started && ended)
    && (detected || item.facts.active_seconds != null || seconds !== "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!complete || !context || !verdict || busy) return;
    const declared = Object.fromEntries(missingCounts.map((key) => [`declared_${key}`, Number(counts[key])]));
    const payload: ArchiveIntakeSubmit = {
      verification: { expected_revision: item.revision, tester: tester.trim(), station_id: station, verdict, notes: notes.trim(), ...declared,
        ...(!detected && !item.facts.started_at ? { declared_started_at: new Date(started).toISOString(), declared_ended_at: new Date(ended).toISOString() } : {}),
        ...(!detected && item.facts.active_seconds == null ? { declared_active_seconds: Number(seconds) } : {}) },
      context: { ...context.context } as ArchiveContext, expected_submission_revision: item.submission_revision ?? 0,
    };
    const signature = JSON.stringify(payload);
    if (receipt.current.signature !== signature) receipt.current = { signature, key: crypto.randomUUID() };
    setBusy(true); setError(null);
    try { await onSubmitted(await archivesApi.intakeSubmit(item.id, payload, receipt.current.key)); }
    catch (failure) { setError(failure); }
    finally { setBusy(false); }
  }

  return <Panel title={item.submission_status === "REJECTED" ? "修改补录并重新送审" : "补录确认"} description={`${item.test_name} · ${item.actual_object}`}>
    <form className="archive-form" onSubmit={(event) => void submit(event)}><div className="archive-form-grid">
      <label>测试人<input required maxLength={100} value={tester} onChange={(event) => setTester(event.target.value)} /></label>
      <label>工位<select required value={station} onChange={(event) => setStation(event.target.value)}><option value="">选择工位</option>{options.data?.stations.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
      <label>测试结论<select required value={verdict} onChange={(event) => setVerdict(event.target.value as ArchiveVerdict | "")}><option value="">选择结论</option><option value="passed">通过</option><option value="failed">失败</option><option value="blocked">待判定 / 阻塞</option></select></label>
      <label>测试计划<select required value={campaign} onChange={(event) => { setCampaign(event.target.value); setContextId(""); }}><option value="">选择测试计划</option>{editableCampaigns.map((entry) => <option key={entry.id} value={entry.id}>{entry.code} · {entry.name}</option>)}</select></label>
      <label className="archive-intake-context">测试用例 / 样品<select required value={context?.id ?? ""} disabled={!campaign || contexts.isLoading} onChange={(event) => setContextId(event.target.value)}><option value="">选择用例和样品</option>{available.map((entry) => <option key={entry.id} value={entry.id}>{entry.case_code} · {entry.case_name} · {entry.asset_code}</option>)}</select></label>
      {missingCounts.map((key) => <label key={key}>{key === "completed_cycles" ? "完成循环" : "异常次数"}<input required type="number" min={0} max={2147483647} step={1} value={counts[key]} onChange={(event) => setCounts({ ...counts, [key]: event.target.value })} /></label>)}
      <label className="archive-form-wide">备注{notesRequired ? "（请说明人工判断或工位差异）" : "（选填）"}<textarea rows={2} required={notesRequired} maxLength={1000} value={notes} onChange={(event) => setNotes(event.target.value)} placeholder={notesRequired ? "简述判断依据或现场测试情况" : "有补充情况时填写"} /></label>
    </div>
      {detected ? <p className="archive-intake-time">{formatArchiveTimestamp(detected.started_at)} 至 {formatArchiveTimestamp(detected.ended_at)} · {detected.recorded_seconds} 秒<small>北京时间 · 已根据文件名和 CSV 自动识别，结束对应末条采样</small></p>
        : <div className="archive-form-grid">{!item.facts.started_at && <><label>实际开始时间<input required type="datetime-local" step="0.001" value={started} onChange={(event) => setStarted(event.target.value)} /></label><label>实际结束时间<input required type="datetime-local" step="0.001" value={ended} onChange={(event) => setEnded(event.target.value)} /></label></>}{item.facts.active_seconds == null && <label>运行时长（秒）<input required type="number" min={0} max={315360000} step="0.001" value={seconds} onChange={(event) => setSeconds(event.target.value)} /></label>}</div>}
      {stationMismatch && <p className="archive-muted">所选工位与原计划不同，本次采用实际工位，请在备注中说明。</p>}
      <button type="submit" disabled={busy || !complete}>{busy ? "正在送审…" : item.submission_status === "REJECTED" ? "确认修改并重新送审" : "确认并送审"}</button>
      {[options.error, campaigns.error, contexts.error, error].filter(Boolean).map((failure, index) => <ErrorState key={index} error={failure} retry={() => { setError(null); void cache.invalidateQueries({ queryKey: ["archives", "detail", item.id] }); void options.refetch(); void campaigns.refetch(); if (campaign) void contexts.refetch(); }} />)}
    </form>
  </Panel>;
}
