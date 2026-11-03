import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowUp, AtSign, Paperclip, RotateCcw, X } from "lucide-react";
import { assistantApi, MAX_DIAGNOSTIC_ATTACHMENT_BYTES, type AssistantAttachment, type AssistantReference } from "../../assistantApi";
import { requestId } from "../../client";

type QueueFile = { key: string; fingerprint: string; file?: File; name: string; size: number; state: "uploading" | "ready" | "failed" | "cancelled" | "removing"; loaded: number; attachment?: AssistantAttachment; error?: string };
type Draft = { text: string; references: AssistantReference[]; files: QueueFile[] };
export type ComposerSubmission = { text: string; references: AssistantReference[]; attachments: string[]; mode: "send" | "steer" | "queue"; clear: (nextThread?: string) => void };
const storagePrefix = "rp1-assistant-draft:";
function loadDraft(key: string): Draft {
  try { const value = JSON.parse(sessionStorage.getItem(key) ?? "null"); if (value && typeof value.text === "string" && Array.isArray(value.references) && Array.isArray(value.files)) return { text: value.text.slice(0, 8000), references: value.references, files: value.files.filter((f: QueueFile) => f.state === "ready" && f.attachment && Date.parse(f.attachment.expires_at) > Date.now()) }; } catch { /* Invalid local drafts are discarded. */ }
  return { text: "", references: [], files: [] };
}
function persist(key: string, draft: Draft) { try { sessionStorage.setItem(key, JSON.stringify({ ...draft, files: draft.files.filter(f => f.state === "ready").map(({ file: _file, ...rest }) => rest) })); } catch { /* Input continues working when storage quota is full. */ } }
function fileBytes(file: File): Promise<ArrayBuffer> { return file.arrayBuffer ? file.arrayBuffer() : new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = reject; reader.readAsArrayBuffer(file); }); }
async function fingerprint(file: File) {
  const bytes = await fileBytes(file);
  if (globalThis.crypto?.subtle) return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", new Uint8Array(bytes))), b => b.toString(16).padStart(2, "0")).join("");
  // Compatibility path for browsers without WebCrypto. This is only queue
  // deduplication; immutable evidence hashes are always issued by the server.
  let hash = 2166136261; for (const byte of new Uint8Array(bytes)) hash = Math.imul(hash ^ byte, 16777619);
  return `${file.size}:${hash >>> 0}`;
}
function bytes(size: number) { return size < 1024 * 1024 ? `${(size / 1024).toFixed(1)} KiB` : `${(size / 1024 / 1024).toFixed(1)} MiB`; }

