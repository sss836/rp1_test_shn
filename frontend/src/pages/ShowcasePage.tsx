import {lazy,Suspense,useEffect,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {apiRequest} from "../client";
import {HomePage} from "./HomePage";
import type {DashboardHome} from "../types";
import {formatDate} from "../navigation";
const ShowcaseCurves=lazy(()=>import("./ShowcaseCurves"));
export function ShowcaseLauncher({domain}:{domain:"RESEARCH"|"FACTORY"}){
 const topics=useQuery({queryKey:["showcase-topics"],queryFn:({signal})=>apiRequest<Array<{id:string;alias:string;domain:string}>>("/api/v1/showcase/topics",{signal})});const [error,setError]=useState("");
 return <section><h1>{domain==="FACTORY"?"生产展示":"测试展示"}</h1><p>讲解会话只提供获准主题，进入后需重新登录才能回到内部操作。</p>{topics.error&&<p role="alert">{topics.error.message}</p>}{error&&<p role="alert">{error}</p>}{topics.data?.filter(t=>t.domain===domain).map(t=><button key={t.id} onClick={()=>void apiRequest<{url:string}>("/api/v1/showcase/session",{method:"POST",body:JSON.stringify({topic_id:t.id})}).then(v=>location.assign(v.url)).catch(e=>setError(String(e)))}>进入 {t.alias}</button>)}{!topics.isPending&&!topics.data?.some(t=>t.domain===domain)&&<p>暂无获准主题。需配置主题别名、归档范围和素材权属后启用。</p>}</section>;
}
type Showcase={selected_curves?:import("../components/TelemetryChart").CurveSeries[];alias:string;domain:"RESEARCH"|"FACTORY";source:string;generated_at:string;data_cutoff_at:string|null;projection:DashboardHome;completed_count:number;passed_count:number;denominator:number;pass_rate:number|null;exposure_note:string;pending_note:string;media_allowed:boolean};
export function ShowcaseViewer(){
 const query=useQuery({queryKey:["showcase-view"],queryFn:({signal})=>apiRequest<Showcase>("/api/v1/showcase/view",{signal}),refetchInterval:30000,retry:0});const [now,setNow]=useState(Date.now);useEffect(()=>{const id=setInterval(()=>setNow(Date.now()),1000);return()=>clearInterval(id);},[]);
 if(query.error||!query.data)return <main><h1>只读展示</h1><p>{query.error?.message??"正在读取获准主题"}</p><a href="/">讲解员登录</a></main>;
 const data=query.data;return <main className="showcase-view"><header><h1>{data.domain==="FACTORY"?"生产展示":"测试展示"} · {data.alias}</h1><button onClick={()=>void document.documentElement.requestFullscreen?.()}>全屏</button><a href="/">讲解员登录</a></header><p>来源：{data.source} · 历史范围截止 {formatDate(data.data_cutoff_at)} · 展示快照 {formatDate(data.generated_at)}{now-Date.parse(data.generated_at)>300000&&" · 快照已陈旧，非实时"}</p><p>{data.domain==="FACTORY"?`已确认完成 ${data.completed_count} · 合格 ${data.passed_count} / 已判 ${data.denominator} · 合格率 ${data.pass_rate===null?"—":(data.pass_rate*100).toFixed(1)+"%"}`:"仅统计本主题获准且已批准的测试记录。"} {data.pending_note}</p><p>{data.exposure_note}</p><div className="showcase-projection"><HomePage assetKind="MODULE" projection={data.projection} mediaAllowed={data.media_allowed} navigate={()=>{}}/></div>{!!data.selected_curves?.length&&<Suspense fallback={<p>正在载入精选曲线</p>}><ShowcaseCurves series={data.selected_curves}/></Suspense>}</main>;
}
