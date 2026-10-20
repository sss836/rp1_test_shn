// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getInstanceByDom, type EChartsType } from "echarts/core";
import { breakCurveAtGaps, TimeSeriesChart, type PlotSeries } from "./TimeSeriesChart";
import * as chartTools from "./timeSeriesChartTools";

// Exercise the real ECharts tooltip/model lifecycle. SVG only replaces the
// browser's Canvas drawing surface, which jsdom does not implement.
vi.mock("echarts/core", async (importOriginal) => {
  const core = await importOriginal<typeof import("echarts/core")>();
  const { SVGRenderer } = await import("echarts/renderers");
  core.use([SVGRenderer]);
  core.setPlatformAPI({ measureText: (text) => ({ width: text.length * 7 }) });
  return { ...core, init: (host: HTMLElement) => core.init(host, undefined, { renderer: "svg", width: 800, height: host.classList.contains("chart-overview-canvas") ? 84 : 475 }) };
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const first: PlotSeries = { id: "first", name: "A 采集", unit: "N·m", points: [[0, 1], [1, 2]] };
const second: PlotSeries = { id: "second", name: "B 采集", unit: "N·m", points: [[0, 3], [1, 4]] };
const props = { domain: { start: 0, end: 1 }, viewport: { start: 0, end: 1 }, references: [], onViewport: vi.fn(), loading: false, resolution: "原始分辨率" };

function tooltipOption(instance: EChartsType) {
  return (instance.getOption().tooltip as { formatter: (params: unknown[]) => HTMLElement; confine: boolean; enterable: boolean; renderMode: string }[])[0];
}

it("renders tooltip identities as literal text and preserves precise finite values, units, missing values and sample times", () => {
  const identity = "完整执行 UUID 11111111-2222-3333-4444-555555555555 · joint-1";
  const items: PlotSeries[] = [
    { ...first, name: identity, legendName: "<img src=x>{a|b}", legendDetail: "run-A · joint-1", points: [[0.123456789, 1e-7]] },
    { ...second, legendName: "<script>目标</script>", legendDetail: "run-A · joint-2", points: [[0.25, null]] },
  ];
  const view = render(<TimeSeriesChart {...props} series={items} />);
  const option = tooltipOption(mainInstance(view.container));
  expect(option).toMatchObject({ confine: true, enterable: true, renderMode: "html" });
  const content = option.formatter(items.map((item) => ({ seriesId: item.id, value: item.points[0] })));
  expect(content.querySelector("img,script")).toBeNull();
  expect(content.textContent).toContain("<img src=x>{a|b}");
  expect(content.textContent).toContain("<script>目标</script>");
  expect(content.textContent).toContain("run-A · joint-1");
  expect(content.textContent).toContain("run-A · joint-2");
  expect(content.textContent).toContain("1e-7 N·m");
  expect(content.textContent).toContain("缺失 N·m");
  expect(content.textContent).toContain("采样时间：0.123456789 s");
  expect(content.querySelector("strong")?.title).toBe(identity);
  expect(screen.getByLabelText(`显示曲线 ${identity}`)).toBeTruthy();
  const invalid = option.formatter([{ seriesId: first.id, value: [null, Infinity] }]);
  expect(invalid.textContent).toContain("缺失 N·m");
  expect(invalid.textContent).toContain("采样时间：缺失");
});

it("bounds a tooltip to the resized chart while retaining all series and extreme values for local scrolling", () => {
  const items = Array.from({ length: 40 }, (_, index): PlotSeries => ({ ...first, id: `series-${index}`, name: `执行 ${index} ${"长身份".repeat(40)}`, legendName: `同名执行 ${index}`, legendDetail: `run-${index} · joint-${index}`, points: [[0, index === 0 ? Number.MAX_VALUE : -Number.MIN_VALUE]] }));
  const view = render(<TimeSeriesChart {...props} series={items} />);
  const instance = mainInstance(view.container);
  act(() => instance.resize({ width: 280, height: 196 }));
  const content = tooltipOption(instance).formatter(items.map((item) => ({ seriesId: item.id, value: item.points[0] })));
  expect(content.style.width).toBe("256px");
  expect(content.style.maxHeight).toBe("172px");
  expect(content.classList.contains("chart-point-tooltip")).toBe(true);
  expect(content.tabIndex).toBe(0);
  expect(content.children).toHaveLength(40);
  expect(content.textContent).toContain(`${String(Number.MAX_VALUE)} N·m`);
  expect(content.textContent).toContain("-5e-324 N·m");
  expect(content.textContent).toContain("run-39 · joint-39");
  const ancestor = document.createElement("div"); ancestor.append(content);
  const wheel = vi.fn(); ancestor.addEventListener("wheel", wheel);
  content.dispatchEvent(new WheelEvent("wheel", { bubbles: true }));
  expect(wheel).not.toHaveBeenCalled();
});

it("invalidates a hovered axis tooltip before asynchronous series removal and restores hover on current data", async () => {
  const view = render(<TimeSeriesChart {...props} series={[first, second]} />);
  const host = view.container.querySelector<HTMLElement>(".time-series-canvas")!;
  const instance = getInstanceByDom(host)!;
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("A 采集");
  expect(host.textContent).toContain("B 采集");

  // A leaves the viewport while B stays under the pointer. ECharts preserves
  // B's series index, so cached tooltip indices for A are no longer valid.
  view.rerender(<TimeSeriesChart {...props} series={[second]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("B 采集");
  expect(host.textContent).not.toContain("A 采集");

  // An empty response followed by fresh data must not resurrect old indices.
  view.rerender(<TimeSeriesChart {...props} series={[]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  view.rerender(<TimeSeriesChart {...props} series={[first, second]} />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  act(() => { instance.dispatchAction({ type: "updateAxisPointer", x: 200, y: 180 }); instance.getZr().flush(); });
  expect(host.textContent).toContain("A 采集");
  expect(host.textContent).toContain("B 采集");
});

const fullDomain = { start: 0, end: 100 };
const fullSeries: PlotSeries = { id: "full", name: "全程", unit: "N·m", points: [[0, 1], [20, 2], [40, 3], [60, 2], [80, 4], [100, 2]] };
function mainInstance(container: HTMLElement) { return getInstanceByDom(container.querySelector<HTMLElement>(".time-series-canvas")!)!; }
function mainSeries(instance: EChartsType) { return instance.getOption().series as { id: string; data: [number, number | null][]; connectNulls: boolean; xAxisIndex: number; yAxisIndex: number; markPoint?: { data: { name: string; coord: number[] }[] } }[]; }
function zoom(instance: EChartsType, start: number, end: number) { instance.dispatchAction({ type: "dataZoom", dataZoomId: "x-inside", startValue: start, endValue: end }); }
async function flushViewport() { await act(async () => { await vi.advanceTimersByTimeAsync(300); }); }

it("preserves every explicit gap and sampled null without mutating input", () => {
  const points: PlotSeries["points"] = [[0, 1], [2, 3], [3, null], [5, 5], [8, 8], [10, 3]];
  const copy = structuredClone(points);
  expect(breakCurveAtGaps(points, [{ start: 1, end: 2 }, { start: 6, end: 7 }])).toEqual([[0, 1], [1.5, null], [2, 3], [3, null], [5, 5], [6.5, null], [8, 8], [10, 3]]);
  expect(points).toEqual(copy);
  expect(breakCurveAtGaps([[0, 1], [100, 2]])).toEqual([[0, 1], [100, 2]]);
});

it("debounces the final viewport and does not repeat an already committed event", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => { zoom(instance, 10, 70); zoom(instance, 20, 60); zoom(instance, 20, 60); });
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 20, end: 60 });
  act(() => zoom(instance, 20, 60));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
});

it("cancels pending viewport work on reset, externally changed domain, and unmount", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => zoom(instance, 20, 60));
  fireEvent.click(screen.getByRole("button", { name: "重置视图" }));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith(fullDomain);
  onViewport.mockClear();
  act(() => zoom(instance, 20, 60));
  view.rerender(<TimeSeriesChart {...props} domain={{ start: 0, end: 50 }} viewport={{ start: 0, end: 50 }} onViewport={onViewport} series={[fullSeries]} />);
  await flushViewport();
  expect(onViewport).not.toHaveBeenCalled();
  act(() => zoom(instance, 10, 30));
  view.unmount();
  await flushViewport();
  expect(onViewport).not.toHaveBeenCalled();
});

it("validates manual axes, clips time to the domain, and resets Y zoom", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  fireEvent.click(screen.getByRole("button", { name: "X 范围" }));
  fireEvent.change(screen.getByLabelText("时间起点"), { target: { value: "80" } });
  fireEvent.change(screen.getByLabelText("时间终点"), { target: { value: "20" } });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  expect(screen.getByRole("alert").textContent).toContain("有效时间范围");
  expect(onViewport).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("时间起点"), { target: { value: "-10" } });
  fireEvent.change(screen.getByLabelText("时间终点"), { target: { value: "40" } });
  fireEvent.change(screen.getByLabelText("纵轴下限"), { target: { value: "-5" } });
  fireEvent.change(screen.getByLabelText("纵轴上限"), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 0, end: 40 });
  const instance = mainInstance(view.container);
  expect((instance.getOption().yAxis as { min: number; max: number }[])[0]).toMatchObject({ min: -5, max: 8 });
  fireEvent.click(screen.getByRole("button", { name: "重置视图" }));
  expect((instance.getOption().yAxis as { min?: number }[])[0].min).toBeUndefined();
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(2);
});

