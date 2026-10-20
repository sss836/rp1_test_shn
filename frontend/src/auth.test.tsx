// @vitest-environment jsdom
import { StrictMode, useEffect } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider, useAuth } from "./auth";
import { apiRequest } from "./client";
import type { Session } from "./security";

vi.mock("./pages/HomePage", () => ({ HomePage: () => <h1>受保护的业务首页</h1> }));
const sessionA: Session = {
  user: { id: "reader-a", username: "reader-a", display_name: "测试用户 A", role: "VIEWER", kind: "HUMAN", enabled: true, must_change_password: false },
  campaign_access: [], session_type: "BROWSER", expires_at: "2026-10-16T23:00:00Z"
};
const sessionB: Session = { ...sessionA, user: { ...sessionA.user, id: "reader-b", username: "reader-b", display_name: "测试用户 B" } };
const ok = (data: unknown) => new Response(JSON.stringify({ data }), { status: 200 });
const failure = (status: number, code: string, message: string) => new Response(JSON.stringify({ error: { code, message } }), { status });
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { resolve, reject, promise }; }
let latest: ReturnType<typeof useAuth>;
let cache: QueryClient;
function Probe() { const value = useAuth(); useEffect(() => { latest = value; }, [value]); return <App />; }
function mount(strict = false) {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  cache.setQueryData(["previous-private-data"], "old-cache");
  const tree = <QueryClientProvider client={cache}><AuthProvider><Probe /></AuthProvider></QueryClientProvider>;
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree);
}
function openMenu() { fireEvent.click(screen.getByRole("button", { name: /测试用户 A.*查看者/ })); }
function defaultFetch() {
  const mock = vi.fn<(url: RequestInfo | URL, init?: RequestInit) => Promise<Response>>().mockImplementation(async (url) => {
    if (String(url) === "/api/v1/auth/me") return ok(sessionA);
    throw new Error(`Unexpected API path: ${String(url)}`);
  });
  vi.stubGlobal("fetch", mock);
  return mock;
}
beforeEach(() => {
  window.history.replaceState({}, "", "?view=account&kind=module");
  window.localStorage.setItem("rp1-theme", "day");
  document.cookie = "rp1_csrf=synthetic-test-csrf; path=/";
});
afterEach(() => { cleanup(); cache?.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(["network", "503", "malformed-envelope", "malformed-session"])("keeps startup %s failures behind a retry gate and recovers the existing session without logging in", async (kind) => {
  const fetchMock = defaultFetch();
  if (kind === "network") fetchMock.mockRejectedValueOnce(new TypeError("连接中断"));
  else if (kind === "503") fetchMock.mockResolvedValueOnce(failure(503, "unavailable", "认证服务暂时不可用"));
  else if (kind === "malformed-envelope") fetchMock.mockResolvedValueOnce(new Response("<html>bad gateway</html>", { status: 200 }));
  else fetchMock.mockResolvedValueOnce(ok({ user: { id: "incomplete" } }));
  mount();
  await screen.findByRole("heading", { name: "暂时无法验证登录" });
  expect(screen.queryByRole("heading", { name: "账户设置" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "登录平台" })).toBeNull();
  expect(screen.queryByLabelText("用户名")).toBeNull();
  expect(latest.status).toBe("unavailable");
  fireEvent.click(screen.getByRole("button", { name: "重试验证登录" }));
  await screen.findByRole("heading", { name: "账户设置" });
  expect(latest.session?.user.id).toBe("reader-a");
  expect(latest.session?.expires_at).toBe(sessionA.expires_at);
  expect(cache.getQueryData(["previous-private-data"])).toBeUndefined();
  expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual(["/api/v1/auth/me", "/api/v1/auth/me"]);
  expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
});

it("clears identity and cached business data on an explicit expired-session 401", async () => {
  defaultFetch().mockResolvedValueOnce(failure(401, "session_expired", "会话已过期"));
  mount();
  await screen.findByRole("heading", { name: "登录平台" });
  expect(screen.queryByRole("heading", { name: "暂时无法验证登录" })).toBeNull();
  expect(latest.session).toBeNull();
  expect(cache.getQueryData(["previous-private-data"])).toBeUndefined();
});

it.each(["network", "503"])("shows persistent, retryable logout failure after %s and only clears the UI on confirmed logout", async (kind) => {
  const fetchMock = defaultFetch();
  mount();
  await screen.findByRole("heading", { name: "账户设置" });
  cache.setQueryData(["previous-private-data"], "authenticated-cache");
  if (kind === "network") fetchMock.mockRejectedValueOnce(new TypeError("网络离线"));
  else fetchMock.mockResolvedValueOnce(failure(503, "unavailable", "服务暂时不可用"));
  openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "退出" }));
  await screen.findByRole("button", { name: "重试退出" });
  expect(screen.getByRole("alert").textContent).toContain("退出未完成");
  expect(latest.status).toBe("authenticated");
  openMenu();
  expect(screen.queryByRole("menu")).toBeNull();
  expect(screen.getByRole("button", { name: "重试退出" })).toBeTruthy();
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  fireEvent.click(screen.getByRole("button", { name: "重试退出" }));
  await screen.findByRole("heading", { name: "登录平台" });
  expect(cache.getQueryData(["previous-private-data"])).toBeUndefined();
  expect(latest.logoutError).toBe("");
});

