// @vitest-environment jsdom
// Synthetic fixtures only. Parent wiring and raw-query contracts are exercised
// independently from the real ECharts event/model tests in TimeSeriesChart.test.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";
import type { TimeSeriesChart, PlotSeries } from "../../components/TimeSeriesChart";
import { archivesApi, type Archive, type ArchiveCatalogItem } from "../../archiveApi";
import CurveAnalysisPage from "../CurveAnalysisPage";
import { workspaceApi } from "./api";
import { defaultConfig, loadViews, shareUrl } from "./config";
import * as workbenchConfig from "./config";
import { panelFilters } from "./Panel";
vi.mock("../../auth", () => ({ useAuth: () => ({ session: { user: { id: "public-user-a", role: "TEST_EXECUTOR" } } }) }));
vi.mock("../../components/TimeSeriesChart", () => ({ TimeSeriesChart: (props: ComponentProps<typeof TimeSeriesChart>) => <div data-testid={`chart-${props.title}`} data-active={props.active} data-toolbar={props.showToolbar}><output aria-label={`${props.title} range`}>{JSON.stringify(props.viewport)}</output><output aria-label={`${props.title} exact statistics`}>{JSON.stringify(props.statistics)}</output><output aria-label={`${props.title} series`}>{JSON.stringify(props.series)}</output><button type="button" onClick={() => props.onViewport({ start: 10, end: 20 })}>模拟缩放 {props.title}</button></div> }));
const catalog: ArchiveCatalogItem[] = ["pos_rad", "cmd_pos_rad", "tracking_error_rad", "torque_nm", "temp_c"].flatMap((metric) => ["j1", "j2"].map((subject) => ({ key: `${subject}_${metric}`, subject, metric, unit: metric === "torque_nm" ? "Nm" : metric === "temp_c" ? "°C" : "rad" })));
function archive(id = "base"): Archive { return {
  id, test_name: id === "base" ? "合成负载 5.5 kg" : "合成负载 2.5 kg", archive_code: `ARC-${id}`, execution_code: null, execution_uuid: `execution-${id}`, owner_public_id: "owner", is_owner: true, can_edit: true, origin: "OTHER", actual_object: "合成样品", station_label: "合成工位", filename: "synthetic.csv", size_bytes: 100, sha256: "a".repeat(64), received_bytes: 100, upload_state: "COMPLETE", processing_state: "READY", processing_error: null, classification_state: "UNCLASSIFIED", revision: 1, facts: { active_seconds: 100, started_at: "2026-10-01T00:00:00Z" }, acceptance: { max_torque_abs_nm: 9 }, manifest: { catalog, time_range: { start: 0, end: 100 }, stages: [], cycles: [] }, current_verification: null, classification: null, submission_id: null, submission_revision: null, submission_status: null, execution_id: null, created_at: "2026-10-01T00:00:00Z"
}; }
const clients: QueryClient[] = [];
beforeEach(() => {
  window.history.replaceState({}, "", "?view=curves&id=base"); localStorage.clear();
  vi.spyOn(archivesApi, "detail").mockImplementation(async (id) => archive(id));
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [archive("other")], next_cursor: null });
  vi.spyOn(archivesApi, "curves").mockImplementation(async (id, filters) => ({ archive_id: id, archive_code: `ARC-${id}`, execution_uuid: id, range: { start: filters.start!, end: filters.end! }, time_range: { start: 0, end: 100 }, resolution: "minmax", catalog, gaps: [], series: catalog.filter((entry) => filters.metrics.split(",").includes(entry.key)).map((entry) => ({ ...entry, points: [[filters.start!, 1], [filters.end!, null]], sample_count: 10000, returned_points: 2, downsampled: true })) }));
  vi.spyOn(workspaceApi, "statistics").mockImplementation(async (id, filters) => ({ archive_id: id, archive_code: `ARC-${id}`, execution_uuid: id, range: { start: filters.start, end: filters.end }, time_range: { start: 0, end: 100 }, resolution: "raw", total_rows: 10000, catalog, series: catalog.filter((entry) => filters.metrics.split(",").includes(entry.key)).map((entry) => ({ ...entry, count: 9999, missing: 1, min: -2, max: 3, mean: 0.123456789, rms: 2, first: 1, last: null })) }));
  vi.spyOn(workspaceApi, "samples").mockImplementation(async (id, filters) => ({ archive_id: id, archive_code: `ARC-${id}`, execution_uuid: id, range: { start: filters.start, end: filters.end }, time_range: { start: 0, end: 100 }, resolution: "raw", total_rows: 101, catalog: catalog.filter((entry) => filters.metrics.split(",").includes(entry.key)), rows: [{ time_s: filters.start, _row: filters.offset ?? 0, j1_torque_nm: null }], offset: filters.offset ?? 0, limit: 100, has_more: !filters.offset }));
  vi.spyOn(workspaceApi, "exportCsv").mockRejectedValue(new Error("区间超过导出上限，请缩小时间范围。"));
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("No network in synthetic test")));
});
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); expect(fetch).not.toHaveBeenCalled(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function mount() { const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0, refetchOnWindowFocus: false } } }); clients.push(client); return render(<QueryClientProvider client={client}><CurveAnalysisPage id="base" navigate={vi.fn()} /></QueryClientProvider>); }
const change = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
async function ready() { await screen.findByTestId("chart-力矩"); await waitFor(() => expect(workspaceApi.statistics).toHaveBeenCalledTimes(4)); }
async function addOther() { change("工作台查找测试记录", "合成"); fireEvent.click(screen.getByRole("button", { name: "工作台查找对比记录" })); fireEvent.click(await screen.findByRole("button", { name: /合成负载 2.5 kg.*添加对比/ })); await screen.findByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ }); }
function revealSave() { const details = screen.getByText("保存 / 恢复视图").closest("details")!; details.open = true; }
it("loads four independent metrics, target overlay, and authoritative statistics", async () => {
  mount(); await ready();
  expect(screen.getAllByTestId(/^chart-/)).toHaveLength(4);
  expect(archivesApi.curves).toHaveBeenCalledWith("base", expect.objectContaining({ metrics: "j1_pos_rad,j1_cmd_pos_rad" }), expect.any(AbortSignal));
  await waitFor(() => expect(screen.getByLabelText("力矩 exact statistics").textContent).toContain("0.123456789"));
  expect(screen.getByLabelText("力矩 exact statistics").textContent).toContain('"count":9999');
  expect(screen.getByLabelText("力矩 series").textContent).toContain("合成负载 5.5 kg");
  expect((screen.getByLabelText("工作台阶段") as HTMLSelectElement).disabled).toBe(true);
});
it("synchronizes any panel zoom and keeps that range through metric and joint changes", async () => {
  mount(); await ready(); fireEvent.click(screen.getByRole("button", { name: "模拟缩放 力矩" }));
  await waitFor(() => expect(screen.getAllByLabelText(/ range$/).every((node) => node.textContent === '{"start":10,"end":20}')).toBe(true));
  change("panel-3 参数", "temp_c\u0000°C");
  await waitFor(() => expect(archivesApi.curves).toHaveBeenCalledWith("base", expect.objectContaining({ metrics: "j1_temp_c", start: 10, end: 20 }), expect.any(AbortSignal)));
  fireEvent.click(screen.getByLabelText("panel-3 关节 j2"));
  await waitFor(() => expect(workspaceApi.statistics).toHaveBeenCalledWith("base", expect.objectContaining({ metrics: "j1_temp_c,j2_temp_c", start: 10, end: 20 }), expect.any(AbortSignal)));
});
it("uses identical raw filters for table pages and CSV and keeps missing values blank", async () => {
  mount(); await ready(); fireEvent.click(screen.getByRole("button", { name: "模拟缩放 力矩" }));
  const panel = within(screen.getByRole("article", { name: "力矩面板" })); fireEvent.click(panel.getByRole("button", { name: "检查数据" }));
  await waitFor(() => expect(workspaceApi.samples).toHaveBeenCalledWith("base", { metrics: "j1_torque_nm", start: 10, end: 20, offset: 0, limit: 100 }, expect.any(AbortSignal)));
  await panel.findByText("—"); fireEvent.click(panel.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(workspaceApi.samples).toHaveBeenCalledWith("base", expect.objectContaining({ start: 10, end: 20, offset: 100 }), expect.any(AbortSignal)));
  await waitFor(() => expect((panel.getByRole("button", { name: "下载区间原始 CSV" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(panel.getByRole("button", { name: "下载区间原始 CSV" }));
  await panel.findByRole("alert"); expect(workspaceApi.exportCsv).toHaveBeenCalledWith("base", { metrics: "j1_torque_nm", start: 10, end: 20 }, expect.any(AbortSignal));
});
it("saves only browser configuration and restores unchecked execution selection", async () => {
  mount(); await ready(); await addOther();
  fireEvent.click(screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ }));
  fireEvent.click(screen.getByRole("button", { name: "模拟缩放 力矩" })); revealSave(); change("视图名称", "左肩负载对比"); fireEvent.click(screen.getByRole("button", { name: "保存到此浏览器" }));
  const saved = loadViews("public-user-a")[0]; expect(saved.config.excluded).toEqual(["other"]); expect(saved.config.viewport).toEqual({ start: 10, end: 20 }); expect(JSON.stringify(saved)).not.toContain("points");
  fireEvent.click(screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ })); fireEvent.click(screen.getByRole("button", { name: "恢复视图" }));
  await waitFor(() => expect((screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ }) as HTMLInputElement).checked).toBe(false));
});
it("restores URL state only after permission-checking every comparison and never renders denied data", async () => {
  const config = defaultConfig(archive()); config.comparisons = ["denied"]; config.viewport = { start: 10, end: 20 };
  window.history.replaceState({}, "", shareUrl(config));
  vi.mocked(archivesApi.detail).mockImplementation(async (id) => { if (id === "denied") throw new Error("归档访问被拒绝"); return archive(id); });
  mount(); await ready(); await screen.findByText("第 2 个执行不可访问，未加载曲线。");
  expect(archivesApi.detail).toHaveBeenCalledWith("denied", expect.any(AbortSignal));
  expect(vi.mocked(archivesApi.curves).mock.calls.some(([id]) => id === "denied")).toBe(false);
  expect(vi.mocked(workspaceApi.statistics).mock.calls.some(([id]) => id === "denied")).toBe(false);
  expect(screen.getByLabelText("力矩 range").textContent).toBe('{"start":10,"end":20}');
});
it("rejects corrupt URLs without issuing arbitrary metric or archive queries", async () => {
  window.history.replaceState({}, "", "?view=curves&id=base&curve_view=%7Bbroken"); mount(); await ready();
  expect(screen.getByText(/分享视图无效/)).toBeTruthy(); expect(archivesApi.detail).toHaveBeenCalledTimes(1);
});
it("does not collapse a non-overlapping absolute interval onto a real t=0 sample", () => {
  const record = archive(); record.facts.started_at = "1970-01-01T00:03:20Z";
  const config = defaultConfig(record);
  const filters = panelFilters(record, config.panels[2], { start: 100000, end: 110000 }, config, true);
  expect(filters.start).toBeGreaterThan(filters.end);
});