it("uses separate linked grids in facets and restores axes on return to overlay", () => {
  const view = render(<TimeSeriesChart {...props} series={[first, second]} layout="facets" />);
  const instance = mainInstance(view.container);
  expect((instance.getOption().grid as unknown[])).toHaveLength(2);
  expect(mainSeries(instance).map((item) => [item.xAxisIndex, item.yAxisIndex])).toEqual([[0, 0], [1, 1]]);
  expect((instance.getOption().axisPointer as { link: unknown[] }[])[0].link).toEqual([{ xAxisIndex: [0, 1] }]);
  fireEvent.click(screen.getByRole("checkbox", { name: "联动游标" }));
  expect((instance.getOption().axisPointer as { link: unknown[] }[])[0].link).toEqual([]);
  fireEvent.click(screen.getByRole("checkbox", { name: "显示曲线 A 采集" }));
  expect((instance.getOption().grid as unknown[])).toHaveLength(1);
  expect(mainSeries(instance).find((item) => item.id === "first")?.data).toEqual([]);
  view.rerender(<TimeSeriesChart {...props} series={[first, second]} layout="overlay" />);
  expect((instance.getOption().grid as unknown[])).toHaveLength(1);
  expect(mainSeries(instance).every((item) => item.yAxisIndex === 0)).toBe(true);
});

