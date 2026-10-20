import { afterEach, describe, expect, it, vi } from "vitest";
import { agePresence, associateStations, disableReason, getBenchPresence, getRegisteredStations, type RegisteredStation } from "./benchOverview";
import { apiRequest } from "./client";
import { getHmiStations, type HmiStation } from "./hmiPresence";

vi.mock("./client", () => ({ apiRequest: vi.fn() }));
vi.mock("./hmiPresence", () => ({ getHmiStations: vi.fn() }));
afterEach(() => vi.resetAllMocks());

const station = { id: "a", code: "A01", name: "机械台架", enabled: true, lab_enabled: true, site_enabled: true } as RegisteredStation;
const hmi = { installation_id: "i", bench_id: " a01 ", online: true, snapshot_fresh: true, state: "running", last_seen_at: "2026-10-19T00:00:00Z", snapshot_at: "2026-10-19T00:00:00Z" } as HmiStation;

describe("bench registry and presence", () => {
  it("loads every registry page and preserves disabled benches", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({ items: [station], next_cursor: "page2" }).mockResolvedValueOnce({ items: [{ ...station, id: "b", enabled: false }], next_cursor: null });
    const controller = new AbortController();
    expect(await getRegisteredStations(controller.signal)).toHaveLength(2);
    expect(apiRequest).toHaveBeenLastCalledWith("/api/v1/setup/stations?limit=200&cursor=page2", { signal: controller.signal });
  });
  it("rejects partial presence reads and a repeated pagination cursor", async () => {
    vi.mocked(getHmiStations).mockResolvedValueOnce({ items: [hmi], next_cursor: "p", as_of_at: "2026-10-19T00:00:01Z" } as never).mockRejectedValueOnce(new Error("offline"));
    await expect(getBenchPresence()).rejects.toThrow("offline");
    vi.mocked(apiRequest).mockResolvedValue({ items: [], next_cursor: "same" });
    await expect(getRegisteredStations()).rejects.toThrow("列表分页未推进");
  });
  it("loads presence beyond its first 100 items and deduplicates installations", async () => {
    vi.mocked(getHmiStations).mockResolvedValueOnce({ items: [hmi], next_cursor: "p", as_of_at: "2026-10-19T00:00:01Z" } as never).mockResolvedValueOnce({ items: [hmi, { ...hmi, installation_id: "second" }], next_cursor: null, as_of_at: "2026-10-19T00:00:03Z" } as never);
    const result = await getBenchPresence();
    expect(result.items).toHaveLength(2);
    expect(result.as_of_at).toBe("2026-10-19T00:00:01Z");
  });
  it("matches codes only and preserves every HMI sharing a bench", () => {
    const result = associateStations([station], [hmi, { ...hmi, installation_id: "other" }, { ...hmi, installation_id: "blank", bench_id: "" }]);
    expect(result.byCode.get("A01")).toHaveLength(2);
    expect(result.unmatched.map((item) => item.installation_id)).toEqual(["blank"]);
    expect(associateStations([station, { ...station, id: "duplicate", code: "a01" }], [hmi]).unmatched).toEqual([hmi]);
  });
  it("explains disabled parents and ages snapshots using server time", () => {
    expect(disableReason({ ...station, site_enabled: false })).toBe("站点已停用");
    expect(disableReason({ ...station, lab_enabled: false, enabled: false })).toBe("台架已停用、实验室已停用");
    expect(agePresence(hmi, "2026-10-19T00:00:10Z", 11_000).state).toBe("unknown");
    expect(agePresence(hmi, "2026-10-19T00:00:10Z", 36_000).state).toBe("offline");
  });
});
