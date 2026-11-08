// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AssetIdentityPanel } from "./AssetIdentityPanel";
import { setupApi, type AssetIdentityPayload, type AssetIdentityReceipt, type SetupAsset, type SetupOptions } from "../setupApi";
import type { AssetSummary } from "../types";

const clients: QueryClient[] = [];
const base: AssetSummary = {
  id: "11111111-1111-4111-8111-111111111111", code: "RP1.3-SYS-001", name: "M1 · MTBF V1.1", asset_kind: "WHOLE_MACHINE",
  target_part_code: "SYS", target_part_name: "整机", product_family: "RP1.3", batch_code: null, serial_number: null,
  creation_scenario: "RND_TEST", lifecycle_status: "ACTIVE", current_test_case_code: null, current_execution_id: null,
  current_execution_code: null, health_status: null,
  durations: { total_duration_seconds: 0, effective_exposure_seconds: 0, excluded_seconds: 0, pending_seconds: 0, active_elapsed_seconds: 0 },
  freshness: { status: "NO_DATA", as_of_at: "2026-11-08T00:00:00Z", data_cutoff_at: null, last_received_at: null, lag_seconds: null }
};
function item(changes: Partial<SetupAsset> = {}): SetupAsset {
  return { id: base.id, code: base.code, name: base.name, asset_kind: "WHOLE_MACHINE", target_part_code: "SYS", creation_scenario: "RND_TEST", can_edit: true, serial_number: null, model: "24轴", can_complete_identity: true, blocked_reason: null, ...changes };
}
function options(asset: SetupAsset): SetupOptions { return { assets: [asset], configurations: [], parts: [], programs: [], stations: [] }; }
function receipt(payload: AssetIdentityPayload, key: string, changes: Partial<AssetIdentityReceipt> = {}): AssetIdentityReceipt {
  return { id: base.id, code: base.code, serial_number: payload.serial_number ?? null, model: payload.model ?? "24轴", identity_completed: true, request_key: key, replayed: false, ...changes };
}
function mount(cached?: SetupOptions) {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: 2, gcTime: 0 } } }); clients.push(cache);
  if (cached) cache.setQueryData(["setup-options"], cached);
  render(<QueryClientProvider client={cache}><AssetIdentityPanel asset={base} /></QueryClientProvider>);
  return cache;
}
beforeEach(() => { vi.spyOn(setupApi, "identityOptions").mockResolvedValue(options(item())); });
afterEach(() => { cleanup(); clients.splice(0).forEach(client => client.clear()); vi.restoreAllMocks(); });

it("shows the plan identity, requires a reason and only submits the missing SN", async () => {
  const patch = vi.spyOn(setupApi, "completeIdentity").mockImplementation(async (_id, payload, key) => receipt(payload, key));
  mount();
  await screen.findByText("计划档案 · 身份待补全");
  expect(screen.getByText(base.code)).toBeTruthy(); expect(screen.getByText(base.name)).toBeTruthy();
  expect(screen.getByText("待绑定")).toBeTruthy();
  const sn = screen.getByLabelText("实机 SN") as HTMLInputElement;
  const model = screen.getByLabelText("机型") as HTMLInputElement;
  expect(model.value).toBe("24轴"); expect(model.readOnly).toBe(true);
  fireEvent.change(sn, { target: { value: " 000AbC " } });
  fireEvent.submit(sn.closest("form")!);
  expect(patch).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText("核实理由"), { target: { value: " 已核对实机铭牌 " } });
  fireEvent.submit(sn.closest("form")!);
  await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
  expect(patch.mock.calls[0][0]).toBe(base.id);
  expect(patch.mock.calls[0][1]).toEqual({ serial_number: "000AbC", reason: "已核对实机铭牌" });
  expect(patch.mock.calls[0][2]).toMatch(/^[0-9a-f-]{36}$/);
});

