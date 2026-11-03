import {useRef,useState} from "react";
import {useQuery} from "@tanstack/react-query";
import {apiRequest,requestId} from "../client";
import {formatDate} from "../navigation";
type Note={revision:number;kind:string;note:string;destination:string;actor:string;created_at:string};
const labels:Record<string,string>={REWORK:"待返工",HANDOFF:"已记录交接",ACKNOWLEDGED:"已知悉"};
export function FactoryHandoff({id,canEdit}:{id:string;canEdit:boolean}){
 const [open,setOpen]=useState(false);
 const [kind,setKind]=useState("HANDOFF"),[note,setNote]=useState(""),[destination,setDestination]=useState(""),[error,setError]=useState(""),[busy,setBusy]=useState(false);const pending=useRef<{body:string;key:string}|null>(null);
 const query=useQuery({enabled:open,queryKey:["factory-handoff",id],queryFn:({signal})=>apiRequest<{items:Note[]}>(`/api/v1/archives/${id}/handoffs`,{signal})});
 function save(){const body=JSON.stringify({kind,note,destination,expected_revision:query.data?.items[0]?.revision??0});if(pending.current?.body!==body)pending.current={body,key:requestId()};setBusy(true);void apiRequest(`/api/v1/archives/${id}/handoffs`,{method:"POST",body,headers:{"Idempotency-Key":pending.current.key}}).then(()=>{setNote("");setError("");void query.refetch();}).catch(e=>setError(String(e))).finally(()=>setBusy(false));}
 if(!open)return <button type="button" onClick={()=>setOpen(true)}>返工与交接</button>;
 return <section className="panel"><h2>返工与交接</h2><p>此处记录处置和交接；测量事实、人工判定及审批结果分别保留。</p>{(error||query.error)&&<p role="alert">{error||query.error?.message}</p>}{canEdit&&<><label>处置<select value={kind} onChange={e=>setKind(e.target.value)}>{Object.entries(labels).map(([k,v])=><option key={k} value={k}>{v}</option>)}</select></label>{kind==="HANDOFF"&&<label>交接去向<input value={destination} maxLength={100} onChange={e=>setDestination(e.target.value)}/></label>}<label>实际情况与下一步<textarea value={note} maxLength={1000} onChange={e=>setNote(e.target.value)}/></label><button disabled={busy||query.isPending||!!query.error||!note.trim()||(kind==="HANDOFF"&&!destination.trim())} onClick={save}>记录处置</button></>}{query.data?.items.map(item=><p key={item.revision}>第 {item.revision} 版 · {labels[item.kind]} · {item.note} {item.destination&&`→ ${item.destination}`} · {item.actor} · {formatDate(item.created_at)}</p>)}</section>;
}
