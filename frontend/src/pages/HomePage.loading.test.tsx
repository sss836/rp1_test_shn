// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getDashboardHome } from "../api";
import { ApiError } from "../client";
import type { DashboardHome } from "../types";
import { HomePage } from "./HomePage";

vi.mock("../api", () => ({ getDashboardHome: vi.fn() }));
let cache: QueryClient;
const snapshot: DashboardHome = {
  freshness: { status: "FRESH", as_of_at: "2026-11-05T00:00:00Z", data_cutoff_at: "2026-11-05T00:00:00Z", last_received_at: null, lag_seconds: 0 },
  data_sources: { as_of_at: "2026-11-05T00:00:00Z", data_cutoff_at: "2026-11-05T00:00:00Z", healthy_count: 1, warning_count: 0, sources: [{ id: "source-1", code: "PC-01", source_type: "PLC", configured_status: "ENABLED", freshness_status: "FRESH", last_received_at: null, last_receipt_status: null, last_success_at: null, lag_seconds: 0, last_error: null }] },
  anomaly_count: 0, anomalies: [],
  groups: [{ asset_kind: "WHOLE_MACHINE", objects: [{ id: "SYS", code: "SYS", name: "整机", target_part_name: "整机", target_part_code: "SYS", asset_kind: "WHOLE_MACHINE", status: "RUNNING", part_asset_count: 1, active_execution_count: 1, abnormal_asset_count: 0, part_durations: { total_duration_seconds: 3600, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 }, test_cases: [{ id: "case-1", code: "SYS-001", name: "示例整机用例", target_part_code: "SYS", target_part_name: "整机", status: "RUNNING", current_elapsed_seconds: 0, cumulative_duration_seconds: 3600, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, execution_count: 1, scope_asset_count: 1, historical_execution_count: 0, platform_execution_count: 1, passed_execution_count: 0, failed_execution_count: 0, inconclusive_execution_count: 0, not_evaluated_execution_count: 1, exception_count: 0, first_recorded_at: null, latest_recorded_at: null, station_codes: [], target_duration_seconds: null, target_progress_percent: null }] }] }]
};
async function flush() { await act(async () => { await vi.advanceTimersByTimeAsync(0); }); }
function mount() {
  render(<QueryClientProvider client={cache}><HomePage assetKind="WHOLE_MACHINE" navigate={vi.fn()} /></QueryClientProvider>);
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  cache = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: Infinity } } });
  vi.mocked(getDashboardHome).mockReset();
});
afterEach(() => {
  cleanup();
  cache.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

it("replaces the initial skeleton with a retry gate and does not start an automatic second request", async () => {
  let reject!: (error: unknown) => void;
  vi.mocked(getDashboardHome).mockImplementationOnce(() => new Promise((_resolve, no) => { reject = no; }));
  mount();
  expect(screen.getByLabelText("正在加载总控首页")).toBeTruthy();
  await act(async () => { reject(new ApiError("服务响应超过 12 秒，已停止等待，请重试。", 504, "request_timeout")); });
  await flush();
  expect(screen.queryByLabelText("正在加载总控首页")).toBeNull();
  expect(screen.getByRole("alert").textContent).toContain("超过 12 秒");
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(getDashboardHome).toHaveBeenCalledOnce();
  vi.mocked(getDashboardHome).mockResolvedValueOnce(snapshot);
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await flush();
  expect(getDashboardHome).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("heading", { name: "整机系统测试总控" })).toBeTruthy();
  expect(screen.getByAltText("整机外观")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("retains cached content after background failure, removes live indicators, and recovers on manual retry", async () => {
  cache.setQueryData(["dashboard-home"], snapshot);
  mount();
  expect(screen.getByText("PC-01 · FRESH").getAttribute("data-tone")).toBe("normal");
  vi.mocked(getDashboardHome).mockRejectedValueOnce(new ApiError("服务响应超过 12 秒，已停止等待，请重试。", 504, "request_timeout"));
  await act(async () => { await cache.refetchQueries({ queryKey: ["dashboard-home"] }); });
  await flush();
  expect(screen.getByAltText("整机外观")).toBeTruthy();
  expect(screen.getByText("SYS-001", { selector: ".case-code" })).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("历史快照，不代表当前设备状态");
  expect(screen.getByText(/历史快照 · 当前状态未知/)).toBeTruthy();
  expect(screen.getByText("PC-01 · 历史 FRESH").getAttribute("data-tone")).toBe("muted");
  expect(screen.getAllByText("历史 RUNNING")).toHaveLength(2);
  expect(document.querySelector('.status-mark[data-tone="normal"]')).toBeNull();
  expect(cache.getQueryData<DashboardHome>(["dashboard-home"])?.freshness.status).toBe("FRESH");
  await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
  expect(getDashboardHome).toHaveBeenCalledOnce();
  vi.mocked(getDashboardHome).mockResolvedValueOnce(snapshot);
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await flush();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByText("PC-01 · FRESH").getAttribute("data-tone")).toBe("normal");
});