it("keeps the complete overview data independent of the current narrow window, including gaps", async () => {
  const onViewport = vi.fn();
  const overview: PlotSeries = { ...fullSeries, gaps: [{ start: 5, end: 10 }, { start: 85, end: 90 }] };
  const current: PlotSeries = { ...fullSeries, points: [[40, 3], [60, 2]] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={{ start: 40, end: 60 }} series={[current]} overviewSeries={[overview]} onViewport={onViewport} />);
  const instance = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  expect(mainSeries(instance)[0].data).toContainEqual([7.5, null]);
  expect(mainSeries(instance)[0].data).toContainEqual([87.5, null]);
  expect(mainSeries(instance)[0].connectNulls).toBe(false);
  act(() => instance.dispatchAction({ type: "dataZoom", dataZoomId: "overview-slider", startValue: 10, endValue: 30 }));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledExactlyOnceWith({ start: 10, end: 30 });
  expect(instance.convertToPixel({ xAxisIndex: 0 }, 0)).toBeCloseTo(58);
  expect(instance.convertToPixel({ xAxisIndex: 0 }, 100)).toBeCloseTo(752);
  expect(mainSeries(instance)[0].data.at(-1)).toEqual([100, 2]);
});

it("renders only explicitly supplied markers, omitting markers inside a declared gap", () => {
  const marked: PlotSeries = { ...fullSeries, gaps: [{ start: 30, end: 50 }], anomalies: [{ time: 20, value: 2, label: "采样越限" }, { time: 40, value: 3, label: "不得显示" }] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[marked]} />);
  const plotted = mainSeries(mainInstance(view.container))[0];
  expect(plotted.markPoint?.data).toHaveLength(1);
  expect(plotted.markPoint?.data[0]).toMatchObject({ name: "采样越限", coord: [20, 2], value: 2 });
  expect(plotted.data).toContainEqual([40, null]);
  const host = view.container.querySelector<HTMLElement>(".time-series-canvas")!;
  expect(host.textContent).toContain("采样越限");
  expect(host.textContent).toContain("缺失段");
  expect(host.textContent).not.toContain("不得显示");
});

it("rejects mixed units and hides every incompatible line", () => {
  const view = render(<TimeSeriesChart {...props} series={[first, { ...second, unit: "V" }]} />);
  expect(screen.getByRole("alert").textContent).toContain("单位不一致");
  expect(mainSeries(mainInstance(view.container))).toEqual([]);
});

it("formats fractional relative-time ticks in both charts without overriding absolute-time formatting", () => {
  const domain = { start: 20.056497175141246, end: 60.5461 };
  const view = render(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[fullSeries]} />);
  const main = mainInstance(view.container);
  const overview = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  const formatter = (instance: EChartsType) => (instance.getOption().xAxis as { axisLabel: { formatter?: (value: number) => string } }[])[0].axisLabel.formatter;
  expect(formatter(main)?.(domain.start)).toBe("20.0565");
  expect(formatter(overview)?.(domain.start)).toBe("20.0565");
  expect(view.container.textContent).not.toContain("20.056497175141246");
  view.rerender(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[fullSeries]} absolute />);
  expect(formatter(main)).toBeUndefined();
  expect(formatter(overview)).toBeUndefined();
});

