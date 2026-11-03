import { afterEach, expect, it, vi } from "vitest";
import { archiveDateFilters, archiveDomain, archiveLedgerCsv, archivesApi, exportArchiveLedger, type Archive } from "./archiveApi";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
const row = (id: string): Archive => ({ id, execution_uuid: `uuid-${id}`, archive_code: id, execution_code: null, data_domain: "FACTORY", data_domain_source: "EXPLICIT", serial_number: "001aZ", bench_id: "B-1", test_name: "合成工厂测试", actual_object: "001aZ", station_label: "B-1", current_verification: null, verified_station_code: null, facts: { started_at: "2026-10-28T16:00:00Z", active_seconds: 10 }, submission_status: "PENDING", sha256: "a".repeat(64), owner_public_id: "fixture-owner", is_owner: true, can_edit: true, origin: "HMI", filename: "fixture.csv", size_bytes: 100, received_bytes: 100, upload_state: "COMPLETE", processing_state: "READY", processing_error: null, classification_state: "UNCLASSIFIED", revision: 1, acceptance: {}, manifest: null, classification: null, submission_id: null, submission_revision: null, execution_id: null, created_at: "2026-10-29T00:00:00Z" });

it("uses Beijing calendar dates with an exclusive next-day boundary", () => {
  expect(archiveDateFilters("2026-10-29", "2026-10-29")).toEqual({ started_from: "2026-10-28T16:00:00.000Z", started_to: "2026-10-29T16:00:00.000Z" });
  expect(archiveDateFilters("", "")).toEqual({});
  expect(() => archiveDateFilters("2026-10-30", "2026-10-29")).toThrow("结束日期不得早于");
  expect(() => archiveDateFilters("bad", "")).toThrow("有效日期");
  expect(archiveDomain({})).toBe("UNCLASSIFIED");
});

it("encodes authoritative partition, opaque SN, bench, status and time filters at the API boundary", async () => {
  vi.stubGlobal("document", { cookie: "" });
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: { items: [], next_cursor: null } }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const filters = { data_domain: "FACTORY" as const, sn: "001aZ+ X", bench: "B-1", submission_status: "PENDING", ...archiveDateFilters("2026-10-29", "2026-10-29") };
  await archivesApi.list(filters);
  const url = new URL(fetchMock.mock.calls[0][0], "https://fixture.invalid");
  for (const [key, value] of Object.entries(filters)) expect(url.searchParams.get(key)).toBe(value);
  expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
});

it("exports all filtered pages, preserves repeated opaque SN executions and labels pending data", async () => {
  const list = vi.spyOn(archivesApi, "list").mockResolvedValueOnce({ items: [row("ARC-1")], next_cursor: 42 }).mockResolvedValueOnce({ items: [row("ARC-2")], next_cursor: null });
  const filters = { data_domain: "FACTORY" as const, sn: "001aZ", bench: "B-1", submission_status: "PENDING", ...archiveDateFilters("2026-10-29", "2026-10-29") };
  const controller = new AbortController();
  const blob = await exportArchiveLedger(filters, controller.signal);
  expect(list).toHaveBeenNthCalledWith(1, { ...filters, before: null }, controller.signal);
  expect(list).toHaveBeenNthCalledWith(2, { ...filters, before: 42 }, controller.signal);
  const csv = await blob.text();
  expect(csv).toContain('"工厂"'); expect(csv).toContain('"001aZ"');
  expect(csv.match(/"001aZ"/g)).toHaveLength(4);
  expect(csv).toContain('"ARC-1"'); expect(csv).toContain('"ARC-2"'); expect(csv).toContain('"PENDING"');
});

it("stops a repeated export cursor and neutralizes formula-like text cells", async () => {
  vi.spyOn(archivesApi, "list").mockResolvedValue({ items: [row("ARC")], next_cursor: 7 });
  await expect(exportArchiveLedger({ data_domain: "FACTORY" })).rejects.toThrow("游标重复");
  expect(archiveLedgerCsv([{ ...row("ARC"), test_name: '=HYPERLINK("unsafe")' }])).toContain('"\'=HYPERLINK(""unsafe"")"');
});

it("uses the factory review version and authenticated idempotent mutation endpoint", async () => {
  vi.stubGlobal("document", { cookie: "rp1_csrf=csrf" });
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: row("ARC") }), { status: 200 }));
  vi.stubGlobal("fetch", fetchMock);
  const payload = { expected_submission_revision: 3, decision: "REJECT" as const, reason: "证据需补全" };
  await archivesApi.factoryReview("archive/id", payload, "stable-key");
  expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/archives/archive%2Fid/factory-review");
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  expect(JSON.parse(init.body as string)).toEqual(payload);
  expect(new Headers(init.headers).get("Idempotency-Key")).toBe("stable-key");
  expect(new Headers(init.headers).get("X-CSRF-Token")).toBe("csrf");
});
