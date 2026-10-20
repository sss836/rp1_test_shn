import { useEffect, useRef, useState } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { Download, Maximize2, Minimize2, Table2, X } from "lucide-react";
import { archivesApi, type Archive, type ArchiveCurves } from "../../archiveApi";
import { TimeSeriesChart, type CurveViewport, type PlotSeries, type ReferenceLine } from "../../components/TimeSeriesChart";
import { ErrorState } from "../../components/Workbench";
import { workspaceApi, downloadBlob, type RawFilters, type StatisticsResponse } from "./api";
import { archiveRange, archiveStart, metricIdentity, metricNames, readableName, shortCode, type PanelConfig, type WorkbenchConfig } from "./config";

const TARGET = /^(cmd_pos_rad|target_pos_rad|target_position_rad|pos_ref_rad|target_rad)$/;
const POSITION = /^(pos_rad|position_rad|actual_position_rad)$/;
export function panelColumns(record: Archive, panel: PanelConfig) {
  return (record.manifest?.catalog ?? []).filter((column) => column.unit === panel.unit && panel.subjects.includes(column.subject) && (column.metric === panel.metric || panel.targets && POSITION.test(panel.metric) && TARGET.test(column.metric))).slice(0, 32);
}
export function panelFilters(record: Archive, panel: PanelConfig, viewport: CurveViewport, config: WorkbenchConfig, absolute: boolean): RawFilters {
  const offset = absolute ? archiveStart(record) ?? 0 : 0;
  const scale = absolute ? 1000 : 1;
  const full = archiveRange(record);
  return { metrics: panelColumns(record, panel).map((column) => column.key).join(","), start: Math.max(full.start, (viewport.start - offset) / scale), end: Math.min(full.end, (viewport.end - offset) / scale), ...(config.stage ? { stage: config.stage } : {}), ...(config.cycle ? { cycle: config.cycle } : {}) };
}
function limits(record: Archive, metric: string): ReferenceLine[] {
  const value = metric === "torque_nm" ? record.acceptance.max_torque_abs_nm : undefined;
  return typeof value === "number" && Number.isFinite(value) ? [{ id: "case-upper", name: "用例力矩上限", value }, { id: "case-lower", name: "用例力矩下限", value: -value }] : [];
}
const display = (value: unknown) => value === null || value === undefined ? "—" : typeof value === "number" ? String(value) : String(value);

