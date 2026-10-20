// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import BenchOverviewPage from "./BenchOverviewPage";
import type { RegisteredStation } from "../benchOverview";
import type { HmiStation } from "../hmiPresence";

const mocks = vi.hoisted(() => ({ role: "SYSTEM_ADMIN", registry: {} as Record<string, unknown>, presence: {} as Record<string, unknown> }));
vi.mock("@tanstack/react-query", () => ({ useQuery: ({ queryKey }: { queryKey: string[] }) => queryKey[0] === "bench-registry" ? mocks.registry : mocks.presence }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "reader", role: mocks.role } } }) }));

const station: RegisteredStation = { id: "one", code: "A01", name: "关节可靠性台架", station_type: "MODULE", available: true, enabled: true, lab_enabled: true, site_enabled: true, lab_code: "L1", lab_name: "可靠性实验室", site_code: "S1", site_name: "测试中心", site_location: "一层" };
const hmi: HmiStation = { installation_id: "hmi-one", connection_id: "connection", station_name: "电脑名称", bench_id: "A01", operator_name: "alice", operator_display_name: "操作员", online: true, snapshot_fresh: true, state: "running", connected_at: "2026-10-19T00:00:00Z", last_seen_at: "2026-10-19T00:00:00Z", snapshot_at: "2026-10-19T00:00:00Z", runs: [{ session_key: "left", state: "running", test_id: "LOCAL-1", execution_code: "REL-E1", case_code: "REL-ARM-002", sample_id: "RP1-ARM", test_case: "left_arm", active_seconds: 120, completed_cycles: 4 }], plc: { driver: "mock", communication_state: "LIVE", phase: "RUNNING", stale: false, powered_channels: [1] } };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-19T00:00:01Z"));
  mocks.role = "SYSTEM_ADMIN";
  mocks.registry = { data: [station, { ...station, id: "two", code: "B02", name: "未接入台架", station_type: "WHOLE_MACHINE", available: false, lab_enabled: false }], dataUpdatedAt: Date.now(), refetch: vi.fn() };
  mocks.presence = { data: { items: [hmi, { ...hmi, installation_id: "unbound", station_name: "未绑定电脑", bench_id: "" }], as_of_at: "2026-10-19T00:00:01Z" }, dataUpdatedAt: Date.now(), refetch: vi.fn() };
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

it("shows registered names, all types, disabled/unreported benches and unmatched HMIs", () => {
  render(<BenchOverviewPage navigate={vi.fn()} />);
  expect(screen.getByText("关节可靠性台架")).toBeTruthy();
  expect(screen.getByText("未接入台架")).toBeTruthy();
  expect(screen.getByText("实验室已停用")).toBeTruthy();
  expect(screen.getAllByText("无上报记录").length).toBeGreaterThan(0);
  expect(screen.getByRole("heading", { name: "未关联到唯一建档台架的上位机（1 台）" })).toBeTruthy();
  const row = screen.getByText("关节可靠性台架").closest("tr")!;
  expect(within(row).getAllByText("测试运行中").length).toBeGreaterThan(0);
  expect(within(row).getAllByText("REL-E1").length).toBeGreaterThan(0);
});

it("searches registered names and positions and filters disabled/type without hiding unmatched records", () => {
  render(<BenchOverviewPage navigate={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("搜索台架"), { target: { value: "关节" } });
  expect(screen.queryByText("未接入台架")).toBeNull();
  fireEvent.change(screen.getByLabelText("搜索台架"), { target: { value: "" } });
  fireEvent.change(screen.getByLabelText("台架状态"), { target: { value: "disabled" } });
  expect(screen.queryByText("关节可靠性台架")).toBeNull();
  expect(screen.getByText("未接入台架")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("台架类型"), { target: { value: "MODULE" } });
  expect(screen.getByText("没有符合条件的台架")).toBeTruthy();
});

it("does not infer device availability from another account's invisible presence", () => {
  mocks.role = "TEST_EXECUTOR";
  mocks.presence = { ...mocks.presence, data: { items: [], as_of_at: "2026-10-19T00:00:01Z" } };
  render(<BenchOverviewPage navigate={vi.fn()} />);
  expect(screen.getByText(/不代表台架离线或无人使用/)).toBeTruthy();
  const row = screen.getByText("关节可靠性台架").closest("tr")!;
  expect(within(row).getByText("无可见上报")).toBeTruthy();
  expect(within(row).queryByText("离线")).toBeNull();
});

it("retains the registry but removes live claims on request failure or a hung refresh", () => {
  const { rerender } = render(<BenchOverviewPage navigate={vi.fn()} />);
  act(() => vi.advanceTimersByTime(16_000));
  const row = screen.getByText("关节可靠性台架").closest("tr")!;
  expect(within(row).queryByText("测试运行中")).toBeNull();
  expect(within(row).getByText("状态待确认")).toBeTruthy();
  mocks.presence = { ...mocks.presence, isError: true };
  rerender(<BenchOverviewPage navigate={vi.fn()} />);
  expect(screen.getByRole("alert").textContent).toContain("上位机状态读取失败");
  expect(screen.getByText("未接入台架")).toBeTruthy();
});

it("labels paused refresh as historical and keeps disconnected running snapshots out of current tests", () => {
  mocks.presence = { ...mocks.presence, data: { items: [{ ...hmi, online: false, snapshot_fresh: false, state: "offline" }], as_of_at: "2026-10-19T00:00:01Z" } };
  render(<BenchOverviewPage navigate={vi.fn()} />);
  const row = screen.getByText("关节可靠性台架").closest("tr")!;
  expect(within(row).getAllByText("离线").length).toBeGreaterThan(0);
  expect(within(row).getByText("最后状态：运行中")).toBeTruthy();
  expect(within(row).queryByText("测试运行中")).toBeNull();
  fireEvent.click(screen.getByLabelText("每 5 秒刷新运行状态"));
  expect(screen.getByText(/自动刷新已暂停/)).toBeTruthy();
  expect(within(row).getByText("历史快照")).toBeTruthy();
});