it("freezes the payload and key across a lost acknowledgement and blocks concurrent submissions", async () => {
  let rejectFirst!: (error: Error) => void;
  const patch = vi.spyOn(setupApi, "completeIdentity").mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFirst = reject; }))
    .mockImplementation(async (_id, payload, key) => receipt(payload, key, { replayed: true }));
  mount(); const sn = await screen.findByLabelText("实机 SN");
  fireEvent.change(sn, { target: { value: "00001" } });
  fireEvent.change(screen.getByLabelText("核实理由"), { target: { value: "核对铭牌" } });
  fireEvent.submit(sn.closest("form")!); fireEvent.submit(sn.closest("form")!);
  await waitFor(() => expect(patch).toHaveBeenCalledTimes(1));
  expect((sn as HTMLInputElement).disabled).toBe(true);
  await act(async () => rejectFirst(new Error("回执连接中断")));
  await screen.findByText("回执连接中断");
  expect((sn as HTMLInputElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "重试本次保存" }));
  await waitFor(() => expect(patch).toHaveBeenCalledTimes(2));
  expect(patch.mock.calls[1]).toEqual(patch.mock.calls[0]);
});

it("acknowledges a valid write without waiting forever for the background catalogue refresh", async () => {
  const patch = vi.spyOn(setupApi, "completeIdentity").mockImplementation(async (_id, payload, key) => receipt(payload, key));
  mount(); const sn = await screen.findByLabelText("实机 SN");
  vi.mocked(setupApi.identityOptions).mockImplementation(() => new Promise(() => {}));
  fireEvent.change(sn, { target: { value: "001" } });
  fireEvent.change(screen.getByLabelText("核实理由"), { target: { value: "核对实机" } });
  fireEvent.submit(sn.closest("form")!);
  await screen.findByText("实机身份已补全。");
  expect(patch).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("正在保存…")).toBeNull();
});

it("uses server eligibility and explains an execution lock even for an editable asset", async () => {
  vi.mocked(setupApi.identityOptions).mockResolvedValue(options(item({ can_complete_identity: false, blocked_reason: "样品已关联执行，身份不能修改。" })));
  const patch = vi.spyOn(setupApi, "completeIdentity");
  mount(); await screen.findByText("样品已关联执行，身份不能修改。");
  expect(screen.queryByRole("button", { name: "核实并保存身份" })).toBeNull();
  expect(patch).not.toHaveBeenCalled();
});

it("fails closed when cached eligibility cannot be refreshed and recovers only from a fresh read", async () => {
  vi.mocked(setupApi.identityOptions).mockRejectedValue(new Error("身份查询暂不可用"));
  mount(options(item()));
  await screen.findByText("身份查询暂不可用");
  expect(setupApi.identityOptions).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("button", { name: "核实并保存身份" })).toBeNull();
  vi.mocked(setupApi.identityOptions).mockResolvedValue(options(item()));
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  await screen.findByRole("button", { name: "核实并保存身份" });
});

it("allows only a missing model to be supplied while a bound SN stays read-only", async () => {
  vi.mocked(setupApi.identityOptions).mockResolvedValue(options(item({ serial_number: "000M4", model: null })));
  const patch = vi.spyOn(setupApi, "completeIdentity").mockImplementation(async (_id, payload, key) => receipt(payload, key, { serial_number: "000M4" }));
  mount(); const sn = await screen.findByLabelText("实机 SN") as HTMLInputElement;
  expect(sn.readOnly).toBe(true); expect(sn.value).toBe("000M4");
  fireEvent.change(screen.getByLabelText("机型"), { target: { value: "28轴" } });
  fireEvent.change(screen.getByLabelText("核实理由"), { target: { value: "核对28自由度实机" } });
  fireEvent.submit(sn.closest("form")!);
  await waitFor(() => expect(patch).toHaveBeenCalled());
  expect(patch.mock.calls[0][1]).toEqual({ model: "28轴", reason: "核对28自由度实机" });
});

it("does not claim success for a receipt from another request", async () => {
  vi.spyOn(setupApi, "completeIdentity").mockImplementation(async (_id, payload, key) => receipt(payload, key, { request_key: "different-key" }));
  mount(); const sn = await screen.findByLabelText("实机 SN");
  fireEvent.change(sn, { target: { value: "001" } });
  fireEvent.change(screen.getByLabelText("核实理由"), { target: { value: "核对实机" } });
  fireEvent.submit(sn.closest("form")!);
  await screen.findByText(/身份回执与本次提交不匹配/);
  expect(screen.queryByText("实机身份已补全。")).toBeNull();
});