function RawInspector({ record, panel, filters, scope, onClose }: { record: Archive; panel: PanelConfig; filters: RawFilters; scope: string; onClose: () => void }) {
  const [offset, setOffset] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const downloadRequest = useRef<AbortController | null>(null);
  useEffect(() => () => downloadRequest.current?.abort(), []);
  const query = useQuery({ queryKey: ["workspace", scope, "samples", record.id, filters, offset], queryFn: ({ signal }) => workspaceApi.samples(record.id, { ...filters, offset, limit: 100 }, signal), staleTime: 15_000 });
  async function download() {
    const controller = new AbortController(); downloadRequest.current = controller;
    setDownloading(true); setDownloadError("");
    try {
      const blob = await workspaceApi.exportCsv(record.id, filters, controller.signal);
      if (!controller.signal.aborted) downloadBlob(blob, `${record.execution_code || record.archive_code}-${panel.metric}-${filters.start}-${filters.end}s.csv`);
    } catch (error) { if (!controller.signal.aborted) setDownloadError(error instanceof Error ? error.message : "CSV 下载失败。"); }
    finally { if (!controller.signal.aborted) setDownloading(false); }
  }
  return <section className="wb-inspector" aria-label={`${metricNames[panel.metric] ?? panel.metric} 原始数据检查`}>
    <div className="wb-inspector-heading"><div><h3>原始数据 · 当前区间</h3><p>{readableName(record)} · {shortCode(record)} · {filters.start}–{filters.end} s</p></div><button type="button" className="curve-row-action" aria-label="关闭数据检查" onClick={onClose}><X /></button></div>
    <p className="wb-data-note">原始分辨率 · 空值显示为 —，CSV 保留空字段 · 每页 100 行 · 单次导出最多 250,000 行</p>
    <div className="wb-inspector-actions"><span>{query.data && !query.isError ? `区间 ${query.data.total_rows.toLocaleString("zh-CN")} 行 · 已显示 ${query.data.rows.length ? offset + 1 : 0}–${offset + query.data.rows.length}` : query.isError ? "原始数据不可用" : "正在读取原始行…"}</span><button className="curve-button" type="button" disabled={downloading || query.isFetching || !!query.error || !query.data?.total_rows} onClick={() => void download()}><Download />{downloading ? "正在下载…" : "下载区间原始 CSV"}</button></div>
    {downloadError && <p role="alert" className="curve-error">{downloadError}</p>}
    {query.error && <ErrorState error={query.error} retry={() => void query.refetch()} />}
    {query.isFetching ? <p role="status">正在读取当前区间原始采样…</p> : !query.isError && query.data && <>
      <div className="wb-table-scroll"><table><caption className="sr-only">当前执行与筛选的原始采样，时间单位秒</caption><thead><tr><th>时间 / s</th><th>原始行</th>{query.data.catalog.map((column) => <th key={column.key}>{column.subject || "全局"} · {metricNames[column.metric] ?? column.metric}<small>{column.unit || "无单位"}</small></th>)}</tr></thead><tbody>{query.data.rows.map((row, index) => <tr key={`${row._row}:${index}`}><td>{display(row.time_s)}</td><td>{display(row._row)}</td>{query.data!.catalog.map((column) => <td key={column.key}>{display(row[column.key])}</td>)}</tr>)}</tbody></table></div>
      {!query.data.rows.length && <p>此区间没有原始采样。</p>}
      <div className="wb-pagination"><button className="curve-button" type="button" disabled={!offset} onClick={() => setOffset((value) => Math.max(0, value - 100))}>上一页</button><span>第 {Math.floor(offset / 100) + 1} 页</span><button className="curve-button" type="button" disabled={!query.data.has_more} onClick={() => setOffset((value) => value + 100)}>下一页</button></div>
    </>}
    <details className="wb-query-details"><summary>查询与权限口径</summary><p>归档引用：{record.id}；参数：{filters.metrics}；阶段：{filters.stage || "全部"}；循环：{filters.cycle || "全部"}。区间含起点和终点，使用归档原始 Parquet；每次读取和导出均由服务端校验权限。</p></details>
  </section>;
}

