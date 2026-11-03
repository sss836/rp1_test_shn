// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
it("shows factory identity, reported progress and a distinct formal ledger boundary", () => {
 render(<FactoryMonitor navigate={vi.fn()} />);
 expect(screen.getByText("SN：000aZ · 左臂")).toBeTruthy();
 expect(screen.getByText("2.0 min · 3 循环")).toBeTruthy();
 expect(screen.getByText("目标进度：未上报")).toBeTruthy();
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
 fireEvent.click(screen.getByRole("button", { name: "ARC-FAIL · 000aZ" }));
 expect(navigate).toHaveBeenCalledWith("archive", "archive", "FACTORY");
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
