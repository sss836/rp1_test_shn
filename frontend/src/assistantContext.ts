import { useEffect,useSyncExternalStore } from "react";
import type { PageContext } from "./assistantApi";

let snapshot:Partial<PageContext>|null=null;
const listeners=new Set<()=>void>();
function publish(value:Partial<PageContext>|null){snapshot=value;listeners.forEach((listener)=>listener());}
export function useAssistantPageContext(value:Partial<PageContext>){
  const encoded=JSON.stringify(value);
  useEffect(()=>{publish(JSON.parse(encoded) as Partial<PageContext>);return()=>publish(null);},[encoded]);
}
export function useAssistantContext(){return useSyncExternalStore((listener)=>{listeners.add(listener);return()=>{listeners.delete(listener);};},()=>snapshot);}
