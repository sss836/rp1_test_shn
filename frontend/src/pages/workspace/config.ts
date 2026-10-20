import type { Archive, ArchiveCatalogItem } from "../../archiveApi";
import type { CurveViewport, ReferenceLine } from "../../components/TimeSeriesChart";

export const VIEW_PARAM = "curve_view";
export const VIEW_LIMIT = 16_000;
export const metricNames: Record<string, string> = { pos_rad: "实际位置", position_rad: "实际位置", actual_position_rad: "实际位置", cmd_pos_rad: "目标位置", target_pos_rad: "目标位置", target_position_rad: "目标位置", pos_ref_rad: "目标位置", spd_rad_s: "速度", vel_rad_s: "速度", velocity_rad_s: "速度", torque_nm: "力矩", temp_c: "温度", temperature_c: "温度", current_a: "电流", bus_current_a: "母线电流", voltage_v: "电压", bus_voltage_v: "母线电压", error_id: "故障码", tracking_error_rad: "跟踪误差" };
export type PanelConfig = { id: string; metric: string; unit: string; subjects: string[]; layout: "overlay" | "facets"; targets: boolean; limits: boolean; references: ReferenceLine[] };
export type WorkbenchConfig = { version: 1; base: string; comparisons: string[]; excluded: string[]; panels: PanelConfig[]; viewport: CurveViewport | null; stage: string; cycle: string; alignment: "elapsed" | "absolute"; maxPoints: number; cursors: { a: number | null; b: number | null } };
export type SavedView = { name: string; config: WorkbenchConfig };
const ID = /^[A-Za-z0-9:_-]{1,120}$/;
const plain = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max: number): value is string => typeof value === "string" && value.length <= max && ![...value].some((char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127);
const time = (value: unknown, absolute: boolean): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= (absolute ? 8_640_000_000_000_000 : 315_360_000);
const exact = (value: Record<string, unknown>, keys: string[]) => Object.keys(value).every((key) => keys.includes(key));
export const metricIdentity = (entry: Pick<ArchiveCatalogItem, "metric" | "unit">) => `${entry.metric}\u0000${entry.unit}`;
export const readableName = (record: Archive) => record.test_name?.trim() || record.execution_code || record.archive_code || "未命名执行";
export const shortCode = (record: Archive) => { const code = record.execution_code || record.archive_code; return code.length > 24 ? `${code.slice(0, 12)}…${code.slice(-6)}` : code; };
export function archiveRange(record: Archive): CurveViewport {
  const range = record.manifest?.time_range;
  const start = range && Number.isFinite(range.start) ? range.start : 0;
  const end = range && Number.isFinite(range.end) ? range.end : Number(record.facts.active_seconds) || 1;
  return { start, end: end > start ? end : start + 1 };
}
export function archiveStart(record: Archive) {
  const raw = record.facts.started_at ?? record.current_verification?.declared_started_at;
  const value = raw ? new Date(String(raw)).getTime() : NaN;
  return Number.isFinite(value) ? value : null;
}
export function defaultConfig(record: Archive): WorkbenchConfig {
  const catalog = record.manifest?.catalog ?? [];
  const joint = catalog.find((entry) => /^(pos_rad|position_rad|actual_position_rad)$/.test(entry.metric))?.subject ?? catalog[0]?.subject ?? "";
  const groups = [["pos_rad", "position_rad", "actual_position_rad"], ["tracking_error_rad"], ["torque_nm"], ["temp_c", "temperature_c"]];
  const units = ["rad", "rad", "Nm", "°C"];
  const panels = groups.map((names, index): PanelConfig => {
    const found = catalog.find((entry) => names.includes(entry.metric));
    const subjects = catalog.filter((entry) => entry.metric === found?.metric && entry.unit === found.unit).map((entry) => entry.subject);
    return { id: `panel-${index + 1}`, metric: found?.metric ?? names[0], unit: found?.unit ?? units[index], subjects: [subjects.includes(joint) ? joint : subjects[0] ?? joint], targets: index === 0, limits: true, layout: "overlay", references: [] };
  });
  return { version: 1, base: record.id, comparisons: [], excluded: [], panels, viewport: null, stage: "", cycle: "", alignment: "elapsed", maxPoints: 4000, cursors: { a: null, b: null } };
}
// Parse a bounded, allowlisted configuration. No archive responses, credentials,
// permission flags, arbitrary URLs, or raw samples are accepted or persisted.
export function parseConfig(raw: string): WorkbenchConfig | null {
  if (raw.length > VIEW_LIMIT) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!plain(value) || !exact(value, ["version", "base", "comparisons", "excluded", "panels", "viewport", "stage", "cycle", "alignment", "maxPoints", "cursors"]) || value.version !== 1 || typeof value.base !== "string" || !ID.test(value.base)) return null;
    if (!Array.isArray(value.comparisons) || value.comparisons.length > 7 || !value.comparisons.every((id) => typeof id === "string" && ID.test(id) && id !== value.base) || new Set(value.comparisons).size !== value.comparisons.length) return null;
    if (!Array.isArray(value.excluded) || value.excluded.length > 8 || !value.excluded.every((id) => id === value.base || (value.comparisons as string[]).includes(id)) || new Set(value.excluded).size !== value.excluded.length) return null;
    if (!["elapsed", "absolute"].includes(String(value.alignment)) || ![1000, 4000, 10000, 20000].includes(Number(value.maxPoints)) || typeof value.maxPoints !== "number" || !text(value.stage, 80) || !text(value.cycle, 80)) return null;
    const absolute = value.alignment === "absolute";
    if (value.viewport !== null && (!plain(value.viewport) || !exact(value.viewport, ["start", "end"]) || !time(value.viewport.start, absolute) || !time(value.viewport.end, absolute) || value.viewport.start >= value.viewport.end)) return null;
    if (!plain(value.cursors) || !exact(value.cursors, ["a", "b"]) || ![value.cursors.a, value.cursors.b].every((cursor) => cursor === null || time(cursor, absolute))) return null;
    if (!Array.isArray(value.panels) || !value.panels.length || value.panels.length > 8) return null;
    for (const panel of value.panels) {
      if (!plain(panel) || !exact(panel, ["id", "metric", "unit", "subjects", "layout", "targets", "limits", "references"]) || typeof panel.id !== "string" || !ID.test(panel.id) || !text(panel.metric, 101) || !panel.metric.trim() || !text(panel.unit, 32) || !["overlay", "facets"].includes(String(panel.layout)) || typeof panel.targets !== "boolean" || typeof panel.limits !== "boolean") return null;
      if (!Array.isArray(panel.subjects) || panel.subjects.length > (panel.targets ? 16 : 32) || !panel.subjects.every((subject) => text(subject, 100)) || new Set(panel.subjects).size !== panel.subjects.length) return null;
      if (!Array.isArray(panel.references) || panel.references.length > 8 || !panel.references.every((line) => plain(line) && exact(line, ["id", "name", "value", "temporary"]) && typeof line.id === "string" && ID.test(line.id) && text(line.name, 80) && !!line.name.trim() && typeof line.value === "number" && Number.isFinite(line.value) && line.temporary === true)) return null;
    }
    if (new Set(value.panels.map((panel) => panel.id)).size !== value.panels.length) return null;
    return value as WorkbenchConfig;
  } catch { return null; }
}
export function readUrlConfig(base: string) {
  const raw = new URLSearchParams(window.location.search).get(VIEW_PARAM);
  if (!raw) return { config: null, error: "" };
  const config = parseConfig(raw);
  return config && config.base === base ? { config, error: "" } : { config: null, error: "分享视图无效或与当前执行不匹配，已使用默认面板。" };
}
export function shareUrl(config: WorkbenchConfig) {
  const raw = JSON.stringify(config);
  if (!parseConfig(raw)) throw new Error("当前视图配置无效，无法分享。");
  const url = new URL(window.location.href);
  url.searchParams.set("view", "curves");
  url.searchParams.set("id", config.base);
  url.searchParams.delete("curve_mode");
  url.searchParams.set(VIEW_PARAM, raw);
  return url.toString();
}
export function storageKey(userId: string) { return `rp1:curve-workbench:v1:${encodeURIComponent(userId)}`; }
export function loadViews(userId: string): SavedView[] {
  if (!userId) return [];
  try {
    const raw = localStorage.getItem(storageKey(userId));
    if (!raw || raw.length > VIEW_LIMIT * 20) return [];
    const value: unknown = JSON.parse(raw);
    if (!Array.isArray(value) || value.length > 20) return [];
    return value.flatMap((entry) => {
      if (!plain(entry) || !exact(entry, ["name", "config"]) || !text(entry.name, 60) || !entry.name.trim()) return [];
      const config = parseConfig(JSON.stringify(entry.config));
      return config ? [{ name: entry.name, config }] : [];
    });
  } catch { return []; }
}
export function saveViews(userId: string, views: SavedView[]) {
  if (!userId) throw new Error("当前账号身份不可用，无法保存本机视图。");
  if (views.length > 20 || views.some((view) => !text(view.name, 60) || !view.name.trim() || !parseConfig(JSON.stringify(view.config)))) throw new Error("视图数量或内容超出限制。");
  localStorage.setItem(storageKey(userId), JSON.stringify(views));
}
