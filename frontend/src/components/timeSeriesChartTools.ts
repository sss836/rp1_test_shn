import type { EChartsType } from "echarts/core";

// Link time coordinates only. echarts.connect also links zoom actions, which
// would couple unrelated Y scales across a multi-parameter workspace.
type CursorReceiver = (time: number | null) => void;
const cursorGroups = new Map<string, Set<CursorReceiver>>();
export function joinTimeCursorGroup(group: string, receive: CursorReceiver) {
  const members = cursorGroups.get(group) ?? new Set<CursorReceiver>();
  members.add(receive); cursorGroups.set(group, members);
  return {
    publish(time: number | null) { for (const member of members) if (member !== receive) member(time); },
    leave() { members.delete(receive); if (!members.size) cursorGroups.delete(group); }
  };
}

export type ChartPngMetadata = { title: string; range: string; resolution: string; series: { name: string; unit: string; color: string; dash?: "solid" | "dashed" | "dotted" }[]; cursors?: string };

// Capture the real rendered plot first, then place its identifying metadata in
// a separate canvas margin. No measurements are recreated for the export.
export async function chartPngDataUrl(chart: EChartsType, metadata: ChartPngMetadata): Promise<string> {
  const source = chart.getDataURL({ type: "png", pixelRatio: 2, backgroundColor: "#ffffff" });
  const picture = new Image();
  await new Promise<void>((resolve, reject) => {
    picture.onload = () => resolve();
    picture.onerror = () => reject(new Error("无法读取图表画布，请重试。"));
    picture.src = source;
  });
  const width = chart.getWidth(), plotHeight = chart.getHeight();
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  if (!context) throw new Error("当前浏览器无法导出 PNG。");
  const wrap = (text: string, available: number) => {
    const lines: string[] = []; let line = "";
    for (const character of text) {
      if (line && context.measureText(line + character).width > available) { lines.push(line); line = ""; }
      line += character;
    }
    if (line) lines.push(line);
    return lines;
  };
  context.font = "12px sans-serif";
  const detailLines = [metadata.range, metadata.resolution, metadata.cursors].filter((line): line is string => !!line).flatMap((line) => wrap(line, width - 40));
  const legendLines = metadata.series.map((item) => ({ ...item, lines: wrap(`${item.name} · ${item.unit || "无单位"}`, width - 66) }));
  context.font = "bold 16px sans-serif";
  const titleLines = wrap(metadata.title, width - 40);
  const headerHeight = 25 + titleLines.length * 22 + detailLines.length * 18;
  const legendHeight = 20 + legendLines.reduce((total, item) => total + item.lines.length * 18 + 5, 0);
  canvas.width = Math.round(width * 2); canvas.height = Math.round((headerHeight + plotHeight + legendHeight) * 2);
  context.scale(2, 2); context.fillStyle = "#ffffff"; context.fillRect(0, 0, width, canvas.height / 2);
  context.fillStyle = "#263c58"; context.font = "bold 16px sans-serif";
  let y = 24;
  for (const line of titleLines) { context.fillText(line, 20, y); y += 22; }
  context.font = "12px sans-serif"; context.fillStyle = "#526780";
  for (const line of detailLines) { context.fillText(line, 20, y); y += 18; }
  context.drawImage(picture, 0, headerHeight, width, plotHeight);
  y = headerHeight + plotHeight + 16;
  for (const item of legendLines) {
    context.strokeStyle = item.color; context.lineWidth = 2;
    context.setLineDash(item.dash === "dashed" ? [6, 3] : item.dash === "dotted" ? [2, 3] : []);
    context.beginPath(); context.moveTo(20, y - 4); context.lineTo(40, y - 4); context.stroke();
    context.fillStyle = "#526780";
    for (const line of item.lines) { context.fillText(line, 48, y); y += 18; }
    y += 5;
  }
  const result = canvas.toDataURL("image/png");
  if (!result.startsWith("data:image/png")) throw new Error("PNG 导出失败，请缩小图表后重试。");
  return result;
}
