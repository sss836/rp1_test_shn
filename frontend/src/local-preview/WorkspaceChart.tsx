import {useEffect,useRef} from "react";
import * as echarts from "echarts";
import type {CurveSeries} from "../components/TelemetryChart";
import {COLORS,curveLabel,parts} from "./model";

type Props={series:CurveSeries[];live:boolean;follow:boolean;windowSeconds:number;reset:number;joints:string[];onInspect:()=>void;onPoint:(x:number,y:number)=>void;markers:Array<[number,number]>};
export function chartOption(series:CurveSeries[],live:boolean,joints:string[],markers:Array<[number,number]>) {
  const elapsed=(v:number)=>`${Number((v/1000).toFixed(2))} s`;
  return {
    animation:false,
    legend:{show:false},
    tooltip:{trigger:"axis",confine:true,axisPointer:{type:"cross"},valueFormatter:(v:unknown)=>typeof v==="number"?v.toFixed(4):"—"},
    grid:{left:70,right:24,top:30,bottom:90},
    xAxis:{type:live?"time":"value",axisLabel:live?{formatter:(v:number)=>new Date(v).toLocaleTimeString("zh-CN",{hour12:false})}:{formatter:elapsed},axisPointer:{label:{formatter:(p:{value:number})=>live?new Date(p.value).toLocaleTimeString("zh-CN",{hour12:false}):elapsed(p.value)}},splitLine:{show:false}},
    yAxis:{type:"value",name:series[0]?.unit??"",scale:true,splitLine:{lineStyle:{color:"#e8edf3"}}},
    dataZoom:[{id:"inside",type:"inside",filterMode:"none",zoomOnMouseWheel:true,moveOnMouseMove:true},{id:"timeline",type:"slider",filterMode:"none",bottom:14,height:24,showDetail:false,brushSelect:false}],
    series:series.flatMap(s=>{
      const color=COLORS[Math.max(0,joints.indexOf(parts(s.id).joint))%COLORS.length], target=parts(s.id).metric==="command_position_rad";
      return [{id:s.id,name:curveLabel(s.id),type:"line",showSymbol:false,connectNulls:false,smooth:false,data:s.points,lineStyle:{color,width:1.8,type:target?"dashed":"solid"},itemStyle:{color},...(markers.length && s===series[0]?{markPoint:{symbolSize:32,data:markers.map((p,i)=>({name:i===0?"A":"B",coord:p,value:i===0?"A":"B"}))}}:{})},
        ...(s.extrema?[0,1].map(i=>({id:s.id+"-"+i,name:curveLabel(s.id)+(i===0?" 最小值":" 最大值"),type:"line",showSymbol:false,connectNulls:false,silent:true,data:s.extrema!.map(v=>[v[0],v[i+1]]),lineStyle:{color,opacity:.22,width:1},tooltip:{show:false}})):[])];
    }),
  };
}
export function WorkspaceChart(props:Props) {
  const root=useRef<HTMLDivElement>(null),chart=useRef<echarts.ECharts|null>(null),callbacks=useRef(props);
  useEffect(()=>{callbacks.current=props;},[props]);
  useEffect(()=>{
    if(!root.current)return;
    const instance=echarts.init(root.current);chart.current=instance;
    instance.on("datazoom",()=>callbacks.current.onInspect());
    instance.getZr().on("click",event=>{
      const pixel:[number,number]=[event.offsetX,event.offsetY];
      if(!instance.containPixel({gridIndex:0},pixel))return;
      let closest:[number,number]|undefined,distance=Infinity;
      for(const series of callbacks.current.series)for(const p of series.points){
        if(p[1]===null)continue;
        const at=instance.convertToPixel({xAxisIndex:0,yAxisIndex:0},p) as number[];
        const d=Math.hypot(at[0]-pixel[0],at[1]-pixel[1]);
        if(d<distance){distance=d;closest=[p[0],p[1]];}
      }
      if(closest && distance<=24)callbacks.current.onPoint(...closest);
    });
    const observer=new ResizeObserver(()=>instance.resize());observer.observe(root.current);
    return()=>{observer.disconnect();instance.dispose();chart.current=null;};
  },[]);
  useEffect(()=>{chart.current?.setOption(chartOption(props.series,props.live,props.joints,props.markers),{replaceMerge:["series"]});},[props.series,props.live,props.joints,props.markers]);
  useEffect(()=>{
    if(!props.follow)return;
    const xs=props.series.flatMap(s=>s.points.map(p=>p[0])),end=xs.length?Math.max(...xs):0;
    if(!end)return;
    chart.current?.setOption({dataZoom:[{id:"inside",startValue:props.live?end-props.windowSeconds*1000:Math.min(...xs),endValue:end},{id:"timeline",startValue:props.live?end-props.windowSeconds*1000:Math.min(...xs),endValue:end}]});
  },[props.series,props.follow,props.live,props.windowSeconds,props.reset]);
  return <div ref={root} className="workspace-plot" role="img" aria-label={`${props.series[0]?.unit??""}曲线，滚轮缩放、拖动平移，点击曲线取点`}/>;
}
