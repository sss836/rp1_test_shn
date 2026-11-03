// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FactoryMonitor } from "./FactoryMonitor";
import { benchState, runReceipt, type FactoryBench, type FactoryMonitorData } from "../factoryApi";
const auth = vi.hoisted(() => ({ role: "FACTORY_REVIEWER" }));
const query = vi.hoisted(() => ({ data: undefined as unknown, dataUpdatedAt: 0, isError: false, isPending: false, isFetching: false, refetch: vi.fn() }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "factory-user", role: auth.role } } }) }));
vi.mock("@tanstack/react-query", () => ({ useQuery: () => query }));
const bench: FactoryBench = { station_id: "bench", bench_id: "A-01", name: "工厂台架", site_name: "工厂", lab_name: "产线", available: true, connections: [{ connection_id: "connection", installation_id: "installation", station_name: "上位机", bench_id: "A-01", operator_name: "operator", operator_display_name: "操作员甲", online: true, snapshot_at: "2026-11-03T05:00:00Z", last_seen_at: "2026-11-03T05:00:00Z", plc: { driver: "mock", communication_state: "LIVE", phase: "RUNNING", stale: false }, runs: [{ session_key: "left", state: "running", execution_uuid: "one-execution", test_id: "LOCAL-01", serial_number: "000aZ", test_case: "left_arm", active_seconds: 120, completed_cycles: 3 }] }] };
const data: FactoryMonitorData = { items: [bench], receipts: [], next_cursor: null, work_items: [], work_next_cursor: null, as_of_at: "2026-11-03T05:00:00Z", offline_after_seconds: 45, snapshot_stale_after_seconds: 20 };
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date(data.as_of_at)); query.data = structuredClone(data); query.dataUpdatedAt = Date.now(); query.isError = false; auth.role = "FACTORY_REVIEWER"; });
afterEach(() => { cleanup(); vi.useRealTimers(); });
it("shows compact factory identity and measured progress without inventing targets", () => {
 render(<FactoryMonitor navigate={vi.fn()} />);
 const main = document.querySelector(".factory-bench-main") as HTMLElement;
 expect(within(main).getByText("000aZ")).toBeTruthy();
 expect(within(main).getByText("左臂")).toBeTruthy();
 expect(within(main).getByText("2.0 min · 3 循环")).toBeTruthy();
 expect(screen.queryByText(/目标进度/)).toBeNull();
 expect(screen.queryByRole("progressbar")).toBeNull();
 expect(screen.getByText("模拟 PLC")).toBeTruthy();
 expect(screen.getByText(/现场计数为当前页/)).toBeTruthy();
});
it("does not keep a live green running state when requests hang or refresh is paused", () => {
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(document.querySelector(".factory-state")?.textContent).toBe("测试运行中");
 act(() => vi.advanceTimersByTime(16000));
 expect(document.querySelector(".factory-state")?.textContent).toBe("状态待确认");
 expect(screen.getByText(/当前显示历史快照/)).toBeTruthy();
 expect(screen.getByText(/最后上报内容/)).toBeTruthy();
});
it("distinguishes fresh heartbeat from stale test samples and respects strict factory conflict", () => {
 expect(benchState(bench, data, 21000, true)).toBe("unknown");
 expect(benchState(bench, data, 46000, true)).toBe("offline");
 expect(benchState({ ...bench, connections: [{ ...bench.connections[0], scope_conflict: true }] }, data, 0, true)).toBe("unknown");
 expect(benchState({ ...bench, connections: [] }, data, 0, true)).toBe("unreported");
});
it("never associates retests by SN and cannot claim a missing execution identity is awaiting upload", () => {
 const done = { ...bench.connections[0].runs[0], state: "completed" };
 expect(runReceipt(done, [])).toBe("awaiting_upload");
 expect(runReceipt({ ...done, execution_uuid: undefined }, [])).toBe("identity_unknown");
 expect(runReceipt(done, [{ archive_id: "archive", execution_uuid: "different-execution", archive_code: "ARC-OTHER", serial_number: "000aZ", bench_id: "A-01", test_name: "复测", processing_error: null, receipt_state: "archived", created_at: data.as_of_at }])).toBe("awaiting_upload");
});
it("opens pending approval in its own factory scope and shows parser failures", () => {
 query.data = { ...data, work_items: [{ archive_id: "archive", execution_uuid: "one-execution", archive_code: "ARC-FAIL", serial_number: "000aZ", bench_id: "A-01", test_name: "测试", processing_error: "时间列缺失", receipt_state: "parse_error", created_at: data.as_of_at }] };
 const navigate = vi.fn(); render(<FactoryMonitor navigate={navigate} />);
 expect(screen.getByText("时间列缺失")).toBeTruthy();
 fireEvent.click(screen.getByRole("button", { name: "查看 000aZ 的记录" }));
 expect(navigate).toHaveBeenCalledWith("archive", "archive", "FACTORY");
});

