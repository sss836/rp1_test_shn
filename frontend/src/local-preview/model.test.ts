import {describe,it,expect} from "vitest";
import {curveLabel,availableGroups,evidenceSeries,capabilitiesFromFrames,displaySeries,liveSeries,mergeState,metricIds,type Capability,type Frame,type PreviewState} from "./model";
const frame=(sequence:number,t=sequence*.05):Frame=>({sequence,received_utc_s:t,values:{"left_elbow_joint/position_rad":sequence},quality:{"left_elbow_joint/position_rad":"valid"}});
const state=(frames:Frame[],stream_id="a"):PreviewState=>({run_id:"r",stream_id,frames,stale:false,age_s:0,observed_rate_hz:20,window_s:60,last_sequence:frames.at(-1)?.sequence??0});
const caps:Capability[]=[{id:"left_elbow_joint/position_rad",unit:"rad",source:"x",available:true},{id:"left_elbow_joint/command_position_rad",unit:"rad",source:"x",available:true},{id:"left_elbow_joint/bus_voltage_v",unit:"V",source:"x",available:false}];
describe("曲线数据契约",()=>{
 it("历史间隙同时打断均值和极值，不能画出跨间隙连线",()=>{const s=evidenceSeries([{id:"x",unit:"rad",points:[[0,1],[1000,2],[2000,3]],extrema:[[0,0,2],[1000,1,3],[2000,2,4]]}],[[.5,1.5]])[0];expect(s.points.filter(p=>p[0]===1000).every(p=>p[1]===null)).toBe(true);expect(s.extrema?.filter(p=>p[0]===1000).every(p=>p[1]===null&&p[2]===null)).toBe(true);});
 it("按指标与关节选择，不混入异单位或缺失来源",()=>{expect(metricIds(caps,["left_elbow_joint"],"position")).toHaveLength(2);expect(metricIds(caps,["left_elbow_joint"],"position",false)).toEqual([caps[0].id]);expect(availableGroups(caps).map(g=>g.id)).toEqual(["position"]);});
 it("增量去重、60秒/1200帧上限、换执行不拼接",()=>{let current=state([]);for(let i=1;i<=40;i++)current=mergeState(current,state(Array.from({length:60},(_,j)=>frame((i-1)*60+j+1))));expect(current.frames).toHaveLength(1200);const same=mergeState(current,state(current.frames.slice(-20)));expect(same.frames).toEqual(current.frames);expect(mergeState(current,state([frame(1)],"b")).frames).toEqual([frame(1)]);});
 it("页面内存预算截断大帧",()=>{const large=frame(1);large.values={...large.values,["x".repeat(100000)]:1};const next=mergeState(undefined,state(Array.from({length:100},(_,i)=>({...large,sequence:i+1,received_utc_s:i*.05}))));expect(new TextEncoder().encode(JSON.stringify(next.frames)).length).toBeLessThanOrEqual(4*1024*1024);});
 it("断帧、陈旧值保留断点，度数转换不改原值",()=>{const second=frame(3);second.quality[caps[0].id]="stale";const raw=liveSeries(state([frame(1),second]),[caps[0].id],caps);expect(raw[0].points.map(p=>p[1])).toEqual([1,null,null]);const shown=displaySeries(raw,true);expect(shown[0].unit).toBe("°");expect(shown[0].points[0][1]).toBeCloseTo(180/Math.PI);expect(raw[0].points[0][1]).toBe(1);});
 it("压缩到600点以内仍保留尖峰极值和空白",()=>{const frames=Array.from({length:1200},(_,i)=>frame(i+1));frames[300].values[caps[0].id]=9999;frames[500].quality[caps[0].id]="missing";const s=liveSeries(state(frames),[caps[0].id],caps)[0];expect(s.points.length).toBeLessThanOrEqual(600);expect(s.extrema?.some(v=>v[2]===9999)).toBe(true);expect(s.points.some(v=>v[1]===null)).toBe(true);});
 it("初始无通道，收到缓存后发现；无变化保持目录稳定",()=>{const found=capabilitiesFromFrames([], [frame(1)]);expect(found[0].available).toBe(true);expect(capabilitiesFromFrames(found,[frame(2)])).toBe(found);});
});

it("109估算力矩与108力矩同组、标签分明，Iq与Kt保留独立单位",()=>{
 const channels:Capability[]=[{id:"old/torque_nm",unit:"N·m",source:"drive",available:true},{id:"new/estimated_torque_nm",unit:"N·m",source:"device_kt_times_iq",available:true},{id:"new/iq_current_a",unit:"A",source:"drive",available:true},{id:"new/torque_kt_nm_a",unit:"N·m/A",source:"device_parameter_0x16",available:true}];
 expect(metricIds(channels,["old","new"],"torque")).toEqual(["old/torque_nm","new/estimated_torque_nm"]);
 expect(curveLabel(channels[1].id)).toContain("估算力矩");
 expect(metricIds(channels,["new"],"iq")).toEqual([channels[2].id]);
 expect(availableGroups(channels).map(g=>g.id)).toEqual(["torque","iq","kt"]);
});
