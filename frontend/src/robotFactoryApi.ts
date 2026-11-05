import { ApiError, apiRequest } from "./client";

export type FactoryRobot = {
  robot_id: string;
  source_id: string;
  source_kind: "real" | "simulated";
  serial_number: string | null;
  model: string | null;
  station_name: string | null;
  online: boolean;
  last_seen_at: string | null;
  cases: Array<{ case_code: string; test_name: string; record_count: number; latest_status: string | null }>;
};
export type FactoryRobotList = { items: FactoryRobot[]; as_of_at: string; offline_after_seconds: number; truncated?: boolean };
export type RobotResultStatus = "PENDING" | "APPROVED" | "REJECTED";
export type RobotResultArchive = {
  id: string; source_id: string; robot_id: string; serial_number: string; model: string;
  filename: string; size_bytes: number; sha256: string; status: RobotResultStatus;
  revision: number; source_kind: "real" | "simulated"; verified: boolean; completed: boolean;
  reason: string; owner_name: string; created_at: string; can_review?: boolean;
  formal_statistics_eligible: boolean;
};
export type RobotResultDetail = RobotResultArchive & {
  columns: string[];
  rows: Array<{ record_id: string; version: number; values: string[]; case_code: string; test_name: string; level: string; verdict: string }>;
  review_history: Array<{ decision: string; reason: string; reviewer_name?: string; reviewed_at?: string; created_at?: string; revision?: number }>;
};
export type RobotResultFilters = { robot_id?: string; source_id?: string; serial_number?: string; status?: string; before?: number | null };
export type RobotResultReceipt = Pick<RobotResultArchive, "id" | "source_id" | "robot_id" | "serial_number" | "source_kind" | "sha256" | "size_bytes" | "status" | "revision" | "formal_statistics_eligible">;
export type RobotResultUpload = { source_id: string; robot_id: string; source_kind: "real" | "simulated"; filename: string; size_bytes: number; sha256: string; verified: true; completed: true; reason: string };
export const robotResultStatusLabels: Record<RobotResultStatus, string> = { PENDING: "待审核", APPROVED: "已批准工厂汇总", REJECTED: "已驳回" };

export function validateRobotResultReceipt(value: unknown, metadata: RobotResultUpload): RobotResultReceipt {
  const receipt = value && typeof value === "object" ? value as Record<string, unknown> : {};
  if (typeof receipt.id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(receipt.id)
    || receipt.source_id !== metadata.source_id || receipt.robot_id !== metadata.robot_id
    || receipt.source_kind !== metadata.source_kind || receipt.sha256 !== metadata.sha256
    || receipt.size_bytes !== metadata.size_bytes || typeof receipt.serial_number !== "string" || !receipt.serial_number.trim()
    || typeof receipt.status !== "string" || !["PENDING", "APPROVED", "REJECTED"].includes(receipt.status)
    || !Number.isSafeInteger(receipt.revision) || (receipt.revision as number) < 1
    || receipt.formal_statistics_eligible !== false) {
    throw new ApiError("归档回执与本次冻结提交不匹配，尚未确认接收。请保持原文件和信息重试，或核对平台记录。", 502, "factory_result_receipt_mismatch");
  }
  return {
    id: receipt.id, source_id: metadata.source_id, robot_id: metadata.robot_id,
    serial_number: receipt.serial_number, source_kind: metadata.source_kind,
    sha256: metadata.sha256, size_bytes: metadata.size_bytes, status: receipt.status as RobotResultStatus,
    revision: receipt.revision as number, formal_statistics_eligible: false
  };
}

function query(values: Record<string, string | number | boolean | null | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  return params;
}

export const robotFactoryApi = {
  robots(signal?: AbortSignal) { return apiRequest<FactoryRobotList>("/api/v1/factory/robots", { signal }); },
  list(filters: RobotResultFilters, signal?: AbortSignal) { return apiRequest<{ items: RobotResultArchive[]; next_cursor: number | null; as_of_at: string }>(`/api/v1/factory/robot-result-archives?${query({ ...filters, limit: 50 })}`, { signal }); },
  detail(id: string, signal?: AbortSignal) { return apiRequest<RobotResultDetail>(`/api/v1/factory/robot-result-archives/${encodeURIComponent(id)}`, { signal }); },
  fileHref(id: string) { return `/api/v1/factory/robot-result-archives/${encodeURIComponent(id)}/file`; },
  async upload(file: File, metadata: RobotResultUpload, key: string, signal?: AbortSignal) {
    const receipt = await apiRequest<unknown>(`/api/v1/factory/robot-result-archives?${query(metadata)}`, { method: "POST", body: file, signal, headers: { "Content-Type": "text/csv", "Idempotency-Key": key } });
    return validateRobotResultReceipt(receipt, metadata);
  },
  review(id: string, expected_revision: number, decision: "APPROVE" | "REJECT", reason: string, key: string) {
    return apiRequest<RobotResultDetail>(`/api/v1/factory/robot-result-archives/${encodeURIComponent(id)}/review`, { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ expected_revision, decision, reason }) });
  }
};
