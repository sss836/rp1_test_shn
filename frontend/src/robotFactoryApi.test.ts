import { afterEach, expect, it, vi } from "vitest";
import { robotFactoryApi, validateRobotResultReceipt, type RobotResultUpload } from "./robotFactoryApi";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });
it("uploads original bytes only to the dedicated factory route with explicit verification and stable identity", async () => {
  vi.stubGlobal("document", { cookie: "rp1_csrf=fixture-csrf" });
  const file = new File(["原始CSV\r\n"], "结果.csv", { type: "text/csv" });
  const metadata: RobotResultUpload = { source_id: "PC-01", robot_id: "RP1-01", source_kind: "real", filename: file.name, size_bytes: file.size, sha256: "a".repeat(64), verified: true, completed: true, reason: "已人工核实" };
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { ...metadata, id: "11111111-1111-4111-8111-111111111111", serial_number: "SN-01", status: "PENDING", revision: 1, formal_statistics_eligible: false } }), { status: 201 }));
  vi.stubGlobal("fetch", fetchMock);
  await robotFactoryApi.upload(file, metadata, "stable-key");
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  const parsed = new URL(url, "https://fixture.invalid");
  expect(parsed.pathname).toBe("/api/v1/factory/robot-result-archives");
  expect(parsed.searchParams.get("verified")).toBe("true");
  expect(parsed.searchParams.get("completed")).toBe("true");
  expect(parsed.searchParams.get("source_id")).toBe("PC-01");
  expect(parsed.searchParams.get("reason")).toBe("已人工核实");
  expect(init.body).toBe(file);
  expect(init.credentials).toBe("include");
  expect(new Headers(init.headers).get("Content-Type")).toBe("text/csv");
  expect(new Headers(init.headers).get("Idempotency-Key")).toBe("stable-key");
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("fixture-csrf");
});

it.each([
  { id: "invalid-uuid" }, { sha256: "b".repeat(64) }, { source_id: "PC-OTHER" },
  { robot_id: "OTHER" }, { size_bytes: 101 }, { source_kind: "simulated" },
  { status: "UNKNOWN" }, { status: ["PENDING"] }, { revision: 0 }, { revision: 1.5 },
  { formal_statistics_eligible: true }, { formal_statistics_eligible: undefined }
])("rejects an unconfirmed archive receipt with mismatched fields %o", changes => {
  const metadata: RobotResultUpload = { source_id: "PC-01", robot_id: "RP1-01", source_kind: "real", filename: "result.csv", size_bytes: 100, sha256: "a".repeat(64), verified: true, completed: true, reason: "已核实" };
  const receipt = { ...metadata, id: "11111111-1111-4111-8111-111111111111", serial_number: "SN-01", status: "PENDING", revision: 1, formal_statistics_eligible: false, ...changes };
  expect(() => validateRobotResultReceipt(receipt, metadata)).toThrow(/归档回执与本次冻结提交不匹配/);
});

it("sends revision-guarded review separately from upload", async () => {
  vi.stubGlobal("document", { cookie: "" });
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { id: "archive-a" } }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  await robotFactoryApi.review("archive-a", 3, "REJECT", "证据不完整", "review-key");
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/factory/robot-result-archives/archive-a/review");
  expect(JSON.parse(init.body as string)).toEqual({ expected_revision: 3, decision: "REJECT", reason: "证据不完整" });
  expect(new Headers(init.headers).get("Idempotency-Key")).toBe("review-key");
});


const reads = [
  ["robots", () => robotFactoryApi.robots()],
  ["list", () => robotFactoryApi.list({ source_id: "PC-01" })],
  ["detail", () => robotFactoryApi.detail("archive-a")]
] as const;
it.each(reads)("ends the %s read after one 12-second budget when fetch or body stalls", async (_name, read) => {
  vi.useFakeTimers();
  vi.stubGlobal("document", { cookie: "" });
  for (const stage of ["fetch", "body"] as const) {
    const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => stage === "fetch"
      ? new Promise<Response>(() => {})
      : Promise.resolve({ ok: true, status: 200, text: () => new Promise<string>(() => {}) } as Response));
    vi.stubGlobal("fetch", fetchMock);
    const result = read().catch((error: unknown) => error);
    const signal = fetchMock.mock.calls[0][1]?.signal as AbortSignal;
    await vi.advanceTimersByTimeAsync(11_999);
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toMatchObject({ status: 504, code: "request_timeout", message: expect.stringContaining("12 秒") });
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  }
});

it("keeps upload and review outside the factory read deadline", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("document", { cookie: "" });
  const waiting: Array<(response: Response) => void> = [];
  const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => new Promise<Response>(resolve => { waiting.push(resolve); }));
  vi.stubGlobal("fetch", fetchMock);
  const file = new File(["synthetic"], "result.csv");
  const metadata: RobotResultUpload = { source_id: "PC-01", robot_id: "RP1-01", source_kind: "real", filename: file.name, size_bytes: file.size, sha256: "a".repeat(64), verified: true, completed: true, reason: "已核实" };
  const upload = robotFactoryApi.upload(file, metadata, "stable-key");
  const review = robotFactoryApi.review("archive-a", 1, "REJECT", "核实意见", "review-key");
  await vi.advanceTimersByTimeAsync(60_000);
  expect(vi.getTimerCount()).toBe(0);
  for (const [, init] of fetchMock.mock.calls) expect(init?.signal?.aborted).not.toBe(true);
  waiting[0](new Response(JSON.stringify({ data: { ...metadata, id: "11111111-1111-4111-8111-111111111111", serial_number: "SN-01", status: "PENDING", revision: 1, formal_statistics_eligible: false } }), { status: 201 }));
  waiting[1](new Response(JSON.stringify({ data: { id: "archive-a", status: "REJECTED" } }), { status: 200 }));
  await expect(upload).resolves.toMatchObject({ status: "PENDING" });
  await expect(review).resolves.toMatchObject({ status: "REJECTED" });
});
