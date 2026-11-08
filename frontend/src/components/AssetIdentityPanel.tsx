import { useRef, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { requestId } from "../client";
import { setupApi, type AssetIdentityPayload, type SetupAsset } from "../setupApi";
import { creationScenarioName } from "../assetRegistration";
import type { AssetSummary } from "../types";
import { ErrorState, LoadingState, Panel } from "./Workbench";

export function useAssetIdentityOptions(enabled = true) {
  return useQuery({ queryKey: ["setup-options"], queryFn: ({ signal }) => setupApi.identityOptions(signal), enabled, retry: false });
}

function IdentityForm({ asset, enabled, refresh }: { asset: SetupAsset; enabled: boolean; refresh: () => Promise<void> }) {
  const cache = useQueryClient();
  const [serial, setSerial] = useState("");
  const [model, setModel] = useState("");
  const [reason, setReason] = useState("");
  const [inputError, setInputError] = useState("");
  const [frozen, setFrozen] = useState<{ payload: AssetIdentityPayload; key: string } | null>(null);
  const busy = useRef(false);
  const mutation = useMutation({
    mutationFn: async (evidence: { payload: AssetIdentityPayload; key: string }) => {
      const result = await setupApi.completeIdentity(asset.id, evidence.payload, evidence.key);
      if (result.id !== asset.id || result.code !== asset.code || result.request_key !== evidence.key
        || (evidence.payload.serial_number !== undefined && result.serial_number !== evidence.payload.serial_number)
        || (evidence.payload.model !== undefined && result.model !== evidence.payload.model)
        || (Boolean(asset.serial_number) && result.serial_number !== asset.serial_number)
        || (Boolean(asset.model) && result.model !== asset.model)) {
        throw new Error("身份回执与本次提交不匹配，请重新读取样品资料核对。");
      }
      return result;
    },
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["setup-options"] });
      void cache.invalidateQueries({ queryKey: ["assets"] });
      void cache.invalidateQueries({ queryKey: ["asset"] });
    },
    onSettled: () => { busy.current = false; }
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy.current || mutation.isSuccess || !enabled) return;
    const payload: AssetIdentityPayload = { reason: reason.trim() };
    if (!asset.serial_number && serial.trim()) payload.serial_number = serial.trim();
    if (!asset.model && model.trim()) payload.model = model.trim();
    if (!frozen && (!payload.reason || (!payload.serial_number && !payload.model))) {
      setInputError("请填写至少一项待补全身份及核实理由。"); return;
    }
    const evidence = frozen ?? { payload, key: requestId() };
    setInputError(""); setFrozen(evidence); busy.current = true;
    mutation.mutate(evidence);
  }
  const locked = mutation.isPending || frozen !== null || !enabled;
  return <form className="setup-form" onSubmit={submit}>
    <h3>补全实机身份</h3>
    <p>按实机铭牌和配置核实填写。已有值保持锁定，保存会记录操作者和理由。</p>
    <p>请先补全 SN 和机型，再追加真实配置或关联测试计划；关联后实机身份将锁定。</p>
    <div className="setup-fields">
      <label>实机 SN<input value={asset.serial_number || serial} readOnly={Boolean(asset.serial_number)} disabled={locked} maxLength={255} placeholder="核对铭牌后填写，保留前导零" onChange={event => setSerial(event.target.value)} /></label>
      <label>机型{asset.model ? <input value={asset.model} readOnly disabled={locked} /> : asset.asset_kind === "WHOLE_MACHINE" ? <select value={model} disabled={locked} onChange={event => setModel(event.target.value)}><option value="">待确认，可稍后补全</option><option value="24轴">24自由度 / 24轴</option><option value="28轴">28自由度 / 28轴</option></select> : <input value={model} disabled={locked} maxLength={255} onChange={event => setModel(event.target.value)} />}</label>
      <label>核实理由<textarea required value={reason} disabled={locked} maxLength={2048} rows={2} onChange={event => setReason(event.target.value)} /></label>
    </div>
    {(inputError || mutation.error) && <p role="alert">{inputError || (mutation.error instanceof Error ? mutation.error.message : "保存失败，请核对后重试。")}</p>}
    {mutation.isSuccess && <p role="status">{mutation.data.identity_completed ? "实机身份已补全。" : "身份信息已保存，其他待补全项可继续核实。"}</p>}
    {!mutation.isSuccess && <button className="primary-button" disabled={mutation.isPending || !enabled || (!frozen && (!reason.trim() || (!serial.trim() && !model.trim())))}>{mutation.isPending ? "正在保存…" : frozen ? "重试本次保存" : "核实并保存身份"}</button>}
    {frozen && !mutation.isPending && !mutation.isSuccess && <button type="button" disabled={!enabled} onClick={() => { void refresh().then(() => { setFrozen(null); mutation.reset(); }).catch(() => { setInputError("身份资料重新读取失败，请重试读取后再修改。"); }); }}>重新读取并核对</button>}
  </form>;
}

export function AssetIdentityPanel({ asset }: { asset: AssetSummary }) {
  const query = useAssetIdentityOptions();
  const registered = query.data?.assets.find(entry => entry.id === asset.id);
  const refresh = async () => {
    const result = await query.refetch();
    if (result.isError) throw result.error;
  };
  return <Panel title="样品身份" eyebrow="ASSET IDENTITY">
    <dl className="fact-grid">
      <div><dt>标准编号</dt><dd>{asset.code}</dd></div>
      <div><dt>显示名</dt><dd>{registered?.name ?? asset.name}</dd></div>
      <div><dt>用途</dt><dd>{creationScenarioName(registered?.creation_scenario ?? asset.creation_scenario ?? "RND_TEST")}</dd></div>
      <div><dt>实机 SN</dt><dd>{registered ? registered.serial_number || "待绑定" : asset.serial_number || "待绑定"}</dd></div>
      <div><dt>机型</dt><dd>{registered ? registered.model || "待确认" : query.isError ? "未读取" : "读取中"}</dd></div>
      <div><dt>身份状态</dt><dd>{registered ? registered.serial_number ? registered.model ? "身份已登记" : "机型待补全" : "计划档案 · 身份待补全" : query.isError ? "读取失败" : "正在读取"}</dd></div>
    </dl>
    {query.isPending ? <LoadingState label="正在读取身份资料" /> : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : !registered ? <p>当前账号无法读取该样品的身份管理资料。</p> : <>
      {registered.blocked_reason && <p>{registered.blocked_reason}</p>}
      {registered.can_complete_identity === true && <IdentityForm key={`${registered.id}:${registered.serial_number ?? ""}:${registered.model ?? ""}`} asset={registered} enabled={!query.isFetching} refresh={refresh} />}
    </>}
  </Panel>;
}
