import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getDashboardHome } from "./api";
import { apiRequest, invalidateAuthenticationRequests, setUnauthorizedHandler } from "./client";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { resolve, promise };
}
const ok = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200 });
const unauthorized = () => new Response(JSON.stringify({ error: { code: "session_revoked", message: "会话失效" } }), { status: 401 });

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("document", { cookie: "" });
});
afterEach(() => {
  setUnauthorizedHandler(null);
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("optional API request deadline", () => {
  it("ends the homepage wait at 12 seconds even if fetch never settles", async () => {
    const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const result = getDashboardHome().catch((error: unknown) => error);
    const signal = fetchMock.mock.calls[0][1]?.signal as AbortSignal;
    await vi.advanceTimersByTimeAsync(11_999);
    expect(signal.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(await result).toMatchObject({ status: 504, code: "request_timeout", message: expect.stringContaining("12 秒") });
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(fetchMock.mock.calls[0][1]).not.toHaveProperty("timeoutMs");
  });

  it("uses the same budget for response headers and a permanently pending body", async () => {
    const headers = deferred<Response>();
    const text = vi.fn(() => new Promise<string>(() => {}));
    const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => headers.promise);
    vi.stubGlobal("fetch", fetchMock);
    const result = apiRequest("/api/v1/dashboard/home", { timeoutMs: 12_000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(8_000);
    headers.resolve({ ok: true, status: 200, text } as unknown as Response);
    await vi.advanceTimersByTimeAsync(0);
    expect(text).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(4_000);
    expect(await result).toMatchObject({ code: "request_timeout" });
    expect((fetchMock.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("preserves caller cancellation and removes its listener and deadline", async () => {
    const caller = new AbortController();
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetchMock);
    const result = apiRequest("/api/v1/dashboard/home", { signal: caller.signal, timeoutMs: 12_000 }).catch((error: unknown) => error);
    const reason = new DOMException("离开首页", "AbortError");
    caller.abort(reason);
    expect(await result).toBe(reason);
    expect((fetchMock.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true);
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not start a request when its caller already aborted", async () => {
    const caller = new AbortController();
    caller.abort();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiRequest("/api/v1/dashboard/home", { signal: caller.signal, timeoutMs: 12_000 })).rejects.toMatchObject({ name: "AbortError" });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cleans up the timer and caller listener after success without later aborting", async () => {
    const caller = new AbortController();
    const remove = vi.spyOn(caller.signal, "removeEventListener");
    const fetchMock = vi.fn().mockResolvedValue(ok({ loaded: true }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(apiRequest("/api/v1/example", { signal: caller.signal, timeoutMs: 12_000 })).resolves.toEqual({ loaded: true });
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(12_000);
    expect((fetchMock.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(false);
  });

  it("keeps other requests without an opt-in budget unchanged", async () => {
    const pending = deferred<Response>();
    const caller = new AbortController();
    const fetchMock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => pending.promise);
    vi.stubGlobal("fetch", fetchMock);
    const result = apiRequest("/api/v1/example", { signal: caller.signal });
    expect(fetchMock.mock.calls[0][1]?.signal).toBe(caller.signal);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(caller.signal.aborted).toBe(false);
    pending.resolve(ok({ loaded: true }));
    await expect(result).resolves.toEqual({ loaded: true });
  });

  it("cleans up after a transport failure and preserves the original error", async () => {
    const error = new TypeError("网络离线");
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(error));
    await expect(apiRequest("/api/v1/example", { timeoutMs: 12_000 })).rejects.toBe(error);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not invalidate auth when a timed-out transport later returns 401", async () => {
    const pending = deferred<Response>();
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => pending.promise));
    const result = apiRequest("/api/v1/dashboard/home", { timeoutMs: 12_000 }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(await result).toMatchObject({ code: "request_timeout" });
    pending.resolve(unauthorized());
    await vi.advanceTimersByTimeAsync(0);
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("still invalidates current session-related 401 responses within the budget", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(unauthorized()));
    await expect(apiRequest("/api/v1/dashboard/home", { timeoutMs: 12_000 })).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps stale-identity 401 protection when a request has a deadline", async () => {
    const pending = deferred<Response>();
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    vi.stubGlobal("fetch", vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(() => pending.promise));
    const result = apiRequest("/api/v1/dashboard/home", { timeoutMs: 12_000 }).catch((error: unknown) => error);
    invalidateAuthenticationRequests();
    pending.resolve(unauthorized());
    expect(await result).toMatchObject({ status: 401 });
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