it("removes old limits, markers, and shaded gaps when their inputs are disabled", () => {
  const marked: PlotSeries = { ...fullSeries, gaps: [{ start: 30, end: 50 }], anomalies: [{ time: 20, value: 2, label: "超限" }] };
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[marked]} references={[{ id: "limit", name: "上限", value: 3 }]} />);
  const instance = mainInstance(view.container);
  view.rerender(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[fullSeries]} />);
  const plotted = instance.getOption().series as { markLine?: { data?: unknown[] }; markPoint?: { data?: unknown[] }; markArea?: { data?: unknown[] } }[];
  expect(plotted[0].markLine?.data ?? []).toEqual([]);
  expect(plotted[0].markPoint?.data ?? []).toEqual([]);
  expect(plotted[0].markArea?.data ?? []).toEqual([]);
});

function dragOnZrender(instance: EChartsType, from: number, to: number) {
  const start = instance.convertToPixel({ xAxisIndex: 0 }, from) as number;
  const end = instance.convertToPixel({ xAxisIndex: 0 }, to) as number;
  const y = instance.convertToPixel({ yAxisIndex: 0 }, 2.5) as number;
  for (const [type, x] of [["mousedown", start], ["mousemove", end], ["mouseup", end]] as const) {
    instance.getZr().trigger(type, { offsetX: x, offsetY: y, event: { button: 0, preventDefault() {}, stopPropagation() {} } });
  }
}

it("routes a real ECharts brush controller drag to a viewport request and clears the selection", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} onViewport={onViewport} series={[fullSeries]} />);
  fireEvent.click(screen.getByRole("button", { name: "框选缩放" }));
  const instance = mainInstance(view.container);
  const brushAreas: unknown[][] = [];
  instance.on("brush", (event: unknown) => { brushAreas.push((event as { areas: unknown[] }).areas); });
  act(() => dragOnZrender(instance, 20, 60));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
  expect(onViewport.mock.calls[0][0].start).toBeCloseTo(20);
  expect(onViewport.mock.calls[0][0].end).toBeCloseTo(60);
  expect(brushAreas.at(-1)).toEqual([]);
});

it("routes a real ECharts roam controller pan to the shifted time window", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={{ start: 20, end: 80 }} onViewport={onViewport} series={[fullSeries]} />);
  const instance = mainInstance(view.container);
  act(() => dragOnZrender(instance, 40, 50));
  await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(1);
  expect(onViewport.mock.calls[0][0].start).toBeCloseTo(10);
  expect(onViewport.mock.calls[0][0].end).toBeCloseTo(70);
});

it("renders a compact independent panel without overview or toolbars but keeps PNG available", () => {
  const view = render(<TimeSeriesChart {...props} series={[first]} compact height={260} showOverview={false} showToolbar={false} showCursorControls={false} />);
  expect(view.container.querySelector(".time-series-canvas")?.getAttribute("style")).toContain("260px");
  expect(view.container.querySelector(".chart-overview-canvas")).toBeNull();
  expect(screen.queryByRole("button", { name: "X 范围" })).toBeNull();
  expect(screen.queryByRole("form", { name: "固定时间游标" })).toBeNull();
  expect(screen.getByRole("button", { name: "PNG" })).toBeTruthy();
  expect(mainSeries(mainInstance(view.container))[0].data).toEqual(first.points);
  view.rerender(<TimeSeriesChart {...props} series={[first]} />);
  expect(view.container.querySelector(".chart-overview-canvas")).not.toBeNull();
  expect(mainSeries(mainInstance(view.container))[0].data).toEqual(first.points);
});

it("shares exact X cursor values between different sampling grids without coupling Y zoom", () => {
  const sparse: PlotSeries = { id: "sparse", name: "稀疏", unit: "°C", points: [[0, 21], [17, 24], [66, 35], [100, 26]] };
  const view = render(<><TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[fullSeries]} cursorGroup="shared-test" showOverview={false} /><TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[sparse]} cursorGroup="shared-test" showOverview={false} /></>);
  const instances = [...view.container.querySelectorAll<HTMLElement>(".time-series-canvas")].map((host) => getInstanceByDom(host)!);
  const receive = vi.fn(); instances[1].on("updateAxisPointer", receive);
  act(() => instances[0].dispatchAction({ type: "updateAxisPointer", x: instances[0].convertToPixel({ xAxisIndex: 0 }, 35), y: 50 }));
  const cursor = receive.mock.calls.at(-1)?.[0] as { axesInfo: { axisDim: string; value: number }[] };
  expect(cursor.axesInfo.find((axis) => axis.axisDim === "x")?.value).toBeCloseTo(35);
  act(() => instances[0].dispatchAction({ type: "dataZoom", dataZoomId: "y-inside", start: 20, end: 80 }));
  const zooms = instances[1].getOption().dataZoom as { id: string; start: number; end: number }[];
  expect(zooms.find((item) => item.id === "y-inside")).toMatchObject({ start: 0, end: 100 });
  act(() => instances[0].dispatchAction({ type: "updateAxisPointer", currTrigger: "leave" }));
  expect((receive.mock.calls.at(-1)?.[0] as { axesInfo: unknown[] }).axesInfo).toEqual([]);
});