it("keeps historical identities in collapsed details and hides empty placeholder metrics", () => {
 const historicalId = "LOCAL_left_arm_261103000000_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
 query.data = { ...data, items: [{ ...bench, connections: [{ ...bench.connections[0], online: false, runs: [
   { session_key: "empty", state: "idle", test_case: "left_arm", active_seconds: 0, completed_cycles: 0 },
   { ...bench.connections[0].runs[0], state: "completed", test_id: historicalId },
 ] }] }] };
 render(<FactoryMonitor navigate={vi.fn()} />);
 const main = document.querySelector(".factory-bench-main") as HTMLElement;
 expect(main.textContent).toContain("当前无在线上报");
 expect(main.textContent).not.toContain(historicalId);
 expect(main.textContent).not.toContain("2.0 min");
 expect(screen.queryByText("0.0 min · 0 循环")).toBeNull();
 const fullId = screen.getByText(historicalId);
 const details = fullId.closest("details")!;
 expect(details.open).toBe(false);
 expect(details.querySelectorAll(".factory-run-detail")).toHaveLength(1);
 fireEvent.click(details.querySelector("summary")!);
 expect(details.open).toBe(true);
 expect(within(details).getByText(historicalId)).toBeTruthy();
});

it("uses the same case-insensitive exact UUID match for receipt state and record links", () => {
 const uuid = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA";
 query.data = { ...data,
   items: [{ ...bench, connections: [{ ...bench.connections[0], runs: [{ ...bench.connections[0].runs[0], execution_uuid: uuid, state: "completed" }] }] }],
   receipts: [{ archive_id: "exact-archive", execution_uuid: uuid.toLowerCase(), archive_code: "ARC-EXACT", serial_number: "000aZ", bench_id: "A-01", test_name: "本次复测", processing_error: null, receipt_state: "awaiting_review", created_at: data.as_of_at }] };
 const navigate = vi.fn();
 render(<FactoryMonitor navigate={navigate} />);
 fireEvent.click(screen.getByText("查看上报详情"));
 fireEvent.click(screen.getByRole("button", { name: "查看记录" }));
 expect(navigate).toHaveBeenCalledWith("archive", "exact-archive", "FACTORY");
 expect(screen.queryByText("已完成，平台未收到归档")).toBeNull();
});

it("never discloses conflicting research run information even in details", () => {
 query.data = { ...data, items: [{ ...bench, connections: [{ ...bench.connections[0], scope_conflict: true,
   runs: [{ ...bench.connections[0].runs[0], serial_number: "RESEARCH-PRIVATE-SN", test_id: "RESEARCH-PRIVATE-ID" }] }] }] };
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.queryByText(/RESEARCH-PRIVATE/)).toBeNull();
 fireEvent.click(screen.getByText("最近上报详情"));
 expect(screen.queryByText(/RESEARCH-PRIVATE/)).toBeNull();
 expect(screen.getByText("明确属于其他分区的测试信息已隐藏。")).toBeTruthy();
});

it("marks an anonymous completed run as identity unknown without manufacturing zero measurements", () => {
 query.data = { ...data, items: [{ ...bench, connections: [{ ...bench.connections[0], runs: [
   { session_key: "incomplete", state: "completed", active_seconds: 0, completed_cycles: 0 },
 ] }] }] };
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.getByText("执行身份未上报，待核对")).toBeTruthy();
 expect(screen.queryByText("0.0 min · 0 循环")).toBeNull();
 expect(screen.queryByText("已完成，平台未收到归档")).toBeNull();
});

it("does not offer monitoring controls to unprivileged roles", () => {
 auth.role = "VIEWER";
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.queryByRole("heading", { name: "现场台架" })).toBeNull();
 expect(screen.queryByRole("button", { name: "刷新" })).toBeNull();
});

it("clears current live summaries immediately when automatic refresh is paused", () => {
 render(<FactoryMonitor navigate={vi.fn()} />);
 fireEvent.click(screen.getByRole("checkbox", { name: "自动刷新" }));
 expect(document.querySelector(".factory-state")?.textContent).toBe("状态待确认");
 expect(document.querySelector(".factory-bench-main")?.textContent).not.toContain("2.0 min");
 expect(screen.getByText(/自动刷新已暂停/)).toBeTruthy();
});
it("shows a fault even while another session runs and supports state filtering", () => {
 query.data = { ...data, items: [{ ...bench, connections: [{ ...bench.connections[0], runs: [...bench.connections[0].runs, { ...bench.connections[0].runs[0], session_key: "right", state: "fault" }] }] }] };
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.getByText(/A-01 · 故障/)).toBeTruthy();
 fireEvent.change(screen.getByLabelText("台架状态"), { target: { value: "idle" } });
 expect(screen.getByText("没有符合条件的工厂台架")).toBeTruthy();
});

it("labels legacy factory-bench reports without claiming an unconfirmed run is waiting for upload", () => {
 query.data = { ...data, items: [{ ...bench, connections: [{ ...bench.connections[0], scope_unconfirmed: true, runs: [{ ...bench.connections[0].runs[0], state: "completed" }] }] }] };
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.getByText(/分区未上报，按已登记工厂台架展示/)).toBeTruthy();
 expect(screen.queryByText("已完成，平台未收到归档")).toBeNull();
});
