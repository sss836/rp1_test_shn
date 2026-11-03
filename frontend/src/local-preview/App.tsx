import {useCallback,useEffect,useMemo,useRef,useState} from "react";
import type {CurveSeries} from "../components/TelemetryChart";
import {WorkspaceChart} from "./WorkspaceChart";
import {COLORS,GROUPS,TIME_RANGES,timeRangeLabel,availableGroups,capabilitiesFromFrames,curveLabel,displaySeries,evidenceSeries,jointLabel,liveSeries,mergeState,metricIds,parts,type Capability,type GroupId,type PreviewState} from "./model";

const EMPTY_SERIES:CurveSeries[]=[];
type SavedEvent={type:string;time_s?:number;started_s?:number;trigger_s?:number};
type QueryResult={signature:string;series:CurveSeries[];resolution_s:number;indexing?:boolean;indexed_bytes?:number;source_bytes?:number;duration_s?:number;start_s?:number;end_s?:number;saved_until_s?:number;edge_approximation?:boolean};
async function api<T>(path:string,signal:AbortSignal,body?:unknown):Promise<T>{
  const response=await fetch(path,{signal,credentials:"same-origin",...(body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{})});
  if(!response.ok){const data=await response.json();throw new Error(typeof data.detail==="string"?data.detail:`请求失败 ${response.status}`);}
  return response.json() as Promise<T>;
}
export default function App(){
  const [ready,setReady]=useState(false),[connectionError,setConnectionError]=useState(""),[error,setError]=useState(""),[data,setData]=useState<PreviewState>(),[caps,setCaps]=useState<Capability[]>([]);
  const [group,setGroup]=useState<GroupId>("position"),[joints,setJoints]=useState<string[]>([]),[target,setTarget]=useState(true),[degrees,setDegrees]=useState(true);
  const [paused,setPaused]=useState(false),[mode,setMode]=useState<"live"|"history"|"events">("live"),[rangePreset,setRangePreset]=useState("30"),[reset,setReset]=useState(0);
  const [manualKey,setManualKey]=useState(""),[manualCount,setManualCount]=useState(0);
  const [customUnit,setCustomUnit]=useState("s");
  const unitFactor=customUnit==="h"?3600:customUnit==="min"?60:1;
  const [start,setStart]=useState("0"),[end,setEnd]=useState("60"),[result,setResult]=useState<QueryResult>(),[loading,setLoading]=useState(false);
  const [events,setEvents]=useState<SavedEvent[]>([]),[nextOffset,setNextOffset]=useState<number|null>(null),[eventBusy,setEventBusy]=useState(false);
  const [markers,setMarkers]=useState<Array<[number,number]>>([]),[notice,setNotice]=useState("");
  const selectionInitialized=useRef(false);
  const stateRef=useRef<PreviewState|undefined>(undefined),queryRef=useRef<AbortController|null>(null),eventRef=useRef<AbortController|null>(null);
  useEffect(()=>{
    const controller=new AbortController(),ticket=new URLSearchParams(location.hash.slice(1)).get("ticket");
    window.history.replaceState(null,"",location.pathname+location.search);
    void (async()=>{
      if(ticket)await api("/api/v1/preview/session",controller.signal,{ticket});
      const c=await api<{metrics:Capability[]}>("/api/v1/preview/capabilities",controller.signal);
      setCaps(c.metrics);const initial=c.metrics.find(m=>m.available);if(initial){setJoints([parts(initial.id).joint]);selectionInitialized.current=true;}setReady(true);
    })().catch(e=>{if(!controller.signal.aborted)setConnectionError(String(e));});
    return()=>controller.abort();
  },[]);
  useEffect(()=>{
    if(!ready || (paused && mode==="live"))return;
    const controller=new AbortController();let timer:ReturnType<typeof setTimeout>;
    const poll=async()=>{
      try{
        const next=await api<PreviewState>(`/api/v1/preview/state?after=${stateRef.current?.last_sequence??0}`,controller.signal);
        if(controller.signal.aborted)return;
        const merged=mergeState(stateRef.current,next);stateRef.current=merged;setData(merged);
        setCaps(old=>capabilitiesFromFrames(old,next.frames));
        if(next.frames.length && !selectionInitialized.current){setJoints([parts(Object.keys(next.frames.at(-1)!.values)[0]??"/").joint].filter(Boolean));selectionInitialized.current=true;}
        setConnectionError("");
      }catch(e){if(!controller.signal.aborted)setConnectionError(String(e));}
      finally{if(!controller.signal.aborted)timer=setTimeout(()=>void poll(),mode==="live"?250:2000);}
    };
    void poll();return()=>{controller.abort();clearTimeout(timer);};
  },[ready,paused,mode]);
  useEffect(()=>{
    const close=()=>navigator.sendBeacon?.("/api/v1/preview/close");window.addEventListener("pagehide",close);
    return()=>{window.removeEventListener("pagehide",close);queryRef.current?.abort();eventRef.current?.abort();};
  },[]);
  const allJoints=useMemo(()=>[...new Set(caps.map(c=>parts(c.id).joint))],[caps]);
  const groups=useMemo(()=>availableGroups(caps),[caps]);
  const ids=useMemo(()=>metricIds(caps,joints,group,target),[caps,joints,group,target]);
  const rawLive=useMemo(()=>liveSeries(data,ids,caps),[data,ids,caps]);
  const runId=data?.run_id;
  const savedView=mode!=="live"||rangePreset==="all"||rangePreset==="custom"||Number(rangePreset)>=60;
  const queryFrom=rangePreset==="custom"?start:"",queryTo=rangePreset==="custom"?end:"";
  const signature=JSON.stringify([runId,ids,rangePreset,queryFrom,queryTo]);
  const chosen=!savedView?rawLive:result?.signature===signature?result.series:EMPTY_SERIES;
  const shown=useMemo(()=>displaySeries(chosen,degrees),[chosen,degrees]);
  const activeGroup=GROUPS.find(g=>g.id===group)!;
  const selectedKey=ids.join(",")+degrees+mode;
  useEffect(()=>{setMarkers([]);setReset(v=>v+1);queryRef.current?.abort();setLoading(false);},[selectedKey,runId]);
  useEffect(()=>setMarkers([]),[start,end]);
  const load=useCallback((from=start,to=end)=>{
    if(!runId){setError("当前没有执行记录");return;}
    if(!ids.length){setError("请先选择关节和指标");return;}
    const a=Number(from),b=Number(to);
    if(!from.trim()||!to.trim()||!Number.isFinite(a+b)||a<0||b<=a){setError("请输入有效的执行内秒数，结束须晚于开始。");return;}
    setRangePreset("custom");setStart(from);setEnd(to);setManualKey(JSON.stringify([from,to]));setManualCount(v=>v+1);setError("");
  },[runId,ids,start,end]);
  useEffect(()=>{
    if(!ready||!runId||!ids.length||!savedView||(mode==="live"&&paused&&rangePreset!=="custom"))return;
    if(rangePreset==="custom"&&manualKey!==JSON.stringify([queryFrom,queryTo]))return;
    const controller=new AbortController();queryRef.current=controller;let timer:ReturnType<typeof setTimeout>;
    const query=new URLSearchParams({metrics:ids.join(","),view:mode==="live"?"实时长窗":"历史记录"});
    if(rangePreset==="custom"){query.set("start",queryFrom);query.set("end",queryTo);}else query.set("window",rangePreset);
    const poll=async()=>{
      let indexing=false;setLoading(true);
      try{
        const value=await api<Omit<QueryResult,"signature">&{gaps?:Array<[number,number]>}>(`/api/v1/preview/runs/${encodeURIComponent(runId)}/curves?${query}`,controller.signal);
        if(controller.signal.aborted)return;
        indexing=!!value.indexing;setResult({...value,series:evidenceSeries(value.series,value.gaps??[]),signature});setError("");
        if(rangePreset!=="custom"){if(value.start_s!==undefined)setStart(String(value.start_s));if(value.end_s!==undefined)setEnd(String(value.end_s));}
      }catch(e){if(!controller.signal.aborted){setResult(undefined);setError(String(e));}}
      finally{if(!controller.signal.aborted){setLoading(false);if(indexing||(mode==="live"&&!paused&&rangePreset!=="custom"))timer=setTimeout(()=>void poll(),1000);}}
    };
    void poll();return()=>{controller.abort();clearTimeout(timer);};
  },[ready,runId,ids,rangePreset,savedView,mode,paused,queryFrom,queryTo,manualKey,manualCount,signature]);
  const loadEvents=useCallback((offset=0)=>{
    if(!runId)return;
    eventRef.current?.abort();const controller=new AbortController();eventRef.current=controller;setEventBusy(true);
    void api<{items:SavedEvent[];next_offset:number|null}>(`/api/v1/preview/runs/${encodeURIComponent(runId)}/events?offset=${offset}`,controller.signal)
      .then(v=>{if(!controller.signal.aborted){setEvents(old=>offset?[...old,...v.items].slice(-300):v.items);setNextOffset(v.next_offset);}})
      .catch(e=>{if(!controller.signal.aborted)setError(String(e));})
      .finally(()=>{if(!controller.signal.aborted)setEventBusy(false);});
  },[runId]);
  useEffect(()=>{if(ready && mode==="events")loadEvents();return()=>eventRef.current?.abort();},[ready,mode,loadEvents]);
  const chooseGroup=(next:GroupId)=>{
    if(metricIds(caps,joints,next,target).length>12){setTarget(false);setNotice("已保留全部关节，暂不叠加目标；同时最多显示12条曲线。");}else setNotice("");
    setGroup(next);setMarkers([]);
  };
  const chooseJoints=(next:string[])=>{if(metricIds(caps,next,group,target).length>12){setNotice("同时最多12条曲线；请减少关节，或关闭“叠加目标”。");return;}setJoints(next);setMarkers([]);setNotice("");};
  const inspect=useCallback(()=>setPaused(true),[]);
  const point=useCallback((x:number,y:number)=>{setPaused(true);setMarkers(old=>old.length===2?[[x,y]]:[...old,[x,y]]);},[]);
  const chooseRange=(value:string)=>{queryRef.current?.abort();setLoading(false);setRangePreset(value);setManualKey("");setPaused(value==="custom");setError("");setMarkers([]);setReset(v=>v+1);};
  const editRange=(which:"start"|"end",value:string)=>{queryRef.current?.abort();setLoading(false);const next=value===""?"":String(Number(value)*unitFactor);if(which==="start")setStart(next);else setEnd(next);setRangePreset("custom");setManualKey("");};
  const stale=!!connectionError || data?.stale;
  const status=!ready?"正在连接":mode==="live"&&paused?"画面已暂停":stale?"测量已陈旧 / 连接中断":data?.frames.length?"实时跟随":"等待测量";
  const statusClass=!ready||!data?.frames.length?"waiting":mode==="live"&&paused?"paused":stale?"stale":"live";
  const availableJoints=allJoints.filter(j=>metricIds(caps,[j],group,target).length>0);
  return <main className="curve-workspace">
    <header className="workspace-header"><div><small>RP1 / 本机测量</small><h1>曲线工作台</h1></div><div className="header-actions"><span className="readonly-badge">只读预览</span><button onClick={()=>window.print()}>打印</button></div></header>
    <div className="session-strip"><span className={`live-state ${statusClass}`}><i/>{status}</span><span>观测 {data?.observed_rate_hz?.toFixed(1)??"—"} Hz</span><span>{mode==="live"&&paused?"冻结前数据年龄":"数据年龄"} {data?.age_s?.toFixed(2)??"—"} s</span><details><summary>本次执行</summary><code>{runId||"尚无执行"}</code><p>显示已有测量缓存；暂停、缩放和关闭网页均不停止测试。</p></details></div>
    {(error||connectionError)&&<p role="alert" className="error-banner">{error||connectionError}</p>}
    <div className="workspace-body">
      <aside className="channel-panel" aria-label="曲线通道选择"><div className="panel-title"><h2>选择关节</h2><span>{joints.length} / {allJoints.length}</span></div><p className="muted">点名称单独查看，勾选可对比</p>
        <div className="joint-actions"><button onClick={()=>{if(metricIds(caps,availableJoints,group,target).length>12){setTarget(false);setJoints(availableJoints);setNotice("已显示全部关节的实际值；减少到6个关节后可叠加目标。");}else chooseJoints(availableJoints);}}>全选</button><button onClick={()=>chooseJoints([])}>清空</button></div>
        <div className="joint-list">{allJoints.map((joint,i)=>{const available=metricIds(caps,[joint],group,target).length>0;return <div className={`joint-row ${joints.includes(joint)?"selected":""}`} key={joint} title={joint}><input aria-label={`对比${jointLabel(joint)}`} type="checkbox" checked={joints.includes(joint)} disabled={!available} onChange={e=>chooseJoints(e.target.checked?[...joints,joint]:joints.filter(j=>j!==joint))}/><i style={{background:COLORS[i%COLORS.length]}}/><button disabled={!available} aria-pressed={joints.includes(joint)} onClick={()=>chooseJoints([joint])}>{jointLabel(joint)}</button>{!available&&<small>无数据</small>}</div>;})}</div>
        {!allJoints.length&&<p className="empty-small">等待上位机已有测量，不会主动扫描设备。</p>}
        <div className="channel-foot"><span>已选 {ids.length} / 12 条曲线</span><p>同一指标叠加对比；虚线为目标值。</p></div>
        <details className="availability"><summary>未提供的指标</summary>{GROUPS.filter(g=>!groups.some(a=>a.id===g.id)).map(g=><p key={g.id}>{g.label}：当前缓存无有效来源</p>)}<p>缺失数据保持空白，不补零。</p></details>
      </aside>
      <section className="plot-workspace" aria-label="曲线分析">
        <nav className="view-tabs" aria-label="数据来源">{([["live","实时曲线"],["history","历史记录"],["events","事件片段"]] as const).map(([key,label])=><button key={key} aria-pressed={mode===key} onClick={()=>{setMode(key);setError("");setMarkers([]);}}>{label}</button>)}</nav>
        <nav className="metric-tabs" aria-label="指标类别">{groups.map(g=><button key={g.id} aria-pressed={group===g.id} onClick={()=>chooseGroup(g.id)}>{g.label}</button>)}</nav>
        <div className="chart-toolbar"><div><h2>{activeGroup.label}</h2><span className="muted">{mode==="live"?"同单位实时对比":"已保存记录 · 执行内时间"}</span></div><div className="plot-actions">
          {group==="position"&&<label><input type="checkbox" checked={target} disabled={!target&&metricIds(caps,joints,group,true).length>12} onChange={e=>{setTarget(e.target.checked);setMarkers([]);}}/>叠加目标</label>}
          {(group==="position"||group==="speed")&&<select aria-label="角度显示单位" value={degrees?"deg":"rad"} onChange={e=>setDegrees(e.target.value==="deg")}><option value="deg">{group==="speed"?"°/s":"°"}</option><option value="rad">{group==="speed"?"rad/s":"rad"}</option></select>}
          <select aria-label="时间范围" value={rangePreset} onChange={e=>chooseRange(e.target.value)}>{TIME_RANGES.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select>
          {mode==="live"?<button className="primary" onClick={()=>{if(paused&&rangePreset==="custom")chooseRange("30");else{setPaused(v=>!v);setReset(v=>v+1);}}}>{paused?"继续实时":"暂停画面"}</button>:<button onClick={()=>setReset(v=>v+1)}>重置缩放</button>}
        </div></div>
        {notice&&<p role="status" className="notice">{notice}</p>}
        {(mode!=="live"||rangePreset==="custom")&&<form className="history-query" onSubmit={e=>{e.preventDefault();load();}}><label>单位<select aria-label="自定义时间单位" value={customUnit} onChange={e=>setCustomUnit(e.target.value)}><option value="s">秒</option><option value="min">分钟</option><option value="h">小时</option></select></label><label>执行内开始<input aria-label={customUnit==="s"?"开始秒数":"开始时间"} type="number" min="0" step="any" value={start===""?"":String(Number(start)/unitFactor)} onChange={e=>editRange("start",e.target.value)}/></label><span>—</span><label>执行内结束<input aria-label={customUnit==="s"?"结束秒数":"结束时间"} type="number" min="0" step="any" value={end===""?"":String(Number(end)/unitFactor)} onChange={e=>editRange("end",e.target.value)}/></label><button type="submit" className="primary" disabled={loading||!ids.length}>{loading?"查询中…":"查询曲线"}</button>{runId&&<a href={`/api/v1/preview/runs/${encodeURIComponent(runId)}/export.csv`}>下载已封存 CSV</a>}</form>}
        {savedView&&result?.signature===signature&&result.indexing&&<p role="status" className="notice">正在整理已保存曲线 {result.source_bytes?`${Math.min(100,100*(result.indexed_bytes??0)/result.source_bytes).toFixed(0)}%`:""}，测试和记录继续。</p>}
        {mode==="events"&&<div className="event-list"><button disabled={eventBusy} onClick={()=>loadEvents()}>刷新事件</button>{events.map((event,i)=>{const t=event.time_s??event.started_s??event.trigger_s??0;return <button key={i} onClick={()=>{const a=String(Math.max(0,t-2)),b=String(t+2);setStart(a);setEnd(b);load(a,b);}}>{t.toFixed(2)} s · {event.type}</button>;})}{nextOffset!==null&&<button disabled={eventBusy} onClick={()=>loadEvents(nextOffset)}>下一批事件</button>}{!events.length&&<span className="muted">{eventBusy?"读取中…":"尚无已保存的事件索引"}</span>}</div>}
        <div className="plot-card">
          <div className="curve-legend" aria-label="当前曲线">{shown.map(s=>{const c=COLORS[Math.max(0,allJoints.indexOf(parts(s.id).joint))%COLORS.length];return <span key={s.id}><i style={{borderColor:c,borderTopStyle:parts(s.id).metric==="command_position_rad"?"dashed":"solid"}}/>{curveLabel(s.id)}</span>;})}</div>
          {shown.length&&shown.some(s=>s.points.length)?<WorkspaceChart series={shown} live={!savedView} follow={mode!=="live"||!paused} windowSeconds={Number(rangePreset)||60} reset={reset} joints={allJoints} onInspect={inspect} onPoint={point} markers={markers}/>:<div className="plot-empty"><strong>{!ids.length?"选择关节，开始对比":!savedView?"等待当前测量":"查询所选时段的已保存曲线"}</strong><p>{!savedView?"收到有效测量后自动显示；缺失与断流保留断点。":"运行中也可读取已保存时段；自定义范围后点击“查询曲线”。"}</p></div>}
          <div className="plot-footer"><span>{!savedView?`本地时间 · ${timeRangeLabel(rangePreset)}`:`执行内秒数 · ${timeRangeLabel(rangePreset)}${result?.signature===signature?` · 概览分辨率 ${result.resolution_s.toFixed(3)} s，浅线保留极值${result.saved_until_s!==undefined?` · 已保存至 ${result.saved_until_s.toFixed(1)} s`:""}${result.edge_approximation?" · 边界按聚合桶":""}`:""}`}</span><span>滚轮缩放 · 拖动平移 · 点击取 A / B 点</span></div>
        </div>
        <div className="cursor-strip"><span>{markers.length?`A ${markers[0][1].toFixed(4)} ${shown[0]?.unit??""}`:"点击曲线可测量两点差值"}{markers.length===2&&` · B ${markers[1][1].toFixed(4)} ${shown[0]?.unit??""} · Δt ${((markers[1][0]-markers[0][0])/1000).toFixed(3)} s · Δy ${(markers[1][1]-markers[0][1]).toFixed(4)} ${shown[0]?.unit??""}`}</span>{!!markers.length&&<button onClick={()=>setMarkers([])}>清除标记</button>}</div>
        <p className="workspace-note">曲线时间范围不改变测试时长。{mode==="live"?"拖动或取点时暂停画面，点击“继续实时”回到最新；测量和记录继续。":"仅显示实际保存的证据，无法恢复未保存的样本。"}{savedView&&" 长时段显示有界概览；放大后可进一步查看已保存细节。"}</p>
      </section>
    </div>
  </main>;
}
