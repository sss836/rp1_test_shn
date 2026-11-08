// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AssetsPage } from "./ReadModelPages";
import { getAssets } from "../api";
import { setupApi } from "../setupApi";
import type { AssetSummary } from "../types";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { role: "VIEWER" } } }) }));
vi.mock("../api", () => ({ getAssets: vi.fn(), getAllAssets: vi.fn() }));
const clients: QueryClient[] = [];
const sample = (id: string, name: string): AssetSummary => ({
  id, code: `RP1.3-SYS-${id}`, name, asset_kind: "WHOLE_MACHINE", target_part_code: "SYS", target_part_name: "整机",
  product_family: "RP1.3", batch_code: null, serial_number: null, creation_scenario: "RND_TEST", lifecycle_status: "ACTIVE",
  current_test_case_code: null, current_execution_id: null, current_execution_code: null, health_status: null,
  durations: { total_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 },
  freshness: { status: "NO_DATA", as_of_at: "2026-11-08T00:00:00Z", data_cutoff_at: null, last_received_at: null, lag_seconds: null }
});
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); });

it("keeps standard sample codes beside plan names, models and unbound SNs without implying a running test", async () => {
  const samples = [sample("001", "M1 · MTBF V1.1"), sample("004", "M4 · MTBF V1.1"), sample("005", "test_RD")];
  vi.mocked(getAssets).mockResolvedValue({ items: samples, page: { limit: 100, next_cursor: null }, as_of_at: "2026-11-08T00:00:00Z", data_cutoff_at: null });
  vi.spyOn(setupApi, "identityOptions").mockResolvedValue({ assets: samples.map((asset, index) => ({ ...asset, target_part_code: "SYS", model: index === 0 ? "24轴" : index === 1 ? "28轴" : null, can_complete_identity: false, blocked_reason: "当前账号只读" })), configurations: [], programs: [], parts: [], stations: [] });
  const navigate = vi.fn(); const cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(cache);
  render(<QueryClientProvider client={cache}><AssetsPage assetKind="WHOLE_MACHINE" part={null} selectPart={vi.fn()} clearPart={vi.fn()} navigate={navigate} /></QueryClientProvider>);
  await screen.findByText("24轴"); await screen.findByText("28轴");
  expect(screen.getAllByText("计划档案")).toHaveLength(3);
  expect(screen.getAllByText("待绑定")).toHaveLength(3);
  expect(screen.getByText("test_RD · 研发测试")).toBeTruthy();
  expect(screen.queryByText("当前稳定")).toBeNull();
  const card = screen.getByText("RP1.3-SYS-004").closest("article")!;
  expect(within(card).getByText("M4 · MTBF V1.1 · 研发测试")).toBeTruthy();
  expect(within(card).getByText("28轴")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "RP1.3-SYS-005" }));
  await waitFor(() => expect(navigate).toHaveBeenCalledWith("asset", "RP1.3-SYS-005"));
});
