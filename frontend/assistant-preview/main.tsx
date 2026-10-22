import { useEffect,useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { AssistantPanel } from "../src/components/AssistantPanel";
import type { PageContext,AssistantCard,ChatJob,ChatHistory } from "../src/assistantApi";
import "../src/styles.css";
import "./preview.css";

const archive="11111111-1111-4111-8111-111111111111";
const campaign="22222222-2222-4222-8222-222222222222";
const histories=new Map<string,ChatHistory>();
const jobs=new Map<string,ChatJob>();
let entryId=0;
const envelope=(data:unknown,status=200)=>new Response(JSON.stringify({data}),{status,headers:{"Content-Type":"application/json"}});
const error=(message:string)=>new Response(JSON.stringify({error:{code:"preview_only",message}}),{status:400});
function evidence(context:PageContext,message:string):{reply:string;card:AssistantCard}{
  const updated_at=new Date().toISOString();
  if(message.includes("工位")||context.view==="stations")return {reply:"【合成预览】示例工位A在线，状态为空闲；示例工位B离线。这里只展示心跳快照，不表示真实设备状态。",card:{type:"station_status",title:"当前可见工位状态（合成）",source:"/api/v1/hmi/stations（预览合成接口）",updated_at,range:"最新心跳快照",unit:"工位",scope:"界面示例；未连接设备",data:{items:[{station_name:"示例工位A",state:"IDLE",online:true,last_seen_at:updated_at},{station_name:"示例工位B",state:"OFFLINE",online:false,last_seen_at:updated_at}]},target:{view:"stations",id:null}}};
  if(message.includes("计划")||message.includes("进展")||context.view==="planning")return {reply:"【合成预览】示例计划配置3项，已有2次执行记录。此处仅展示进展卡片，不生成正式统计。",card:{type:"test_progress",title:"测试计划与执行进展（合成）",source:"/api/v1/setup/campaigns（预览合成接口）",updated_at,range:"当前快照",unit:"项/次",scope:"运行进展示例",data:{items:[{id:campaign,name:"示例耐久计划",configured_items:3,executions:[{status:"COMPLETED",count:1},{status:"RUNNING",count:1}]}]},target:{view:"planning",id:campaign}}};
  return {reply:"【合成预览】所选1—2秒区间内，有效位置样本2条、缺失1条，平均值15 rad。该回复为预设演示，不调用DeepSeek。",card:{type:"curve_statistics",title:"归档原始区间精确统计（合成）",source:`/api/v1/archives/${archive}/statistics（预览合成接口）`,updated_at,range:{start:1,end:2},unit:"rad",scope:"合成原始区间检查，不代表正式审批",data:{range:{start:1,end:2},series:[{metric:"pos_rad",subject:"joint",unit:"rad",count:2,missing:1,mean:15,min:10,max:20}]},target:{view:"archive",id:archive}}};
}

// The preview has no API proxy. All assistant calls stay in this page's memory.
window.fetch=async(input,init)=>{
  const path=new URL(typeof input==="string"?input:input instanceof URL?input.href:input.url,location.href).pathname;
  if(init?.signal?.aborted)throw new DOMException("已取消","AbortError");
  if(path==="/api/v1/assistant/budget")return envelope({day:new Date().toISOString().slice(0,10),spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:true});
  if(path==="/api/v1/assistant/threads")return envelope([...histories.keys()].map(id=>({id,updated_at:new Date().toISOString()})));
  if(path.startsWith("/api/v1/assistant/threads/"))return envelope(histories.get(path.split("/").at(-1)!)??{messages:[],jobs:[]});
  if(path==="/api/v1/assistant/jobs"&&init?.method==="POST"){
    const payload=JSON.parse(String(init.body));
    if(jobs.has(payload.request_key))return envelope(jobs.get(payload.request_key));
    const thread=payload.thread_id??crypto.randomUUID();const {reply,card}=evidence(payload.context,payload.message);
    const job:ChatJob={id:payload.request_key,thread_id:thread,state:"COMPLETED",rounds:2,context:payload.context,result:{reply,cards:[card]}};
    jobs.set(job.id,job);const history=histories.get(thread)??{messages:[],jobs:[]};
    history.messages.push({id:++entryId,job_id:job.id,role:"user",body:payload.message,context:payload.context,cards:[],created_at:new Date().toISOString()});
    history.messages.push({id:++entryId,job_id:job.id,role:"assistant",body:reply,context:payload.context,cards:[card],created_at:new Date().toISOString()});
    history.jobs.unshift({id:job.id,state:job.state});histories.set(thread,history);
    return envelope({id:job.id,thread_id:thread,state:"QUEUED"},202);
  }
  if(path.startsWith("/api/v1/assistant/jobs/"))return envelope(jobs.get(path.split("/").at(-1)!));
  return error("此页面只提供合成查询预览，不连接业务接口或执行写入。");
};

function Preview(){
  const [view,setView]=useState("curves"),[notice,setNotice]=useState("");
  useEffect(()=>{window.dispatchEvent(new Event("rp1-open-assistant"));},[]);
  const context:PageContext=view==="curves"?{view,kind:"MODULE",archive_id:archive,metrics:["pos_rad"],subjects:["joint"],start:1,end:2}:{view,kind:"MODULE",...(view==="planning"?{object_id:campaign}:{})};
  return <div className="demo-shell"><header className="demo-header"><strong>RP1 可靠性平台</strong><span>助手界面预览 · 合成数据</span></header>
    <main className="demo-main"><h1>先看看助手效果</h1><p className="demo-note">这里使用实际助手面板组件和预设合成回执。无需登录，不调用模型，不保存业务对象，刷新页面即重置。</p>
      <nav className="demo-tabs">{[["curves","曲线分析"],["stations","工位状态"],["planning","测试进展"]].map(([key,label])=><button key={key} className={key===view?"selected":""} onClick={()=>{setView(key);setNotice("");}}>{label}</button>)}</nav>
      <section className="demo-content"><h2>{view==="curves"?"示例归档 · 位置曲线":view==="stations"?"示例工位快照":"示例耐久计划"}</h2>
        {view==="curves"?<><p>当前选区：1—2秒 · 指标：pos_rad · 单位：rad</p><svg viewBox="0 0 600 160" role="img" aria-label="合成位置曲线"><path d="M40 15V135H580" fill="none" stroke="#afbdd0"/><rect x="210" y="15" width="140" height="120" fill="#1769e818"/><path d="M40 115 150 95 250 75 350 35 460 85 580 110" fill="none" stroke="#1769e8" strokeWidth="3"/><text x="45" y="154">0 s</text><text x="215" y="154">1 s</text><text x="350" y="154">2 s</text><text x="550" y="154">3 s</text></svg><p>在右侧输入：<strong>解释当前选区</strong></p></>:view==="stations"?<><p>示例工位A · 在线 · 空闲</p><p>示例工位B · 离线</p><p>在右侧输入：<strong>查询工位状态</strong></p></>:<><p>配置3项 · 已有2次执行记录</p><p>在右侧输入：<strong>查询当前计划进展</strong></p></>}
        <button onClick={()=>window.dispatchEvent(new Event("rp1-open-assistant"))}>打开右侧助手</button>{notice&&<p role="status">{notice}</p>}
      </section><p className="demo-note">“打开对应对象”在本预览中只切换合成页面。正式站点仍是原运行版本。</p>
    </main><AssistantPanel accountKey="isolated-interface-preview" context={context} navigate={(target,id)=>{setView(target==="archive"?"curves":target);setNotice(`已定位合成对象：${id??target}`);}}/></div>;
}
const cache=new QueryClient({defaultOptions:{queries:{retry:false}}});
createRoot(document.getElementById("root")!).render(<QueryClientProvider client={cache}><Preview/></QueryClientProvider>);