it("uses supplied exact statistics instead of sampled points and supports isolate, restore and styles", () => {
  const stats = { first: { count: 12000, missing: 31, min: -0.0000012, max: 999, mean: 57, rms: 81 }, second: { count: 0, missing: 20, min: null, max: null, mean: null, rms: null } };
  const view = render(<TimeSeriesChart {...props} series={[first, second]} statistics={stats} />);
  const rows = within(screen.getByRole("table")).getAllByRole("row");
  expect(within(rows[1]).getAllByRole("cell").map((cell) => cell.textContent).slice(0, 6)).toEqual(["12,000", "31", "-1.2000e-6", "999", "57", "81"]);
  expect(within(rows[2]).getAllByRole("cell").map((cell) => cell.textContent).slice(0, 6)).toEqual(["0", "20", "—", "—", "—", "—"]);
  fireEvent.click(screen.getByRole("button", { name: "单独显示 B 采集" }));
  expect(mainSeries(mainInstance(view.container))[0].data).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: "恢复全部曲线" }));
  expect(mainSeries(mainInstance(view.container))[0].data).toEqual(first.points);
  fireEvent.change(screen.getByLabelText("颜色 A 采集"), { target: { value: "#ff0000" } });
  fireEvent.change(screen.getByLabelText("线型 A 采集"), { target: { value: "dotted" } });
  expect((mainInstance(view.container).getOption().series as { lineStyle: unknown }[])[0].lineStyle).toMatchObject({ color: "#ff0000", type: "dotted" });
  expect(first.color).toBeUndefined();
  view.rerender(<TimeSeriesChart {...props} series={[first, second]} statistics={stats} statisticsLoading />);
  expect(within(screen.getByRole("table")).queryByText("12,000")).toBeNull();
});

it("keeps fixed A/B time cursors separate from gap samples and validates their bounds", () => {
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[{ ...fullSeries, gaps: [{ start: 30, end: 50 }] }]} />);
  fireEvent.change(screen.getByLabelText("A 时间（s）"), { target: { value: "35" } });
  fireEvent.change(screen.getByLabelText("B 时间（s）"), { target: { value: "65" } });
  fireEvent.click(screen.getByRole("button", { name: "固定游标" }));
  expect(screen.getByLabelText("游标时间差").textContent).toContain("Δt 30 s");
  const lines = (mainInstance(view.container).getOption().series as { markLine: { data: { xAxis: number }[] } }[])[0].markLine.data;
  expect(lines.map((line) => line.xAxis)).toEqual([35, 65]);
  expect(mainSeries(mainInstance(view.container))[0].data).toContainEqual([40, null]);
  fireEvent.change(screen.getByLabelText("B 时间（s）"), { target: { value: "101" } });
  fireEvent.click(screen.getByRole("button", { name: "固定游标" }));
  expect(screen.getByRole("alert").textContent).toContain("采集范围内");
  expect(screen.getByLabelText("游标时间差").textContent).toContain("Δt 30 s");
  fireEvent.click(screen.getByRole("button", { name: "清除游标" }));
  expect(screen.getByLabelText("游标时间差").textContent).toBe("Δt —");
});

it("shows controlled cursor updates and converts absolute milliseconds to delta seconds", () => {
  const onFixedCursorsChange = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={{ start: 1000, end: 10000 }} viewport={{ start: 1000, end: 10000 }} series={[first]} absolute fixedCursors={{ a: 2000, b: 5500 }} onFixedCursorsChange={onFixedCursorsChange} />);
  expect(screen.getByLabelText("游标时间差").textContent).toContain("Δt 3.5 s");
  view.rerender(<TimeSeriesChart {...props} domain={{ start: 1000, end: 10000 }} viewport={{ start: 1000, end: 10000 }} series={[first]} absolute fixedCursors={{ a: 3000, b: 5500 }} onFixedCursorsChange={onFixedCursorsChange} />);
  expect((screen.getByLabelText("A 时间（ms）") as HTMLInputElement).value).toBe("3000");
  fireEvent.click(screen.getByRole("button", { name: "清除游标" }));
  expect(onFixedCursorsChange).toHaveBeenCalledWith({ a: null, b: null });
});

