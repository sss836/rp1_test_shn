import { apiRequest, ApiError } from "../../client";
import type { ArchiveCatalogItem } from "../../archiveApi";
export type RawFilters = { metrics: string; start: number; end: number; stage?: string; cycle?: string };
export type ExactStatistic = ArchiveCatalogItem & { count: number; missing: number; min: number | null; max: number | null; mean: number | null; rms: number | null; first: number | null; last: number | null };
type RawIdentity = { archive_id: string; execution_uuid: string; archive_code: string; range: { start: number; end: number }; time_range: { start: number; end: number }; catalog: ArchiveCatalogItem[]; resolution: "raw"; total_rows: number };
export type StatisticsResponse = RawIdentity & { series: ExactStatistic[] };
export type SamplesResponse = RawIdentity & { rows: Record<string, string | number | null>[]; offset: number; limit: number; has_more: boolean };
function endpoint(id: string, action: string, filters: RawFilters & { offset?: number; limit?: number }) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) if (value !== undefined && value !== "") params.set(key, String(value));
  return `/api/v1/archives/${encodeURIComponent(id)}/${action}?${params}`;
}
export const workspaceApi = {
  statistics: (id: string, filters: RawFilters, signal?: AbortSignal) => apiRequest<StatisticsResponse>(endpoint(id, "statistics", filters), { signal }),
  samples: (id: string, filters: RawFilters & { offset?: number; limit?: number }, signal?: AbortSignal) => apiRequest<SamplesResponse>(endpoint(id, "samples", filters), { signal }),
  exportCsv: async (id: string, filters: RawFilters, signal?: AbortSignal) => {
    const headers = new Headers({ "X-Request-Id": crypto.randomUUID() });
    const devUser = import.meta.env.DEV ? import.meta.env.VITE_DEV_USER_PUBLIC_ID?.trim() : "";
    if (devUser) headers.set("X-User-Public-Id", devUser);
    const response = await fetch(endpoint(id, "export.csv", filters), { credentials: "include", headers, signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => null) as { error?: { message?: string; code?: string } } | null;
      throw new ApiError(payload?.error?.message ?? `CSV 下载失败（HTTP ${response.status}）。`, response.status, payload?.error?.code);
    }
    if (!response.headers.get("content-type")?.includes("text/csv")) throw new Error("服务未返回 CSV，请重新登录后重试。");
    return response.blob();
  }
};
export function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename; anchor.click();
  // Keep the blob alive through the browser's download dispatch.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
