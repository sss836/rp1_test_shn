// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { archivesApi, type Archive } from "./archiveApi";

const auth = vi.hoisted(() => ({ user: { id: "reader-a", username: "reader", display_name: "测试用户", role: "TEST_EXECUTOR" as "TEST_EXECUTOR" | "VIEWER", must_change_password: false } }));
vi.mock("./auth", () => ({ useAuth: () => ({ status: "authenticated", session: { user: auth.user }, logout: vi.fn() }) }));
vi.mock("./pages/HomePage", () => ({ HomePage: () => <h1>总控测试替身</h1> }));
vi.mock("./pages/CsvImportPage", () => ({ default: () => <h1>补录测试替身</h1> }));
// The route test keeps the real archive selector; chart rendering has its own regression suite.
vi.mock("./pages/ArchivesPage", async (original) => ({
  ...await original<typeof import("./pages/ArchivesPage")>(),
  ArchiveDetailPage: ({ id }: { id: string }) => <div data-testid="archive-detail">{id}</div>
}));
vi.mock("./pages/CurveAnalysisPage", () => ({ default: ({ id }: { id: string }) => <div data-testid="curve-detail" data-search={window.location.search}>{id}</div> }));

const ready: Archive = {
  id: "chosen-archive", execution_uuid: "chosen-execution", archive_code: "ARC-TEST-001", execution_code: null,
  owner_public_id: "owner", is_owner: false, can_edit: false, origin: "HISTORY", test_name: "关节温升", actual_object: "测试样品", station_label: "测试工位",
  filename: "synthetic.csv", size_bytes: 100, sha256: "a".repeat(64), received_bytes: 100, upload_state: "COMPLETE", processing_state: "READY", processing_error: null,
  classification_state: "UNCLASSIFIED", revision: 1, facts: {}, acceptance: {}, manifest: null, current_verification: null, classification: null,
  submission_id: null, submission_revision: null, submission_status: null, execution_id: null, created_at: "2026-10-16T00:00:00Z"
};
let cache: QueryClient;
function mount(search = "?view=curves&kind=whole") {
  window.history.replaceState({}, "", search);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  return render(<QueryClientProvider client={cache}><App /></QueryClientProvider>);
}
function nav() { return within(screen.getByRole("navigation", { name: "主要页面" })); }
function expectActive(label: string) {
  const active = nav().getAllByRole("button").filter((button) => button.getAttribute("aria-current") === "page");
  expect(active).toHaveLength(1);
  expect(active[0]).toBe(nav().getByRole("button", { name: label }));
}
beforeEach(() => {
  auth.user.id = "reader-a"; auth.user.role = "TEST_EXECUTOR";
  window.localStorage.setItem("rp1-theme", "day");
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Unexpected network request")));
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [ready], next_cursor: null });
});
afterEach(() => { cleanup(); cache?.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("opens an independent top-level chooser, selects the returned READY ID and supports browser back", async () => {
  mount("?view=archives&kind=module");
  await screen.findByRole("heading", { name: "数据归档" });
  fireEvent.click(nav().getByRole("button", { name: "曲线分析" }));
  await screen.findByRole("heading", { name: "选择记录开始分析" });
  expectActive("曲线分析");
  expect(new URLSearchParams(window.location.search).has("id")).toBe(false);
  expect(screen.queryByText("页面地址缺少业务标识")).toBeNull();
  expect(screen.queryByRole("button", { name: "数据补录" })).toBeNull();
  expect(screen.getByText(/不随顶部整机 \/ 模块切换筛选/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "打开曲线分析" }));
  expect((await screen.findByTestId("curve-detail")).textContent).toBe(ready.id);
  expect(new URLSearchParams(window.location.search).get("kind")).toBe("module");
  expectActive("曲线分析");
  await act(async () => { window.history.back(); });
  await screen.findByRole("heading", { name: "选择记录开始分析" });
  expectActive("曲线分析");
  expect(fetch).not.toHaveBeenCalled();
});

it("initializes a no-ID URL on refresh and keeps whole/module archive scope unchanged", async () => {
  mount();
  await screen.findByRole("heading", { name: "选择记录开始分析" });
  fireEvent.click(screen.getByRole("button", { name: "模块" }));
  expect(new URLSearchParams(window.location.search).get("kind")).toBe("module");
  expect(screen.getByRole("button", { name: "打开曲线分析" })).toBeTruthy();
  expect(archivesApi.list).toHaveBeenCalledTimes(1);
  expect(archivesApi.list).toHaveBeenCalledWith({ q: "", classification_state: "", origin: "", before: null }, expect.any(AbortSignal));
});

it("preserves legacy curve detail URL options and highlights curves, while archive/import remain archives", async () => {
  const oldLink = "?view=curves&kind=module&id=legacy-record&curve_mode=single&workspace=saved-view";
  mount(oldLink);
  expect((await screen.findByTestId("curve-detail")).dataset.search).toBe(oldLink);
  expectActive("曲线分析");
  expect(archivesApi.list).not.toHaveBeenCalled();
  for (const [view, testId] of [["archive", "archive-detail"], ["import", null]] as const) {
    await act(async () => {
      window.history.replaceState({}, "", `?view=${view}&kind=module&id=legacy-record`);
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    if (testId) expect((await screen.findByTestId(testId)).textContent).toBe("legacy-record");
    else await screen.findByRole("heading", { name: "补录测试替身" });
    expectActive("数据归档");
  }
  fireEvent.click(nav().getByRole("button", { name: "曲线分析" }));
  await screen.findByRole("heading", { name: "选择记录开始分析" });
  expectActive("曲线分析");
});

it("keeps non-ready records visible and disabled with failure reason and a reachable archive detail", async () => {
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [ready, ...(["WAITING", "QUEUED", "PROCESSING", "FAILED"] as const).map((state) => ({ ...ready, id: state, archive_code: `ARC-${state}`, processing_state: state, processing_error: state === "FAILED" ? "CSV 缺少时间列" : null }))], next_cursor: null });
  mount();
  const actions = await screen.findAllByRole("button", { name: "打开曲线分析" });
  expect(actions.map((node) => (node as HTMLButtonElement).disabled)).toEqual([false, true, true, true, true]);
  expect(screen.getByText("解析失败：CSV 缺少时间列")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "ARC-FAILED" }));
  expect((await screen.findByTestId("archive-detail")).textContent).toBe("FAILED");
  expectActive("数据归档");
  expect(fetch).not.toHaveBeenCalled();
});

