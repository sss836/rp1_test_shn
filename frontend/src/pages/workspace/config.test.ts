// @vitest-environment jsdom
import { beforeEach, expect, it } from "vitest";
import type { Archive } from "../../archiveApi";
import { defaultConfig, loadViews, parseConfig, readUrlConfig, saveViews, shareUrl, storageKey, VIEW_LIMIT } from "./config";
const record = { id: "archive-main", manifest: { catalog: [{ key: "joint_pos_rad", subject: "joint", metric: "pos_rad", unit: "rad" }] } } as Archive;
beforeEach(() => { localStorage.clear(); window.history.replaceState({}, "", "?view=curves&id=archive-main"); });
it("creates four useful panels on one joint and preserves a missing channel as missing", () => {
  const config = defaultConfig(record);
  expect(config.panels.map((panel) => panel.metric)).toEqual(["pos_rad", "tracking_error_rad", "torque_nm", "temp_c"]);
  expect(config.panels.every((panel) => panel.subjects[0] === "joint")).toBe(true);
  expect(config.panels[0].targets).toBe(true);
  expect(parseConfig(JSON.stringify(config))).toEqual(config);
});
it("roundtrips bounded config, selected execution visibility and Unicode labels through URL", () => {
  const config = defaultConfig(record);
  config.comparisons = ["archive-other"]; config.excluded = ["archive-main"];
  config.viewport = { start: 2143, end: 2147 };
  config.panels[2].references = [{ id: "temporary-1", name: "观察阈值", value: 2.5, temporary: true }];
  window.history.replaceState({}, "", "?view=curves&id=archive-main&curve_mode=single");
  window.history.replaceState({}, "", shareUrl(config));
  expect(readUrlConfig("archive-main").config).toEqual(config);
  expect(new URL(window.location.href).searchParams.get("view")).toBe("curves");
  expect(new URL(window.location.href).searchParams.has("curve_mode")).toBe(false);
  expect(readUrlConfig("archive-other").error).not.toBe("");
});
it.each([
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, version: 2 }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, viewport: { start: 2, end: 1 } }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, viewport: { start: 0, end: "NaN" } }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, viewport: { start: 0, end: 315360001 } }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, maxPoints: "4000" }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, comparisons: ["../../private"] }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, comparisons: Array(8).fill("other") }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, excluded: ["unknown"] }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, token: "never-store" }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, panels: [...config.panels, config.panels[0]] }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, panels: [{ ...config.panels[0], points: [[0, 1]] }] }),
  (config: ReturnType<typeof defaultConfig>) => ({ ...config, panels: [{ ...config.panels[0], subjects: Array(33).fill("joint") }] }),
])("rejects malformed, unbounded, and non-configuration payloads %#", (mutate) => {
  expect(parseConfig(JSON.stringify(mutate(defaultConfig(record))))).toBeNull();
});
it("rejects corrupt and oversized URL JSON", () => {
  expect(parseConfig("{")).toBeNull(); expect(parseConfig(" ".repeat(VIEW_LIMIT + 1))).toBeNull();
});
it("isolates browser views by stable user identity and stores only validated configuration", () => {
  const views = [{ name: "左肩负载对比", config: defaultConfig(record) }];
  saveViews("public-user-a", views);
  expect(loadViews("public-user-a")).toEqual(views);
  expect(loadViews("public-user-b")).toEqual([]);
  expect(() => saveViews("", views)).toThrow();
  localStorage.setItem(storageKey("public-user-a"), '{"unexpected":"secret"}');
  expect(loadViews("public-user-a")).toEqual([]);
});
