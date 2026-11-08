// @vitest-environment jsdom
// Synthetic 17-column result and mock HTTP clients only.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RobotFactoryResults } from "./RobotFactoryResults";
import { robotFactoryApi, type RobotResultDetail } from "../robotFactoryApi";
import { ApiError } from "../client";

const identity = vi.hoisted(() => ({ role: "TEST_EXECUTOR" }));
vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "fixture-user", role: identity.role } } }) }));
const clients: QueryClient[] = [];
function record(overrides: Partial<RobotResultDetail> = {}): RobotResultDetail {
  return { id: "result-a", source_id: "PC-01", robot_id: "RP1-A", serial_number: "001aZ", model: "24轴", filename: "result.csv", size_bytes: 100, sha256: "a".repeat(64), status: "PENDING", revision: 1, source_kind: "real", verified: true, completed: true, reason: "人工核实", owner_name: "提交人甲", created_at: "2026-11-05T00:00:00Z", can_review: true, formal_statistics_eligible: false, columns: ["记录编号", "上位机编号", "样品编号", "机型", "测试体系", "测试分类", "记录层级", "测试项目编号", "原始检查ID", "测试内容", "开始时间", "结束时间", "测试人员", "实际结果", "通过标准", "是否通过", "备注"], rows: [{ record_id: "00001", version: 1, values: ["00001", "PC-01", "001aZ", "24轴", "SYS", "基础检查", "项目", "SYS-001", "", "基础自检", "", "", "执行员", "001.23000000000000000009", "原标准", "通过", "人工核实"], case_code: "SYS-001", test_name: "基础自检", level: "项目", verdict: "通过" }], review_history: [], ...overrides };
}
function mount(initialArchiveId?: string, retry: boolean | number = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry, gcTime: 0 } } }); clients.push(client);
  render(<QueryClientProvider client={client}><RobotFactoryResults robotId="RP1-A" sourceId="PC-01" initialArchiveId={initialArchiveId} /></QueryClientProvider>);
}
beforeEach(() => {
  identity.role = "TEST_EXECUTOR";
  vi.spyOn(robotFactoryApi, "list").mockResolvedValue({ items: [record()], next_cursor: null, as_of_at: "2026-11-05T00:00:00Z" });
  vi.spyOn(robotFactoryApi, "detail").mockResolvedValue(record());
  vi.spyOn(robotFactoryApi, "review").mockResolvedValue(record({ status: "APPROVED", revision: 2, can_review: false }));
  vi.spyOn(robotFactoryApi, "upload").mockImplementation(async (_file, metadata) => ({ ...record({ id: "11111111-1111-4111-8111-111111111111" }), ...metadata }));
  vi.stubGlobal("crypto", { randomUUID: () => "11111111-1111-4111-8111-111111111111", subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).buffer) } });
});

it("opens the exact factory archive linked by the native app without approving it automatically", async () => {
  mount("result-a");
  await screen.findByRole("region", { name: "整机结果详情" });
  await screen.findByText("001.23000000000000000009");
  expect(robotFactoryApi.detail).toHaveBeenCalledWith("result-a", expect.any(AbortSignal));
  expect(robotFactoryApi.review).not.toHaveBeenCalled();
  expect(robotFactoryApi.upload).not.toHaveBeenCalled();
});
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("filters by exact source and robot while preserving all original 17 cell strings", async () => {
  mount(); fireEvent.click(await screen.findByRole("button", { name: "查看整机结果" }));
  await screen.findByText("001.23000000000000000009");
  expect(robotFactoryApi.list).toHaveBeenCalledWith(expect.objectContaining({ source_id: "PC-01", robot_id: "RP1-A" }), expect.any(AbortSignal));
  const table = screen.getByRole("region", { name: "整机结果详情" }).querySelector(".robot-result-values")!;
  expect(table.querySelectorAll("th")).toHaveLength(17);
  expect(table.querySelectorAll("tbody td")).toHaveLength(17);
  expect(table.querySelector("tbody td")?.textContent).toBe("00001");
  expect(table.querySelectorAll("tbody td")[2].textContent).toBe("001aZ");
  expect(screen.getByRole("link", { name: "下载原始结果 CSV" }).getAttribute("href")).toBe("/api/v1/factory/robot-result-archives/result-a/file");
  expect(robotFactoryApi.upload).not.toHaveBeenCalled();
});