export function Composer({ accountKey, thread, active, sending, blocked, waitingPermission=false, suggestion, onSubmit, onError }: { accountKey: string; thread: string; active: boolean; sending: boolean; blocked: boolean; waitingPermission?:boolean; suggestion: { text: string; key: number; append?:boolean } | null; onSubmit: (submission: ComposerSubmission) => void; onError: (message: string) => void }) {
  const storageKey = `${storagePrefix}${encodeURIComponent(accountKey)}:${thread || "new"}`;
  const storageKeyRef = useRef(storageKey);
  useEffect(() => { storageKeyRef.current = storageKey; }, [storageKey]);
  const [draft, setDraft] = useState<Draft>(() => loadDraft(storageKey));
  const draftRef = useRef(draft), alive = useRef(true), uploads = useRef(new Map<string, AbortController>());
  const [dragging, setDragging] = useState(false), [preview, setPreview] = useState<AssistantAttachment | null>(null), [showReferences, setShowReferences] = useState(false), [kind, setKind] = useState<AssistantReference["kind"]>("assets"), [query, setQuery] = useState(""), [references, setReferences] = useState<AssistantReference[]>([]), [referenceError, setReferenceError] = useState("");
  const picker = useRef<HTMLInputElement>(null), input = useRef<HTMLTextAreaElement>(null);
  const update = (fn: (previous: Draft) => Draft) => { if (!alive.current) return; const next = fn(draftRef.current); draftRef.current = next; setDraft(next); persist(storageKeyRef.current, next); };
  useEffect(() => { alive.current = true; const running = uploads.current; return () => { alive.current = false; for (const abort of running.values()) abort.abort(); running.clear(); }; }, []);
  useEffect(() => {
    if(!suggestion)return;
    const previous=draftRef.current,text=suggestion.append&&previous.text?`${previous.text}\n\n${suggestion.text}`:suggestion.text;
    if(text.length>8000){onError("当前草稿加上此补充超过8000字，请先保存或发送现有草稿，再恢复该补充；原补充仍保留在结果卡中。");return;}
    const next={...previous,text};draftRef.current=next;setDraft(next);persist(storageKeyRef.current,next);input.current?.focus();
  },[suggestion,onError]);
  useEffect(() => {
    if (!showReferences || kind === undefined) return;
    const abort = new AbortController(); const timeout = window.setTimeout(() => {
      void assistantApi.references(kind, query, abort.signal).then(result => { if (!abort.signal.aborted) { setReferences(result.items); setReferenceError(""); } }).catch((e: unknown) => { if (!abort.signal.aborted) { setReferences([]); setReferenceError(e instanceof Error ? e.message : "对象查询失败"); } });
    }, 200);
    return () => { window.clearTimeout(timeout); abort.abort(); };
  }, [showReferences, kind, query]);
  async function upload(item: QueueFile) {
    if (!item.file) return;
    const abort = new AbortController(); uploads.current.set(item.key, abort);
    update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "uploading", error: "", loaded: 0 } : f) }));
    try {
      const attachment = await assistantApi.uploadAttachment(item.file, abort.signal, (loaded) => { if (!abort.signal.aborted) update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, loaded } : f) })); });
      if (!abort.signal.aborted) update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "ready", attachment, loaded: item.size } : f) }));
    } catch (e) { if (!abort.signal.aborted) update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "failed", error: e instanceof Error ? e.message : "上传失败" } : f) })); }
    finally { if (uploads.current.get(item.key) === abort) uploads.current.delete(item.key); }
  }
  async function addFiles(files: FileList | File[] | null) {
    const selected = Array.from(files ?? []); if (!selected.length) return;
    if (selected.length > 3) { onError("每条消息最多3个附件。"); return; }
    if (selected.some(f => !f.size || f.size > MAX_DIAGNOSTIC_ATTACHMENT_BYTES)) { onError("附件须为非空且每个不超过32MiB。"); return; }
    for (const file of selected) {
      let hash: string; try { hash = await fingerprint(file); } catch { onError(`无法读取 ${file.name}，请重新选择文件。`); continue; }
      if (!alive.current) return;
      if (draftRef.current.files.some(f => f.fingerprint === hash)) continue;
      if (draftRef.current.files.length >= 3) { onError("每条消息最多3个附件。"); break; }
      const item: QueueFile = { key: requestId(), fingerprint: hash, file, name: file.name, size: file.size, loaded: 0, state: "uploading" };
      // Ref reserves the slot synchronously even when several paste/drop events
      // arrive before React commits the queue state.
      const next = { ...draftRef.current, files: [...draftRef.current.files, item] }; draftRef.current = next; setDraft(next); persist(storageKeyRef.current, next); void upload(item);
    }
    if (picker.current) picker.current.value = "";
  }
  function cancel(item: QueueFile) { uploads.current.get(item.key)?.abort(); uploads.current.delete(item.key); update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "cancelled" } : f) })); }
  async function remove(item: QueueFile) {
    if (item.state === "uploading") cancel(item);
    if (item.attachment) {
      const abort = new AbortController(); uploads.current.set(item.key, abort);
      update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "removing" } : f) }));
      try { await assistantApi.deleteAttachment(item.attachment.id, abort.signal); if (abort.signal.aborted) return; }
      catch (e) { if (!abort.signal.aborted) { onError(e instanceof Error ? e.message : "附件移除失败，已保留该附件。"); update(d => ({ ...d, files: d.files.map(f => f.key === item.key ? { ...f, state: "ready" } : f) })); } return; }
      finally { uploads.current.delete(item.key); }
    }
    update(d => ({ ...d, files: d.files.filter(f => f.key !== item.key) }));
  }
  function submit(mode: ComposerSubmission["mode"], event?: FormEvent) {
    event?.preventDefault(); const snapshot = draftRef.current;
    if (sending || blocked || (!snapshot.text.trim() && !snapshot.files.some(f => f.state === "ready")) || snapshot.files.some(f => f.state === "uploading" || f.state === "removing")) return;
    if(mode==="steer"&&waitingPermission)return;
    if (mode === "steer" && snapshot.files.length) { onError("运行中补充只发送文字；带附件的内容请选择下一条排队。"); return; }
    const ready = snapshot.files.filter(f => f.state === "ready" && f.attachment), ids = ready.map(f => f.attachment!.id), keys = new Set(ready.map(f => f.key));
    onSubmit({ text: snapshot.text.trim(), references: snapshot.references, attachments: ids, mode, clear: (nextThread?: string) => {
      if (!alive.current) return;
      update(current => { const remaining = { ...current, text: current.text === snapshot.text ? "" : current.text, references: current.references === snapshot.references ? [] : current.references, files: current.files.filter(f => !keys.has(f.key)) }; if (nextThread && nextThread !== thread) persist(`${storagePrefix}${encodeURIComponent(accountKey)}:${nextThread}`, remaining); return remaining; });
    } });
  }
  const busyUploads = draft.files.some(f => f.state === "uploading" || f.state === "removing");
  const disabled = sending || blocked || busyUploads || (!draft.text.trim() && !draft.files.some(f => f.state === "ready"));
  return <div className={`assistant-draft${dragging ? " is-dragging" : ""}`} onDragOver={e => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragging(true); } }} onDragLeave={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDragging(false); }} onDrop={e => { e.preventDefault(); setDragging(false); void addFiles(e.dataTransfer.files); }}>
    {dragging && <div className="assistant-drop-hint">松开添加到附件队列</div>}
    {draft.files.length > 0 && <ul className="assistant-upload-queue" aria-label="诊断附件队列">{draft.files.map(item => <li key={item.key}>
      {item.attachment?.kind === "image" && <button className="assistant-image-thumb" type="button" onClick={() => setPreview(item.attachment!)} aria-label={`放大${item.name}`}><img src={`/api/v1/assistant/attachments/${encodeURIComponent(item.attachment.id)}`} alt={item.name}/></button>}
      <div className="assistant-upload-copy"><strong>{item.name}</strong><span>{bytes(item.size)} · {item.state === "ready" ? "上传完成" : item.state === "failed" ? "上传失败" : item.state === "cancelled" ? "已取消" : item.state === "removing" ? "正在移除" : `${Math.min(100, Math.floor(item.loaded / item.size * 100))}% · ${bytes(item.loaded)} 已上传`}</span>{item.state === "uploading" && <progress aria-label={`${item.name}上传进度`} value={item.loaded} max={item.size}/>}{item.error && <span role="alert">{item.error}</span>}</div>
      {item.state === "uploading" && <button className="assistant-text-button" type="button" onClick={() => cancel(item)} aria-label={`取消上传${item.name}`}>取消</button>}
      {(item.state === "failed" || item.state === "cancelled") && item.file && <button type="button" className="assistant-icon-button" aria-label={`重试${item.name}`} onClick={() => void upload(item)}><RotateCcw size={14}/></button>}
      <button type="button" className="assistant-icon-button" aria-label={`移除${item.name}`} disabled={item.state === "removing"} onClick={() => void remove(item)}><X size={14}/></button>
    </li>)}</ul>}
    {draft.references.length > 0 && <div className="assistant-references" aria-label="本次引用对象">{draft.references.map(ref => <span key={`${ref.kind}:${ref.id}`}>@{ref.label}<button type="button" aria-label={`移除引用${ref.label}`} onClick={() => update(d => ({ ...d, references: d.references.filter(r => r.kind !== ref.kind || r.id !== ref.id) }))}><X size={11}/></button></span>)}<small>仅引用可见对象，查询时再次校验权限</small></div>}
    {showReferences && <section className="assistant-reference-picker" aria-label="引用业务对象"><div><label>类型<select value={kind} onChange={e => setKind(e.target.value as AssistantReference["kind"])}><option value="assets">样品</option><option value="stations">工位</option><option value="executions">执行</option></select></label><button className="assistant-text-button" type="button" onClick={() => picker.current?.click()}>@文件（添加附件）</button></div><input aria-label="搜索引用对象" maxLength={150} placeholder="输入名称或编号，系统查询可见对象" value={query} onChange={e => setQuery(e.target.value)}/>{referenceError && <p role="alert">{referenceError}</p>}<ul>{references.map(ref => <li key={`${ref.kind}:${ref.id}`}><button type="button" onClick={() => { if (draftRef.current.references.length >= 8) { onError("每条消息最多引用8个对象。"); return; } update(d => ({ ...d, references: d.references.some(r => r.kind === ref.kind && r.id === ref.id) || d.references.length >= 8 ? d.references : [...d.references, ref] })); setShowReferences(false); input.current?.focus(); }}><strong>{ref.label}</strong>{ref.detail && <small>{ref.detail}</small>}</button></li>)}</ul>{!references.length && !referenceError && <p>没有匹配对象，可更换名称查询。</p>}</section>}
    <form onSubmit={e => submit(active ? "queue" : "send", e)}><div className="assistant-composer"><label htmlFor="assistant-message" className="assistant-sr-only">消息</label><textarea id="assistant-message" ref={input} rows={2} maxLength={8000} placeholder={active ? "继续写草稿：补充当前诊断，或作为下一条排队…" : "提问，或拖入文件、粘贴截图…"} value={draft.text} onChange={e => update(d => ({ ...d, text: e.target.value }))} onPaste={e => { if (e.clipboardData.files.length) { e.preventDefault(); void addFiles(e.clipboardData.files); } }} onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/>
      <div className="assistant-composer-actions"><div><button type="button" className="assistant-icon-button" aria-label="添加诊断附件" title="选择文件，也可拖拽或粘贴截图" disabled={draft.files.length >= 3} onClick={() => picker.current?.click()}><Paperclip size={17}/></button><button type="button" className="assistant-icon-button" aria-label="引用对象" title="@样品、工位、执行或文件" aria-expanded={showReferences} onClick={() => setShowReferences(!showReferences)}><AtSign size={17}/></button></div><input ref={picker} aria-label="选择诊断附件" type="file" hidden multiple onChange={e => void addFiles(e.target.files)}/><span>{active ? "草稿独立保存" : "已关联当前页面"}</span><button className="assistant-send" type="submit" aria-label={active ? "下一条排队" : "发送"} title={active ? "当前结束后执行下一条" : "发送"} disabled={disabled}><ArrowUp size={18}/></button></div></div>
      {active && <div className="assistant-followup-actions"><button type="button" disabled={disabled || waitingPermission || !!draft.files.length || !draft.text.trim() || !!draft.references.length} className="assistant-adopt-button" onClick={() => submit("steer")}>补充当前诊断</button><span>{waitingPermission?"等待允许查看资料时，补充先保留在草稿中。":"补充不增加预算或授权；新动作请选择下一条排队。"}</span></div>}
      <p className="assistant-privacy-note">支持CSV、tar、tar.gz、zip及其他格式，每条最多3个、每个32MiB。仅用于诊断，不是正式归档或审批，也不进入正式统计；未知格式会说明读取限制。请先去除个人信息与密钥。</p><p className="assistant-input-hint">Enter {active ? "排队" : "发送"}<span>Shift + Enter 换行 · 草稿保存在本浏览器会话</span></p></form>
    {preview && <div className="assistant-image-overlay" role="dialog" aria-modal="true" aria-label={`图片预览${preview.name}`} onKeyDown={e => { if (e.key === "Escape") setPreview(null); }} onClick={() => setPreview(null)}><button type="button" autoFocus aria-label="关闭图片预览" onClick={() => setPreview(null)}><X size={22}/></button><figure onClick={e => e.stopPropagation()}><img src={`/api/v1/assistant/attachments/${encodeURIComponent(preview.id)}`} alt={preview.name}/><figcaption>{preview.name}</figcaption></figure></div>}
  </div>;
}