it("returns to previous viewports and discards history when the collection domain changes", async () => {
  const onViewport = vi.fn();
  const view = render(<TimeSeriesChart {...props} domain={fullDomain} viewport={fullDomain} series={[fullSeries]} onViewport={onViewport} />);
  const instance = mainInstance(view.container);
  act(() => zoom(instance, 20, 70)); await flushViewport();
  act(() => zoom(instance, 30, 60)); await flushViewport();
  fireEvent.click(screen.getByRole("button", { name: "返回上个范围" }));
  expect(onViewport).toHaveBeenLastCalledWith({ start: 20, end: 70 });
  fireEvent.click(screen.getByRole("button", { name: "返回上个范围" }));
  expect(onViewport).toHaveBeenLastCalledWith(fullDomain);
  act(() => zoom(instance, 30, 60)); await flushViewport();
  view.rerender(<TimeSeriesChart {...props} series={[first]} onViewport={onViewport} />);
  expect((screen.getByRole("button", { name: "返回上个范围" }) as HTMLButtonElement).disabled).toBe(true);
});

it("labels absolute axes with the browser timezone and exports readable UTC range and cursor metadata", async () => {
  const start = Date.parse("2026-10-16T12:00:00.000Z"), end = Date.parse("2026-10-16T12:00:04.000Z");
  const domain = { start, end }, timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const png = vi.spyOn(chartTools, "chartPngDataUrl").mockResolvedValue("data:image/png;base64,actual-canvas");
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  const view = render(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[{ ...first, points: [[start, 1], [end, 2]] }]} absolute fixedCursors={{ a: start + 1000, b: end }} />);
  const axis = () => (mainInstance(view.container).getOption().xAxis as { name: string; type: string }[])[0];
  expect(axis()).toMatchObject({ type: "time", name: `绝对时间（浏览器时区：${timeZone}）` });
  expect(mainInstance(view.container).getOption().useUTC).toBe(false);
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "PNG" })));
  expect(png.mock.calls[0][1].range).toBe(`绝对时间范围（UTC）：2026-10-16T12:00:00.000Z – 2026-10-16T12:00:04.000Z · 图中时区：${timeZone} · 单位：N·m`);
  expect(png.mock.calls[0][1].cursors).toBe("固定游标（UTC）：A 2026-10-16T12:00:01.000Z · B 2026-10-16T12:00:04.000Z · Δt 3 s");
  expect(png.mock.calls[0][1].range).not.toContain(String(start));
  view.rerender(<TimeSeriesChart {...props} series={[first]} xLabel="循环起点 / s" />);
  expect(axis()).toMatchObject({ type: "value", name: "循环起点 / s" });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "PNG" })));
  expect(png.mock.calls[1][1].range).toBe("循环起点 / s：0 – 1 · 单位：N·m");
  view.rerender(<TimeSeriesChart {...props} series={[first]} />);
  expect(axis().name).toBe("相对测试开始 / s");
  view.rerender(<TimeSeriesChart {...props} domain={domain} viewport={domain} series={[first]} absolute xLabel="采集墙钟时间" />);
  expect(axis().name).toBe("采集墙钟时间");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "PNG" })));
  expect(png.mock.calls[2][1].range).toContain("采集墙钟时间 · 绝对时间范围（UTC）：2026-10-16T12:00:00.000Z");
});

