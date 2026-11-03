import { apiRequest } from "./client";

export type FactoryRun = {
  session_key: string; state: string; execution_uuid?: string | null; execution_code?: string;
  test_id?: string; case_code?: string; sample_id?: string; serial_number?: string;
  test_case?: string; active_seconds: number; completed_cycles: number;
};
export type FactoryConnection = {
  connection_id: string; installation_id: string; station_name: string; bench_id: string;
  operator_name: string; operator_display_name: string; online: boolean;
  last_seen_at: string; snapshot_at: string; scope_conflict?: boolean; scope_unconfirmed?: boolean; scope_source?: string; runs: FactoryRun[];
  plc: { driver: string; communication_state: string; phase: string; stale: boolean };
};
export type FactoryBench = {
  station_id: string; bench_id: string; name: string; site_name: string; lab_name: string;
  available: boolean; connections: FactoryConnection[];
};
export type ReceiptState = "awaiting_upload" | "identity_unknown" | "uploading" | "upload_error" | "parsing" | "parse_error" | "awaiting_verification" | "awaiting_context" | "ready_to_submit" | "awaiting_review" | "rejected" | "archived";
export type FactoryReceipt = {
  archive_id: string; execution_uuid: string; archive_code: string; test_name: string;
  serial_number: string | null; bench_id: string | null; processing_error: string | null;
  receipt_state: ReceiptState; created_at: string;
};
export type FactoryMonitorData = {
  items: FactoryBench[]; receipts: FactoryReceipt[]; next_cursor: string | null;
  work_items: FactoryReceipt[]; work_next_cursor: number | null; as_of_at: string;
  offline_after_seconds: number; snapshot_stale_after_seconds: number;
};
export const factoryApi = {
  monitor(cursor: string | null, before: number | null, signal?: AbortSignal) {
    const params = new URLSearchParams({ limit: "50", work_limit: "20" });
    if (cursor) params.set("cursor", cursor);
    if (before) params.set("work_before", String(before));
    return apiRequest<FactoryMonitorData>(`/api/v1/factory/monitor?${params}`, { signal });
  }
};
export const receiptLabels: Record<ReceiptState, string> = {
  awaiting_upload: "已完成，平台未收到归档", identity_unknown: "执行身份未上报，待核对",
  uploading: "上传中", upload_error: "文件校验失败", parsing: "正在解析", parse_error: "解析失败",
  awaiting_verification: "待核实", awaiting_context: "待补正式归属", ready_to_submit: "待送审",
  awaiting_review: "待审批", rejected: "已驳回，待处理", archived: "已批准归档"
};
export type BenchState = "running" | "paused" | "fault" | "offline" | "unknown" | "idle" | "completed" | "unreported";
export const benchLabels: Record<BenchState, string> = {
  running: "测试运行中", paused: "已暂停", fault: "故障", offline: "离线",
  unknown: "状态待确认", idle: "在线空闲", completed: "测试已结束", unreported: "无上报记录"
};
export function connectionFresh(connection: FactoryConnection, data: FactoryMonitorData, elapsed: number, live: boolean) {
  const at = Date.parse(data.as_of_at) + Math.max(0, elapsed);
  const age = at - Date.parse(connection.last_seen_at);
  const online = live && connection.online && Number.isFinite(age) && age < data.offline_after_seconds * 1000;
  const snapshotAge = at - Date.parse(connection.snapshot_at);
  return { online, fresh: online && Number.isFinite(snapshotAge) && snapshotAge < data.snapshot_stale_after_seconds * 1000 };
}
export function benchRunning(bench: FactoryBench, data: FactoryMonitorData, elapsed: number, live: boolean) {
  return bench.connections.some(c => !c.scope_conflict && connectionFresh(c, data, elapsed, live).fresh && c.runs.some(r => ["running", "stopping"].includes(r.state)));
}
export function benchState(bench: FactoryBench, data: FactoryMonitorData, elapsed: number, live: boolean): BenchState {
  if (!bench.connections.length) return "unreported";
  if (!live) return "unknown";
  const online = bench.connections.filter(c => connectionFresh(c, data, elapsed, live).online);
  if (!online.length) return "offline";
  if (online.some(c => c.scope_conflict)) return "unknown";
  const fresh = online.filter(c => connectionFresh(c, data, elapsed, live).fresh);
  if (!fresh.length) return "unknown";
  if (fresh.some(c => c.plc.phase === "FAULT" || c.runs.some(r => r.state === "fault"))) return "fault";
  if (fresh.length !== online.length || fresh.some(c => c.runs.some(r => r.state === "unknown"))) return "unknown";
  if (fresh.some(c => c.runs.some(r => ["running", "stopping"].includes(r.state)))) return "running";
  if (fresh.some(c => c.runs.some(r => r.state === "paused"))) return "paused";
  if (fresh.some(c => c.runs.some(r => r.state === "completed"))) return "completed";
  return "idle";
}
export function runReceipt(run: FactoryRun, receipts: FactoryReceipt[]): ReceiptState | null {
  if (!run.execution_uuid) return run.state === "completed" ? "identity_unknown" : null;
  const record = receipts.find(item => item.execution_uuid.toLowerCase() === run.execution_uuid?.toLowerCase());
  return record?.receipt_state ?? (run.state === "completed" ? "awaiting_upload" : null);
}
