import type {CurveSeries} from "../components/TelemetryChart";

export type Frame = {sequence:number; received_utc_s:number; values:Record<string,number|null>; quality:Record<string,string>};
export type PreviewState = {run_id:string; stream_id:string; frames:Frame[]; stale:boolean; age_s:number|null; observed_rate_hz:number|null; window_s:number; last_sequence:number};
export type Capability = {id:string; unit:string; source:string; available:boolean};
export const TIME_RANGES=[['5','最近5秒'],['10','最近10秒'],['30','最近30秒'],['60','最近1分钟'],['600','最近10分钟'],['3600','最近1小时'],['all','本次测试全部'],['custom','自定义时段']] as const;
export function timeRangeLabel(value:string){return TIME_RANGES.find(r=>r[0]===value)?.[1]??'自定义时段';}
export const GROUPS = [
  {id:"position", label:"位置跟踪", keys:["position_rad", "command_position_rad"], unit:"rad"},
  {id:"speed", label:"速度", keys:["speed_rad_s"], unit:"rad/s"},
  {id:"torque", label:"力矩", keys:["torque_nm"], unit:"N·m"},
  {id:"temperature", label:"温度", keys:["temperature_c"], unit:"°C"},
  {id:"voltage", label:"母线电压", keys:["bus_voltage_v"], unit:"V"},
  {id:"current", label:"母线电流", keys:["bus_current_a"], unit:"A"},
  {id:"fault", label:"故障码", keys:["error_id"], unit:"code"},
  {id:"age", label:"反馈延迟", keys:["feedback_age_s"], unit:"s"},
] as const;
export type GroupId = typeof GROUPS[number]["id"];
const METRIC_NAMES:Record<string,string> = {position_rad:"实际位置", command_position_rad:"目标位置", speed_rad_s:"速度", torque_nm:"力矩", temperature_c:"温度", bus_voltage_v:"母线电压", bus_current_a:"母线电流", error_id:"故障码", feedback_age_s:"反馈延迟"};
const JOINT_NAMES:Record<string,string> = {shoulder_pitch:"肩俯仰", shoulder_roll:"肩侧摆", shoulder_yaw:"肩旋转", elbow:"肘关节", wrist_roll:"腕旋转", wrist_yaw:"腕偏航", wrist_pitch:"腕俯仰"};
export const COLORS = ["#2563eb", "#e17c28", "#0c9278", "#9a54cb", "#df4764", "#008da8", "#8b751b"];
export function parts(id:string) {const at=id.lastIndexOf("/"); return {joint:id.slice(0,at), metric:id.slice(at+1)};}
export function jointLabel(joint:string) {
  const match=/^(left|right)_(.+)_joint$/.exec(joint);
  return match && JOINT_NAMES[match[2]] ? `${match[1]==="left"?"左":"右"}${JOINT_NAMES[match[2]]}` : joint;
}
export function curveLabel(id:string) {const {joint,metric}=parts(id);return `${jointLabel(joint)} · ${METRIC_NAMES[metric]??metric}`;}
export function metricIds(caps:Capability[], joints:string[], group:GroupId, target=true) {
  const keys:readonly string[]=GROUPS.find(g=>g.id===group)!.keys;
  return caps.filter(c=>c.available && joints.includes(parts(c.id).joint) && keys.includes(parts(c.id).metric) && (target || parts(c.id).metric!=="command_position_rad")).map(c=>c.id);
}
export function availableGroups(caps:Capability[]) {return GROUPS.filter(g=>caps.some(c=>c.available && (g.keys as readonly string[]).includes(parts(c.id).metric)));}
export function capabilitiesFromFrames(current:Capability[], frames:Frame[]) {
  const entries=new Map(current.map(c=>[c.id,c]));
  for(const frame of frames) for(const [id,value] of Object.entries(frame.values)) {
    const previous=entries.get(id), metric=parts(id).metric;
    entries.set(id,{id,unit:previous?.unit??GROUPS.find(g=>(g.keys as readonly string[]).includes(metric))?.unit??"",source:previous?.source??"cached_observation",available:!!previous?.available || value!==null});
  }
  const next=[...entries.values()];
  return next.length===current.length && next.every((c,i)=>c.id===current[i].id && c.available===current[i].available)?current:next;
}
// Incremental display cache: a stream change clears old points, never joins two runs.
export function mergeState(previous:PreviewState|undefined, next:PreviewState):PreviewState {
  const prior=previous?.stream_id===next.stream_id?previous.frames:[];
  const last=prior.at(-1)?.sequence??0;
  let frames=[...prior,...next.frames.filter(f=>f.sequence>last)];
  const cutoff=(frames.at(-1)?.received_utc_s??0)-Math.min(next.window_s,60);
  frames=frames.filter(f=>f.received_utc_s>=cutoff).slice(-1200);
  let bytes=0,first=frames.length;
  while(first>0) {const size=new TextEncoder().encode(JSON.stringify(frames[first-1])).length;if(bytes+size>4*1024*1024)break;bytes+=size;first--;}
  return {...next,frames:frames.slice(first)};
}
export function liveSeries(data:PreviewState|undefined, ids:string[], caps:Capability[]):CurveSeries[] {
  return ids.map(id=>{
    const points:Array<[number,number|null]>=[];let previous:Frame|undefined;
    for(const frame of data?.frames??[]) {
      if(previous && (frame.sequence!==previous.sequence+1 || frame.received_utc_s-previous.received_utc_s>.2)) points.push([(previous.received_utc_s+frame.received_utc_s)*500,null]);
      points.push([frame.received_utc_s*1000,frame.quality[id]==="valid"?frame.values[id]??null:null]);previous=frame;
    }
    if(points.length<=600)return {id,unit:caps.find(c=>c.id===id)?.unit??"",points};
    // Mean plus extrema retains spikes. A bin containing a gap remains a gap.
    const compact:Array<[number,number|null]>=[],extrema:Array<[number,number|null,number|null]>=[];
    const stride=Math.ceil(points.length/600);
    for(let i=0;i<points.length;i+=stride){const bin=points.slice(i,i+stride), x=bin[0][0], values=bin.map(p=>p[1]).filter((v):v is number=>v!==null);const valid=values.length===bin.length;compact.push([x,valid?values.reduce((a,b)=>a+b,0)/values.length:null]);extrema.push([x,valid?Math.min(...values):null,valid?Math.max(...values):null]);}
    return {id,unit:caps.find(c=>c.id===id)?.unit??"",points:compact,extrema};
  });
}
export function displaySeries(series:CurveSeries[], degrees:boolean):CurveSeries[] {
  return series.map(s=>{const factor=degrees && (s.unit==="rad" || s.unit==="rad/s")?180/Math.PI:1;return {...s,unit:factor===1?s.unit:s.unit==="rad"?"°":"°/s",points:s.points.map(([x,y])=>[x,y===null?null:y*factor]),extrema:s.extrema?.map(([x,a,b])=>[x,a===null?null:a*factor,b===null?null:b*factor])};});
}

// Explicit historical discontinuities also break the min/max traces.
export function evidenceSeries(series:CurveSeries[], gaps:Array<[number,number]>):CurveSeries[] {
  const ranges=gaps.map(([a,b])=>[Math.min(a,b)*1000,Math.max(a,b)*1000]);
  return series.map(s=>({...s,
    points:[...s.points.map(([x,y]):[number,number|null]=>[x,ranges.some(([a,b])=>a<x&&x<b)?null:y]),...ranges.map(([a,b]):[number,null]=>[(a+b)/2,null])].sort((a,b)=>a[0]-b[0]),
    extrema:s.extrema?[...s.extrema.map(([x,a,b]):[number,number|null,number|null]=>ranges.some(([lo,hi])=>lo<x&&x<hi)?[x,null,null]:[x,a,b]),...ranges.map(([a,b]):[number,null,null]=>[(a+b)/2,null,null])].sort((a,b)=>a[0]-b[0]):undefined,
  }));
}