export function WorkspacePanel({ panel, base, records, executionColors, visible, scope, config, domain, viewport, absolute, focused, onFocus, onChange, onViewport, onCursors }: {
  panel: PanelConfig; base: Archive; records: Archive[]; executionColors: Record<string, string>; visible: boolean; scope: string; config: WorkbenchConfig; domain: CurveViewport; viewport: CurveViewport; absolute: boolean; focused: boolean;
  onFocus: () => void; onChange: (panel: PanelConfig) => void; onViewport: (viewport: CurveViewport) => void; onCursors: (cursors: { a: number | null; b: number | null }) => void;
}) {
  const [inspecting, setInspecting] = useState(false);
  const [inspectId, setInspectId] = useState("");
  const [referenceName, setReferenceName] = useState("");
  const [referenceValue, setReferenceValue] = useState("");
  const catalog = base.manifest?.catalog ?? [];
  const metrics = [...new Map(catalog.map((entry) => [metricIdentity(entry), entry])).values()];
  const subjects = [...new Set(catalog.filter((entry) => entry.metric === panel.metric && entry.unit === panel.unit).map((entry) => entry.subject))];
  const available = records.filter((record) => panelColumns(record, panel).some((column) => column.metric === panel.metric));
  const filters = available.map((record) => panelFilters(record, panel, viewport, config, absolute));
  const eligible = (index: number) => !!available[index] && !!filters[index].metrics && filters[index].start <= filters[index].end;
  const curveQueries = useQueries({ queries: Array.from({ length: 8 }, (_, index) => ({
    queryKey: ["workspace", scope, "curves", panel.id, available[index]?.id ?? index, filters[index], config.maxPoints],
    queryFn: ({ signal }: { signal: AbortSignal }): Promise<ArchiveCurves> => archivesApi.curves(available[index].id, { ...filters[index], max_points: config.maxPoints }, signal), enabled: eligible(index), staleTime: 15_000
  })) }).slice(0, available.length);
  const statQueries = useQueries({ queries: Array.from({ length: 8 }, (_, index) => ({
    queryKey: ["workspace", scope, "statistics", available[index]?.id ?? index, filters[index]],
    queryFn: ({ signal }: { signal: AbortSignal }): Promise<StatisticsResponse> => workspaceApi.statistics(available[index].id, filters[index], signal), enabled: eligible(index), staleTime: 15_000
  })) }).slice(0, available.length);
  const overviewQueries = useQueries({ queries: Array.from({ length: 8 }, (_, index) => {
    const record = available[index];
    const full = record ? archiveRange(record) : { start: 0, end: 1 };
    return { queryKey: ["workspace", scope, "overview", panel.id, record?.id ?? index, filters[index]?.metrics, config.stage, config.cycle], queryFn: ({ signal }: { signal: AbortSignal }) => archivesApi.curves(record.id, { ...filters[index], ...full, max_points: 800 }, signal), enabled: focused && eligible(index), staleTime: 60_000 };
  }) }).slice(0, available.length);
  function toSeries(queries: { data?: ArchiveCurves; isError: boolean }[]): PlotSeries[] {
    return queries.flatMap((query, index) => {
      if (query.isError) return [];
      const record = available[index];
      const selected = panelColumns(record, panel);
      const time = (value: number) => absolute ? archiveStart(record)! + value * 1000 : value;
      return (query.data?.series ?? []).filter((curve) => selected.some((column) => column.key === curve.key && column.unit === curve.unit)).map((curve) => ({
        id: `${record.id}:${curve.key}`, name: `${readableName(record)} · ${record.execution_code || record.archive_code || record.id} · ${curve.subject || "全局"}${curve.metric !== panel.metric ? " · 目标" : ""}`,
        legendName: `${readableName(record).replace(/\s*[（(]历史\s*CSV[）)]\s*$/i, "")}${curve.metric !== panel.metric ? " · 目标" : ""}`,
        legendDetail: `${shortCode(record)} · ${curve.subject || "全局"} · ${curve.unit || "无单位"}`,
        unit: curve.unit, reference: curve.metric !== panel.metric, color: executionColors[record.id], dash: curve.metric !== panel.metric ? "dashed" as const : "solid" as const,
        points: curve.points.map(([x, y]): [number, number | null] => [time(x), y]), gaps: (query.data?.gaps ?? []).map((gap) => ({ start: time(gap.start), end: time(gap.end) }))
      }));
    });
  }
  const series = toSeries(curveQueries);
  const statistics = Object.fromEntries(statQueries.flatMap((query, index) => query.isError || query.isFetching ? [] : (query.data?.series ?? []).map((stat) => [`${available[index].id}:${stat.key}`, stat])));
  const shown = curveQueries.reduce((total, query) => total + (query.isError ? 0 : query.data?.series.reduce((sum, curve) => sum + curve.returned_points, 0) ?? 0), 0);
  const loading = curveQueries.some((query) => query.isFetching);
  const downsampled = curveQueries.some((query) => !query.isError && query.data?.resolution === "minmax");
  const resolution = `${downsampled ? "峰谷概览" : "原始分辨率"} · ${shown.toLocaleString("zh-CN")} 点 · ${config.maxPoints.toLocaleString("zh-CN")} 点/执行上限`;
  const configured = limits(base, panel.metric);
  const inspectable = available.filter((_, index) => eligible(index));
  const inspected = inspectable.find((record) => record.id === inspectId) ?? inspectable[0];
  const inspectedFilters = inspected ? filters[available.indexOf(inspected)] : null;
  const title = `${metricNames[panel.metric] ?? panel.metric}${panel.targets && POSITION.test(panel.metric) ? " / 目标" : ""}`;
  function switchMetric(identity: string) {
    const chosen = metrics.find((entry) => metricIdentity(entry) === identity);
    if (!chosen) return;
    const nextSubjects = catalog.filter((entry) => entry.metric === chosen.metric && entry.unit === chosen.unit).map((entry) => entry.subject);
    const retained = panel.subjects.filter((subject) => nextSubjects.includes(subject));
    onChange({ ...panel, metric: chosen.metric, unit: chosen.unit, subjects: retained.length ? retained : nextSubjects.slice(0, 1), references: [], targets: POSITION.test(chosen.metric) });
  }
  return <article hidden={!visible} className={`wb-panel${focused ? " focused" : ""}`} aria-label={`${title}面板`}>
    <div className="wb-panel-heading"><div><span className="wb-panel-kicker">{panel.subjects.join(" / ").toUpperCase() || "未选关节"}</span><h2>{title}<small>{panel.unit || "无单位"}{panel.metric === "tracking_error_rad" ? " · 派生" : ""}</small></h2></div><div className="wb-panel-actions"><button className="curve-button" type="button" disabled={!available.length} aria-pressed={inspecting} onClick={() => setInspecting((value) => !value)}><Table2 />检查数据</button><button className="curve-row-action" type="button" aria-label={focused ? `退出${title}聚焦` : `聚焦${title}`} onClick={onFocus}>{focused ? <Minimize2 /> : <Maximize2 />}</button></div></div>
    <div className="wb-panel-settings"><label>参数<select aria-label={`${panel.id} 参数`} value={metricIdentity(panel)} onChange={(event) => switchMetric(event.target.value)}>{!metrics.some((entry) => metricIdentity(entry) === metricIdentity(panel)) && <option value={metricIdentity(panel)}>{title} · 未提供</option>}{metrics.map((entry) => <option key={metricIdentity(entry)} value={metricIdentity(entry)}>{metricNames[entry.metric] ?? entry.metric} · {entry.unit || "无单位"}</option>)}</select></label><details className="curve-disclosure wb-subject-picker"><summary>关节 · {panel.subjects.length} 个</summary><div className="curve-popover">{subjects.map((subject) => <label key={subject}><input type="checkbox" aria-label={`${panel.id} 关节 ${subject}`} checked={panel.subjects.includes(subject)} disabled={!panel.subjects.includes(subject) && panel.subjects.length >= (panel.targets ? 16 : 32)} onChange={() => onChange({ ...panel, subjects: panel.subjects.includes(subject) ? panel.subjects.filter((entry) => entry !== subject) : [...panel.subjects, subject] })} />{subject || "全局"}</label>)}</div></details><label>展示<select aria-label={`${panel.id} 展示方式`} value={panel.layout} onChange={(event) => onChange({ ...panel, layout: event.target.value as "overlay" | "facets" })}><option value="overlay">叠加</option><option value="facets">分面</option></select></label><details className="curve-disclosure"><summary>参考线</summary><div className="curve-popover wb-reference-popover"><label><input type="checkbox" checked={panel.limits && !!configured.length} disabled={!configured.length} onChange={(event) => onChange({ ...panel, limits: event.target.checked })} />用例上下限{!configured.length && "（未配置）"}</label>{POSITION.test(panel.metric) && <label><input type="checkbox" checked={panel.targets} onChange={(event) => onChange({ ...panel, targets: event.target.checked, subjects: panel.subjects.slice(0, event.target.checked ? 16 : 32) })} />目标轨迹</label>}<form onSubmit={(event) => { event.preventDefault(); if (referenceName.trim() && referenceValue.trim() && Number.isFinite(Number(referenceValue))) { onChange({ ...panel, references: [...panel.references, { id: crypto.randomUUID(), name: referenceName.trim(), value: Number(referenceValue), temporary: true }] }); setReferenceName(""); setReferenceValue(""); } }}><label>临时参考名称<input maxLength={80} value={referenceName} onChange={(event) => setReferenceName(event.target.value)} /></label><label>参考值 / {panel.unit}<input type="number" step="any" value={referenceValue} onChange={(event) => setReferenceValue(event.target.value)} /></label><button className="curve-button" disabled={panel.references.length >= 8 || !referenceName.trim() || !referenceValue.trim() || !Number.isFinite(Number(referenceValue))}>添加参考线</button></form>{panel.references.map((line) => <div key={line.id}>{line.name} · {line.value}<button className="curve-row-action" type="button" aria-label={`删除参考线 ${line.name}`} onClick={() => onChange({ ...panel, references: panel.references.filter((entry) => entry.id !== line.id) })}><X /></button></div>)}<p>临时参考不改变正式判据。参数变更后参考线清空。</p></div></details></div>
    {!available.length ? <div className="wb-panel-empty"><strong>{!panel.subjects.length ? "请选择关节" : "当前选择未提供此参数"}</strong><p>缺少同参数、同单位或所选关节的原始数据。缺失不会变为零。</p></div> : <>
      <TimeSeriesChart active={visible} title={title} series={series} overviewSeries={focused ? toSeries(overviewQueries) : undefined} overviewLoading={overviewQueries.some((query) => query.isFetching)} references={[...(panel.limits ? configured : []), ...panel.references]} domain={domain} viewport={viewport} onViewport={onViewport} loading={loading} resolution={resolution} absolute={absolute} layout={panel.layout} compact={!focused} height={focused ? 420 : 270} showToolbar showOverview={focused} cursorGroup={`workbench-${scope}`} fixedCursors={config.cursors} onFixedCursorsChange={onCursors} showCursorControls={false} statistics={statistics} statisticsLoading={statQueries.some((query) => query.isFetching)} />
      {curveQueries.map((query, index) => query.error && <div key={available[index].id} className="wb-panel-error"><p>{readableName(available[index])} 曲线读取失败</p><ErrorState error={query.error} retry={() => void query.refetch()} /></div>)}
      {focused && overviewQueries.map((query, index) => query.error && <div key={available[index].id} className="wb-panel-error"><p>全程概览读取失败 · {readableName(available[index])}</p><ErrorState error={query.error} retry={() => void query.refetch()} /></div>)}
      {statQueries.map((query, index) => query.error && <div key={available[index].id} className="wb-panel-error"><p>精确统计不可用 · {readableName(available[index])}</p><ErrorState error={query.error} retry={() => void query.refetch()} /></div>)}
      {!loading && !!series.length && series.every((entry) => entry.points.every((point) => point[1] === null)) && <p className="wb-data-note" role="status">当前范围没有有效采样。原始空值保留，曲线不跨缺失连接。</p>}
      {available.some((_, index) => !eligible(index)) && <p className="wb-data-note">部分执行与当前时间区间不重叠，未查询其采样或统计。</p>}
      {available.length < records.length && <p className="wb-data-note">{records.length - available.length} 次执行缺少同参数、同单位或所选关节，未在本面板绘制。</p>}
    </>}
    {inspecting && inspected && inspectedFilters && <div className="wb-inspector-wrap"><label className="wb-inspect-select">检查执行<select aria-label={`${panel.id} 检查执行`} value={inspected.id} onChange={(event) => setInspectId(event.target.value)}>{inspectable.map((record) => <option key={record.id} value={record.id}>{readableName(record)} · {shortCode(record)}</option>)}</select></label><RawInspector key={`${inspected.id}:${JSON.stringify(inspectedFilters)}`} record={inspected} panel={panel} filters={inspectedFilters} scope={scope} onClose={() => setInspecting(false)} /></div>}
  </article>;
}
