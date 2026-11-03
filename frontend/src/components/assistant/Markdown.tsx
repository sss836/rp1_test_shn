import { Fragment, useState, type ReactNode } from "react";

export function safeSourceHref(raw: string): string | null {
  try { const url = new URL(raw); return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : null; } catch { return null; }
}
function inline(text: string): ReactNode[] {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  return text.split(pattern).map((part, index) => {
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
    if (link) { const href = safeSourceHref(link[2]); return href ? <a key={index} href={href} target="_blank" rel="noopener noreferrer">{link[1]}</a> : <Fragment key={index}>{link[1]}（链接不可用）</Fragment>; }
    return <Fragment key={index}>{part}</Fragment>;
  });
}
function Code({ code, language }: { code: string; language: string }) {
  const [status, setStatus] = useState("");
  const body = <div className="assistant-code"><div><span>{language || "文本"}</span><button type="button" onClick={() => { void navigator.clipboard?.writeText(code).then(() => setStatus("已复制")).catch(() => setStatus("复制失败，请选择文字复制")); }}>复制代码</button><span role="status">{status}</span></div><pre><code>{code}</code></pre></div>;
  return code.split("\n").length > 12 ? <details className="assistant-log"><summary>展开日志 / 代码（{code.split("\n").length}行）</summary>{body}</details> : body;
}
export function Markdown({ text }: { text: string }) {
  const lines = text.replace(/\r\n/g, "\n").split("\n"), blocks: ReactNode[] = [];
  for (let i = 0; i < lines.length;) {
    const line = lines[i], key = i;
    if (!line.trim()) { i++; continue; }
    if (line.startsWith("```")) {
      const language = line.slice(3).trim(); const body: string[] = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) body.push(lines[i++]);
      if (i < lines.length) i++;
      blocks.push(<Code key={key} code={body.join("\n")} language={language}/>); continue;
    }
    if (line.includes("|") && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1] ?? "")) {
      const cells = (row: string) => row.trim().replace(/^\||\|$/g, "").split("|").map(s => s.trim());
      const header = cells(line), rows: string[][] = []; i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      blocks.push(<div key={key} className="assistant-table-scroll"><table><thead><tr>{header.map((s, n) => <th key={n}>{inline(s)}</th>)}</tr></thead><tbody>{rows.map((row, r) => <tr key={r}>{header.map((_, c) => <td key={c}>{inline(row[c] ?? "")}</td>)}</tr>)}</tbody></table></div>); continue;
    }
    const heading = /^(#{1,6})\s+(.+)/.exec(line);
    if (heading) { blocks.push(<p className="assistant-markdown-heading" key={key}>{inline(heading[2])}</p>); i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const items: string[] = [], ordered = /^\s*\d+\./.test(line);
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      const content = items.map((item, n) => <li key={n}>{inline(item)}</li>); blocks.push(ordered ? <ol key={key}>{content}</ol> : <ul key={key}>{content}</ul>); continue;
    }
    const body = [line]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*([-*]|\d+\.)\s)/.test(lines[i]) && !(lines[i].includes("|") && /^\s*\|?\s*:?-+/.test(lines[i + 1] ?? ""))) body.push(lines[i++]);
    blocks.push(<p key={key}>{inline(body.join("\n"))}</p>);
  }
  return <div className="assistant-markdown">{blocks}</div>;
}
