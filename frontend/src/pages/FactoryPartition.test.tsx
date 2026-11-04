// @vitest-environment jsdom
// All records, identities and station options are synthetic. No network or PLC.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { archivesApi, type Archive, type FactoryStatistics } from "../archiveApi";
import { setupApi } from "../setupApi";
import ArchivesPage, { ArchiveDetailPage } from "./ArchivesPage";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "fixture-executor", role: "TEST_EXECUTOR" } } }) }));
const clients: QueryClient[] = [];
const statistics: FactoryStatistics = { total_count: 8, pending_count: 3, approved_count: 2, passed_count: 1, failed_count: 1, blocked_count: 0, active_seconds: 120 };
function archive(overrides: Partial<Archive> = {}): Archive {
  return { id: "factory-archive", execution_uuid: "factory-execution", archive_code: "ARC-F-001", data_domain: "FACTORY", data_domain_source: "EXPLICIT", serial_number: "001aZ", bench_id: "B-1", can_review: false, owner_public_id: "fixture-owner", is_owner: true, can_edit: true,
    origin: "HMI", test_name: "工厂合成试验", actual_object: "001aZ", station_label: "B-1", filename: "synthetic.csv", size_bytes: 10, sha256: "a".repeat(64), received_bytes: 10, upload_state: "COMPLETE", processing_state: "READY", processing_error: null, classification_state: "UNCLASSIFIED", revision: 2,
    facts: { started_at: "2026-10-29T00:00:00Z", ended_at: "2026-10-29T00:02:00Z", active_seconds: 999999, completed_cycles: 1, exception_count: 0, machine_status: "completed", automatic_verdict: "unconfigured" }, acceptance: {}, manifest: null,
    current_verification: { revision: 2, tester: "测试员", station_id: "station-1", station_code: "B-1", verdict: "passed", notes: "无", discrepancy_reason: "人工核实", created_at: "2026-10-29T00:02:00Z" }, classification: null, submission_id: null, submission_revision: null, submission_status: null, execution_id: null, execution_code: null, created_at: "2026-10-29T00:03:00Z", ...overrides };
}
function mount(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } }); clients.push(client);
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}
beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("Fixture prohibits network access")));
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [archive()], next_cursor: null });
  vi.spyOn(archivesApi, "factoryStatistics").mockResolvedValue(statistics);
  vi.spyOn(archivesApi, "detail").mockResolvedValue(archive());
  vi.spyOn(setupApi, "options").mockResolvedValue({ stations: [{ id: "station-1", code: "B-1", name: "合成台架", station_type: "RIG" }], assets: [], configurations: [], parts: [], programs: [] });
  vi.spyOn(setupApi, "campaigns").mockResolvedValue([]);
});
afterEach(() => { cleanup(); clients.splice(0).forEach((client) => client.clear()); expect(fetch).not.toHaveBeenCalled(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("defaults to one research partition and switches unknown evidence independently of missing formal cases", async () => {
  mount(<ArchivesPage navigate={vi.fn()} />);
  await screen.findByRole("button", { name: "ARC-F-001" });
  expect(archivesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ data_domain: "RESEARCH", before: null }), expect.any(AbortSignal));
  fireEvent.click(screen.getByRole("button", { name: "分区待归类" }));
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ data_domain: "UNCLASSIFIED", before: null }), expect.any(AbortSignal)));
  expect(screen.getByText(/分区待归类与缺少正式用例关联是两种不同状态/)).toBeTruthy();
  expect(archivesApi.factoryStatistics).not.toHaveBeenCalled();
});

