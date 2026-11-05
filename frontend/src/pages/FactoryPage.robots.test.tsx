// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import FactoryPage from "./FactoryPage";
import { factoryApi } from "../factoryApi";
import { robotFactoryApi } from "../robotFactoryApi";
import { archivesApi } from "../archiveApi";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "fixture-viewer", role: "VIEWER" } } }) }));
const clients: QueryClient[] = [];
beforeEach(() => {
  vi.spyOn(robotFactoryApi, "list").mockResolvedValue({ items: [], next_cursor: null, as_of_at: "2026-11-05T00:00:00Z" });
  vi.spyOn(factoryApi, "monitor");
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [], next_cursor: null });
  vi.spyOn(archivesApi, "factoryStatistics").mockResolvedValue({ total_count: 0, pending_count: 0, approved_count: 0, passed_count: 0, failed_count: 0, blocked_count: 0, active_seconds: 0 });
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("No network in this test")));
});
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
it("opens a viewer's exact robot/source result page without mounting unrelated PLC or execution ledgers", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client);
  const navigate = vi.fn();
  render(<QueryClientProvider client={client}><FactoryPage robotId="same-robot" sourceId="PC-02" navigate={navigate} /></QueryClientProvider>);
  await screen.findByText(/暂无符合条件的整机工厂结果/);
  expect(robotFactoryApi.list).toHaveBeenCalledWith(expect.objectContaining({ robot_id: "same-robot", source_id: "PC-02" }), expect.any(AbortSignal));
  expect(factoryApi.monitor).not.toHaveBeenCalled();
  expect(archivesApi.list).not.toHaveBeenCalled();
  expect(fetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "上传测试记录" })).toBeNull();
  expect(screen.queryByRole("button", { name: "提交整机结果 CSV" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "查看全部工厂测试" }));
  expect(navigate).toHaveBeenCalledWith("factory", undefined, "FACTORY");
});

it("lets a viewer return to all factory results without mounting the restricted PLC monitor", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client);
  const navigate = vi.fn();
  const page = render(<QueryClientProvider client={client}><FactoryPage robotId="same-robot" sourceId="PC-02" navigate={navigate} /></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", { name: "查看全部工厂测试" }));
  page.rerender(<QueryClientProvider client={client}><FactoryPage navigate={navigate} /></QueryClientProvider>);
  await screen.findByText("暂无符合条件的测试记录");
  expect(factoryApi.monitor).not.toHaveBeenCalled();
  expect(archivesApi.list).toHaveBeenCalledWith(expect.objectContaining({ data_domain: "FACTORY" }), expect.any(AbortSignal));
  expect(fetch).not.toHaveBeenCalled();
});