it("preserves styles, hidden rows, Y range, zoom mode and history across hidden and focused layouts", async () => {
  const onViewport = vi.fn();
  const data = [fullSeries, { ...fullSeries, id: "alternate", name: "对比" }];
  const input = { ...props, domain: fullDomain, viewport: fullDomain, series: data, onViewport, showCursorControls: false, cursorGroup: "focus-state" };
  const view = render(<TimeSeriesChart {...input} compact showOverview={false} />);
  fireEvent.change(screen.getByLabelText("颜色 全程"), { target: { value: "#ff0000" } });
  fireEvent.change(screen.getByLabelText("线型 全程"), { target: { value: "dotted" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "显示曲线 对比" }));
  fireEvent.click(screen.getByRole("button", { name: "Y 范围" }));
  fireEvent.change(screen.getByLabelText("纵轴下限"), { target: { value: "-5" } });
  fireEvent.change(screen.getByLabelText("纵轴上限"), { target: { value: "8" } });
  fireEvent.click(screen.getByRole("button", { name: "应用范围" }));
  fireEvent.click(screen.getByRole("button", { name: "框选缩放" }));
  let instance = mainInstance(view.container);
  act(() => instance.dispatchAction({ type: "dataZoom", dataZoomId: "y-inside", start: 15, end: 85 }));
  act(() => zoom(instance, 20, 70)); await flushViewport();
  view.rerender(<TimeSeriesChart {...input} viewport={{ start: 20, end: 70 }} compact showOverview={false} active={false} />);
  expect(mainInstance(view.container)).toBe(instance);
  const calls = onViewport.mock.calls.length;
  act(() => zoom(instance, 30, 50)); await flushViewport();
  expect(onViewport).toHaveBeenCalledTimes(calls);
  view.rerender(<TimeSeriesChart {...input} viewport={{ start: 20, end: 70 }} showOverview />);
  instance = mainInstance(view.container);
  expect((screen.getByRole("checkbox", { name: "显示曲线 对比" }) as HTMLInputElement).checked).toBe(false);
  expect((instance.getOption().series as { lineStyle: unknown }[])[0].lineStyle).toMatchObject({ color: "#ff0000", type: "dotted" });
  expect(mainSeries(instance)[1].data).toEqual([]);
  expect((instance.getOption().yAxis as { min: number; max: number }[])[0]).toMatchObject({ min: -5, max: 8 });
  expect((instance.getOption().dataZoom as { id: string; start: number; end: number }[]).find((entry) => entry.id === "y-inside")).toMatchObject({ start: 15, end: 85 });
  expect(screen.getByRole("button", { name: "框选缩放" }).getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(screen.getByRole("button", { name: "返回上个范围" }));
  expect(onViewport).toHaveBeenLastCalledWith(fullDomain);
});

it("skips hidden crosshair receivers and resizes on reactivation before sharing finite pixels", () => {
  const view = render(<><TimeSeriesChart {...props} series={[first]} cursorGroup="visibility-test" showOverview={false} /><TimeSeriesChart {...props} series={[second]} cursorGroup="visibility-test" showOverview={false} /></>);
  const instances = [...view.container.querySelectorAll<HTMLElement>(".time-series-canvas")].map((element) => getInstanceByDom(element)!);
  const receive = vi.spyOn(instances[1], "dispatchAction"), resize = vi.spyOn(instances[1], "resize"), pixel = vi.spyOn(instances[1], "convertToPixel");
  view.rerender(<><TimeSeriesChart {...props} series={[first]} cursorGroup="visibility-test" showOverview={false} /><TimeSeriesChart {...props} series={[second]} cursorGroup="visibility-test" showOverview={false} active={false} /></>);
  receive.mockClear(); pixel.mockClear();
  act(() => instances[0].dispatchAction({ type: "updateAxisPointer", x: instances[0].convertToPixel({ xAxisIndex: 0 }, .5), y: 50 }));
  expect(pixel).not.toHaveBeenCalled(); expect(receive).not.toHaveBeenCalled();
  view.rerender(<><TimeSeriesChart {...props} series={[first]} cursorGroup="visibility-test" showOverview={false} /><TimeSeriesChart {...props} series={[second]} cursorGroup="visibility-test" showOverview={false} /></>);
  expect(resize).toHaveBeenCalled(); receive.mockClear();
  act(() => instances[0].dispatchAction({ type: "updateAxisPointer", x: instances[0].convertToPixel({ xAxisIndex: 0 }, .6), y: 50 }));
  expect(receive.mock.calls.some(([action]) => action.type === "updateAxisPointer" && typeof action.x === "number" && Number.isFinite(action.x))).toBe(true);
});

it("sorts raw numeric legend statistics with nulls last, announces direction and leaves chart data unchanged", () => {
  const third = { ...first, id: "third", name: "C 缺失" };
  const stats = { first: { count: 12, missing: 3, min: -10, max: 20, mean: 2, rms: 10 }, second: { count: 3, missing: 11, min: -2, max: 100, mean: 10, rms: 2 }, third: { count: 0, missing: 20, min: null, max: null, mean: null, rms: null } };
  const view = render(<TimeSeriesChart {...props} series={[first, second, third]} statistics={stats} />);
  const plotted = JSON.stringify(mainSeries(mainInstance(view.container)));
  const order = () => within(screen.getByRole("table")).getAllByRole("checkbox").map((input) => input.getAttribute("aria-label"));
  for (const [label, ascending] of [["Min", ["A 采集", "B 采集", "C 缺失"]], ["Max", ["A 采集", "B 采集", "C 缺失"]], ["Mean", ["A 采集", "B 采集", "C 缺失"]], ["RMS", ["B 采集", "A 采集", "C 缺失"]], ["有效数", ["C 缺失", "B 采集", "A 采集"]]] as const) {
    const button = screen.getByRole("button", { name: `按 ${label} 排序` });
    fireEvent.click(button);
    expect(button.closest("th")?.getAttribute("aria-sort")).toBe("ascending");
    expect(order()).toEqual(ascending.map((name) => `显示曲线 ${name}`));
    fireEvent.click(button);
    expect(button.closest("th")?.getAttribute("aria-sort")).toBe("descending");
    const descending = label === "有效数" ? [...ascending].reverse() : [ascending[1], ascending[0], ascending[2]];
    expect(order()).toEqual(descending.map((name) => `显示曲线 ${name}`));
    expect(JSON.stringify(mainSeries(mainInstance(view.container)))).toBe(plotted);
  }
  expect(first.points).toEqual([[0, 1], [1, 2]]);
});

it("renders a concise legend with the full identity available to keyboard and assistive controls", () => {
  const name = "完整测试名称 · ARC-1234567890abcdef · left_shoulder_pitch_joint · 目标";
  render(<TimeSeriesChart {...props} series={[{ ...first, name, legendName: "左臂 5.5 kg · 目标", legendDetail: "ARC-123…def · J1 · rad" }]} compact showOverview={false} />);
  expect(screen.getByText("左臂 5.5 kg · 目标").closest(".chart-series-identity")?.getAttribute("title")).toBe(name);
  expect(screen.getByRole("checkbox", { name: `显示曲线 ${name}` })).toBeTruthy();
  expect(screen.getByLabelText(`颜色 ${name}`)).toBeTruthy();
  expect(screen.getByRole("button", { name: "框选缩放" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "返回上个范围" })).toBeTruthy();
});

function renderedSymbols(instance: EChartsType) {
  // Inspect real ECharts render data, not just a proposed option. A symbol-less
  // one-point line has no visible stroke, even though its value is retained.
  return (instance as unknown as { getModel: () => { getSeriesByIndex: (index: number) => { getData: () => { getItemGraphicEl: (index: number) => { getBoundingRect: () => { width: number } } | undefined } } } }).getModel().getSeriesByIndex(0).getData();
}

it("draws only isolated finite samples as real symbols in both the main plot and overview", () => {
  const points: PlotSeries["points"] = [[0, 999], [1, null], [2, -999], [3, null], [4, 1], [5, 2], [6, null], [7, 3]];
  const item = { ...first, points };
  const range = { start: 0, end: 7 };
  const view = render(<TimeSeriesChart {...props} domain={range} viewport={range} series={[item]} overviewSeries={[item]} />);
  const main = mainInstance(view.container);
  const overview = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  for (const instance of [main, overview]) {
    expect(mainSeries(instance)[0].data).toEqual(points);
    expect(mainSeries(instance)[0].connectNulls).toBe(false);
    const data = renderedSymbols(instance);
    for (const index of [0, 2, 7]) expect(data.getItemGraphicEl(index)?.getBoundingRect().width).toBeGreaterThanOrEqual(4);
    for (const index of [1, 3, 4, 5, 6]) expect(data.getItemGraphicEl(index)).toBeUndefined();
  }
  const tooltip = tooltipOption(main).formatter([{ seriesId: first.id, value: points[2] }]);
  expect(tooltip.textContent).toContain("-999 N·m");
  expect(tooltip.textContent).toContain("采样时间：2 s");
  fireEvent.click(screen.getByLabelText(`显示曲线 ${first.name}`));
  expect(mainSeries(main)[0].data).toEqual([]);
  expect(mainSeries(overview)).toEqual([]);
});

it.each([
  { name: "single finite sample", points: [[1, 1e-7]], symbolIndices: [0] },
  { name: "all missing", points: [[0, null], [1, null]], symbolIndices: [] },
  { name: "same-time missing separator", points: [[0, 999], [0, null], [0, -999]], symbolIndices: [0, 2] },
] as { name: string; points: PlotSeries["points"]; symbolIndices: number[] }[])("keeps $name accurate without joining gaps or filling missing data", ({ points, symbolIndices }) => {
  const view = render(<TimeSeriesChart {...props} series={[{ ...first, points }]} />);
  const instance = mainInstance(view.container);
  expect(mainSeries(instance)[0].data).toEqual(points);
  const data = renderedSymbols(instance);
  points.forEach((_, index) => expect(!!data.getItemGraphicEl(index)).toBe(symbolIndices.includes(index)));
});

it("identifies isolated samples after explicit gap separators are inserted, including distinct overview data", () => {
  const item: PlotSeries = { ...first, points: [[0, 999], [1, -999], [2, 3]], gaps: [{ start: 0, end: 1 }, { start: 1, end: 2 }] };
  const range = { start: 0, end: 2 };
  const view = render(<TimeSeriesChart {...props} domain={range} viewport={range} series={[{ ...first, points: [[0, 1], [2, 2]] }]} overviewSeries={[item]} />);
  expect(renderedSymbols(mainInstance(view.container)).getItemGraphicEl(0)).toBeUndefined();
  const overview = getInstanceByDom(view.container.querySelector<HTMLElement>(".chart-overview-canvas")!)!;
  expect(mainSeries(overview)[0].data).toEqual([[0, 999], [0.5, null], [1, -999], [1.5, null], [2, 3]]);
  const data = renderedSymbols(overview);
  for (const index of [0, 2, 4]) expect(data.getItemGraphicEl(index)?.getBoundingRect().width).toBeGreaterThanOrEqual(4);
  for (const index of [1, 3]) expect(data.getItemGraphicEl(index)).toBeUndefined();
});
