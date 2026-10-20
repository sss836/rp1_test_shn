import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { init, use as registerEcharts, type EChartsType } from "echarts/core";
import type { EChartsOption, TooltipComponentFormatterCallbackParams } from "echarts";
import { LineChart, type LineSeriesOption } from "echarts/charts";
import { AxisPointerComponent, BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { ChevronDown, Download, Move, RotateCcw, Scan, Undo2 } from "lucide-react";
import { chartPngDataUrl, joinTimeCursorGroup } from "./timeSeriesChartTools";
import "./timeSeriesChart.css";

registerEcharts([LineChart, AxisPointerComponent, BrushComponent, DataZoomComponent, GridComponent, MarkAreaComponent, MarkLineComponent, MarkPointComponent, TitleComponent, TooltipComponent, CanvasRenderer]);
export type CurveViewport = { start: number; end: number };
export type FixedTimeCursors = { a: number | null; b: number | null };
export type CurveStatistics = { count: number; missing: number; min: number | null; max: number | null; mean: number | null; rms: number | null };
export type PlotSeries = {
  id: string; name: string; legendName?: string; legendDetail?: string; unit: string; reference?: boolean; points: [number, number | null][];
  color?: string; dash?: "solid" | "dashed" | "dotted";
  gaps?: { start: number; end: number }[];
  anomalies?: { time: number; value: number; label: string }[];
};
export type ReferenceLine = { id: string; name: string; value: number; temporary?: boolean };
const COLORS = ["#237ce5", "#16a6a1", "#8970d0", "#c88218", "#b55c8b", "#d26a48", "#529456", "#697f9d"];
const STAT_COLUMNS = [{ key: "count", label: "有效数" }, { key: "missing", label: "缺失数" }, { key: "min", label: "Min" }, { key: "max", label: "Max" }, { key: "mean", label: "Mean" }, { key: "rms", label: "RMS" }] as const;
type LegendSort = { key: keyof CurveStatistics; direction: "ascending" | "descending" };
const EMPTY_SERIES: PlotSeries[] = [];
const EMPTY_CURSORS: FixedTimeCursors = { a: null, b: null };
const sameViewport = (a: CurveViewport, b: CurveViewport) => Math.abs(a.start - b.start) < 1e-6 && Math.abs(a.end - b.end) < 1e-6;
const numberText = (value: number) => value.toLocaleString("zh-CN", { maximumFractionDigits: 4 });

export function validViewport(start: number, end: number): start is number {
  return Number.isFinite(start) && Number.isFinite(end) && start < end;
}
export function clampViewport(value: CurveViewport, domain: CurveViewport): CurveViewport | null {
  const start = Math.max(domain.start, value.start), end = Math.min(domain.end, value.end);
  return validViewport(start, end) ? { start, end } : null;
}
function normalizedGaps(gaps: PlotSeries["gaps"] = []) {
  const result: { start: number; end: number }[] = [];
  for (const gap of gaps.filter((item) => validViewport(item.start, item.end)).map((item) => ({ ...item })).sort((a, b) => a.start - b.start)) {
    const previous = result[result.length - 1];
    if (previous && gap.start < previous.end) previous.end = Math.max(previous.end, gap.end);
    else result.push(gap);
  }
  return result;
}
// Each explicit gap gets its own null separator, even when several gaps fall in
// one downsample bucket. Preserve null samples as supplied; never infer gaps
// from long intervals, and never mutate the caller's arrays.
export function breakCurveAtGaps(points: PlotSeries["points"], gaps: PlotSeries["gaps"] = []): PlotSeries["points"] {
  const ordered = points.filter(([time]) => Number.isFinite(time)).map(([time, value]): [number, number | null] => [time, value !== null && Number.isFinite(value) ? value : null]).sort((a, b) => a[0] - b[0]);
  if (!ordered.length) return ordered;
  const intervals = normalizedGaps(gaps);
  let gapIndex = 0;
  const broken = ordered.map(([time, value]): [number, number | null] => {
    while (gapIndex < intervals.length && intervals[gapIndex].end <= time) gapIndex++;
    const gap = intervals[gapIndex];
    return [time, gap && time > gap.start && time < gap.end ? null : value];
  });
  const first = ordered[0][0], last = ordered[ordered.length - 1][0];
  for (const gap of intervals) {
    const start = Math.max(first, gap.start), end = Math.min(last, gap.end);
    if (start < end) broken.push([start + (end - start) / 2, null]);
  }
  return broken.sort((a, b) => a[0] - b[0]);
}

// One-point segments have no stroke when symbols are disabled. Mark only those
// retained finite samples; connected samples and null separators stay unmarked.
function isolatedSymbols(points: PlotSeries["points"], size: number): Pick<LineSeriesOption, "showSymbol" | "showAllSymbol" | "symbol" | "symbolSize"> {
  const isolated = new Set(points.flatMap((point, index) => point[1] !== null && points[index - 1]?.[1] == null && points[index + 1]?.[1] == null ? [index] : []));
  return { showSymbol: isolated.size > 0, showAllSymbol: true, symbolSize: size, symbol: (_value, params) => isolated.has(params.dataIndex) ? "circle" : "none" };
}

type Props = {
  series: PlotSeries[]; domain: CurveViewport; viewport: CurveViewport; references: ReferenceLine[];
  onViewport: (next: CurveViewport) => void; loading: boolean; resolution: string; xLabel?: string; absolute?: boolean;
  title?: string; referenceControls?: ReactNode; layout?: "overlay" | "facets";
  overviewSeries?: PlotSeries[]; overviewLoading?: boolean;
  cursorGroup?: string; active?: boolean; compact?: boolean; height?: number; showOverview?: boolean; showToolbar?: boolean;
  statistics?: Record<string, CurveStatistics>; statisticsLoading?: boolean;
  fixedCursors?: FixedTimeCursors; onFixedCursorsChange?: (next: FixedTimeCursors) => void; showCursorControls?: boolean;
};
type ZoomItem = { dataZoomId?: string; dataZoomIndex?: number; start?: number; end?: number; startValue?: number; endValue?: number };

export function TimeSeriesChart({ series, domain, viewport, references, onViewport, loading, resolution, xLabel, absolute = false, title = "曲线对比", referenceControls, layout = "overlay", overviewSeries, overviewLoading = false, cursorGroup, active = true, compact = false, height, showOverview = true, showToolbar = true, statistics, statisticsLoading = false, fixedCursors, onFixedCursorsChange, showCursorControls = true }: Props) {
  const host = useRef<HTMLDivElement>(null), overviewHost = useRef<HTMLDivElement>(null);
  const chart = useRef<EChartsType | null>(null), overviewChart = useRef<EChartsType | null>(null);
  const callback = useRef(onViewport), bounds = useRef(domain), currentViewport = useRef(viewport);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null), pending = useRef<CurveViewport | null>(null);
  const activeRef = useRef(active);
  const yZoom = useRef({ start: 0, end: 100 });
  const rangeControls = useRef<HTMLDivElement>(null);
  const [legendSort, setLegendSort] = useState<LegendSort | null>(null);
  const [hidden, setHidden] = useState<string[]>([]);
  const [styles, setStyles] = useState<Record<string, { color?: string; dash?: PlotSeries["dash"] }>>({});
  const [localCursors, setLocalCursors] = useState<FixedTimeCursors>(EMPTY_CURSORS);
  const cursors = fixedCursors ?? localCursors;
  const [cursorA, setCursorA] = useState(cursors.a === null ? "" : String(cursors.a)), [cursorB, setCursorB] = useState(cursors.b === null ? "" : String(cursors.b));
  const [cursorError, setCursorError] = useState(""), [exportError, setExportError] = useState(""), [exporting, setExporting] = useState(false);
  const history = useRef<CurveViewport[]>([]), [historySize, setHistorySize] = useState(0);
  const [mode, setMode] = useState<"pan" | "box">("pan"), [linked, setLinked] = useState(true);
  const [rangeOpen, setRangeOpen] = useState<"x" | "y" | null>(null);
  const [xStart, setXStart] = useState(String(viewport.start)), [xEnd, setXEnd] = useState(String(viewport.end));
  const [yMin, setYMin] = useState(""), [yMax, setYMax] = useState("");
  const [yRange, setYRange] = useState<{ min?: number; max?: number }>({});
  const [rangeError, setRangeError] = useState("");
  const unitMismatch = new Set(series.map((item) => item.unit)).size > 1;
  const visibleSeries = useMemo(() => unitMismatch ? EMPTY_SERIES : series.filter((item) => !hidden.includes(item.id)), [series, hidden, unitMismatch]);
  const visibleOverview = useMemo(() => unitMismatch ? EMPTY_SERIES : (overviewSeries ?? series).filter((item) => !hidden.includes(item.id) && (!series.length || item.unit === series[0].unit)), [series, overviewSeries, hidden, unitMismatch]);
  // Sort only the legend presentation, using supplied raw-window statistics.
  // Null/non-finite values stay last in both directions; chart series never move.
  const legendSeries = useMemo(() => {
    if (!legendSort || statisticsLoading) return series;
    const value = (item: PlotSeries) => { const result = statistics?.[item.id]?.[legendSort.key]; return typeof result === "number" && Number.isFinite(result) ? result : null; };
    return [...series].sort((a, b) => {
      const left = value(a), right = value(b);
      if (left === null) return right === null ? 0 : 1;
      if (right === null) return -1;
      return (left - right) * (legendSort.direction === "ascending" ? 1 : -1);
    });
  }, [series, statistics, statisticsLoading, legendSort]);
  const rows = layout === "facets" ? Math.max(1, visibleSeries.length) : 1;
  const chartHeight = layout === "facets" ? Math.max(height ?? 330, rows * (compact ? 150 : 190) + 40) : Math.max(180, height ?? (compact ? 300 : 440));
  const browserTimeZone = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "本地时区", []);
  const timeAxisLabel = xLabel ?? (absolute ? `绝对时间（浏览器时区：${browserTimeZone}）` : "相对测试开始 / s");
  const colorFor = (item: PlotSeries) => styles[item.id]?.color ?? item.color ?? COLORS[Math.max(0, series.findIndex((entry) => entry.id === item.id)) % COLORS.length];
  const dashFor = (item: PlotSeries) => styles[item.id]?.dash ?? item.dash ?? (item.reference ? "dashed" : "solid");

  const cancelPending = useCallback(() => {
    if (debounce.current !== null) clearTimeout(debounce.current);
    debounce.current = null; pending.current = null;
  }, []);
  const commitViewport = useCallback((next: CurveViewport, remember = true) => {
    if (remember && !sameViewport(next, currentViewport.current)) { history.current.push({ ...currentViewport.current }); history.current = history.current.slice(-30); setHistorySize(history.current.length); }
    cancelPending(); currentViewport.current = next;
    callback.current(next);
  }, [cancelPending]);
  useEffect(() => { callback.current = onViewport; }, [onViewport]);
  useEffect(() => {
    activeRef.current = active;
    if (!active) {
      cancelPending();
      chart.current?.dispatchAction({ type: "updateAxisPointer", currTrigger: "leave" });
      chart.current?.dispatchAction({ type: "hideTip" });
    }
  }, [active, cancelPending]);
  useEffect(() => {
    cancelPending();
    if (!sameViewport(bounds.current, { start: domain.start, end: domain.end })) { history.current = []; setHistorySize(0); }
    bounds.current = { start: domain.start, end: domain.end }; currentViewport.current = { start: viewport.start, end: viewport.end };
    setXStart(String(viewport.start)); setXEnd(String(viewport.end));
  }, [domain.start, domain.end, viewport.start, viewport.end, cancelPending]);
  useEffect(() => { setCursorA(cursors.a === null ? "" : String(cursors.a)); setCursorB(cursors.b === null ? "" : String(cursors.b)); }, [cursors.a, cursors.b]);
  useEffect(() => {
    if (!rangeOpen) return;
    const close = (event: PointerEvent) => { if (!rangeControls.current?.contains(event.target as Node)) setRangeOpen(null); };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [rangeOpen]);
  useEffect(() => {
    if (!host.current) return;
    const instance = init(host.current, undefined, { renderer: "canvas" });
    const overview = showOverview && overviewHost.current ? init(overviewHost.current, undefined, { renderer: "canvas" }) : null;
    chart.current = instance; overviewChart.current = overview;
    const resize = new ResizeObserver(() => { if (activeRef.current && host.current?.clientWidth) { instance.resize(); overview?.resize(); } });
    resize.observe(host.current); if (overviewHost.current) resize.observe(overviewHost.current);
    function queueViewport(value: CurveViewport) {
      if (!activeRef.current) return;
      const next = clampViewport(value, bounds.current);
      if (!next) return;
      if (sameViewport(next, currentViewport.current)) { cancelPending(); return; }
      if (pending.current && sameViewport(next, pending.current)) return;
      cancelPending(); pending.current = next;
      debounce.current = setTimeout(() => {
        debounce.current = null; pending.current = null;
        if (sameViewport(next, currentViewport.current)) return;
        commitViewport(next);
      }, 220);
    }
    const zoomHandler = (owner: EChartsType) => (event: unknown) => {
      const payload = event as ZoomItem & { batch?: ZoomItem[] };
      for (const item of payload.batch ?? [payload]) {
        const options = owner.getOption().dataZoom as { id?: string }[] | undefined;
        const id = item.dataZoomId ?? (item.dataZoomIndex !== undefined ? options?.[item.dataZoomIndex]?.id : undefined);
        if (id?.startsWith("y-")) {
          if (item.start !== undefined) yZoom.current.start = item.start;
          if (item.end !== undefined) yZoom.current.end = item.end;
          continue;
        }
        if (item.start === undefined && item.startValue === undefined) continue;
        const full = bounds.current;
        queueViewport({ start: item.startValue ?? full.start + (item.start ?? 0) / 100 * (full.end - full.start), end: item.endValue ?? full.start + (item.end ?? 100) / 100 * (full.end - full.start) });
        break;
      }
    };
    instance.on("datazoom", zoomHandler(instance)); if (overview) overview.on("datazoom", zoomHandler(overview));
    instance.on("brushEnd", (event: unknown) => {
      const area = (event as { areas?: { coordRange?: number[] }[] }).areas?.[0];
      if (area?.coordRange?.length === 2) queueViewport({ start: area.coordRange[0], end: area.coordRange[1] });
      instance.dispatchAction({ type: "brush", areas: [] });
    });
    return () => { cancelPending(); resize.disconnect(); instance.dispose(); overview?.dispose(); chart.current = null; overviewChart.current = null; };
  }, [showOverview, cancelPending, commitViewport]);

  useEffect(() => {
    const instance = chart.current;
    if (!instance || !cursorGroup || !linked || !active) return;
    let receiving = false;
    const group = joinTimeCursorGroup(cursorGroup, (time) => {
      if (instance.isDisposed() || !activeRef.current) return;
      receiving = true;
      try {
        if (time === null || time < currentViewport.current.start || time > currentViewport.current.end) {
          instance.dispatchAction({ type: "updateAxisPointer", currTrigger: "leave" });
          instance.dispatchAction({ type: "hideTip" });
        } else {
          // Transfer the time value, never another chart's sampled data index.
          const x = instance.convertToPixel({ xAxisIndex: 0 }, time);
          if (typeof x !== "number" || !Number.isFinite(x)) return;
          instance.dispatchAction({ type: "updateAxisPointer", x, y: 50, axesInfo: [{ axisDim: "x", axisIndex: 0, value: time }] });
        }
      } finally { receiving = false; }
    });
    const share = (event: unknown) => {
      if (receiving || !activeRef.current) return;
      const value = (event as { axesInfo?: { axisDim?: string; value?: number }[] }).axesInfo?.find((axis) => axis.axisDim === "x")?.value;
      group.publish(typeof value === "number" && Number.isFinite(value) ? value : null);
    };
    instance.on("updateAxisPointer", share);
    return () => { instance.off("updateAxisPointer", share); group.leave(); };
  }, [cursorGroup, linked, showOverview, active]);

  useEffect(() => {
    if (!chart.current || !active || loading && !series.length) return;
    const axes = Array.from({ length: rows }, (_, index) => index);
    const rowHeight = (chartHeight - 40) / rows;
    const refs = references.filter((item) => Number.isFinite(item.value));
    const colorFor = (item: PlotSeries) => styles[item.id]?.color ?? item.color ?? COLORS[Math.max(0, series.findIndex((entry) => entry.id === item.id)) % COLORS.length];
    const pointTooltip = (params: TooltipComponentFormatterCallbackParams) => {
      // ECharts accepts DOM nodes. Never interpolate identities into HTML or
      // rich-text tokens: textContent keeps even markup-looking labels literal.
      const content = document.createElement("div");
      content.className = "chart-point-tooltip";
      content.tabIndex = 0;
      content.setAttribute("role", "region");
      content.setAttribute("aria-label", "采样读数，可滚动查看全部曲线");
      content.style.width = `${Math.max(80, Math.min(320, (chart.current?.getWidth() ?? 344) - 24))}px`;
      content.style.maxHeight = `${Math.max(48, (chart.current?.getHeight() ?? chartHeight) - 24)}px`;
      content.addEventListener("wheel", (event) => event.stopPropagation());
      content.addEventListener("pointerdown", (event) => event.stopPropagation());
      for (const point of Array.isArray(params) ? params : [params]) {
        const item = visibleSeries.find((entry) => entry.id === point.seriesId);
        if (!item) continue;
        const row = document.createElement("div"); row.className = "chart-point-tooltip-row";
        const label = document.createElement("strong"); label.title = item.name;
        const name = Array.from(item.legendName ?? item.name);
        label.textContent = `${series.indexOf(item) + 1}. ${name.slice(0, 24).join("")}${name.length > 24 ? "…" : ""}`;
        label.style.borderLeftColor = colorFor(item);
        const detail = document.createElement("div"); detail.className = "chart-point-tooltip-detail";
        detail.textContent = item.legendDetail ?? (item.reference ? "参考曲线" : "采集曲线");
        const values = Array.isArray(point.value) ? point.value : [];
        const value = values[1], time = values[0];
        const reading = document.createElement("div"); reading.className = "chart-point-tooltip-value";
        reading.textContent = `${typeof value === "number" && Number.isFinite(value) ? Object.is(value, -0) ? "-0" : String(value) : "缺失"} ${item.unit || "无单位"}`;
        const sample = document.createElement("div"); sample.className = "chart-point-tooltip-detail";
        const instant = typeof time === "number" && Number.isFinite(time) ? time : null;
        sample.textContent = instant === null ? "采样时间：缺失" : absolute
          ? `采样时间：${String(instant)} ms${Number.isFinite(new Date(instant).getTime()) ? ` · ${new Date(instant).toISOString()}` : ""}`
          : `采样时间：${String(instant)} s`;
        row.append(label, detail, reading, sample); content.append(row);
      }
      return content;
    };
    const fixedLines = ([['A', cursors.a], ['B', cursors.b]] as const).filter((entry): entry is readonly ["A" | "B", number] => entry[1] !== null && Number.isFinite(entry[1]) && entry[1] >= domain.start && entry[1] <= domain.end).map(([name, time]) => ({ name: `${name} · ${numberText(time)}`, xAxis: time, lineStyle: { color: name === "A" ? "#237ce5" : "#a16b18", type: "solid" as const, width: 1.2 }, label: { position: name === "A" ? "insideStartTop" as const : "insideEndTop" as const } }));
    const option: EChartsOption = {
      animation: false, useUTC: false, backgroundColor: "#fff", textStyle: { color: "#596b82", fontFamily: "Inter, system-ui, sans-serif", fontSize: 12 },
      title: layout === "facets" ? visibleSeries.map((item, index) => ({ text: item.name, left: 58, top: index * rowHeight + 6, textStyle: { color: colorFor(item), fontSize: 12, fontWeight: 600 } })) : [],
      grid: axes.map((index) => ({ left: 58, right: 48, top: layout === "facets" ? index * rowHeight + 34 : 24, ...(layout === "facets" ? { height: rowHeight - 64 } : { bottom: 46 }) })),
      tooltip: { trigger: "axis", confine: true, renderMode: "html", enterable: true, padding: 0, hideDelay: 150, transitionDuration: 0, axisPointer: { type: linked ? "cross" : "line", snap: false }, formatter: pointTooltip },
      axisPointer: { link: linked ? [{ xAxisIndex: axes }] : [] },
      brush: { xAxisIndex: axes, brushType: "lineX", brushMode: "single", transformable: false, removeOnClick: true, toolbox: [], inBrush: { colorAlpha: 1 }, outOfBrush: { colorAlpha: 1 }, brushStyle: { color: "rgba(35,124,229,.1)", borderColor: "#237ce5", borderWidth: 1 } },
      xAxis: axes.map((index) => ({ type: absolute ? "time" : "value", axisPointer: { snap: false }, gridIndex: index, min: domain.start, max: domain.end, name: index === rows - 1 ? timeAxisLabel : undefined, nameLocation: "middle", nameGap: 29, axisLine: { lineStyle: { color: "#d6e0eb" } }, axisTick: { show: false }, axisLabel: { color: "#596d86", fontSize: 12, show: index === rows - 1, formatter: absolute ? undefined : numberText }, splitLine: { lineStyle: { color: "#edf1f6", width: 1 } } })),
      yAxis: axes.map((index) => ({ type: "value", gridIndex: index, name: series[0]?.unit || "数值", scale: true,
        min: yRange.min ?? (refs.length ? (extent: { min: number }) => Math.min(Number.isFinite(extent.min) ? extent.min : refs[0].value, ...refs.map((item) => item.value)) : undefined),
        max: yRange.max ?? (refs.length ? (extent: { max: number }) => Math.max(Number.isFinite(extent.max) ? extent.max : refs[0].value, ...refs.map((item) => item.value)) : undefined),
        nameTextStyle: { color: "#596d86", align: "right", fontSize: 12 }, axisLabel: { color: "#596d86", fontSize: 12 }, axisTick: { show: false }, axisLine: { show: false }, splitLine: { lineStyle: { color: "#e8edf4", width: 1 } } })),
      dataZoom: [
        { id: "x-inside", type: "inside", xAxisIndex: axes, filterMode: "none", startValue: viewport.start, endValue: viewport.end, zoomOnMouseWheel: "ctrl", moveOnMouseMove: mode === "pan", moveOnMouseWheel: false },
        { id: "x-slider", type: "slider", show: false, xAxisIndex: axes, filterMode: "none", startValue: viewport.start, endValue: viewport.end, showDataShadow: false },
        { id: "y-inside", type: "inside", yAxisIndex: axes, filterMode: "none", start: yZoom.current.start, end: yZoom.current.end, zoomOnMouseWheel: "shift", moveOnMouseMove: false, moveOnMouseWheel: false },
        { id: "y-slider", type: "slider", yAxisIndex: axes, filterMode: "none", start: yZoom.current.start, end: yZoom.current.end, right: 9, width: 12, top: 30, bottom: 48, showDataShadow: false, borderColor: "#dce5ef", backgroundColor: "#f6f8fb", fillerColor: "rgba(35,124,229,.08)", handleStyle: { color: "#fff", borderColor: "#a9bed5" }, brushSelect: false }
      ],
      series: series.filter(() => !unitMismatch).map((item): LineSeriesOption => {
        const visibleIndex = visibleSeries.findIndex((entry) => entry.id === item.id), isHidden = visibleIndex < 0;
        const index = layout === "facets" ? Math.max(0, visibleIndex) : 0;
        const gaps = normalizedGaps(item.gaps), color = colorFor(item);
        const data = isHidden ? [] : breakCurveAtGaps(item.points, gaps);
        return {
          id: item.id, name: item.name, type: "line", xAxisIndex: index, yAxisIndex: index, ...isolatedSymbols(data, 6), connectNulls: false, smooth: false,
          data, lineStyle: { width: item.reference ? 1.5 : 1.8, type: styles[item.id]?.dash ?? item.dash ?? (item.reference ? "dashed" : "solid"), color }, itemStyle: { color }, emphasis: { focus: "series" },
          markLine: !isHidden && (layout === "facets" || visibleIndex === 0) ? { silent: true, symbol: "none", label: { position: "insideEndTop", formatter: "{b}", fontSize: 11 }, lineStyle: { type: "dashed", color: "#cc7278", width: 1 }, data: [...refs.map((reference) => ({ name: reference.name, yAxis: reference.value, lineStyle: { color: reference.temporary ? "#b18b43" : "#cc7278" } })), ...fixedLines] } : undefined,
          markArea: !isHidden && gaps.length ? { silent: true, itemStyle: { color: "rgba(150,164,183,.075)" }, label: { show: true, position: "insideTop", color: "#65758b", fontSize: 11 }, data: gaps.map((gap) => [{ name: "缺失段", xAxis: gap.start }, { xAxis: gap.end }]) } : undefined,
          markPoint: !isHidden && item.anomalies?.length ? { symbol: "triangle", symbolRotate: 180, symbolSize: 9, itemStyle: { color: "#d95c67" }, label: { show: true, formatter: "{b}", position: "top", distance: 8, color: "#b54b58", fontSize: 11, backgroundColor: "rgba(255,255,255,.9)", padding: [2, 3], borderRadius: 3 }, tooltip: { trigger: "item", formatter: "{b}: {c}" }, data: item.anomalies.filter((point) => Number.isFinite(point.time) && Number.isFinite(point.value) && point.time >= viewport.start && point.time <= viewport.end && !gaps.some((gap) => point.time > gap.start && point.time < gap.end)).map((point) => ({ name: point.label, coord: [point.time, point.value], value: point.value })) } : undefined
        };
      })
    };
    // Clear the cached hovered series indices before async responses replace
    // series. The next pointer event recomputes the tooltip from the new model.
    chart.current.dispatchAction({ type: "hideTip" });
    chart.current.setOption(option, { replaceMerge: ["series", "grid", "xAxis", "yAxis", "dataZoom", "title"], lazyUpdate: false });
    chart.current.dispatchAction({ type: "takeGlobalCursor", key: "brush", brushOption: { brushType: mode === "box" ? "lineX" : false, brushMode: "single" } });
    chart.current.resize();
  }, [series, visibleSeries, domain, viewport, references, yRange, linked, mode, layout, rows, chartHeight, timeAxisLabel, absolute, loading, unitMismatch, styles, cursors.a, cursors.b, showOverview, active]);

  useEffect(() => {
    if (!overviewChart.current || !active) return;
    // The slider controls a separate, empty axis. Thus the overview keeps the
    // complete domain while its handles select the main chart's time window.
    const option: EChartsOption = {
      animation: false, useUTC: false, grid: { left: 58, right: 48, top: 8, bottom: 25 },
      xAxis: [{ type: absolute ? "time" : "value", min: domain.start, max: domain.end, axisTick: { show: false }, axisLine: { lineStyle: { color: "#dce5ef" } }, axisLabel: { fontSize: 11, color: "#66788f", formatter: absolute ? undefined : numberText }, splitLine: { show: false } }, { type: "value", min: domain.start, max: domain.end, show: false }],
      yAxis: { type: "value", scale: true, show: false },
      dataZoom: [{ id: "overview-slider", type: "slider", xAxisIndex: 1, filterMode: "none", startValue: viewport.start, endValue: viewport.end, left: 58, right: 48, top: 7, height: 44, showDataShadow: false, showDetail: false, brushSelect: true, borderColor: "#dce5ef", backgroundColor: "transparent", fillerColor: "rgba(35,124,229,.07)", handleStyle: { color: "#fff", borderColor: "#91accb" }, moveHandleSize: 0 }],
      series: visibleOverview.map((item): LineSeriesOption => {
        const data = breakCurveAtGaps(item.points, item.gaps);
        const color = styles[item.id]?.color ?? item.color ?? COLORS[Math.max(0, series.findIndex((entry) => entry.id === item.id)) % COLORS.length];
        return { id: item.id, name: item.name, type: "line", xAxisIndex: 0, yAxisIndex: 0, silent: true, ...isolatedSymbols(data, 4), connectNulls: false, smooth: false, data, itemStyle: { color }, lineStyle: { width: 1, opacity: .62, color, type: styles[item.id]?.dash ?? item.dash ?? (item.reference ? "dashed" : "solid") } };
      })
    };
    overviewChart.current.setOption(option, { replaceMerge: ["series"], lazyUpdate: false });
  }, [series, visibleOverview, domain, viewport, absolute, styles, showOverview, active]);

  function applyRange() {
    const next = clampViewport({ start: Number(xStart), end: Number(xEnd) }, domain);
    if (!xStart.trim() || !xEnd.trim() || !next) { setRangeError("请输入起点小于终点且与采集范围相交的有效时间范围。"); return; }
    if (yMin && !Number.isFinite(Number(yMin)) || yMax && !Number.isFinite(Number(yMax)) || yMin && yMax && Number(yMin) >= Number(yMax)) { setRangeError("纵轴下限必须小于上限。"); return; }
    yZoom.current = { start: 0, end: 100 };
    setRangeError(""); setYRange({ min: yMin ? Number(yMin) : undefined, max: yMax ? Number(yMax) : undefined });
    setRangeOpen(null); commitViewport(next);
  }
  function reset() {
    cancelPending(); yZoom.current = { start: 0, end: 100 };
    setYMin(""); setYMax(""); setYRange({}); setRangeError(""); setRangeOpen(null);
    setXStart(String(domain.start)); setXEnd(String(domain.end));
    chart.current?.dispatchAction({ type: "brush", areas: [] });
    chart.current?.dispatchAction({ type: "dataZoom", dataZoomId: "y-slider", start: 0, end: 100 });
    commitViewport({ ...domain });
  }
  function updateCursors(next: FixedTimeCursors) { setLocalCursors(next); onFixedCursorsChange?.(next); }
  function applyCursors() {
    const a = cursorA.trim() ? Number(cursorA) : null, b = cursorB.trim() ? Number(cursorB) : null;
    if ([a, b].some((value) => value !== null && (!Number.isFinite(value) || value < domain.start || value > domain.end))) { setCursorError("游标时间必须在采集范围内，留空可取消游标。"); return; }
    setCursorError(""); updateCursors({ a, b });
  }
  function goBack() {
    const previous = history.current.pop(); setHistorySize(history.current.length);
    if (previous) { const next = clampViewport(previous, domain); if (next) commitViewport(next, false); }
  }
  const cursorSummary = cursors.a !== null && cursors.b !== null ? `A ${numberText(cursors.a)} · B ${numberText(cursors.b)} · Δt ${numberText((cursors.b - cursors.a) / (absolute ? 1000 : 1))} s` : "";
  async function exportPng() {
    const instance = chart.current;
    if (!instance) return;
    setExporting(true); setExportError("");
    try {
      instance.dispatchAction({ type: "hideTip" });
      const range = absolute
        ? `${xLabel ? `${xLabel} · ` : ""}绝对时间范围（UTC）：${new Date(viewport.start).toISOString()} – ${new Date(viewport.end).toISOString()} · 图中时区：${browserTimeZone}`
        : `${timeAxisLabel}：${viewport.start} – ${viewport.end}`;
      const exportCursors = absolute && cursors.a !== null && cursors.b !== null
        ? `固定游标（UTC）：A ${new Date(cursors.a).toISOString()} · B ${new Date(cursors.b).toISOString()} · Δt ${numberText((cursors.b - cursors.a) / 1000)} s`
        : cursorSummary;
      const dataUrl = await chartPngDataUrl(instance, { title, range: `${range} · 单位：${series[0]?.unit || "无单位"}`, resolution, cursors: exportCursors, series: visibleSeries.map((item) => ({ name: item.name, unit: item.unit, color: colorFor(item), dash: dashFor(item) })) });
      const anchor = document.createElement("a"); anchor.href = dataUrl; anchor.download = `${title.replace(/[\\/:*?"<>|]/g, "_")}-${viewport.start}-${viewport.end}.png`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
    } catch (error) { setExportError(error instanceof Error ? error.message : "PNG 导出失败，请重试。"); }
    finally { setExporting(false); }
  }
  function statisticsText(value: number | null | undefined) {
    if (value === undefined || value === null || !Number.isFinite(value)) return "—";
    return value !== 0 && (Math.abs(value) < 0.0001 || Math.abs(value) >= 1e7) ? value.toExponential(4) : numberText(value);
  }
  return <section className={`time-series-workspace c-v3-chart c-v3-chart--${layout}${compact ? " c-v3-chart--compact" : ""}`} aria-label="可交互曲线">
    <header className="chart-heading"><h3>{title}</h3><div className="chart-heading-actions"><span className="chart-resolution">{resolution}</span><button type="button" className="chart-export" onClick={() => void exportPng()} disabled={exporting || loading || !visibleSeries.length || unitMismatch}><Download aria-hidden="true" />{exporting ? "正在导出…" : "PNG"}</button></div></header>
    {exportError && <p role="alert" className="chart-inline-error">{exportError}</p>}
    {showToolbar && <div className="chart-toolbar">
      <div className="chart-range-controls" ref={rangeControls} onKeyDown={(event) => { if (event.key === "Escape") { const kind = rangeOpen; setRangeOpen(null); rangeControls.current?.querySelector<HTMLButtonElement>(`button[data-range="${kind}"]`)?.focus(); } }}>
        <button type="button" data-range="x" aria-label="X 范围" title="X 时间范围" aria-expanded={rangeOpen === "x"} onClick={() => setRangeOpen(rangeOpen === "x" ? null : "x")}><b>{compact ? "X" : "X 范围"}</b><span className="chart-range-value">{numberText(viewport.start)} – {numberText(viewport.end)}</span><ChevronDown aria-hidden="true" /></button>
        <button type="button" data-range="y" aria-label="Y 范围" title="Y 纵轴范围" aria-expanded={rangeOpen === "y"} onClick={() => setRangeOpen(rangeOpen === "y" ? null : "y")}><b>{compact ? "Y" : "Y 范围"}</b><span className="chart-range-value">{yRange.min === undefined && yRange.max === undefined ? "自动" : `${yRange.min ?? "自动"} – ${yRange.max ?? "自动"}`}</span><ChevronDown aria-hidden="true" /></button>
        {rangeOpen && <form className="chart-range-popover" aria-label="坐标范围" onSubmit={(event) => { event.preventDefault(); applyRange(); }}>
          <label>时间起点<input autoFocus={rangeOpen === "x"} type="number" step="any" value={xStart} onChange={(event) => setXStart(event.target.value)} /></label>
          <label>时间终点<input type="number" step="any" value={xEnd} onChange={(event) => setXEnd(event.target.value)} /></label>
          <label>纵轴下限<input autoFocus={rangeOpen === "y"} type="number" step="any" placeholder="自动" value={yMin} onChange={(event) => setYMin(event.target.value)} /></label>
          <label>纵轴上限<input type="number" step="any" placeholder="自动" value={yMax} onChange={(event) => setYMax(event.target.value)} /></label>
          {rangeError && <p role="alert">{rangeError}</p>}<button type="submit">应用范围</button>
        </form>}
        <button type="button" className="chart-reset" aria-label="重置视图" title="重置时间和纵轴范围" onClick={reset}><RotateCcw aria-hidden="true" />{compact ? "重置" : "重置视图"}</button>
        <button type="button" aria-label="返回上个范围" title="返回此面板的上个时间范围" onClick={goBack} disabled={!historySize}><Undo2 aria-hidden="true" />{compact ? "上一步" : "返回上个范围"}</button>
      </div>
      <div className="chart-interaction-controls"><button type="button" aria-pressed={mode === "box"} onClick={() => setMode("box")}><Scan aria-hidden="true" />框选缩放</button><button type="button" aria-pressed={mode === "pan"} onClick={() => setMode("pan")}><Move aria-hidden="true" />平移</button><label><input type="checkbox" checked={linked} onChange={(event) => setLinked(event.target.checked)} />联动游标</label></div>
    </div>}
    {showCursorControls && <form className="chart-cursor-controls" aria-label="固定时间游标" onSubmit={(event) => { event.preventDefault(); applyCursors(); }}>
      <label>A 时间（{absolute ? "ms" : "s"}）<input type="number" step="any" value={cursorA} onChange={(event) => setCursorA(event.target.value)} placeholder="未固定" /></label>
      <label>B 时间（{absolute ? "ms" : "s"}）<input type="number" step="any" value={cursorB} onChange={(event) => setCursorB(event.target.value)} placeholder="未固定" /></label>
      <button type="submit">固定游标</button><button type="button" onClick={() => { updateCursors(EMPTY_CURSORS); setCursorError(""); }}>清除游标</button>
      <output aria-label="游标时间差">{cursorSummary || "Δt —"}</output><span className="chart-cursor-note">时间差；不计算插值 Δy</span>
      {cursorError && <p role="alert">{cursorError}</p>}
    </form>}
    {referenceControls && <div className="chart-reference-controls">{referenceControls}</div>}
    {unitMismatch && <p role="alert" className="curve-error">单位不一致，无法比较。请选择同参数、同单位的曲线。</p>}
    <div className="curve-canvas-wrap"><div ref={host} className="time-series-canvas" style={{ height: chartHeight }} role="img" aria-label={`${visibleSeries.length} 条${series[0]?.unit ?? ""}曲线，可通过下方图例控制显示，通过时间与纵轴范围控制缩放`} />{loading && <span className="curve-refresh" role="status">正在读取当前视窗…</span>}</div>
    <div className="chart-legend-heading"><span>{statisticsLoading ? "正在读取原始区间统计…" : statistics ? "当前区间 · 原始采样精确统计" : "曲线图例 · 区间统计未提供"}</span><button type="button" disabled={!hidden.length} onClick={() => setHidden([])}>恢复全部曲线</button></div>
    <div className="chart-legend-table-wrap" tabIndex={0} role="region" aria-label="曲线图例">
      <table className="chart-legend-table"><thead><tr><th scope="col">曲线 / 单位</th>{STAT_COLUMNS.map(({ key, label }) => <th key={key} scope="col" aria-sort={legendSort?.key === key ? legendSort.direction : undefined}><button className="chart-stat-sort" type="button" aria-label={`按 ${label} 排序`} title={`按原始区间 ${label} 数值排序，空值始终在末尾`} onClick={() => setLegendSort((current) => ({ key, direction: current?.key === key && current.direction === "ascending" ? "descending" : "ascending" }))}>{label}<span aria-hidden="true">{legendSort?.key === key ? legendSort.direction === "ascending" ? " ↑" : " ↓" : " ↕"}</span></button></th>)}<th scope="col">显示</th></tr></thead>
        <tbody>{legendSeries.map((item) => { const stat = statisticsLoading ? undefined : statistics?.[item.id]; return <tr key={item.id} className={hidden.includes(item.id) ? "chart-series-hidden" : ""}>
          <th scope="row"><label><input type="checkbox" aria-label={`显示曲线 ${item.name}`} checked={!hidden.includes(item.id)} onChange={() => setHidden((current) => current.includes(item.id) ? current.filter((id) => id !== item.id) : [...current, item.id])} /><i aria-hidden="true" style={{ borderColor: colorFor(item), borderTopStyle: dashFor(item) }} /><span className="chart-series-identity" title={item.name}><span className="chart-series-name">{item.legendName ?? item.name}</span><small className="chart-series-detail">{item.legendDetail ?? `${item.reference ? "参考 · " : ""}${item.unit || "无单位"}`}</small></span></label></th>
          {(["count", "missing", "min", "max", "mean", "rms"] as const).map((key) => <td key={key} title={stat?.[key] == null ? "无统计值" : String(stat[key])}>{statisticsText(stat?.[key])}</td>)}
          <td><div className="chart-series-actions"><button type="button" aria-label={`单独显示 ${item.name}`} onClick={() => setHidden(series.filter((entry) => entry.id !== item.id).map((entry) => entry.id))}>单独</button><details><summary aria-label={`样式 ${item.name}`}>样式</summary><div className="chart-series-style"><label>颜色<input type="color" aria-label={`颜色 ${item.name}`} value={/^#[0-9a-f]{6}$/i.test(colorFor(item)) ? colorFor(item) : COLORS[0]} onChange={(event) => setStyles((current) => ({ ...current, [item.id]: { ...current[item.id], color: event.target.value } }))} /></label><label>线型<select aria-label={`线型 ${item.name}`} value={dashFor(item)} onChange={(event) => setStyles((current) => ({ ...current, [item.id]: { ...current[item.id], dash: event.target.value as PlotSeries["dash"] } }))}><option value="solid">实线</option><option value="dashed">虚线</option><option value="dotted">点线</option></select></label></div></details></div></td>
        </tr>; })}</tbody>
      </table>
    </div>
    {showOverview && <div className="chart-overview"><div className="chart-overview-label"><span>{overviewSeries ? "全程概览" : "概览 · 已加载数据"}</span>{overviewLoading && <span role="status">概览读取中…</span>}<span>拖动两端手柄调整时间范围</span></div><div ref={overviewHost} className="chart-overview-canvas" role="img" aria-label="时间范围概览" /></div>}
    {!compact && <footer className="curve-chart-footer"><span>Ctrl＋滚轮横向缩放 · Shift＋滚轮纵向缩放 · 右侧滑块调整纵轴</span><span>缺失段保持断线</span></footer>}
  </section>;
}