it("keeps all factory filtering and pagination on the server and uses approved statistics without summing visible facts", async () => {
  vi.mocked(archivesApi.list).mockImplementation(async (filters) => ({ items: [archive({ id: filters?.before ? "page-2" : "page-1", archive_code: filters?.before ? "ARC-PAGE-2" : "ARC-PAGE-1" })], next_cursor: filters?.before ? null : 99 }));
  mount(<ArchivesPage domain="FACTORY" navigate={vi.fn()} />);
  await screen.findByText("已批准测试数");
  expect(screen.getByText("台账记录数").nextElementSibling?.textContent).toBe("8");
  expect(screen.getByText("已批准测试数").nextElementSibling?.textContent).toBe("2");
  expect(screen.getByText("正式运行时长").nextElementSibling?.textContent).toBe("2.0 min");
  for (const [label, value] of [["SN", "001aZ"], ["台架编号", "B-1"], ["开始日期（北京时间）", "2026-10-29"], ["结束日期（含当天）", "2026-10-29"]]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  const filters = { data_domain: "FACTORY", sn: "001aZ", bench: "B-1", started_from: "2026-10-28T16:00:00.000Z", started_to: "2026-10-29T16:00:00.000Z", submission_status: "" };
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith({ ...filters, before: null }, expect.any(AbortSignal)));
  expect(archivesApi.factoryStatistics).toHaveBeenLastCalledWith({ sn: "001aZ", bench: "B-1", started_from: filters.started_from, started_to: filters.started_to }, expect.any(AbortSignal));
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await screen.findByRole("button", { name: "ARC-PAGE-2" });
  expect(archivesApi.list).toHaveBeenLastCalledWith({ ...filters, before: 99 }, expect.any(AbortSignal));
  fireEvent.change(screen.getByLabelText("台账范围"), { target: { value: "PENDING" } });
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith({ ...filters, submission_status: "PENDING", before: null }, expect.any(AbortSignal)));
  expect(await screen.findByRole("heading", { name: "工厂 · 待审批台账" })).toBeTruthy();
});

it("streams the full pending ledger from the server without accumulating pages in the browser", async () => {
  vi.mocked(archivesApi.list).mockResolvedValue({items:[archive()],next_cursor:55});
  mount(<ArchivesPage domain="FACTORY" navigate={vi.fn()}/>);
  await screen.findByRole("button",{name:"导出标准记录 CSV"});
  fireEvent.change(screen.getByLabelText("台账范围"),{target:{value:"PENDING"}});
  await waitFor(()=>expect(screen.getByRole("button",{name:"导出标准记录 CSV"}).getAttribute("href")).toContain("submission_status=PENDING"));
  expect(screen.getByRole("button",{name:"导出标准记录 CSV"}).getAttribute("href")).toContain("data_domain=FACTORY");
  expect(archivesApi.list).not.toHaveBeenCalledWith(expect.objectContaining({before:55}),expect.any(AbortSignal));
});

it("requires explicit formal context for factory OTHER and never auto classifies or submits", async () => {
  const submit = vi.spyOn(archivesApi, "submit");
  const classify = vi.spyOn(archivesApi, "classify");
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  await screen.findByLabelText("提交说明");
  expect(screen.getByLabelText("测试计划")).toBeTruthy();
  expect(screen.getByText(/OTHER 可先查看曲线/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("提交说明"), { target: { value: "已核对本次SN和原始证据" } });
  expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(true);
  expect(submit).not.toHaveBeenCalled(); expect(classify).not.toHaveBeenCalled();
});

it("requires actual factory verification before submission and keeps station/SN evidence distinct", async () => {
  let current = archive({ current_verification: null });
  vi.mocked(archivesApi.detail).mockImplementation(async () => current);
  const verify = vi.spyOn(archivesApi, "verify").mockImplementation(async (_id, payload) => { current = { ...current, revision: 3, current_verification: { ...payload, revision: 3, station_code: "B-1", created_at: current.created_at } }; return current; });
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  await screen.findByLabelText("实际测试人");
  expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(true);
  for (const [label, value] of [["实际测试人", "测试员甲"], ["核实工位", "station-1"], ["人工核实结论", "passed"], ["测试员记录", "本次复测"], ["与自动结论不同的原因", "按实测结果核实"], ["核实说明", "核实真实工位与SN"]]) fireEvent.change(screen.getByLabelText(label), { target: { value } });
  fireEvent.click(screen.getByRole("button", { name: "保存核实版本" }));
  await waitFor(() => expect(verify).toHaveBeenCalledWith("factory-archive", expect.objectContaining({ expected_revision: 2, tester: "测试员甲", station_id: "station-1", verdict: "passed" }), expect.any(String)));
  expect(current.serial_number).toBe("001aZ"); expect(current.bench_id).toBe("B-1");
});

it("blocks factory submission when verified station differs from the execution bench", async () => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ bench_id: "A-OTHER" }));
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  await screen.findByText(/本次台架 A-OTHER 与核实工位 B-1 不一致/);
  fireEvent.change(screen.getByLabelText("提交说明"), { target: { value: "准备送审" } });
  expect((screen.getByRole("button", { name: "送审" }) as HTMLButtonElement).disabled).toBe(true);
});