it("requires both completion and human verification before explicit upload; no automatic review", async () => {
  mount(); fireEvent.click(screen.getByRole("button", { name: "提交整机结果 CSV" }));
  fireEvent.change(screen.getByLabelText("标准结果 CSV"), { target: { files: [new File(["synthetic 17 columns"], "result.csv", { type: "text/csv" })] } });
  fireEvent.change(screen.getByLabelText("核实及提交说明"), { target: { value: "已核实证据和SN" } });
  const submit = screen.getByRole("button", { name: "明确提交，送交审核" }) as HTMLButtonElement;
  expect(submit.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("已确认文件中的测试已结束"));
  expect(submit.disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("已核实 SN、机型、工位、项目、结果与原始证据，并明确提交本文件"));
  expect(submit.disabled).toBe(false);
  fireEvent.submit(submit.closest("form")!);
  await waitFor(() => expect(robotFactoryApi.upload).toHaveBeenCalledOnce());
  expect(vi.mocked(robotFactoryApi.upload).mock.calls[0][1]).toMatchObject({ source_id: "PC-01", robot_id: "RP1-A", verified: true, completed: true, source_kind: "real" });
  expect(robotFactoryApi.review).not.toHaveBeenCalled();
  expect((await screen.findByRole("status")).textContent).toContain("待审核");
});

