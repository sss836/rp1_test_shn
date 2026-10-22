import { apiRequest, requestId } from "./client";
import type { AssetKind, TargetPartCode } from "./types";

export type CatalogCase = { can_edit?: boolean; id: string; code: string; name: string; asset_kind: AssetKind; target_part_code: TargetPartCode | null; target_part_name: string | null; enabled: boolean; domain: string; version_id: string | null; version: string | null; procedure_spec: Record<string, unknown> | null };
export type CampaignStage = { sequence_no: number; stage_name: string };
export type Campaign = { id: string; code: string; name: string; asset_kind: AssetKind; status: string; program_code: string | null; program_name: string | null; can_edit: boolean; asset_count: number; case_count: number; stages?: CampaignStage[]; planned_start: string | null; planned_end: string | null };
export type SetupOptions = {
  parts: { code: TargetPartCode; name: string; asset_kind: AssetKind }[];
  programs: { can_edit?: boolean; id: string; code: string; name: string; status: string }[];
  stations: { can_edit?: boolean; id: string; code: string; name: string; station_type: string }[];
  assets: { can_edit?: boolean; id: string; code: string; name: string; asset_kind: AssetKind; target_part_code: TargetPartCode }[];
  configurations: { id: string; asset_id: string; fingerprint: string; reliability_impact: string }[];
};
export type PlannedContext = { id: string; asset_id: string; asset_code: string; case_code: string; case_name: string; version: string; planned_runs: number; status: string; context: Record<string, string> };

export const setupApi = {
  catalog: (signal?: AbortSignal) => apiRequest<CatalogCase[]>("/api/v1/setup/catalog", { signal }),
  options: (signal?: AbortSignal) => apiRequest<SetupOptions>("/api/v1/setup/options", { signal }),
  campaigns: (signal?: AbortSignal) => apiRequest<Campaign[]>("/api/v1/setup/campaigns", { signal }),
  contexts: (id: string, signal?: AbortSignal) => apiRequest<PlannedContext[]>(`/api/v1/setup/campaigns/${encodeURIComponent(id)}/contexts`, { signal }),
  versions: (id: string, signal?: AbortSignal) => apiRequest<CaseVersions>(`/api/v1/setup/cases/${encodeURIComponent(id)}/versions`, { signal }),
  create: (path: string, payload: Record<string, unknown>, key = requestId(), method = "POST") => apiRequest<{ id: string }>(`/api/v1/setup/${path}`, {
    method, headers: { "Idempotency-Key": key, "X-Change-Reason": String(payload.reason ?? "") }, body: JSON.stringify(payload)
  })
};

export type CaseVersions = { case: { can_edit: boolean; name: string }; versions: Array<{ id: string; version: string; status: string; published_at: string | null; procedure_spec: Record<string, unknown>; stage_definitions: unknown[]; equipment_requirements: Record<string, unknown>; sampling_requirements: Record<string, unknown>; termination_rules: Record<string, unknown>; decision_rules: Record<string, unknown>; metric_requirements: unknown[]; fault_rules: Record<string, unknown>; deviation_notes: string; source_references: unknown[] }> };
