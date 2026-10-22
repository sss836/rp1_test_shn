import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useAssistantPageContext } from "../../assistantContext";
import { useQueries, useQuery } from "@tanstack/react-query";
import { ChevronDown, Columns2, Info, Link, Plus, RotateCcw, Save, Search, X } from "lucide-react";
import { archivesApi, type Archive } from "../../archiveApi";
import { useAuth } from "../../auth";
import { ErrorState } from "../../components/Workbench";
import type { CurveViewport } from "../../components/TimeSeriesChart";
import { WorkspacePanel } from "./Panel";
import { archiveRange, archiveStart, defaultConfig, loadViews, readableName, readUrlConfig, saveViews, shareUrl, shortCode, type SavedView, type WorkbenchConfig } from "./config";

const COLORS = ["#3274d9", "#149e98", "#9365c7", "#d08b24", "#b75579", "#548a47", "#6376ad", "#916d4c"];
function colorsForExecutions(previous: Record<string, string>, ids: string[]) {
  const retained = (id: string) => Object.hasOwn(previous, id) ? previous[id] : undefined;
  const used = new Set(ids.flatMap((id) => retained(id) ? [retained(id)!] : []));
  const next = Object.fromEntries(ids.map((id) => {
    const color = retained(id) ?? COLORS.find((candidate) => !used.has(candidate)) ?? COLORS[0];
    used.add(color);
    return [id, color];
  }));
  return ids.length === Object.keys(previous).length && ids.every((id) => previous[id] === next[id]) ? previous : next;
}
function choices(values: unknown[]) { return values.flatMap((entry) => {
  if (typeof entry === "string" || typeof entry === "number") return [{ value: String(entry), label: String(entry) }];
  if (entry && typeof entry === "object") { const value = entry as Record<string, unknown>; const id = value.code ?? value.stage ?? value.cycle ?? value.cycle_no ?? value.id; if (id !== undefined) return [{ value: String(id), label: String(value.name ?? value.label ?? id) }]; }
  return [];
}); }
function TimeControls({ viewport, domain, absolute, onChange, cursors, onCursors }: { viewport: CurveViewport; domain: CurveViewport; absolute: boolean; onChange: (value: CurveViewport | null) => void; cursors: WorkbenchConfig["cursors"]; onCursors: (value: WorkbenchConfig["cursors"]) => void }) {
  const [start, setStart] = useState(String(viewport.start));
  const [end, setEnd] = useState(String(viewport.end));
  const [a, setA] = useState(cursors.a === null ? "" : String(cursors.a));
  const [b, setB] = useState(cursors.b === null ? "" : String(cursors.b));
  const [error, setError] = useState("");
  useEffect(() => { setStart(String(viewport.start)); setEnd(String(viewport.end)); }, [viewport.start, viewport.end]);
  useEffect(() => { setA(cursors.a === null ? "" : String(cursors.a)); setB(cursors.b === null ? "" : String(cursors.b)); }, [cursors.a, cursors.b]);
  const unit = absolute ? "Unix ms" : "s";
  return <div className="wb-time-controls"><form className="wb-time-form" onSubmit={(event) => { event.preventDefault(); const left = Number(start), right = Number(end); if (!start.trim() || !end.trim() || !Number.isFinite(left) || !Number.isFinite(right) || left >= right || right <= domain.start || left >= domain.end) { setError("请输入与数据范围重叠且起点小于终点的有效时间。"); return; } setError(""); onChange({ start: Math.max(left, domain.start), end: Math.min(right, domain.end) }); }}><span className="wb-time-label">全局时间 <small>{unit}</small></span><label><input type="number" step="any" aria-label="全局时间起点" value={start} onChange={(event) => setStart(event.target.value)} /></label><span>—</span><label><input type="number" step="any" aria-label="全局时间终点" value={end} onChange={(event) => setEnd(event.target.value)} /></label><button type="submit" className="curve-button primary">应用区间</button><button type="button" className="curve-button" onClick={() => { setError(""); onChange(null); }}><RotateCcw />全程</button></form>
    <details className="wb-cursor-details"><summary>A / B 时间游标{cursors.a !== null && cursors.b !== null && <span>Δt {(cursors.b - cursors.a) / (absolute ? 1000 : 1)} s</span>}</summary><form onSubmit={(event) => { event.preventDefault(); const values = [a, b].map((entry) => entry.trim() ? Number(entry) : null); if (values.some((value) => value !== null && (!Number.isFinite(value) || value < domain.start || value > domain.end))) { setError("游标时间必须位于全程范围内。"); return; } setError(""); onCursors({ a: values[0], b: values[1] }); }}><label>A / {unit}<input aria-label="全局游标 A" type="number" step="any" value={a} onChange={(event) => setA(event.target.value)} /></label><label>B / {unit}<input aria-label="全局游标 B" type="number" step="any" value={b} onChange={(event) => setB(event.target.value)} /></label><button className="curve-button" type="submit">固定游标</button><button className="curve-button" type="button" onClick={() => onCursors({ a: null, b: null })}>清除</button><p>所有面板共享 A/B 时间，Δt = B − A；不对降采样数据插值计算 Δy。</p></form></details>
    <details className="curve-disclosure wb-interaction-help"><summary><Info />操作帮助</summary><p className="curve-popover">拖动图内平移；选“框选缩放”后拖出区间，各面板同步。Ctrl＋滚轮缩放时间，Shift＋滚轮或右侧滑块缩放纵轴。X / Y 设置精确范围，“上一步”返回上个时间范围。</p></details>
    {absolute && <p className="wb-data-note">{new Date(viewport.start).toISOString()} — {new Date(viewport.end).toISOString()}</p>}{error && <p className="curve-error" role="alert">{error}</p>}
  </div>;
}

