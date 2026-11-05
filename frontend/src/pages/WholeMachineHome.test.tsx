// @vitest-environment jsdom
// Synthetic HTTP contracts only; no station, PLC or robot is contacted.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import WholeMachineHome from "./WholeMachineHome";
import { robotFactoryApi, type FactoryRobot } from "../robotFactoryApi";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "fixture-reader", role: "VIEWER" } } }) }));
vi.mock("./HomePage", () => ({ HomePage: ({ assetKind }: { assetKind: string }) => <div data-testid="original-home">{assetKind}</div> }));
const clients: QueryClient[] = [];
const robot = (source_id: string, online: boolean, serial_number: string): FactoryRobot => ({ source_id, source_kind: "real", robot_id: "SAME-ROBOT", serial_number, model: "RP1", station_name: source_id, online, last_seen_at: "2026-11-05T00:00:00Z", cases: [{ case_code: "SYS-001", test_name: "基础自检", record_count: 1, latest_status: "通过" }] });
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client);
  const onRecords = vi.fn();
  render(<QueryClientProvider client={client}><WholeMachineHome onRecords={onRecords} navigate={vi.fn()} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "工厂测试" }));
  return onRecords;
}
beforeEach(() => { vi.spyOn(robotFactoryApi, "robots").mockResolvedValue({ items: [robot("PC-01", true, "SN-A"), robot("PC-02", false, "SN-B")], as_of_at: "2026-11-05T00:00:01Z", offline_after_seconds: 45 }); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); });

it("keeps same robot IDs from different sources separate and navigates with the exact source", async () => {
  const onRecords = mount();
  const cards = await screen.findAllByRole("article", { name: "机器人 SAME-ROBOT" });
  expect(cards).toHaveLength(2);
  expect(within(cards[0]).getByText("在线")).toBeTruthy();
  expect(within(cards[1]).getByText("离线")).toBeTruthy();
  fireEvent.click(within(cards[1]).getByRole("button", { name: "查看测试记录" }));
  expect(onRecords).toHaveBeenCalledWith("SAME-ROBOT", "PC-02");
  expect(within(cards[1]).getByText("SYS-001")).toBeTruthy();
  expect(within(cards[1]).getByText("基础自检")).toBeTruthy();
  expect(screen.queryByText(/温度|观测时长|更新时间/)).toBeNull();
  expect(cards[0].querySelector("img")?.getAttribute("src")).toBe("/assets/rp1-waving-robot.webp");
});

it("filters offline history and searches identity without discarding its performed cases", async () => {
  mount(); await screen.findAllByRole("article");
  fireEvent.click(within(screen.getByRole("group", { name: "机器人连接状态" })).getByRole("button", { name: "离线" }));
  expect(screen.getAllByRole("article")).toHaveLength(1);
  expect(screen.getByText("SN-B")).toBeTruthy();
  expect(screen.getByText("基础自检")).toBeTruthy();
  fireEvent.change(screen.getByRole("searchbox", { name: "搜索机器人" }), { target: { value: "PC-01" } });
  expect(screen.queryByRole("article")).toBeNull();
  fireEvent.click(within(screen.getByRole("group", { name: "机器人连接状态" })).getByRole("button", { name: "全部" }));
  expect(screen.getByText("SN-A")).toBeTruthy();
});

it("removes online claims after a failed refresh and preserves historical cases", async () => {
  mount(); await screen.findAllByRole("article");
  vi.mocked(robotFactoryApi.robots).mockRejectedValueOnce(new Error("source unreachable"));
  fireEvent.click(screen.getByRole("button", { name: "刷新" }));
  await screen.findByRole("alert");
  for (const card of screen.getAllByRole("article")) {
    expect(within(card).queryByText("在线")).toBeNull();
    expect(within(card).getByText("离线")).toBeTruthy();
    expect(within(card).getByText("基础自检")).toBeTruthy();
  }
});

it("keeps the original system scope screen without pretending to bind it to one robot", async () => {
  mount(); await screen.findAllByRole("article");
  fireEvent.click(screen.getByRole("button", { name: "研发测试" }));
  expect(screen.getByTestId("original-home").textContent).toBe("WHOLE_MACHINE");
  expect(screen.getByText(/按已关联的测试范围汇总/)).toBeTruthy();
  await waitFor(() => expect(screen.queryByRole("article")).toBeNull());
});

it("defaults to research and does not fetch factory data for the research dashboard", () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client);
  render(<QueryClientProvider client={client}><WholeMachineHome navigate={vi.fn()} onRecords={vi.fn()} /></QueryClientProvider>);
  expect(screen.getByTestId("original-home").textContent).toBe("WHOLE_MACHINE");
  expect(robotFactoryApi.robots).not.toHaveBeenCalled();
  expect(screen.queryByRole("article")).toBeNull();
});

it("labels truncated lists as a bounded page rather than a fleet total", async () => {
  vi.mocked(robotFactoryApi.robots).mockResolvedValue({ items: [robot("PC-01", false, "SN-A")], as_of_at: "2026-11-05T00:00:01Z", offline_after_seconds: 45, truncated: true });
  mount();
  expect((await screen.findByRole("status")).textContent).toContain("搜索和连接状态筛选只作用于本页");
  expect(screen.getByText("本页 1 台")).toBeTruthy();
});

it("marks an online simulated source clearly instead of presenting it as a real robot", async () => {
  vi.mocked(robotFactoryApi.robots).mockResolvedValue({ items: [{ ...robot("PC-MOCK", true, "SN-SIM"), source_kind: "simulated" }], as_of_at: "2026-11-05T00:00:01Z", offline_after_seconds: 45 });
  mount();
  const card = await screen.findByRole("article", { name: "机器人 SAME-ROBOT" });
  expect(within(card).getByText("模拟数据")).toBeTruthy();
  expect(within(card).getByText("在线")).toBeTruthy();
});
