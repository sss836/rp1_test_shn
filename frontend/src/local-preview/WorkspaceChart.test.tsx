// @vitest-environment jsdom
import {cleanup,render} from "@testing-library/react";
import {afterEach,beforeEach,it,expect,vi} from "vitest";
import {WorkspaceChart,chartOption} from "./WorkspaceChart";
const mock=vi.hoisted(()=>({setOption:vi.fn(),dispose:vi.fn(),on:vi.fn(),zrOn:vi.fn(),resize:vi.fn(),containPixel:vi.fn(()=>true),convertToPixel:vi.fn((_:unknown,p:number[])=>p)}));
vi.mock("echarts",()=>({init:()=>({...mock,getZr:()=>({on:mock.zrOn})})}));
const series=[{id:"left_elbow_joint/position_rad",unit:"rad",points:[[100,3],[200,null]] as Array<[number,number|null]>}];
beforeEach(()=>{vi.clearAllMocks();vi.stubGlobal("ResizeObserver",class{observe(){}disconnect(){}});});
afterEach(()=>{cleanup();vi.unstubAllGlobals();});
it("历史缩放不会因状态重绘回弹；重置才恢复全范围",()=>{
 const p={series,live:false,follow:true,windowSeconds:30,reset:0,joints:["left_elbow_joint"],onInspect:vi.fn(),onPoint:vi.fn(),markers:[] as Array<[number,number]>};
 const {rerender}=render(<WorkspaceChart {...p}/>);expect(mock.setOption).toHaveBeenCalledTimes(2);
 const inspect=mock.on.mock.calls.find(c=>c[0]==="datazoom")![1];inspect();expect(p.onInspect).toHaveBeenCalledOnce();
 rerender(<WorkspaceChart {...p}/>);expect(mock.setOption).toHaveBeenCalledTimes(2);
 rerender(<WorkspaceChart {...p} reset={1}/>);expect(mock.setOption).toHaveBeenCalledTimes(3);
});
it("冻结时更新数据不写缩放范围，继续实时才跟随",()=>{
 const p={series,live:true,follow:false,windowSeconds:5,reset:0,joints:["left_elbow_joint"],onInspect:vi.fn(),onPoint:vi.fn(),markers:[] as Array<[number,number]>};
 const {rerender}=render(<WorkspaceChart {...p}/>);expect(mock.setOption).toHaveBeenCalledTimes(1);
 rerender(<WorkspaceChart {...p} follow/>);expect(mock.setOption.mock.calls.at(-1)![0].dataZoom[0]).toMatchObject({startValue:-4800,endValue:200});
});
it("取点选择附近的真实显示点，空白处不捏造数值",()=>{
 const point=vi.fn();render(<WorkspaceChart series={series} live follow windowSeconds={30} reset={0} joints={[]} onInspect={()=>{}} onPoint={point} markers={[]}/>);
 const click=mock.zrOn.mock.calls.find(c=>c[0]==="click")![1];click({offsetX:101,offsetY:3});expect(point).toHaveBeenCalledWith(100,3);
 click({offsetX:1000,offsetY:300});expect(point).toHaveBeenCalledOnce();
});
it("图例在图外，滑块和坐标轴分开；目标虚线与实际同色",()=>{
 const option=chartOption([...series,{...series[0],id:"left_elbow_joint/command_position_rad"}],true,["left_elbow_joint"],[]);
 expect(option.legend.show).toBe(false);expect(option.grid.bottom).toBeGreaterThan(70);expect(option.dataZoom[1]).toMatchObject({height:24,bottom:14});expect(option.series[1].lineStyle).toMatchObject({color:option.series[0].lineStyle.color,type:"dashed"});
});