function useNarrowWorkbench() {
  const [narrow, setNarrow] = useState(() => window.matchMedia?.("(max-width: 760px)").matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.("(max-width: 760px)");
    if (!media) return;
    const update = (event: MediaQueryListEvent) => setNarrow(event.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return narrow;
}

export function CurveWorkbench({ item, navigate, onSingle, initialViewport, initialAlignment, onViewportChange }: { item: Archive; navigate: (view: "archive" | "curves", id: string) => void; onSingle: () => void; initialViewport?: CurveViewport | null; initialAlignment?: "elapsed" | "absolute"; onViewportChange?: (value: CurveViewport | null, alignment: "elapsed" | "absolute") => void }) {
  const { session } = useAuth();
  const userId = session?.user.id ?? "";
  const [initial] = useState(() => readUrlConfig(item.id));
  const [config, setConfig] = useState<WorkbenchConfig>(() => {
    const restored = initial.config ?? defaultConfig(item);
    // URL configuration initializes the first visit. A later mode transition
    // carries its current viewport together with its coordinate system.
    return initialAlignment === undefined ? restored : { ...restored, viewport: initialViewport ?? null, alignment: initialAlignment };
  });
  const [colorRegistry, setColorRegistry] = useState<Record<string, string>>(() => colorsForExecutions({}, [item.id, ...config.comparisons]));
  const narrow = useNarrowWorkbench();
  const [selectionOpen, setSelectionOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [focusSearch, setFocusSearch] = useState(false);
  const [focus, setFocus] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [search, setSearch] = useState("");
  const excluded = config.excluded;
  function setExcluded(update: (current: string[]) => string[]) { setConfig((current) => ({ ...current, excluded: update(current.excluded) })); }
  const [note, setNote] = useState(initial.error);
  const [viewName, setViewName] = useState("");
  const [views, setViews] = useState<SavedView[]>(() => loadViews(userId));
  const [savedSelection, setSavedSelection] = useState("");
  const [shared, setShared] = useState("");
  const [generation, setGeneration] = useState(0);
  const searchInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusSearch && (!narrow || selectionOpen)) { searchInput.current?.focus(); setFocusSearch(false); }
  }, [focusSearch, narrow, selectionOpen]);
  const scope = `${userId || "session"}:${generation}`;
  const compareQueries = useQueries({ queries: Array.from({ length: 7 }, (_, index) => ({ queryKey: ["workspace", scope, "authorized-detail", config.comparisons[index] ?? index], queryFn: ({ signal }: { signal: AbortSignal }) => archivesApi.detail(config.comparisons[index], signal), enabled: !!config.comparisons[index], staleTime: 0, refetchOnMount: "always" as const })) }).slice(0, config.comparisons.length);
  // Only fresh, successful permission-checked detail results may become panels.
  const compared = compareQueries.flatMap((query) => query.data && !query.isError && !query.isFetching && query.data.processing_state === "READY" ? [query.data] : []);
  const allRecords = [item, ...compared];
  // Keep surviving archive identities in their existing color slots. Excluded
  // and still-loading IDs reserve their slots; removed IDs release theirs.
  // Derive the complete map before rendering so async load order cannot flash
  // an unassigned/default color before the registry effect runs.
  const executionColors = colorsForExecutions(colorRegistry, [item.id, ...config.comparisons]);
  useEffect(() => { setColorRegistry(executionColors); }, [executionColors]);
  const records = allRecords.filter((record) => !excluded.includes(record.id));
  const noAbsolute = records.some((record) => archiveStart(record) === null);
  const resetAlignment = config.alignment === "absolute" && noAbsolute;
  const absolute = config.alignment === "absolute" && !noAbsolute;
  const selectedViewport = resetAlignment ? null : config.viewport;
  const selectedCursors = resetAlignment ? { a: null, b: null } : config.cursors;
  useEffect(() => {
    if (!resetAlignment) return;
    setConfig((current) => ({ ...current, alignment: "elapsed", viewport: null, cursors: { a: null, b: null } }));
    setNote("选中执行缺少可信开始时间，已切换为相对测试开始；原绝对时间区间与 A/B 游标已清空，请重新选择。");
    onViewportChange?.(null, "elapsed");
  }, [resetAlignment, onViewportChange]);
  const ranges = (records.length ? records : [item]).map((record) => { const range = archiveRange(record); const offset = absolute ? archiveStart(record)! : 0; return { start: offset + range.start * (absolute ? 1000 : 1), end: offset + range.end * (absolute ? 1000 : 1) }; });
  const domain = { start: Math.min(...ranges.map((range) => range.start)), end: Math.max(...ranges.map((range) => range.end)) };
  const viewport = selectedViewport && selectedViewport.start < domain.end && selectedViewport.end > domain.start ? { start: Math.max(domain.start, selectedViewport.start), end: Math.min(domain.end, selectedViewport.end) } : domain;
  const stageChoices = choices(item.manifest?.stages ?? []), cycleChoices = choices(item.manifest?.cycles ?? []);
  const effectiveConfig = { ...config, cursors: selectedCursors, viewport: selectedViewport ? viewport : null, stage: stageChoices.some((entry) => entry.value === config.stage) ? config.stage : "", cycle: cycleChoices.some((entry) => entry.value === config.cycle) ? config.cycle : "", alignment: absolute ? "absolute" as const : "elapsed" as const };
  const assistantRecord=records[0]??item,assistantPanel=config.panels.find((panel)=>panel.id===focus)??config.panels[0];
  const assistantOffset=absolute?archiveStart(assistantRecord)??0:0;
  const assistantStart=Math.max(archiveRange(assistantRecord).start,(viewport.start-assistantOffset)/(absolute?1000:1));
  const assistantEnd=Math.min(archiveRange(assistantRecord).end,(viewport.end-assistantOffset)/(absolute?1000:1));
  useAssistantPageContext({view:"curves",archive_id:assistantRecord.id,metrics:assistantPanel?[assistantPanel.metric]:[],subjects:assistantPanel?.subjects.slice(0,8)??[],selection_empty:assistantStart>assistantEnd,...(assistantStart<=assistantEnd?{start:assistantStart,end:assistantEnd}:{}),stage:effectiveConfig.stage||undefined,cycle:effectiveConfig.cycle||undefined});
  const compareSearch = useQuery({ queryKey: ["workspace", scope, "search", search], queryFn: ({ signal }) => archivesApi.list({ q: search, processing_state: "READY" }, signal), enabled: !!search });
  function changeViewport(value: CurveViewport | null) { setConfig((current) => ({ ...current, viewport: value })); onViewportChange?.(value, absolute ? "absolute" : "elapsed"); }
  function restore(view: SavedView) {
    if (view.config.base !== item.id) { setNote("此视图属于另一执行，请从对应执行打开或使用该视图的分享链接。"); return; }
    setConfig(view.config); setGeneration((value) => value + 1); setFocus(null); setShared(""); onViewportChange?.(view.config.viewport, view.config.alignment); setNote(`已恢复“${view.name}”，正在重新校验执行访问权限。`);
  }
  function save() {
    const name = viewName.trim(); if (!name) return;
    try { const next = [...views.filter((view) => view.name !== name), { name, config: effectiveConfig }]; saveViews(userId, next); setViews(next); setSavedSelection(name); setNote(`“${name}”已保存在此浏览器的当前账号下。`); }
    catch (error) { setNote(error instanceof Error ? error.message : "浏览器存储不可用。"); }
  }
  async function copyLink() {
    let url: string;
    try { url = shareUrl(effectiveConfig); }
    catch (error) { setShared(""); setNote(`分享链接生成失败：${error instanceof Error ? error.message : "请检查当前视图配置。"}`); return; }
    setShared(url);
    try { await navigator.clipboard.writeText(url); setNote("分享链接已复制；接收者仍需登录并拥有归档访问权限。"); }
    catch { setNote("自动复制不可用，请从下方链接框复制分享链接；接收者仍需拥有访问权限。"); }
  }
  return <>
    <header className="curve-page-heading wb-heading"><div className="curve-heading-copy"><h1 data-page-heading tabIndex={-1}>曲线分析工作台</h1><p>{readableName(item)}<span>{shortCode(item)}</span></p></div><div className="curve-heading-actions"><button className="curve-button" type="button" onClick={() => { onViewportChange?.(config.viewport ? viewport : null, absolute ? "absolute" : "elapsed"); onSingle(); }}><Columns2 />单图对比</button><button className="curve-button" type="button" aria-label="返回归档详情" onClick={() => navigate("archive", item.id)}>归档详情</button><button className="curve-button primary" type="button" onClick={() => { setSelectionOpen(true); setFocusSearch(true); }}><Plus />添加执行</button></div></header>
    <div className="wb-view-strip"><details className="curve-disclosure wb-save-details"><summary className="curve-button"><Save />保存 / 恢复视图</summary><div className="curve-popover"><label>视图名称<input aria-label="视图名称" maxLength={60} value={viewName} onChange={(event) => setViewName(event.target.value)} placeholder="例如：左肩负载对比" /></label><button className="curve-button primary" type="button" disabled={!viewName.trim() || !userId || views.length >= 20 && !views.some((view) => view.name === viewName.trim())} onClick={save}>保存到此浏览器</button><label>已保存视图<select aria-label="已保存视图" value={savedSelection} onChange={(event) => setSavedSelection(event.target.value)}><option value="">请选择</option>{views.filter((view) => view.config.base === item.id).map((view) => <option key={view.name}>{view.name}</option>)}</select></label><div className="wb-saved-actions"><button className="curve-button" type="button" disabled={!savedSelection} onClick={() => { const view = views.find((entry) => entry.name === savedSelection); if (view) restore(view); }}>恢复视图</button><button className="curve-button" type="button" disabled={!savedSelection} onClick={() => { try { const next = views.filter((view) => view.name !== savedSelection); saveViews(userId, next); setViews(next); setSavedSelection(""); } catch { setNote("浏览器存储不可用，删除未完成。"); } }}>删除视图</button></div><p>仅保存在此浏览器、当前账号。最多 20 个命名视图；不保存原始数据或登录凭据。</p></div></details><button className="curve-button" type="button" onClick={() => void copyLink()}><Link />复制分享链接</button><span><i />{focus ? "单面板聚焦" : `${config.panels.length} 个联动面板`} · 时间与游标同步 · 区间统计来自原始采样</span>{focus && <button className="curve-button" type="button" onClick={() => setFocus(null)}>返回全部面板</button>}</div>
    {note && <p role="status" className="curve-selection-note">{note}</p>}{shared && <label className="wb-share-link">当前视图链接<input readOnly aria-label="当前视图链接" value={shared} onFocus={(event) => event.target.select()} /></label>}
    <div className="curve-workbench wb-workbench"><aside className="curve-execution-rail wb-execution-rail" aria-label="执行比较选择">
      <button type="button" className="wb-mobile-disclosure" aria-expanded={selectionOpen} aria-controls="wb-selected-executions" onClick={() => setSelectionOpen((value) => !value)}><span><strong>已选执行 {records.length} / 8</strong><small>已排除 {excluded.length} · {selectionOpen ? "收起执行" : "展开全部执行"}</small></span><ChevronDown /></button>
      <p className="wb-mobile-base" title={`${readableName(item)} · ${shortCode(item)}`}>基准：{readableName(item)} · {shortCode(item)}{excluded.includes(item.id) && "（已排除）"}</p>
      {compareSearch.isFetching && <p role="status">正在查找执行…</p>}{compareSearch.error && <ErrorState error={compareSearch.error} retry={() => void compareSearch.refetch()} />}
      <div id="wb-selected-executions" hidden={narrow && !selectionOpen}><h2>已选执行 <span>{records.length} / 8</span></h2><form className="curve-execution-search" onSubmit={(event) => { event.preventDefault(); setSearch(draft.trim()); }}><input ref={searchInput} aria-label="工作台查找测试记录" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="搜索测试名称或编号" /><button className="curve-button" type="submit" aria-label="工作台查找对比记录" disabled={!draft.trim()}><Search /></button></form>
      {compareSearch.data && <div className="curve-results" aria-label="工作台对比搜索结果">{compareSearch.data.items.filter((entry) => entry.id !== item.id && !config.comparisons.includes(entry.id)).map((entry) => <button key={entry.id} type="button" disabled={config.comparisons.length >= 7 || entry.processing_state !== "READY"} onClick={() => setConfig((current) => ({ ...current, comparisons: current.comparisons.includes(entry.id) ? current.comparisons : [...current.comparisons, entry.id].slice(0, 7) }))}><span><strong>{readableName(entry)}</strong><small>{shortCode(entry)} · 添加对比</small></span><Plus /></button>)}<button className="curve-record-link" type="button" onClick={() => { setSearch(""); setDraft(""); }}>收起搜索结果</button></div>}
      <ul className="curve-execution-list">{allRecords.map((record, index) => <li key={record.id} className={`curve-execution-item${excluded.includes(record.id) ? "" : " selected"}`}><div className="curve-execution-row"><label><input type="checkbox" aria-label={`工作台参与对比 ${readableName(record)} ${shortCode(record)}`} checked={!excluded.includes(record.id)} onChange={() => setExcluded((current) => current.includes(record.id) ? current.filter((id) => id !== record.id) : [...current, record.id])} /><span className="curve-series-dot" style={{ "--series-color": executionColors[record.id] } as CSSProperties} /><strong title={readableName(record)}>{readableName(record)}</strong></label>{index > 0 && <button className="curve-row-action" type="button" aria-label={`移除执行 ${readableName(record)}`} onClick={() => setConfig((current) => ({ ...current, comparisons: current.comparisons.filter((id) => id !== record.id), excluded: current.excluded.filter((id) => id !== record.id) }))}><X /></button>}</div><div className="curve-execution-meta"><span title={record.execution_code || record.archive_code}>{shortCode(record)}</span><span className="curve-execution-badge">{index ? "对比" : "基准"}</span><p>{record.actual_object} · {record.station_label}</p></div></li>)}</ul>
      </div>
      {compareQueries.map((query, index) => query.isFetching ? <p key={config.comparisons[index]} role="status">正在校验第 {index + 2} 个执行…</p> : query.error ? <div key={config.comparisons[index]} className="wb-access-error"><p>第 {index + 2} 个执行不可访问，未加载曲线。</p><ErrorState error={query.error} retry={() => void query.refetch()} /><button className="curve-record-link" type="button" onClick={() => setConfig((current) => ({ ...current, comparisons: current.comparisons.filter((id) => id !== config.comparisons[index]), excluded: current.excluded.filter((id) => id !== config.comparisons[index]) }))}>移除此引用</button></div> : query.data?.processing_state !== "READY" ? <p key={config.comparisons[index]}>第 {index + 2} 个执行尚未就绪。</p> : null)}
      <button type="button" className="wb-mobile-disclosure wb-settings-disclosure" aria-expanded={settingsOpen} aria-controls="wb-global-settings" onClick={() => setSettingsOpen((value) => !value)}><span><strong>全局设置 · {absolute ? "绝对时间" : "相对测试开始"}</strong><small>{config.maxPoints.toLocaleString("zh-CN")} 点/执行 · 阶段 {effectiveConfig.stage || (stageChoices.length ? "全部" : "未记录")} · 循环 {effectiveConfig.cycle || (cycleChoices.length ? "全部" : "未记录")}</small></span><ChevronDown /></button>
      <div id="wb-global-settings" hidden={narrow && !settingsOpen}><section className="curve-comparison-options"><h3>全局查询设置</h3><label>时间对齐<select aria-label="工作台时间对齐" value={absolute ? "absolute" : "elapsed"} onChange={(event) => setConfig((current) => ({ ...current, alignment: event.target.value as "elapsed" | "absolute", viewport: null, cursors: { a: null, b: null } }))}><option value="elapsed">测试开始</option><option value="absolute" disabled={noAbsolute}>绝对时间</option></select></label><label>视窗点数上限<select aria-label="工作台视窗点数上限" value={config.maxPoints} onChange={(event) => setConfig((current) => ({ ...current, maxPoints: Number(event.target.value) }))}>{[1000, 4000, 10000, 20000].map((value) => <option key={value} value={value}>{value.toLocaleString("zh-CN")}</option>)}</select></label><label>阶段<select aria-label="工作台阶段" value={effectiveConfig.stage} disabled={!stageChoices.length} onChange={(event) => setConfig((current) => ({ ...current, stage: event.target.value }))}><option value="">{stageChoices.length ? "全部" : "未记录"}</option>{stageChoices.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></label><label>循环<select aria-label="工作台循环" value={effectiveConfig.cycle} disabled={!cycleChoices.length} onChange={(event) => setConfig((current) => ({ ...current, cycle: event.target.value }))}><option value="">{cycleChoices.length ? "全部" : "未记录"}</option>{cycleChoices.map((entry) => <option key={entry.value} value={entry.value}>{entry.label}</option>)}</select></label></section><p className="curve-contract-note"><Info />各面板独立选择参数和关节，仅比较同参数、同单位。</p></div>{noAbsolute && <p className="curve-reference-help">选中执行缺少可信开始时间，绝对时间对齐不可用。</p>}{(!stageChoices.length || !cycleChoices.length) && <p className="curve-reference-help">当前归档未提供{!stageChoices.length && "阶段"}{!stageChoices.length && !cycleChoices.length && "、"}{!cycleChoices.length && "循环"}索引，对应筛选不可用。</p>}</aside>
      <div className="wb-main-stage" role="region" aria-label="联动分析面板"><TimeControls viewport={viewport} domain={domain} absolute={absolute} onChange={changeViewport} cursors={effectiveConfig.cursors} onCursors={(cursors) => setConfig((current) => ({ ...current, cursors }))} /><div className={`wb-panel-grid${focus ? " is-focused" : ""}`}>{config.panels.map((panel) => <WorkspacePanel key={panel.id} panel={panel} base={item} records={records} executionColors={executionColors} visible={!focus || panel.id === focus} scope={scope} config={effectiveConfig} domain={domain} viewport={viewport} absolute={absolute} focused={focus === panel.id} onFocus={() => setFocus((current) => current === panel.id ? null : panel.id)} onChange={(next) => setConfig((current) => ({ ...current, panels: current.panels.map((entry) => entry.id === next.id ? next : entry) }))} onViewport={(value) => changeViewport(value)} onCursors={(cursors) => setConfig((current) => ({ ...current, cursors }))} />)}</div><p className="wb-bottom-note">拖动图表缩放后，所有面板重查相同时间段。统计和原始表使用相同筛选；样本与临时参考不改变归档核实、审批状态或正式判据。</p></div>
    </div>
  </>;
}