it("shows loading, retries a list failure, then presents an empty result without business-ID errors", async () => {
  let reject!: (error: Error) => void;
  vi.mocked(archivesApi.list).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
  mount();
  await waitFor(() => expect(archivesApi.list).toHaveBeenCalled());
  expect(document.querySelector('[aria-busy="true"]')).toBeTruthy();
  await act(async () => { reject(new Error("归档查询暂不可用")); });
  expect((await screen.findByRole("alert")).textContent).toContain("归档查询暂不可用");
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [], next_cursor: null });
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByRole("heading", { name: "暂无符合条件的归档" });
  expect(screen.queryByText("页面地址缺少业务标识")).toBeNull();
});

it("uses server search/filter/pagination and resets the cursor when filters change", async () => {
  vi.mocked(archivesApi.list).mockImplementation(async (filters) => ({ items: [{ ...ready, id: filters?.before ? "page-2" : "page-1", archive_code: filters?.before ? "ARC-PAGE-2" : "ARC-PAGE-1" }], next_cursor: filters?.before ? null : 18 }));
  mount();
  fireEvent.click(await screen.findByRole("button", { name: "加载更多" }));
  await screen.findByRole("button", { name: "ARC-PAGE-2" });
  expect(archivesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ before: 18 }), expect.any(AbortSignal));
  const search = screen.getByLabelText("执行编号 / 测试名称");
  expect(search.getAttribute("maxlength")).toBe("200");
  fireEvent.change(search, { target: { value: "  新记录  " } });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ q: "新记录", before: null }), expect.any(AbortSignal)));
  fireEvent.change(screen.getByLabelText("归属状态"), { target: { value: "CLASSIFIED" } });
  fireEvent.change(screen.getByLabelText("记录来源"), { target: { value: "HISTORY" } });
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith({ q: "新记录", classification_state: "CLASSIFIED", origin: "HISTORY", before: null }, expect.any(AbortSignal)));
  expect(fetch).not.toHaveBeenCalled();
});

it("isolates cached archive lists by authenticated user and keeps VIEWER permission wording", async () => {
  const view = mount();
  await screen.findByRole("button", { name: ready.archive_code });
  auth.user = { ...auth.user, id: "reader-b", role: "VIEWER" };
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [], next_cursor: null });
  view.rerender(<QueryClientProvider client={cache}><App /></QueryClientProvider>);
  expect(screen.queryByRole("button", { name: ready.archive_code })).toBeNull();
  await screen.findByRole("heading", { name: "暂无符合条件的归档" });
  expect(screen.getByText("显示你有计划权限的已批准记录。")).toBeTruthy();
  expect(nav().queryByRole("button", { name: "测试数据审批" })).toBeNull();
  expect(archivesApi.list).toHaveBeenCalledTimes(2);
});
