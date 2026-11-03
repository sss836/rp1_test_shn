// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ArchivesPage from "./ArchivesPage";
import { archivesApi, type Archive } from "../archiveApi";

vi.mock("../auth", () => ({ useAuth: () => ({ session: { user: { id: "factory-layout-user", role: "FACTORY_REVIEWER" } } }) }));
vi.mock("../archiveApi", async () => {
  const actual = await vi.importActual<typeof import("../archiveApi")>("../archiveApi");
  return { ...actual, archivesApi: { ...actual.archivesApi, list: vi.fn(), factoryStatistics: vi.fn() } };
});

const publicId = "11111111-1111-4111-8111-111111111111";
const executionId = "22222222-2222-4222-8222-222222222222";
const longCode = "ARC-22222222222242228222222222222222";
const localCode = "LOCAL_REL-ARM-001_261103092633090589_22222222222242228222222222222222";
function record(extra: Partial<Archive> = {}): Archive {
  return { id: publicId, execution_uuid: executionId, archive_code: longCode, owner_public_id: "owner", is_owner: false, can_edit: false,
    data_domain: "FACTORY", data_domain_source: "EXPLICIT", serial_number: "001aZ", bench_id: "A8", origin: "HMI", test_name: "左短臂老化测试", actual_object: "左短臂",
    station_label: "A8", filename: "synthetic.csv", size_bytes: 200, sha256: "a".repeat(64), received_bytes: 0, upload_state: "UPLOADING", processing_state: "WAITING", processing_error: null,
    classification_state: "UNCLASSIFIED", revision: 1, facts: { started_at: "2026-11-03T09:26:00Z" }, acceptance: {}, manifest: null, current_verification: null, classification: null,
    submission_id: null, submission_revision: null, submission_status: null, execution_id: null, execution_code: localCode, created_at: "2026-11-03T09:30:00Z", ...extra };
}
const clients: QueryClient[] = [];
function mount(navigate = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(client);
  render(<QueryClientProvider client={client}><div className="factory-console"><ArchivesPage embedded domain="FACTORY" navigate={navigate} /></div></QueryClientProvider>);
  return navigate;
}
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [record()], next_cursor: null });
  vi.mocked(archivesApi.factoryStatistics).mockResolvedValue({ total_count: 2, pending_count: 0, approved_count: 0, passed_count: 0, failed_count: 0, blocked_count: 0, active_seconds: 0 });
});
afterEach(() => { cleanup(); clients.forEach(client => client.clear()); clients.length = 0; });

it("keeps long identifiers in details and opens the exact archive in factory scope", async () => {
  const navigate = mount();
  const name = await screen.findByRole("button", { name: "查看 001aZ 记录" });
  expect(screen.getByRole("heading", { name: "测试记录" })).toBeTruthy();
  expect(screen.getByText(longCode).closest("details")?.open).toBe(false);
  expect(screen.getByText(localCode).closest("details")?.open).toBe(false);
  expect(screen.getByText(executionId).closest("details")).toBeTruthy();
  expect(screen.getByText("上传未完成")).toBeTruthy();
  expect(screen.getByText("未送审")).toBeTruthy();
  expect(screen.queryByText("上传中")).toBeNull();
  expect(screen.queryByRole("button", { name: "批准工厂记录" })).toBeNull();
  expect((screen.getByRole("button", { name: "查看 001aZ 曲线" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(name);
  expect(navigate).toHaveBeenCalledWith("archive", publicId, "FACTORY");
});

it("preserves opaque SN, date filters, full-ledger export and independent statistics", async () => {
  vi.mocked(archivesApi.list).mockImplementation(async filters => ({ items: [record({ id: filters?.before ? "page-2" : publicId })], next_cursor: filters?.before ? null : 99 }));
  mount();
  await screen.findByRole("button", { name: "查看 001aZ 记录" });
  for (const [label, value] of [["SN", "000aZ"], ["台架编号", "A8"], ["开始日期（北京时间）", "2026-11-03"], ["结束日期（含当天）", "2026-11-03"]]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  const physical = { sn: "000aZ", bench: "A8", started_from: "2026-11-02T16:00:00.000Z", started_to: "2026-11-03T16:00:00.000Z" };
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith({ ...physical, data_domain: "FACTORY", submission_status: "", before: null }, expect.any(AbortSignal)));
  expect(archivesApi.factoryStatistics).toHaveBeenLastCalledWith(physical, expect.any(AbortSignal));
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith(expect.objectContaining({ before: 99 }), expect.any(AbortSignal)));
  fireEvent.change(screen.getByLabelText("台账范围"), { target: { value: "PENDING" } });
  await waitFor(() => expect(archivesApi.list).toHaveBeenLastCalledWith({ ...physical, data_domain: "FACTORY", submission_status: "PENDING", before: null }, expect.any(AbortSignal)));
  const href = screen.getByRole("button", { name: "导出台账 CSV" }).getAttribute("href")!;
  const params = new URLSearchParams(href.split("?")[1]);
  expect(params.get("sn")).toBe("000aZ"); expect(params.get("data_domain")).toBe("FACTORY"); expect(params.get("submission_status")).toBe("PENDING"); expect(params.has("before")).toBe(false);
  expect(archivesApi.factoryStatistics).toHaveBeenLastCalledWith(physical, expect.any(AbortSignal));
});

it("shows file and approval states separately and enables curves only after parsing", async () => {
  vi.mocked(archivesApi.list).mockResolvedValue({ items: [record({ upload_state: "COMPLETE", processing_state: "READY", submission_status: "PENDING" })], next_cursor: null });
  const navigate = mount();
  const name = await screen.findByRole("button", { name: "查看 001aZ 记录" });
  const row = name.closest("tr")!;
  expect(within(row).getByText("文件就绪")).toBeTruthy();
  expect(within(row).getByText("待审批")).toBeTruthy();
  expect(within(row).queryByText("已批准")).toBeNull();
  fireEvent.click(within(row).getByRole("button", { name: "查看 001aZ 曲线" }));
  expect(navigate).toHaveBeenCalledWith("curves", publicId, "FACTORY");
});