it("keeps nonfocused panels mounted and preserves raw inspector selection and pagination", async () => {
  mount(); await ready(); await addOther();
  const article = screen.getByRole("article", { name: "力矩面板" });
  const panel = within(article), chart = panel.getByTestId("chart-力矩");
  expect(screen.getAllByTestId(/^chart-/).every((entry) => entry.dataset.toolbar === "true")).toBe(true);
  fireEvent.click(panel.getByRole("button", { name: "检查数据" }));
  change("panel-3 检查执行", "other");
  await panel.findByText("第 1 页"); fireEvent.click(panel.getByRole("button", { name: "下一页" }));
  await panel.findByText("第 2 页");
  fireEvent.click(screen.getByRole("button", { name: "聚焦实际位置 / 目标" }));
  expect(article.hidden).toBe(true);
  expect(screen.getAllByTestId(/^chart-/)).toHaveLength(4);
  expect(chart.dataset.active).toBe("false");
  expect(screen.queryByRole("article", { name: "力矩面板" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "返回全部面板" }));
  expect(screen.getByRole("article", { name: "力矩面板" })).toBe(article);
  expect(panel.getByTestId("chart-力矩")).toBe(chart);
  expect(chart.dataset.active).toBe("true");
  expect((panel.getByLabelText("panel-3 检查执行") as HTMLSelectElement).value).toBe("other");
  expect(panel.getByText("第 2 页")).toBeTruthy();
});

