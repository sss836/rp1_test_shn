import { afterEach, expect, it, vi } from "vitest";
import { setupApi } from "./setupApi";

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("sends a separate identity PATCH with the caller's stable key and only explicit fields", async () => {
  vi.stubGlobal("document", { cookie: "rp1_csrf=synthetic-csrf" });
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { id: "sample", serial_number: "000AbC" } }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  await setupApi.completeIdentity("sample/one", { serial_number: "000AbC", reason: "已核对铭牌" }, "frozen-identity-key");
  const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/v1/setup/assets/sample%2Fone/identity");
  expect(init.method).toBe("PATCH");
  expect(JSON.parse(init.body as string)).toEqual({ serial_number: "000AbC", reason: "已核对铭牌" });
  expect(new Headers(init.headers).get("Idempotency-Key")).toBe("frozen-identity-key");
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("synthetic-csrf");
  expect(init.credentials).toBe("include");
});

it("stops a stalled identity-options response body at its 12-second total deadline", async () => {
  vi.useFakeTimers(); vi.stubGlobal("document", { cookie: "" });
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, text: () => new Promise<string>(() => {}) } as Response);
  vi.stubGlobal("fetch", fetchMock);
  const result = setupApi.identityOptions().catch((error: unknown) => error);
  await vi.advanceTimersByTimeAsync(12_000);
  expect(await result).toMatchObject({ status: 504, code: "request_timeout" });
  expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
