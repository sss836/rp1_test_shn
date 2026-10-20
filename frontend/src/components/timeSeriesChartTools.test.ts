// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import type { EChartsType } from "echarts/core";
import { chartPngDataUrl } from "./timeSeriesChartTools";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function imageStub() {
  const loaded: string[] = [];
  vi.stubGlobal("Image", class {
    onload: (() => void) | null = null;
    set src(value: string) { loaded.push(value); queueMicrotask(() => this.onload?.()); }
  });
  return loaded;
}

it("captures the rendered ECharts PNG and adds identifying metadata and visible series keys", async () => {
  const loaded = imageStub();
  const context = { font: "", fillStyle: "", strokeStyle: "", lineWidth: 0, measureText: (value: string) => ({ width: value.length * 6 }), scale: vi.fn(), fillRect: vi.fn(), fillText: vi.fn(), drawImage: vi.fn(), setLineDash: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as never);
  const toDataUrl = vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue("data:image/png;base64,encoded-export");
  const chart = { getDataURL: vi.fn(() => "data:image/png;base64,actual-chart-canvas"), getWidth: () => 800, getHeight: () => 300 };
  const result = await chartPngDataUrl(chart as unknown as EChartsType, { title: "力矩 · 关节 1", range: "相对测试开始 / s：2143 – 2147 · 单位：N·m", resolution: "原始分辨率 · 4,001 点", cursors: "A 2144 · B 2146 · Δt 2 s", series: [{ name: "执行 A", unit: "N·m", color: "#237ce5", dash: "dashed" }] });
  expect(chart.getDataURL).toHaveBeenCalledExactlyOnceWith({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
  expect(loaded).toEqual(["data:image/png;base64,actual-chart-canvas"]);
  expect(context.drawImage).toHaveBeenCalledTimes(1);
  const text = context.fillText.mock.calls.map((call) => call[0]).join("\n");
  expect(text).toContain("力矩 · 关节 1");
  expect(text).toContain("2143 – 2147");
  expect(text).toContain("原始分辨率 · 4,001 点");
  expect(text).toContain("Δt 2 s");
  expect(text).toContain("执行 A · N·m");
  expect(context.setLineDash).toHaveBeenCalledWith([6, 3]);
  expect(toDataUrl).toHaveBeenCalledExactlyOnceWith("image/png");
  expect(result).toBe("data:image/png;base64,encoded-export");
});

it("rejects export when no canvas context is available", async () => {
  imageStub();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  const chart = { getDataURL: () => "data:image/png;base64,actual-chart-canvas", getWidth: () => 800, getHeight: () => 300 };
  await expect(chartPngDataUrl(chart as unknown as EChartsType, { title: "图表", range: "0–1 s", resolution: "原始", series: [] })).rejects.toThrow("无法导出 PNG");
});
