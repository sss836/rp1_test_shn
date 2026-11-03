import { apiRequest } from "./client";

export type ArchiveDataDomain = "RESEARCH" | "FACTORY" | "UNCLASSIFIED";
export type ArchiveDomainSource = "EXPLICIT" | "HISTORICAL_BENCH" | "NO_EVIDENCE" | "CORRECTION";
export type ArchiveFilters = { q?: string; origin?: string; classification_state?: string; processing_state?: string; submission_status?: string; execution_uuid?: string; data_domain?: ArchiveDataDomain; sn?: string; bench?: string; started_from?: string; started_to?: string; before?: number | null };
export type FactoryStatistics = { total_count: number; pending_count: number; approved_count: number; passed_count: number; failed_count: number; blocked_count: number; active_seconds: number };
export const dataDomainLabels: Record<ArchiveDataDomain, string> = { RESEARCH: "研发", FACTORY: "工厂", UNCLASSIFIED: "分区待归类" };
export const dataDomainSourceLabels: Record<ArchiveDomainSource, string> = { EXPLICIT: "本次显式选择", HISTORICAL_BENCH: "历史台架证据推导", NO_EVIDENCE: "缺少分区证据", CORRECTION: "分区修正版本" };
export type ArchiveOrigin = "HMI" | "HISTORY" | "OTHER";
export type ArchiveVerdict = "passed" | "failed" | "blocked";
export type ArchiveCatalogItem = { key: string; subject: string; metric: string; unit: string };
export type ArchiveDetectedTiming = { version: string; filename: string; evidence_sha256: string; timezone: "UTC"; end_basis: "last_sample"; started_at: string; first_sample_at: string; ended_at: string; recorded_seconds: number; first_offset_seconds: number; last_offset_seconds: number };
export type ArchiveVerification = { revision: number; tester: string; station_id: string; station_code?: string; station_name?: string; verdict: ArchiveVerdict; notes: string; discrepancy_reason: string; declared_started_at?: string | null; declared_ended_at?: string | null; declared_active_seconds?: number | null; declared_completed_cycles?: number | null; declared_exception_count?: number | null; timing_evidence?: ArchiveDetectedTiming; created_at: string };
export type ArchiveContext = { campaign_id: string; cycle_id: string; segment_id: string; asset_id: string; configuration_id: string; test_case_version_id: string; station_id: string };
export type ArchiveIntakeSubmit = { verification: { expected_revision: number; tester: string; station_id: string; verdict: ArchiveVerdict; notes?: string; declared_started_at?: string; declared_ended_at?: string; declared_active_seconds?: number; declared_completed_cycles?: number; declared_exception_count?: number }; context: ArchiveContext; expected_submission_revision: number };
export type Archive = {
  id: string; execution_uuid: string; archive_code: string; owner_public_id: string; is_owner: boolean; can_edit: boolean; can_analyze?: boolean;
  data_domain?: ArchiveDataDomain; data_domain_source?: ArchiveDomainSource; serial_number?: string | null; bench_id?: string | null; can_review?: boolean;
  origin: ArchiveOrigin; test_name: string; actual_object: string; station_label: string; filename: string; size_bytes: number; sha256: string; received_bytes: number;
  upload_state: "UPLOADING" | "COMPLETE" | "CORRUPT"; processing_state: "WAITING" | "QUEUED" | "PROCESSING" | "READY" | "FAILED";
  processing_error: string | null; classification_state: "UNCLASSIFIED" | "CLASSIFIED"; revision: number;
  facts: Record<string, unknown>; acceptance: Record<string, unknown>; detected_timing?: ArchiveDetectedTiming | null;
  manifest: { analysis?: Record<string, unknown>; catalog?: ArchiveCatalogItem[]; time_range?: { start: number; end: number }; stages?: unknown[]; cycles?: unknown[]; row_count?: number; [key: string]: unknown } | null;
  current_verification: ArchiveVerification | null; classification: (ArchiveContext & { revision: number }) | null;
  submission_id: string | null; submission_revision: number | null; submission_status: "PENDING" | "APPROVED" | "REJECTED" | null;
  execution_id: string | null; execution_code: string | null; created_at: string;
  verified_station_code?: string | null; verified_station_name?: string | null;
  verification_history?: { revision: number; payload: ArchiveVerification; reason: string; created_at: string; actor_name?: string }[];
  domain_history?: { revision: number; data_domain: ArchiveDataDomain; data_domain_source: ArchiveDomainSource; serial_number: string | null; bench_id: string | null; evidence: Record<string, unknown>; reason: string; created_at: string; actor_name?: string }[];
  factory_review_history?: { revision: number; decision: "APPROVED" | "REJECTED"; reason: string; created_at: string; actor_name?: string }[];
  classification_history?: { revision: number; payload: ArchiveContext; reason: string; created_at: string; actor_name?: string }[];
};
export type ArchiveCurve = { key: string; subject: string; metric: string; unit: string; points: [number, number | null][]; sample_count: number; returned_points: number; downsampled: boolean };
export type ArchiveAnalysis = { id: string; version: number; source_test_case_version_id: string; source_case_code: string; source_case_name: string; source_version: string; mapping_status: "MAPPED" | "UNCONFIGURED" | "INVALID"; mapping_notes: string; acceptance: Record<string, unknown>; report: Record<string, unknown>; original_sha256: string; original_analysis_version: string | number; actor_name: string; reason: string; created_at: string };
export type ArchiveCurves = { archive_id: string; execution_uuid: string; archive_code: string; range: { start: number; end: number }; series: ArchiveCurve[]; gaps: { start: number; end: number }[]; resolution: "raw" | "minmax"; catalog: ArchiveCatalogItem[]; time_range: { start: number; end: number } };
export const archiveUploadLabels = { UPLOADING: "上传中", COMPLETE: "归档完成", CORRUPT: "校验失败" };
export const archiveProcessingLabels = { WAITING: "等待归档", QUEUED: "等待解析", PROCESSING: "解析中", READY: "可查询", FAILED: "解析失败" };
export const verdictLabels: Record<ArchiveVerdict, string> = { passed: "通过", failed: "失败", blocked: "待判定 / 阻塞" };