it("keeps archive colors identical in every panel and the rail after exclusion and reinclusion", async () => {
  mount(); await ready(); await addOther();
  const series = () => JSON.parse(screen.getByLabelText("力矩 series").textContent!) as PlotSeries[];
  await waitFor(() => expect(series()).toHaveLength(2));
  const expected = series().find((entry) => entry.id === "other:j1_torque_nm")!.color;
  const dot = screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ }).closest("label")!.querySelector<HTMLElement>(".curve-series-dot")!;
  expect(expected).toBe(dot.style.getPropertyValue("--series-color"));
  for (let index = 0; index < 2; index++) {
    fireEvent.click(screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 5.5 kg/ }));
    await waitFor(() => expect(series()).toHaveLength(index === 0 ? 1 : 2));
    for (const output of screen.getAllByLabelText(/ series$/)) {
      const entries = JSON.parse(output.textContent!) as PlotSeries[];
      expect(entries.filter((entry) => entry.id.startsWith("other:")).every((entry) => entry.color === expected)).toBe(true);
    }
    expect(dot.style.getPropertyValue("--series-color")).toBe(expected);
  }
});

it("clears absolute viewport and cursors together when an added execution has no trusted start", async () => {
  const base = archive(), start = Date.parse(String(base.facts.started_at));
  const config = defaultConfig(base);
  config.alignment = "absolute"; config.viewport = { start: start + 10000, end: start + 20000 }; config.cursors = { a: start + 12000, b: start + 16000 };
  window.history.replaceState({}, "", shareUrl(config));
  vi.mocked(archivesApi.detail).mockImplementation(async (id) => { const result = archive(id); if (id === "other") result.facts.started_at = null; return result; });
  mount(); await ready(); await addOther();
  await waitFor(() => expect((screen.getByLabelText("工作台时间对齐") as HTMLSelectElement).value).toBe("elapsed"));
  expect(screen.getByText(/原绝对时间区间与 A\/B 游标已清空/)).toBeTruthy();
  expect((screen.getByLabelText("全局游标 A") as HTMLInputElement).value).toBe("");
  expect((screen.getByLabelText("全局游标 B") as HTMLInputElement).value).toBe("");
  expect(screen.getByLabelText("力矩 range").textContent).toBe('{"start":0,"end":100}');
  revealSave(); change("视图名称", "缺少开始时间"); fireEvent.click(screen.getByRole("button", { name: "保存到此浏览器" }));
  expect(loadViews("public-user-a")[0].config).toMatchObject({ alignment: "elapsed", viewport: null, cursors: { a: null, b: null } });
  fireEvent.click(screen.getByRole("button", { name: "复制分享链接" }));
  await screen.findByText(/自动复制不可用，请从下方链接框复制/);
  expect((screen.getByLabelText("当前视图链接") as HTMLInputElement).value).toContain("curve_view=");
  fireEvent.click(screen.getByRole("checkbox", { name: /工作台参与对比 合成负载 2.5 kg/ }));
  expect((screen.getByLabelText("工作台时间对齐") as HTMLSelectElement).value).toBe("elapsed");
});

