import {useEffect,useRef} from "react";
import * as echarts from "echarts";
export type CurveSeries={id:string;unit:string;points:Array<[number,number|null]>;extrema?:Array<[number,number|null,number|null]>};
export function TelemetryChart({series,onPoint,elapsed=false}:{series:CurveSeries[];onPoint?:(x:number,y:number)=>void;elapsed?:boolean}){
 const root=useRef<HTMLDivElement>(null),chart=useRef<echarts.ECharts|null>(null),point=useRef(onPoint);
 useEffect(()=>{point.current=onPoint;},[onPoint]);
 useEffect(()=>{if(!root.current)return;const instance=echarts.init(root.current);chart.current=instance;instance.group="rp1-preview";echarts.connect("rp1-preview");instance.on("click",p=>{const v=p.value as number[];if(v&&v[1]!=null)point.current?.(v[0],v[1]);});const resize=new ResizeObserver(()=>instance.resize());resize.observe(root.current);return()=>{resize.disconnect();instance.dispose();chart.current=null;};},[]);
 useEffect(()=>{chart.current?.setOption({animation:false,legend:{type:"scroll"},tooltip:{trigger:"axis"},grid:{left:70,right:25,top:45,bottom:65},xAxis:{type:elapsed?"value":"time",name:elapsed?"执行内 s":"UTC",axisLabel:elapsed?{formatter:(v:number)=>String(v/1000)}:{}},yAxis:{type:"value",name:series[0]?.unit??"",scale:true},dataZoom:[{type:"inside"},{type:"slider"}],series:series.flatMap(s=>[{name:s.id,type:"line",showSymbol:false,connectNulls:false,smooth:false,data:s.points},...(s.extrema?[{name:s.id+" 最小值",type:"line",showSymbol:false,connectNulls:false,lineStyle:{opacity:.25},data:s.extrema.map(v=>[v[0],v[1]])},{name:s.id+" 最大值",type:"line",showSymbol:false,connectNulls:false,lineStyle:{opacity:.25},data:s.extrema.map(v=>[v[0],v[2]])}]:[])])},{replaceMerge:["series"]});},[series,elapsed]);
 return <div ref={root} style={{height:320,width:"100%"}} aria-label={`${series[0]?.unit??""}单位曲线`}/>;
}