function queryString(values: Record<string, string | number | null | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  return search.toString();
}
function mutate(id: string, action: string, payload: unknown, reason: string, key: string) {
  return apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}/${action}`, { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": reason }, body: JSON.stringify(payload) });
}
export const archivesApi = {
  list: (filters: ArchiveFilters = {}, signal?: AbortSignal) => apiRequest<{ items: Archive[]; next_cursor: number | null }>(`/api/v1/archives?${queryString({ limit: 50, ...filters })}`, { signal }),
  factoryStatistics: (filters: Pick<ArchiveFilters, "sn" | "bench" | "started_from" | "started_to"> = {}, signal?: AbortSignal) => apiRequest<FactoryStatistics>(`/api/v1/factory/statistics?${queryString(filters)}`, { signal }),
  factoryReview: (id: string, payload: { expected_submission_revision: number; decision: "APPROVE" | "REJECT"; reason: string }, key: string) => mutate(id, "factory-review", payload, payload.reason, key),
  classifyDomain: (id: string, payload: { expected_revision: number; data_domain: "RESEARCH" | "FACTORY"; serial_number?: string; bench_id?: string; reason: string }, key: string) => mutate(id, "domain-classification", payload, payload.reason, key),
  detail: (id: string, signal?: AbortSignal) => apiRequest<Archive>(`/api/v1/archives/${encodeURIComponent(id)}`, { signal }),
  analyses: (id: string, signal?: AbortSignal) => apiRequest<{ items: ArchiveAnalysis[]; original_analysis: Record<string, unknown> }>(`/api/v1/archives/${encodeURIComponent(id)}/analyses`, { signal }),
  analyze: (id: string, payload: { test_case_version_id: string; reason: string }, key: string) => apiRequest<ArchiveAnalysis>(`/api/v1/archives/${encodeURIComponent(id)}/analyses`, { method: "POST", headers: { "Idempotency-Key": key, "X-Change-Reason": payload.reason }, body: JSON.stringify(payload) }),
  curves: (id: string, filters: { metrics: string; subjects?: string; start?: number; end?: number; max_points?: number; stage?: string; cycle?: string }, signal?: AbortSignal) => apiRequest<ArchiveCurves>(`/api/v1/archives/${encodeURIComponent(id)}/curves?${queryString(filters)}`, { signal }),
  verify: (id: string, payload: { expected_revision: number; tester: string; station_id: string; verdict: ArchiveVerdict; notes: string; discrepancy_reason: string; reason: string; declared_started_at?: string; declared_ended_at?: string; declared_active_seconds?: number; declared_completed_cycles?: number; declared_exception_count?: number }, key: string) => mutate(id, "verification", payload, payload.reason, key),
  intakeSubmit: (id: string, payload: ArchiveIntakeSubmit, key: string) => mutate(id, "verify-and-submit", payload, "历史CSV补录送审", key),
  classify: (id: string, payload: { expected_revision: number; context: ArchiveContext; reason: string }, key: string) => mutate(id, "classification", payload, payload.reason, key),
  submit: (id: string, payload: { expected_revision: number; expected_submission_revision: number; reason: string }, key: string) => mutate(id, "submit", payload, payload.reason, key)
};

export function archiveTitle(item: Pick<Archive, "execution_code" | "archive_code" | "test_name">) {
  return item.execution_code || item.archive_code || item.test_name;
}

export function formatArchiveTimestamp(value: string) {
  const clock = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).format(new Date(value));
  const fraction = value.match(/\.(\d+)(?:Z|[+-])/i)?.[1] ?? "";
  return `${clock}.${fraction.padEnd(6, "0").slice(0, 6)}`;
}

export function archiveStation(item: Archive) {
  const code = item.current_verification?.station_code ?? item.verified_station_code;
  const name = item.current_verification?.station_name ?? item.verified_station_name;
  return code ? `${code}${name ? ` · ${name}` : ""}` : item.station_label;
}

export function missingArchiveAcquisitionFacts(item: Archive) {
  const missing = ["active_seconds", "completed_cycles", "exception_count"].filter((key) => typeof item.facts[key] !== "number" || !Number.isFinite(item.facts[key]));
  if (!item.facts.machine_status || item.facts.machine_status === "unknown") missing.push("machine_status");
  return missing;
}

export function missingArchiveSubmissionInfo(item: Archive) {
  const verification = item.current_verification;
  const missing = ["active_seconds", "completed_cycles", "exception_count"].filter((key) => {
    const value = item.facts[key] ?? verification?.[`declared_${key}` as keyof ArchiveVerification];
    return typeof value !== "number" || !Number.isFinite(value);
  });
  if (!item.facts.started_at && !verification?.declared_started_at) missing.push("started_at");
  if (!item.facts.ended_at && !verification?.declared_ended_at) missing.push("ended_at");
  if ((!item.facts.machine_status || item.facts.machine_status === "unknown") && !verification?.verdict) missing.push("verdict");
  return missing;
}

// A missing field is not evidence for an execution mode. Old responses remain
// readable while the authoritative server classification is being rolled out.
export function archiveDomain(item: Pick<Archive, "data_domain">): ArchiveDataDomain {
  return item.data_domain ?? "UNCLASSIFIED";
}

export function archiveStartedAt(item: Archive): string | null {
  return (item.facts.started_at ?? item.current_verification?.declared_started_at ?? item.detected_timing?.started_at ?? null) as string | null;
}

export function archiveLedgerCsv(items: Archive[]) {
  // Treat cells as text when they could be interpreted as spreadsheet formulae.
  const cell = (value: unknown) => {
    const raw = value == null ? "" : String(value);
    const safe = /^[=+\-@\t\r]/.test(raw) ? `'${raw}` : raw;
    return `"${safe.replaceAll('"', '""')}"`;
  };
  const rows: unknown[][] = [["业务分区", "分区依据", "执行编号", "稳定执行标识", "测试名称", "SN", "实际对象", "台架", "核实工位", "采集开始", "审批状态", "核实结论", "原始运行秒数", "核实申报运行秒数", "原始文件SHA256"]];
  for (const item of items) rows.push([dataDomainLabels[archiveDomain(item)], item.data_domain_source ? dataDomainSourceLabels[item.data_domain_source] : "未返回分区依据", archiveTitle(item), item.execution_uuid, item.test_name, item.serial_number, item.actual_object, item.bench_id, archiveStation(item), archiveStartedAt(item), item.submission_status ?? "NOT_SUBMITTED", item.current_verification?.verdict ?? "", item.facts.active_seconds, item.current_verification?.declared_active_seconds, item.sha256]);
  return "\uFEFF" + rows.map((row) => row.map(cell).join(",")).join("\r\n");
}

export async function exportArchiveLedger(filters: ArchiveFilters, signal?: AbortSignal) {
  const items: Archive[] = [];
  const cursors = new Set<number>();
  let before: number | null = null;
  do {
    const page = await archivesApi.list({ ...filters, before }, signal);
    items.push(...page.items);
    before = page.next_cursor;
    if (before !== null) {
      if (cursors.has(before)) throw new Error("归档分页游标重复，导出已停止，请刷新后重试。");
      cursors.add(before);
    }
  } while (before !== null);
  return new Blob([archiveLedgerCsv(items)], { type: "text/csv;charset=utf-8" });
}

export function archiveDateFilters(from: string, to: string): Pick<ArchiveFilters, "started_from" | "started_to"> {
  const started = from ? new Date(`${from}T00:00:00+08:00`) : null;
  const ended = to ? new Date(`${to}T00:00:00+08:00`) : null;
  if (started && !Number.isFinite(started.getTime()) || ended && !Number.isFinite(ended.getTime()) || started && ended && started > ended) throw new Error("请选择有效日期，结束日期不得早于开始日期。");
  return { ...(started ? { started_from: started.toISOString() } : {}), ...(ended ? { started_to: new Date(ended.getTime() + 86400000).toISOString() } : {}) };
}