it("reports overview-only failure and retries it without losing successful main data or statistics", async () => {
  const original = vi.mocked(archivesApi.curves).getMockImplementation()!;
  let failOverview = true;
  vi.mocked(archivesApi.curves).mockImplementation(async (id, filters, signal) => {
    if (filters.max_points === 800 && failOverview) throw new Error("合成概览请求失败");
    return original(id, filters, signal);
  });
  mount(); await ready(); fireEvent.click(screen.getByRole("button", { name: "聚焦力矩" }));
  await screen.findByText(/全程概览读取失败/);
  expect(screen.getByLabelText("力矩 series").textContent).toContain("base:j1_torque_nm");
  expect(screen.getByLabelText("力矩 exact statistics").textContent).toContain('"count":9999');
  failOverview = false; fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await waitFor(() => expect(screen.queryByText(/全程概览读取失败/)).toBeNull());
  expect(vi.mocked(archivesApi.curves).mock.calls.filter(([, filters]) => filters.max_points === 800)).toHaveLength(2);
});


it("reports link generation errors without suggesting a nonexistent copy field", async () => {
  mount(); await ready();
  vi.spyOn(workbenchConfig, "shareUrl").mockImplementation(() => { throw new Error("合成配置无法分享"); });
  fireEvent.click(screen.getByRole("button", { name: "复制分享链接" }));
  expect(screen.getByText("分享链接生成失败：合成配置无法分享")).toBeTruthy();
  expect(screen.queryByLabelText("当前视图链接")).toBeNull();
  expect(screen.queryByText(/请从下方链接框复制/)).toBeNull();
});

