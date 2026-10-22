// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import BenchOverviewPage from "./BenchOverviewPage";
import { getBenchPresence, getRegisteredStations, type RegisteredStation } from "../benchOverview";
import { setupApi } from "../setupApi";

const identity = vi.hoisted(() => ({ id: "administrator", role: "SYSTEM_ADMIN" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: identity } }) }));
vi.mock("../benchOverview", async (original) => ({ ...await original<typeof import("../benchOverview")>(), getRegisteredStations: vi.fn(), getBenchPresence: vi.fn() }));
let cache: QueryClient;
const station: RegisteredStation = { id: "one", code: "A01", name: "已有台架", station_type: "MODULE", enabled: true, available: true, site_code: "SITE", site_name: "测试中心", site_enabled: true, site_location: "", lab_code: "LAB", lab_name: "模块实验室", lab_enabled: true };
function mount() {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={cache}><BenchOverviewPage navigate={vi.fn()} /></QueryClientProvider>);
}
function fillForm() {
  const form = screen.getByLabelText("台架编号").closest("form")!;
  const fields = within(form);
  for (const [label, value] of Object.entries({ 站点编号: "SITE", 站点名称: "测试中心", 实验室编号: "LAB", 实验室名称: "模块实验室", 台架编号: "A02", 台架名称: "新增测试台架", 台架类型: "MODULE", 建档或配置理由: "隔离页面验证" })) fireEvent.change(fields.getByLabelText(label), { target: { value } });
  return form;
}
beforeEach(() => {
  identity.role = "SYSTEM_ADMIN";
  vi.mocked(getRegisteredStations).mockResolvedValue([station]);
  vi.mocked(getBenchPresence).mockResolvedValue({ items: [], as_of_at: new Date().toISOString() });
});
afterEach(() => { cleanup(); cache?.clear(); vi.restoreAllMocks(); vi.clearAllMocks(); });

it("creates from the overview and refreshes the full list after successful save", async () => {
  const create = vi.spyOn(setupApi, "create").mockImplementation(async () => {
    vi.mocked(getRegisteredStations).mockResolvedValue([station, { ...station, id: "two", code: "A02", name: "新增测试台架" }]);
    return { id: "two" };
  });
  mount();
  await screen.findByText("已有台架");
  fireEvent.change(screen.getByLabelText("搜索台架"), { target: { value: "已有" } });
  fireEvent.click(screen.getByRole("button", { name: "新建台架" }));
  fireEvent.submit(fillForm());
  await screen.findByText("新增测试台架");
  expect(create).toHaveBeenCalledWith("stations", expect.objectContaining({ station_code: "A02", name: "新增测试台架", station_type: "MODULE", reason: "隔离页面验证" }),expect.any(String),"POST");
  expect(screen.queryByLabelText("台架编号")).toBeNull();
  expect((screen.getByLabelText("搜索台架") as HTMLInputElement).value).toBe("");
  expect(getRegisteredStations).toHaveBeenCalledTimes(2);
});

it("preserves the entered form and reports a conflict without inventing a new station", async () => {
  vi.spyOn(setupApi, "create").mockRejectedValue(new Error("台架编号已存在"));
  mount();
  await screen.findByText("已有台架");
  fireEvent.click(screen.getByRole("button", { name: "新建台架" }));
  fireEvent.submit(fillForm());
  expect((await screen.findByRole("alert")).textContent).toBe("台架编号已存在");
  expect((screen.getByLabelText("台架编号") as HTMLInputElement).value).toBe("A02");
  expect(screen.queryByText("保存成功。")).toBeNull();
  expect(getRegisteredStations).toHaveBeenCalledTimes(1);
});

it("keeps creation unavailable to viewers", async () => {
  identity.role = "VIEWER";
  mount();
  await screen.findByText("已有台架");
  expect(screen.queryByRole("button", { name: "新建台架" })).toBeNull();
  expect(screen.queryByLabelText("台架编号")).toBeNull();
});
