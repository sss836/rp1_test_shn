import { apiRequest } from "./client";
import { getHmiStations, type HmiStation } from "./hmiPresence";

export type RegisteredStation = {
  id: string;
  code: string;
  name: string;
  station_type: "WHOLE_MACHINE" | "MODULE" | "SHARED";
  enabled: boolean;
  available: boolean;
  site_code: string;
  site_name: string;
  site_location: string;
  site_enabled: boolean;
  lab_code: string;
  lab_name: string;
  lab_enabled: boolean;
};

type Page<T> = { items: T[]; next_cursor: string | null };

async function allPages<T>(read: (cursor: string | null) => Promise<Page<T>>) {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  do {
    const page = await read(cursor);
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && seen.has(cursor)) throw new Error("列表分页未推进，请重新刷新。");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

export function getRegisteredStations(signal?: AbortSignal) {
  return allPages<RegisteredStation>((cursor) => {
    const query = new URLSearchParams({ limit: "200" });
    if (cursor) query.set("cursor", cursor);
    return apiRequest<Page<RegisteredStation>>(`/api/v1/setup/stations?${query}`, { signal });
  });
}

export async function getBenchPresence(signal?: AbortSignal) {
  let asOf = "";
  const items = await allPages<HmiStation>(async (cursor) => {
    const page = await getHmiStations(cursor, signal);
    // The oldest page bounds freshness for the whole result.
    if (!asOf || page.as_of_at < asOf) asOf = page.as_of_at;
    return page;
  });
  return { items: [...new Map(items.map((item) => [item.installation_id, item])).values()], as_of_at: asOf };
}

export function benchCode(value: string) {
  return value.trim().toUpperCase();
}

export function associateStations(registry: RegisteredStation[], presence: HmiStation[]) {
  const counts = new Map<string, number>();
  for (const station of registry) counts.set(benchCode(station.code), (counts.get(benchCode(station.code)) ?? 0) + 1);
  const byCode = new Map<string, HmiStation[]>();
  const unmatched: HmiStation[] = [];
  for (const hmi of presence) {
    const code = benchCode(hmi.bench_id);
    if (!code || counts.get(code) !== 1) unmatched.push(hmi);
    else byCode.set(code, [...(byCode.get(code) ?? []), hmi]);
  }
  return { byCode, unmatched };
}

export function disableReason(station: RegisteredStation) {
  return [!station.enabled && "台架已停用", !station.lab_enabled && "实验室已停用", !station.site_enabled && "站点已停用"].filter(Boolean).join("、") || "已启用";
}

export function agePresence(station: HmiStation, asOf: string, elapsedMs: number): HmiStation {
  const reference = Date.parse(asOf) + Math.max(0, elapsedMs);
  const online = station.online && reference - Date.parse(station.last_seen_at) < 45_000;
  const fresh = online && station.snapshot_fresh && reference - Date.parse(station.snapshot_at) < 20_000;
  return { ...station, online, snapshot_fresh: fresh, state: !online ? "offline" : !fresh ? "unknown" : station.state };
}