it("defines reversed A/B time cursors as signed B minus A, consistently with the chart", async () => {
  mount(); await ready();
  change("全局游标 A", "40"); change("全局游标 B", "10");
  fireEvent.submit(screen.getByLabelText("全局游标 A").closest("form")!);
  expect(screen.getByText("Δt -30 s")).toBeTruthy();
  expect(screen.getByText(/Δt = B − A/)).toBeTruthy();
});


it("retains surviving colors when the middle execution is removed and assigns additions to free slots", async () => {
  const config = defaultConfig(archive()); config.comparisons = ["other", "third"];
  window.history.replaceState({}, "", shareUrl(config));
  vi.mocked(archivesApi.detail).mockImplementation(async (id) => ({ ...archive(id), test_name: `执行 ${id}` }));
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [{ ...archive("new"), test_name: "新增执行" }], next_cursor: null });
  mount();
  const series = () => JSON.parse(screen.getByLabelText("力矩 series").textContent!) as PlotSeries[];
  await waitFor(() => expect(series()).toHaveLength(3));
  const before = Object.fromEntries(series().map((entry) => [entry.id, entry.color]));
  fireEvent.click(screen.getByRole("button", { name: "移除执行 执行 other" }));
  await waitFor(() => expect(series()).toHaveLength(2));
  expect(series().map((entry) => entry.color)).toEqual([before["base:j1_torque_nm"], before["third:j1_torque_nm"]]);
  change("工作台查找测试记录", "新增"); fireEvent.click(screen.getByRole("button", { name: "工作台查找对比记录" }));
  fireEvent.click(await screen.findByRole("button", { name: /新增执行.*添加对比/ }));
  await waitFor(() => expect(series()).toHaveLength(3));
  expect(series().find((entry) => entry.id === "third:j1_torque_nm")?.color).toBe(before["third:j1_torque_nm"]);
  expect(new Set(series().map((entry) => entry.color)).size).toBe(3);
  expect(series().find((entry) => entry.id === "new:j1_torque_nm")?.color).toBe(before["other:j1_torque_nm"]);
});

it("reserves colors for still-loading identities so out-of-order detail results do not recolor series", async () => {
  const config = defaultConfig(archive()); config.comparisons = ["slow", "fast"];
  window.history.replaceState({}, "", shareUrl(config));
  let finishSlow!: (value: Archive) => void;
  const slow = new Promise<Archive>((resolve) => { finishSlow = resolve; });
  vi.mocked(archivesApi.detail).mockImplementation((id) => id === "slow" ? slow : Promise.resolve({ ...archive(id), test_name: `执行 ${id}` }));
  mount();
  const series = () => JSON.parse(screen.getByLabelText("力矩 series").textContent!) as PlotSeries[];
  await waitFor(() => expect(series()).toHaveLength(2));
  const color = series().find((entry) => entry.id === "fast:j1_torque_nm")!.color;
  expect(color).toBe("#9365c7");
  await act(async () => finishSlow({ ...archive("slow"), test_name: "执行 slow" }));
  await waitFor(() => expect(series()).toHaveLength(3));
  expect(series().find((entry) => entry.id === "fast:j1_torque_nm")?.color).toBe(color);
  const dot = screen.getByRole("checkbox", { name: /工作台参与对比 执行 fast/ }).closest("label")!.querySelector<HTMLElement>(".curve-series-dot")!;
  expect(dot.style.getPropertyValue("--series-color")).toBe(color);
});