it("also keeps failed logout visible and retryable on the forced password-change screen", async () => {
  const fetchMock = defaultFetch().mockResolvedValueOnce(ok({ ...sessionA, user: { ...sessionA.user, must_change_password: true } }));
  mount();
  await screen.findByRole("heading", { name: "首次登录，请立即修改密码" });
  fetchMock.mockResolvedValueOnce(failure(503, "unavailable", "暂不可用"));
  fireEvent.click(screen.getByRole("button", { name: "退出当前账户" }));
  expect((await screen.findByRole("alert")).textContent).toContain("退出未完成");
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  fireEvent.click(screen.getByRole("button", { name: "重试退出" }));
  await screen.findByRole("heading", { name: "登录平台" });
});

it("does not restore a logged-out identity when an aborted me response arrives late", async () => {
  const fetchMock = defaultFetch();
  mount();
  await screen.findByRole("heading", { name: "账户设置" });
  const oldMe = deferred<Response>();
  fetchMock.mockImplementationOnce(() => oldMe.promise);
  let refresh!: Promise<void>;
  act(() => { refresh = latest.refetchMe(); });
  const signal = fetchMock.mock.calls.at(-1)?.[1]?.signal;
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await act(async () => { await latest.logout(); });
  expect(signal?.aborted).toBe(true);
  await screen.findByRole("heading", { name: "登录平台" });
  await act(async () => { oldMe.resolve(ok(sessionA)); await refresh; });
  expect(latest.status).toBe("anonymous");
  expect(latest.session).toBeNull();
});

it("does not clear the new identity when a previous identity's business request returns 401 late", async () => {
  const fetchMock = defaultFetch();
  mount();
  await screen.findByRole("heading", { name: "账户设置" });
  const oldRequest = deferred<Response>();
  fetchMock.mockImplementationOnce(() => oldRequest.promise);
  const pending = apiRequest("/api/v1/previous-user-data").catch((error: unknown) => error);
  fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
  await act(async () => { await latest.logout(); });
  fetchMock.mockResolvedValueOnce(ok({ user: sessionB.user, expires_at: sessionB.expires_at })).mockResolvedValueOnce(ok(sessionB));
  await act(async () => { await latest.login("reader-b", "synthetic-password"); });
  expect(latest.session?.user.id).toBe("reader-b");
  cache.setQueryData(["new-private-data"], "new-cache");
  await act(async () => { oldRequest.resolve(failure(401, "authentication_required", "旧会话失效")); await pending; });
  expect(latest.status).toBe("authenticated");
  expect(latest.session?.user.id).toBe("reader-b");
  expect(cache.getQueryData(["new-private-data"])).toBe("new-cache");
  const login = fetchMock.mock.calls.find(([url]) => String(url) === "/api/v1/auth/login");
  expect(JSON.parse(String(login?.[1]?.body))).toEqual({ username: "reader-b", password: "synthetic-password" });
});

it("ignores a canceled me 401 when a newer verification has already succeeded", async () => {
  const fetchMock = defaultFetch(); mount(); await screen.findByRole("heading", { name: "账户设置" });
  const oldMe = deferred<Response>(); fetchMock.mockImplementationOnce(() => oldMe.promise);
  let oldRefresh!: Promise<void>;
  act(() => { oldRefresh = latest.refetchMe(); });
  await act(async () => { await latest.refetchMe(); });
  await act(async () => { oldMe.resolve(failure(401, "authentication_required", "old")); await oldRefresh; });
  expect(latest.status).toBe("authenticated");
});

it("blocks overlapping login while logout is pending and accepts an already-invalid session as signed out", async () => {
  const fetchMock = defaultFetch(); mount(); await screen.findByRole("heading", { name: "账户设置" });
  const closing = deferred<Response>(); fetchMock.mockImplementationOnce(() => closing.promise);
  let logout!: Promise<void>; act(() => { logout = latest.logout(); });
  await expect(latest.login("reader-b", "synthetic-password")).rejects.toThrow("正在处理认证请求");
  expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/v1/auth/login")).toBe(false);
  await act(async () => { closing.resolve(failure(401, "session_revoked", "已撤销")); await logout; });
  expect(latest.status).toBe("anonymous"); expect(latest.logoutPending).toBe(false);
});

it("does not treat a rejected current password as an invalid browser session", async () => {
  const fetchMock = defaultFetch(); mount(); await screen.findByRole("heading", { name: "账户设置" });
  await waitFor(() => expect(latest.status).toBe("authenticated"));
  fetchMock.mockResolvedValueOnce(failure(401, "invalid_credentials", "当前密码错误"));
  await expect(latest.changePassword("wrong", "synthetic-new-password")).rejects.toMatchObject({ code: "invalid_credentials" });
  expect(latest.status).toBe("authenticated");
});

it("handles StrictMode effect cleanup without accepting an abandoned startup response", async () => {
  const first = deferred<Response>();
  defaultFetch().mockImplementationOnce(() => first.promise).mockResolvedValueOnce(ok(sessionB));
  mount(true);
  await screen.findByRole("heading", { name: "账户设置" });
  expect(latest.session?.user.id).toBe("reader-b");
  await act(async () => { first.resolve(ok(sessionA)); });
  expect(latest.session?.user.id).toBe("reader-b");
});
