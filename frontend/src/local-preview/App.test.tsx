// @vitest-environment jsdom
import {cleanup,fireEvent,render,screen,waitFor,act,within} from "@testing-library/react";
import {afterEach,beforeEach,it,expect,vi} from "vitest";
import App from "./App";
import type {CurveSeries} from "../components/TelemetryChart";
const plot=vi.hoisted(()=>({props:null as unknown as {series:CurveSeries[];onInspect:()=>void;onPoint:(x:number,y:number)=>void}}));
vi.mock("./WorkspaceChart",()=>({WorkspaceChart:(props:typeof plot.props)=>{plot.props=props;return <div data-testid="plot">{props.series.map(s=><span key={s.id}>{s.id}</span>)}<button onClick={props.onInspect}>模拟拖动</button><button onClick={()=>props.onPoint(1000,2)}>模拟取点</button></div>;}}));
const joints=["left_shoulder_pitch_joint","left_shoulder_roll_joint","left_shoulder_yaw_joint","left_elbow_joint","left_wrist_roll_joint","left_wrist_yaw_joint","left_wrist_pitch_joint"];
const keys=["position_rad","command_position_rad","speed_rad_s","torque_nm","temperature_c","bus_voltage_v"];
const metrics=joints.flatMap(j=>keys.map(k=>({id:`${j}/${k}`,unit:k.includes("position")?"rad":k==="speed_rad_s"?"rad/s":k==="torque_nm"?"N·m":k==="temperature_c"?"°C":"V",source:"cache",available:k!=="bus_voltage_v"})));
let sequence=1;
const calls: Array<{url:string;signal:AbortSignal}>=[];
beforeEach(()=>{sequence=1;calls.length=0;vi.stubGlobal("fetch",vi.fn(async(path:string,options:{signal:AbortSignal})=>{calls.push({url:path,signal:options.signal});let data:unknown;if(path.includes("capabilities"))data={metrics};else if(path.includes("/state")){const n=sequence++;data={run_id:"run-1",stream_id:"s",frames:[{sequence:n,received_utc_s:1000+n*.05,values:Object.fromEntries(metrics.map(c=>[c.id,c.available?.1:null])),quality:Object.fromEntries(metrics.map(c=>[c.id,c.available?"valid":"missing"]))}],last_sequence:n,stale:false,age_s:0,window_s:60,observed_rate_hz:20};}else if(path.includes("/events"))data={items:[{type:"tracking",time_s:12}],next_offset:null};else data={series:[{id:joints[0]+"/position_rad",unit:"rad",points:[[1000,.2]]}],resolution_s:.1};return {ok:true,json:async()=>data};}));});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
async function mount(){render(<App/>);await screen.findByTestId("plot");}
it("默认仅一关节位置实际/目标；选择器不再平铺底层字段",async()=>{await mount();expect(plot.props.series).toHaveLength(2);expect(plot.props.series.map(s=>s.unit)).toEqual(["°","°"]);expect(within(screen.getByRole("complementary")).getAllByRole("checkbox")).toHaveLength(7);expect(within(screen.getByRole("navigation",{name:"指标类别"})).queryByRole("button",{name:"母线电压"})).toBeNull();fireEvent.click(screen.getByRole("button",{name:"速度"}));expect(plot.props.series).toHaveLength(1);expect(plot.props.series[0].id).toContain("speed_rad_s");});
it("全选自动只看七关节实际值，清空后轮询不会擅自勾回",async()=>{await mount();fireEvent.click(screen.getByRole("button",{name:"全选"}));expect(plot.props.series).toHaveLength(7);expect(plot.props.series.every(s=>!s.id.includes("command"))).toBe(true);fireEvent.click(screen.getByRole("button",{name:"清空"}));await act(async()=>{await new Promise(r=>setTimeout(r,300));});expect(screen.queryByTestId("plot")).toBeNull();fireEvent.click(screen.getByRole("button",{name:"左肘关节"}));expect(plot.props.series).toHaveLength(1);expect(plot.props.series[0].id).toContain("elbow");});
it("拖动暂停画面与轮询，继续实时取后续序号",async()=>{await mount();fireEvent.click(screen.getByRole("button",{name:"模拟拖动"}));const count=calls.filter(c=>c.url.includes("/state")).length;await act(async()=>{await new Promise(r=>setTimeout(r,300));});expect(calls.filter(c=>c.url.includes("/state"))).toHaveLength(count);fireEvent.click(screen.getByRole("button",{name:"继续实时"}));await waitFor(()=>expect(calls.filter(c=>c.url.includes("/state")).length).toBeGreaterThan(count));expect(calls.filter(c=>c.url.includes("/state")).at(-1)?.url).toContain("after=1");});
it("历史条件改变即隐藏旧曲线，事件跳转请求不会被自己的范围修改取消",async()=>{await mount();fireEvent.click(screen.getByRole("button",{name:"历史记录"}));await screen.findByRole("button",{name:"查询曲线"});fireEvent.click(screen.getByRole("button",{name:"查询曲线"}));await screen.findByTestId("plot");fireEvent.change(screen.getByRole("spinbutton",{name:"结束秒数"}),{target:{value:"20"}});expect(screen.queryByTestId("plot")).toBeNull();fireEvent.click(screen.getByRole("button",{name:"事件片段"}));await screen.findByRole("button",{name:"12.00 s · tracking"});fireEvent.click(screen.getByRole("button",{name:"12.00 s · tracking"}));await screen.findByTestId("plot");expect(calls.filter(c=>c.url.includes("/curves?")).at(-1)?.url).toContain("start=10&end=14");expect(calls.filter(c=>c.url.includes("/curves?")).at(-1)?.signal.aborted).toBe(false);});
it("历史失败保留错误，不能被实时缓存轮询清掉",async()=>{await mount();fireEvent.click(screen.getByRole("button",{name:"历史记录"}));await screen.findByRole("button",{name:"查询曲线"});vi.mocked(fetch).mockImplementationOnce(async()=>({ok:false,json:async()=>({detail:"执行尚未封存"})}) as Response);fireEvent.click(screen.getByRole("button",{name:"查询曲线"}));expect((await screen.findByRole("alert")).textContent).toContain("尚未封存");});
it("1分钟、10分钟、1小时及全部读取已保存时段，不受实时缓存截断",async()=>{
 await mount();const range=screen.getByRole("combobox",{name:"时间范围"});
 expect(within(range).getByRole("option",{name:"最近1分钟"})).toBeTruthy();
 for(const value of ["60","600","3600","all"]){
  fireEvent.change(range,{target:{value}});
  await waitFor(()=>expect(calls.filter(c=>c.url.includes("/curves?")).at(-1)?.url).toContain(`window=${value}`));
  await screen.findByTestId("plot");
 }
 expect(calls.every(c=>c.url.startsWith("/api/v1/preview/"))).toBe(true);
});
it("自定义可以跨24小时，参数修改不会继续显示旧范围",async()=>{
 await mount();fireEvent.change(screen.getByRole("combobox",{name:"时间范围"}),{target:{value:"custom"}});
 fireEvent.change(screen.getByRole("spinbutton",{name:"开始秒数"}),{target:{value:"3600"}});
 fireEvent.change(screen.getByRole("spinbutton",{name:"结束秒数"}),{target:{value:"172800"}});
 fireEvent.click(screen.getByRole("button",{name:"查询曲线"}));await screen.findByTestId("plot");
 expect(calls.filter(c=>c.url.includes("/curves?")).at(-1)?.url).toContain("start=3600&end=172800");
 fireEvent.change(screen.getByRole("spinbutton",{name:"结束秒数"}),{target:{value:"9000"}});
 expect(screen.queryByTestId("plot")).toBeNull();
});
it("全部整理进度可见，完成前不伪造完整曲线",async()=>{
 await mount();vi.mocked(fetch).mockImplementationOnce(async()=>({ok:true,json:async()=>({series:[],resolution_s:1,indexing:true,indexed_bytes:500,source_bytes:1000})}) as Response);
 fireEvent.change(screen.getByRole("combobox",{name:"时间范围"}),{target:{value:"all"}});
 expect((await screen.findByRole("status")).textContent).toContain("50%");
 expect(screen.queryByTestId("plot")).toBeNull();await screen.findByTestId("plot",{}, {timeout:2000});
});
it("自定义可按小时输入，取消未完成的长窗口请求后仍能提交",async()=>{
 await mount();let release:((value:Response)=>void)|undefined;
 vi.mocked(fetch).mockImplementationOnce(()=>new Promise(resolve=>{release=resolve;}));
 fireEvent.change(screen.getByRole("combobox",{name:"时间范围"}),{target:{value:"3600"}});
 await waitFor(()=>expect(release).toBeTruthy());
 fireEvent.change(screen.getByRole("combobox",{name:"时间范围"}),{target:{value:"custom"}});
 fireEvent.change(screen.getByRole("combobox",{name:"自定义时间单位"}),{target:{value:"h"}});
 fireEvent.change(screen.getByRole("spinbutton",{name:"开始时间"}),{target:{value:"0.5"}});
 fireEvent.change(screen.getByRole("spinbutton",{name:"结束时间"}),{target:{value:"2"}});
 const button=screen.getByRole("button",{name:"查询曲线"});expect((button as HTMLButtonElement).disabled).toBe(false);
 fireEvent.click(button);await screen.findByTestId("plot");
 expect(calls.filter(c=>c.url.includes("/curves?")).at(-1)?.url).toContain("start=1800&end=7200");
 await act(async()=>release?.({ok:true,json:async()=>({series:[],resolution_s:1})} as Response));
 expect(screen.getByTestId("plot")).toBeTruthy();
});