it.each([false, undefined])("does not expose approval actions unless the server grants can_review=%s", async (can_review) => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ can_edit: false, submission_status: "PENDING", submission_revision: 1, can_review }));
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  await screen.findByText(/当前账号没有本记录的审批权限/);
  expect(screen.queryByRole("button", { name: "批准工厂记录" })).toBeNull(); expect(screen.queryByRole("button", { name: "驳回工厂记录" })).toBeNull();
});

it("submits an explicit factory review revision and retains its idempotency key after a failed response", async () => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ can_edit: false, submission_status: "PENDING", submission_revision: 4, can_review: true }));
  const review = vi.spyOn(archivesApi, "factoryReview").mockRejectedValueOnce(new Error("版本已变化，请重新核对")).mockResolvedValueOnce(archive({ submission_status: "REJECTED", submission_revision: 5 }));
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  const reason = await screen.findByLabelText("审批说明");
  fireEvent.change(reason, { target: { value: "核实结论需要补证" } });
  fireEvent.click(screen.getByRole("button", { name: "驳回工厂记录" }));
  await screen.findByText("版本已变化，请重新核对");
  fireEvent.click(screen.getByRole("button", { name: "驳回工厂记录" }));
  await waitFor(() => expect(review).toHaveBeenCalledTimes(2));
  expect(review.mock.calls[0][1]).toEqual({ expected_submission_revision: 4, decision: "REJECT", reason: "核实结论需要补证" });
  expect(review.mock.calls[0][2]).toBe(review.mock.calls[1][2]);
  expect(screen.queryByText("追加业务分区修正")).toBeNull();
});

it("resubmits rejected factory data with the current submission revision", async () => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ classification_state: "CLASSIFIED", submission_status: "REJECTED", submission_revision: 3 }));
  const submit = vi.spyOn(archivesApi, "submit").mockResolvedValue(archive({ submission_status: "PENDING", submission_revision: 4 }));
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText("提交说明"), { target: { value: "已追加核实证据" } });
  fireEvent.click(screen.getByRole("button", { name: "送审" }));
  await waitFor(() => expect(submit).toHaveBeenCalledWith("factory-archive", expect.objectContaining({ expected_submission_revision: 3 }), expect.any(String)));
});

it("appends unknown-domain evidence without normalizing the SN or conflating research classification", async () => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ data_domain: "UNCLASSIFIED", data_domain_source: "NO_EVIDENCE", serial_number: null, bench_id: null }));
  const classify = vi.spyOn(archivesApi, "classifyDomain").mockResolvedValue(archive());
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  fireEvent.change(await screen.findByLabelText("业务分区"), { target: { value: "FACTORY" } });
  fireEvent.change(screen.getByLabelText("本次工厂 SN"), { target: { value: "000aZ" } });
  fireEvent.change(screen.getByLabelText("本次台架编号"), { target: { value: "B-1" } });
  fireEvent.change(screen.getByLabelText("分区依据说明"), { target: { value: "核对历史执行单" } });
  fireEvent.click(screen.getByRole("button", { name: "保存分区版本" }));
  await waitFor(() => expect(classify).toHaveBeenCalledWith("factory-archive", { expected_revision: 2, data_domain: "FACTORY", serial_number: "000aZ", bench_id: "B-1", reason: "核对历史执行单" }, expect.any(String)));
});

it.each([["APPROVED", "批准"], ["REJECTED", "驳回"]] as const)("renders the backend review status %s as %s and retains evidence without edit controls", async (decision, label) => {
  vi.mocked(archivesApi.detail).mockResolvedValue(archive({ can_edit: false, submission_status: decision, domain_history: [{ revision: 2, data_domain: "FACTORY", data_domain_source: "CORRECTION", serial_number: "001aZ", bench_id: "B-1", evidence: { original_sha256: "evidence" }, reason: "有据更正", created_at: "2026-10-29T00:02:00Z", actor_name: "审核员" }], factory_review_history: [{ revision: 3, decision, reason: "原始证据完整", created_at: "2026-10-29T00:03:00Z", actor_name: "审核员" }] }));
  mount(<ArchiveDetailPage id="factory-archive" navigate={vi.fn()} />);
  const panel = (await screen.findByRole("heading", { name: "分区与工厂审批历史" })).closest("section")!;
  expect(within(panel).getByText(/分区第 2 版/)).toBeTruthy(); expect(within(panel).getByText(new RegExp(`工厂审批第 3 版 · ${label} ·`))).toBeTruthy();
  expect(screen.queryByLabelText("业务分区")).toBeNull(); expect(screen.queryByLabelText("提交说明")).toBeNull();
});