function narrowScreen() {
  let listener: ((event: MediaQueryListEvent) => void) | undefined;
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true, addEventListener: (_: string, callback: (event: MediaQueryListEvent) => void) => { listener = callback; }, removeEventListener: vi.fn() })));
  return (matches: boolean) => act(() => listener?.({ matches } as MediaQueryListEvent));
}

it("folds narrow execution/settings controls with explicit state, preserving selections and mounted panels", async () => {
  narrowScreen();
  const config = defaultConfig(archive()); config.comparisons = ["other", "third"];
  window.history.replaceState({}, "", shareUrl(config));
  vi.mocked(archivesApi.detail).mockImplementation(async (id) => ({ ...archive(id), test_name: `执行 ${id}` }));
  mount();
  const selection = await screen.findByRole("button", { name: /已选执行 3 \/ 8/ });
  const body = document.getElementById(selection.getAttribute("aria-controls")!)!;
  expect(body.hidden).toBe(true);
  expect(selection.getAttribute("aria-expanded")).toBe("false");
  expect(screen.getByText(/基准：执行 base/)).toBeTruthy();
  const settings = screen.getByRole("button", { name: /全局设置 · 相对测试开始/ });
  expect(document.getElementById(settings.getAttribute("aria-controls")!)!.hidden).toBe(true);
  expect(screen.getByText(/当前归档未提供阶段、循环索引/).closest("[hidden]")).toBeNull();
  const chart = screen.getByTestId("chart-力矩");
  fireEvent.click(selection);
  change("工作台查找测试记录", "保留搜索草稿");
  fireEvent.click(screen.getByRole("checkbox", { name: /工作台参与对比 执行 other/ }));
  fireEvent.click(selection);
  expect(selection.textContent).toContain("已选执行 2 / 8");
  expect(selection.textContent).toContain("已排除 1");
  expect(body.hidden).toBe(true);
  fireEvent.click(selection);
  expect((screen.getByLabelText("工作台查找测试记录") as HTMLInputElement).value).toBe("保留搜索草稿");
  expect((screen.getByRole("checkbox", { name: /工作台参与对比 执行 other/ }) as HTMLInputElement).checked).toBe(false);
  fireEvent.click(settings); change("工作台视窗点数上限", "1000"); fireEvent.click(settings);
  expect(settings.textContent).toContain("1,000 点/执行");
  expect(screen.getByTestId("chart-力矩")).toBe(chart);
});

it("opens and focuses search from Add, and restores narrow folding after a desktop resize", async () => {
  const resize = narrowScreen(); mount(); await ready();
  const selection = screen.getByRole("button", { name: /已选执行 1 \/ 8/ });
  const body = document.getElementById(selection.getAttribute("aria-controls")!)!;
  resize(false); expect(body.hidden).toBe(false);
  resize(true); expect(body.hidden).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "添加执行" }));
  expect(body.hidden).toBe(false);
  expect(document.activeElement).toBe(screen.getByLabelText("工作台查找测试记录"));
  fireEvent.click(selection); expect(body.hidden).toBe(true);
  resize(false); expect(body.hidden).toBe(false);
  resize(true); expect(body.hidden).toBe(true);
});

it("keeps access errors and missing trusted-start restrictions outside narrow folded controls", async () => {
  narrowScreen();
  const config = defaultConfig(archive()); config.comparisons = ["denied"];
  window.history.replaceState({}, "", shareUrl(config));
  vi.mocked(archivesApi.detail).mockImplementation(async (id) => {
    if (id === "denied") throw new Error("合成权限失效");
    return { ...archive(id), facts: { active_seconds: 100 } };
  });
  mount();
  const error = await screen.findByText("第 2 个执行不可访问，未加载曲线。");
  expect(error.closest("[hidden]")).toBeNull();
  expect(screen.getByText("选中执行缺少可信开始时间，绝对时间对齐不可用。").closest("[hidden]")).toBeNull();
  expect(document.getElementById("wb-selected-executions")!.hidden).toBe(true);
  expect(document.getElementById("wb-global-settings")!.hidden).toBe(true);
});
