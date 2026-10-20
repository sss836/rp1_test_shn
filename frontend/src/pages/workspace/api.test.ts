// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import { workspaceApi } from "./api";
const filters = { metrics: "j1_torque_nm", start: 2143, end: 2147, stage: "load", cycle: "2" };
afterEach(() => { vi.unstubAllGlobals(); });
it("downloads the same authenticated inclusive raw slice without parsing or filling CSV blanks", async () => {
  const csv = "time_s [s],j1_torque_nm [Nm]\n2143,\n2147,2.5\n";
  const fetchMock = vi.fn().mockResolvedValue(new Response(csv, { headers: { "Content-Type": "text/csv; charset=utf-8" } }));
  vi.stubGlobal("fetch", fetchMock);
  const result = await workspaceApi.exportCsv("archive/id", filters);
  expect(await result.text()).toBe(csv);
  const [url, options] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/v1/archives/archive%2Fid/export.csv?metrics=j1_torque_nm&start=2143&end=2147&stage=load&cycle=2");
  expect(options.credentials).toBe("include"); expect(options.headers.get("X-Request-Id")).toBeTruthy();
});
it("surfaces the server export bound failure instead of downloading JSON as CSV", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: "export_too_large", message: "请缩小区间" } }), { status: 413, headers: { "Content-Type": "application/json" } })));
  await expect(workspaceApi.exportCsv("archive", filters)).rejects.toMatchObject({ status: 413, code: "export_too_large", message: "请缩小区间" });
});
it("rejects an unexpected login document rather than claiming export success", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>login</html>", { headers: { "Content-Type": "text/html" } })));
  await expect(workspaceApi.exportCsv("archive", filters)).rejects.toThrow("服务未返回 CSV");
});