it("retains the exact frozen upload and idempotency key after a lost receipt", async () => {
  vi.mocked(robotFactoryApi.upload).mockRejectedValueOnce(new ApiError("receipt lost", 503));
  mount(); fireEvent.click(screen.getByRole("button", { name: "提交整机结果 CSV" }));
  fireEvent.change(screen.getByLabelText("标准结果 CSV"), { target: { files: [new File(["synthetic"], "result.csv")] } });
  fireEvent.change(screen.getByLabelText("核实及提交说明"), { target: { value: "人工核实" } });
  fireEvent.click(screen.getByLabelText("已确认文件中的测试已结束"));
  fireEvent.click(screen.getByLabelText("已核实 SN、机型、工位、项目、结果与原始证据，并明确提交本文件"));
  fireEvent.submit(screen.getByRole("button", { name: "明确提交，送交审核" }).closest("form")!);
  const retry = await screen.findByRole("button", { name: "重试同一提交" });
  expect((screen.getByLabelText("原上位机编号") as HTMLInputElement).disabled).toBe(true);
  fireEvent.submit(retry.closest("form")!);
  await waitFor(() => expect(robotFactoryApi.upload).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(robotFactoryApi.upload).mock.calls;
  expect(calls[1].slice(0, 3)).toEqual(calls[0].slice(0, 3));
});

it("gates independent review on backend can_review and requires a reason", async () => {
  identity.role = "FACTORY_REVIEWER"; mount();
  expect(screen.queryByRole("button", { name: "提交整机结果 CSV" })).toBeNull();
  fireEvent.click(await screen.findByRole("button", { name: "查看整机结果" }));
  const approve = await screen.findByRole("button", { name: "批准整机工厂汇总" }) as HTMLButtonElement;
  expect(approve.disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("审核意见"), { target: { value: "按原证据核实" } });
  fireEvent.click(approve);
  await waitFor(() => expect(robotFactoryApi.review).toHaveBeenCalledWith("result-a", 1, "APPROVE", "按原证据核实", expect.any(String)));
  expect(robotFactoryApi.upload).not.toHaveBeenCalled();
});

it.each([false, undefined])("does not display review buttons without backend permission %s", async can_review => {
  vi.mocked(robotFactoryApi.detail).mockResolvedValue(record({ can_review }));
  mount(); fireEvent.click(await screen.findByRole("button", { name: "查看整机结果" }));
  await screen.findByText("当前账号没有本记录的审核权限。");
  expect(screen.queryByRole("button", { name: "批准整机工厂汇总" })).toBeNull();
});

it("does not approve simulated records, while allowing an authorized rejection", async () => {
  vi.mocked(robotFactoryApi.detail).mockResolvedValue(record({ source_kind: "simulated" }));
  mount(); fireEvent.click(await screen.findByRole("button", { name: "查看整机结果" }));
  fireEvent.change(await screen.findByLabelText("审核意见"), { target: { value: "模拟数据不能作正式记录" } });
  expect((screen.getByRole("button", { name: "批准整机工厂汇总" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "驳回整机工厂汇总" }));
  await waitFor(() => expect(robotFactoryApi.review).toHaveBeenCalledWith("result-a", 1, "REJECT", "模拟数据不能作正式记录", expect.any(String)));
  expect(within(screen.getByRole("region", { name: "整机结果详情" })).getByText(/不形成研发 MTBF 暴露资格/)).toBeTruthy();
});

it("retains frozen bytes and the original key when a returned receipt SHA does not match", async () => {
  vi.mocked(robotFactoryApi.upload).mockImplementationOnce(async (_file, metadata) => ({ ...record({ id: "11111111-1111-4111-8111-111111111111" }), ...metadata, sha256: "f".repeat(64) }));
  mount(); fireEvent.click(screen.getByRole("button", { name: "提交整机结果 CSV" }));
  fireEvent.change(screen.getByLabelText("标准结果 CSV"), { target: { files: [new File(["original synthetic CSV"], "result.csv")] } });
  fireEvent.change(screen.getByLabelText("核实及提交说明"), { target: { value: "已核实原件" } });
  fireEvent.click(screen.getByLabelText("已确认文件中的测试已结束"));
  fireEvent.click(screen.getByLabelText("已核实 SN、机型、工位、项目、结果与原始证据，并明确提交本文件"));
  fireEvent.submit(screen.getByRole("button", { name: "明确提交，送交审核" }).closest("form")!);
  expect((await screen.findByRole("alert")).textContent).toContain("归档回执与本次冻结提交不匹配");
  expect(screen.queryByRole("region", { name: "整机结果详情" })).toBeNull();
  expect((screen.getByLabelText("标准结果 CSV") as HTMLInputElement).disabled).toBe(true);
  fireEvent.submit(screen.getByRole("button", { name: "重试同一提交" }).closest("form")!);
  await waitFor(() => expect(robotFactoryApi.upload).toHaveBeenCalledTimes(2));
  expect(vi.mocked(robotFactoryApi.upload).mock.calls[1].slice(0, 3)).toEqual(vi.mocked(robotFactoryApi.upload).mock.calls[0].slice(0, 3));
});

it("separates the first approval state revision from original record content versions", async () => {
  mount(); fireEvent.click(await screen.findByRole("button", { name: "查看整机结果" }));
  fireEvent.change(await screen.findByLabelText("审核意见"), { target: { value: "核实原始结果" } });
  fireEvent.click(screen.getByRole("button", { name: "批准整机工厂汇总" }));
  await screen.findByText(/审核状态版本 2/);
  expect(screen.queryByText(/第 2 版/)).toBeNull();
  const evidence = screen.getByText("原文件与校验信息").closest("details")!;
  fireEvent.click(within(evidence).getByText("原文件与校验信息"));
  expect(within(evidence).getByText("00001")).toBeTruthy();
  expect(within(evidence).getByText("内容版本 1")).toBeTruthy();
  expect(screen.getByText("001.23000000000000000009")).toBeTruthy();
});


it.each(["list", "detail"] as const)("shows the first %s failure without inheriting the global retry", async target => {
  vi.mocked(robotFactoryApi[target]).mockRejectedValue(new Error("整机读取暂不可用"));
  mount(target === "detail" ? "result-a" : undefined, 1);
  const region = target === "detail" ? within(screen.getByRole("region", { name: "整机结果详情" })) : screen;
  expect((await region.findByRole("alert")).textContent).toContain("整机读取暂不可用");
  expect(robotFactoryApi[target]).toHaveBeenCalledOnce();
  expect(region.getByRole("button", { name: "重新加载" })).toBeTruthy();
});

it("labels a failed cached detail as history, suspends review and recovers only after an explicit successful read", async () => {
  mount("result-a");
  const detail = within(screen.getByRole("region", { name: "整机结果详情" }));
  await detail.findByRole("button", { name: "批准整机工厂汇总" });
  fireEvent.change(detail.getByLabelText("审核意见"), { target: { value: "已核实" } });
  vi.mocked(robotFactoryApi.detail).mockRejectedValueOnce(new Error("中央服务暂不可用"));
  await act(async () => { await clients[clients.length - 1].refetchQueries({ queryKey: ["robot-result-detail"] }); });
  expect((await detail.findByRole("alert")).textContent).toContain("中央服务暂不可用");
  expect(detail.getByRole("alert").textContent).toContain("历史快照，审批暂不可用");
  expect(detail.getByText("001.23000000000000000009")).toBeTruthy();
  expect(detail.queryByText("当前账号没有本记录的审核权限。")).toBeNull();
  expect(detail.queryByRole("button", { name: "批准整机工厂汇总" })).toBeNull();
  expect(robotFactoryApi.review).not.toHaveBeenCalled();
  let finish!: (value: RobotResultDetail) => void;
  vi.mocked(robotFactoryApi.detail).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  fireEvent.click(detail.getByRole("button", { name: "重新读取详情" }));
  await waitFor(() => expect(robotFactoryApi.detail).toHaveBeenCalledTimes(3));
  expect(detail.queryByRole("button", { name: "批准整机工厂汇总" })).toBeNull();
  expect(detail.queryByText("当前账号没有本记录的审核权限。")).toBeNull();
  await act(async () => { finish(record({ revision: 2 })); });
  expect((await detail.findByRole("button", { name: "批准整机工厂汇总" })).getAttribute("disabled")).toBeNull();
  expect(detail.queryByRole("alert")).toBeNull();
  expect(detail.getByText(/审核状态版本 2/)).toBeTruthy();
  expect(robotFactoryApi.review).not.toHaveBeenCalled();
});
