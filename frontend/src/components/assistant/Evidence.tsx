import type { AssistantCard } from "../../assistantApi";

const labels: Record<string, string> = { version: "来源版本", source_version: "源码版本", installed_version: "安装版本", running_version: "运行版本", sha256: "资料校验（SHA256）", source_sha256: "原始文件校验（SHA256）", snapshot_sha256: "源码快照校验（SHA256）", version_note: "版本说明", sha256_scope: "校验对应的内容", offset: "读取起点（字节）", bytes_read: "已查看字节", total_bytes: "文件总字节", unread_bytes: "尚未查看字节", file_mtime_ns: "文件修改时间（纳秒）", eof: "本段已读至末尾", model_content_complete: "内容完整性", redaction_status: "脱敏范围", source: "来源", updated_at: "来源时间", path: "文件", start: "起始行", end: "结束行", scope: "查看范围", stop_reason: "停止原因", unread: "尚未查看的内容", unprovided_byte_ranges: "尚未查看的字节范围", provided_byte_ranges: "已查看的字节范围" };
const sourceNames:Record<string,string>={task_frozen_attachments:"本次上传的文件",paired_hmi_live_read:"这台上位机提供的资料"};
function readable(value: unknown): string { return typeof value === "object" ? JSON.stringify(value) : String(value); }
function missing(member:Record<string,unknown>){return Array.isArray(member.unprovided_byte_ranges)&&member.unprovided_byte_ranges.length>0;}
export function Evidence({ card }: { card: AssistantCard }) {
  const files = Array.isArray(card.data.attachments) ? card.data.attachments as Record<string, unknown>[] : [];
  const unread=card.data.unread;
  const partial=Number(card.data.unread_bytes??0)>0||unread===true||(typeof unread==='number'&&unread>0)||(typeof unread==='string'&&unread.trim()!==''&&!['0','none'].includes(unread.trim().toLowerCase()))||(Array.isArray(unread)&&unread.length>0)||card.data.eof===false||card.data.model_content_complete===false||card.data.truncated===true;
  return <div className="assistant-evidence-readable">
    <p><strong>检查了哪些资料</strong></p><p>资料来自：{sourceNames[card.source]??card.source??"来源未说明"}</p><p>记录时间：{card.updated_at||"未提供"}</p>
    {card.scope&&<p>查看说明：{card.scope}</p>}
    {partial&&<p className="assistant-evidence-warning">只检查了部分数据，尚不能判断整个文件。</p>}
    {files.map((file,index)=>{
      const provided=Array.isArray(file.provided)?file.provided as Record<string,unknown>[]:[],unread=Number(file.members_without_content_evidence??0),incomplete=unread>0||provided.some(missing);
      return <section className="assistant-evidence-file" key={index}><strong>{String(file.name??"附件")}</strong><p>{file.image_preview_provided===true?"已查看：图片预览":`已查看：${Number(file.members_with_evidence??0)} 个文件的内容`}</p>
        {unread>0&&<p className="assistant-evidence-warning">尚未查看：{unread} 个文件的内容</p>}
        {provided.map((member,n)=><p key={n}>{String(member.member??file.name)}：{missing(member)?"只查看了部分内容":"已取得内容"}</p>)}
        {incomplete&&<p className="assistant-evidence-warning">只检查了部分数据，尚不能判断整个文件。</p>}
      </section>;
    })}
    <p className="assistant-caption">这里列出本次拿到的资料；拿到资料不等于问题已经查清。版本未说明时，需要另行核对当前运行版本。</p>
    <details><summary>技术明细（版本、文件校验与查看范围）</summary>
      {Object.entries(card.data).filter(([key])=>key in labels).map(([key,value])=><p key={key}><strong>{labels[key]}</strong> <code>{readable(value)}</code></p>)}
      {files.map((file,index)=><section key={index}><strong>{String(file.name??"附件")}</strong><p>原始文件校验（SHA256） <code>{String(file.source_sha256??file.sha256??"未提供")}</code></p>{(Array.isArray(file.provided)?file.provided as Record<string,unknown>[]:[]).map((member,n)=><div key={n}><p>{String(member.member??file.name)} · {String(member.size)} 字节</p><p>已查看的字节范围 <code>{readable(member.provided_byte_ranges??[])}</code></p><p>尚未查看的字节范围 <code>{readable(member.unprovided_byte_ranges??[])}</code></p>{Array.isArray(member.csv_statistics_provided)&&member.csv_statistics_provided.length>0&&<p>CSV统计范围 <code>{readable(member.csv_statistics_provided)}</code></p>}</div>)}</section>)}
      <pre>{JSON.stringify(card.data,null,2)}</pre>
    </details>
  </div>;
}
