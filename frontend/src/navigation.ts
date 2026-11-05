import type { ArchiveDataDomain } from "./archiveApi";
import type { AssetKind, TargetPartCode } from "./types";

export type View =
  | "catalog"
  | "planning"
  | "home"
  | "durations"
  | "mtbf"
  | "assets"
  | "live"
  | "stations"
  | "benches"
  | "submissions"
  | "archives"
  | "factory"
  | "import"
  | "archive"
  | "curves"
  | "asset"
  | "test-case"
  | "execution"
  | "account"
  | "access-request"
  | "admin";

export type LocationState = {
  view: View;
  kind: AssetKind;
  id: string | null;
  part: TargetPartCode | null;
  domain?: ArchiveDataDomain;
  source?: string;
  section?: "test" | "production" | "showcase";
};

const views = new Set<View>([
  "catalog",
  "planning",
  "home",
  "durations",
  "mtbf",
  "assets",
  "live",
  "stations",
  "benches",
  "submissions",
  "archives",
  "factory",
  "import",
  "archive",
  "curves",
  "asset",
  "test-case",
  "execution",
  "account",
  "access-request",
  "admin"
]);
const targetParts = new Set<TargetPartCode>([
  "ARM", "SARM", "LEG", "SLEG", "SYS", "UPPER", "LOWER", "WAISTHIP", "CHEST", "HEAD", "BAT"
]);

export function readLocation(search = window.location.search): LocationState {
  const params = new URLSearchParams(search);
  const candidate = params.get("view") as View | null;
  const requestedDomain = params.get("domain");
  const requestedPart = params.get("part") as TargetPartCode | null;
  return {
    view: candidate && views.has(candidate) ? candidate : "home",
    kind: params.get("kind") === "module" ? "MODULE" : "WHOLE_MACHINE",
    id: params.get("id"),
    ...(params.get("source") ? { source: params.get("source")! } : {}),
    part: requestedPart && targetParts.has(requestedPart) ? requestedPart : null,
    ...(["test","production","showcase"].includes(params.get("section")??"") ? {section: params.get("section") as "test"|"production"|"showcase"} : {}),
    ...(["RESEARCH", "FACTORY", "UNCLASSIFIED"].includes(requestedDomain ?? "") ? { domain: requestedDomain as ArchiveDataDomain } : {})
  };
}

export function locationHref(state: LocationState) {
  const params = new URLSearchParams({
    view: state.view,
    kind: state.kind === "MODULE" ? "module" : "whole"
  });
  if (state.section) params.set("section",state.section);
  if (state.id) params.set("id", state.id);
  if (state.source) params.set("source", state.source);
  if (state.domain) params.set("domain", state.domain);
  if (state.part) params.set("part", state.part);
  return `?${params.toString()}`;
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 3600) return `${Math.max(0, seconds / 60).toFixed(1)} min`;
  return `${Math.max(0, seconds / 3600).toFixed(2)} h`;
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "暂无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "无效时间";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false
  }).format(date);
}

export function isShowcaseValue(value: string | null | undefined) {
  return Boolean(value && /^(SHOWCASE|DEMO)-/.test(value));
}

export function productSection(state: LocationState){return state.section ?? (state.view==="factory"||state.domain==="FACTORY"?"production":"test");}
