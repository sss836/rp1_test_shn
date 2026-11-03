import { apiRequest, apiUpload } from "./client";

export const MAX_DIAGNOSTIC_ATTACHMENT_BYTES=32*1024*1024;

// Same-origin Cookie/CSRF client; legacy probes remain available only in the isolated regression mode.
export type AssistantProbe = {
  request_id: string;
  reply: string;
  model: "deepseek-flash";
  verification_mode: "live" | "mock";
  evidence: Array<{
    source: "/api/v1/hmi/stations";
    as_of_at: string;
    next_cursor: string | null;
    items: Array<{ installation_id: string; station_name: string; state: string; online: boolean; last_seen_at: string }>;
  }>;
};
export const assistantApi = {
  capabilities: (signal?: AbortSignal) => apiRequest<{ mode: "isolation_probe"|"assistant_runtime"; tools: string[]; writes_enabled: boolean; features?:{faq_publish?:boolean;support_workspace?:boolean;stream_events?:boolean;steering?:boolean;queued_followup?:boolean;local_permissions?:boolean} }>("/api/v1/assistant/capabilities", { signal }),
  probe: (signal?: AbortSignal) => apiRequest<AssistantProbe>("/api/v1/assistant/probe", { method: "POST", body: "{}", signal }),
  setupCommand: (messages: string[], requestKey: string, signal?: AbortSignal) => apiRequest<SetupOutcome>("/api/v1/assistant/setup/commands", {method:"POST",body:JSON.stringify({messages,request_key:requestKey}),signal}),
  adoptSetup: (proposalId: string, proposalHash: string, publishApproved = false) => apiRequest<SetupOutcome>("/api/v1/assistant/setup/adopt", {method:"POST",body:JSON.stringify({proposal_id:proposalId,proposal_hash:proposalHash,publish_approved:publishApproved})}),
  uploadAttachment:async(file:File,signal?:AbortSignal,onProgress?:(loaded:number,total:number)=>void)=>{
    if(file.size===0||file.size>MAX_DIAGNOSTIC_ATTACHMENT_BYTES)throw new Error("附件须为非空且每个不超过32MiB。");
    if(onProgress)return apiUpload<AssistantAttachment>("/api/v1/assistant/attachments",file,signal,onProgress);
    return apiRequest<AssistantAttachment>("/api/v1/assistant/attachments",{method:"POST",headers:{"Content-Type":file.type||"application/octet-stream","X-Filename-UTF8":encodeURIComponent(file.name),"X-Filename":encodeURIComponent(file.name)},body:file,signal});
  },
  deleteAttachment:(id:string,signal?:AbortSignal)=>apiRequest(`/api/v1/assistant/attachments/${id}`,{method:"DELETE",signal}),
  threads:(signal?:AbortSignal,q="")=>apiRequest<Array<{id:string;title?:string;updated_at:string}>>(`/api/v1/assistant/threads${q?`?q=${encodeURIComponent(q)}`:""}`,{signal}),
  renameThread:(id:string,title:string)=>apiRequest(`/api/v1/assistant/threads/${id}`,{method:"PATCH",body:JSON.stringify({title})}),
  deleteThread:(id:string)=>apiRequest(`/api/v1/assistant/threads/${id}`,{method:"DELETE"}),
  history:(id:string,signal?:AbortSignal)=>apiRequest<ChatHistory>(`/api/v1/assistant/threads/${id}`,{signal}),
  submitJob:(payload:JobPayload,signal?:AbortSignal)=>apiRequest<{id:string;thread_id:string;state:string}>("/api/v1/assistant/jobs",{method:"POST",body:JSON.stringify(payload),signal}),
  job:(id:string,signal?:AbortSignal)=>apiRequest<ChatJob>(`/api/v1/assistant/jobs/${id}`,{signal}),
  cancel:(id:string,signal?:AbortSignal)=>apiRequest(`/api/v1/assistant/jobs/${id}/cancel`,{method:"POST",body:"{}",...(signal?{signal}:{})}),
  budget:(signal?:AbortSignal)=>apiRequest<{day:string;spent_cny:number;reserved_cny:number;limit_cny:number;blocked:boolean;pricing_verified:boolean}>("/api/v1/assistant/budget",{signal}),
  events:(id:string,after:number,signal?:AbortSignal)=>apiRequest<{items:JobEvent[];next_seq:number;state:string;has_more?:boolean}>(`/api/v1/assistant/jobs/${id}/events?after=${after}`,{signal}),
  steer:(id:string,message:string,requestKey:string,signal?:AbortSignal)=>apiRequest(`/api/v1/assistant/jobs/${id}/inputs`,{method:"POST",body:JSON.stringify({request_key:requestKey,message,expected_job_id:id}),signal}),
  references:(kind:AssistantReference["kind"],q:string,signal?:AbortSignal)=>apiRequest<{items:AssistantReference[]}>(`/api/v1/assistant/references?kind=${kind}&q=${encodeURIComponent(q)}`,{signal}),
  feedback:(payload:{request_key:string;job_id:string;rating:"helpful"|"issue";note:string;share_for_faq?:boolean})=>apiRequest<Feedback>("/api/v1/assistant/feedback",{method:"POST",body:JSON.stringify(payload)}),
  feedbackList:(signal?:AbortSignal,shared=false)=>apiRequest<{items:Feedback[]}>(`/api/v1/assistant/feedback${shared?"?shared=true":""}`,{signal}),
  faq:(q:string,signal?:AbortSignal)=>apiRequest<{items:SharedFaq[]}>(`/api/v1/assistant/faq?q=${encodeURIComponent(q)}`,{signal}),
  publishFaq:(payload:{request_key:string;feedback_id:string;question:string;answer:string;faq_id?:string;expected_revision?:number})=>apiRequest<SharedFaq>("/api/v1/assistant/faq",{method:"POST",body:JSON.stringify(payload)}),
  repairs:(id:string,signal?:AbortSignal)=>apiRequest<{items:RepairProposal[]}>(`/api/v1/assistant/jobs/${id}/repairs`,{signal}),
  confirmRepair:(id:string,hash:string)=>apiRequest<RepairProposal>(`/api/v1/assistant/repairs/${id}/confirm`,{method:"POST",body:JSON.stringify({proposal_sha256:hash})}),
  localPermissions:(jobId:string,signal?:AbortSignal)=>apiRequest<LocalPermissions>(`/api/v1/assistant/jobs/${jobId}/permissions`,{signal}),
  decideLocalPermission:(jobId:string,id:string,payload:{request_key:string;expected_request_sha256:string;scope:"file"|"directory"|"deny"},signal?:AbortSignal)=>apiRequest<LocalPermission>(`/api/v1/assistant/jobs/${jobId}/permissions/${id}/decision`,{method:"POST",body:JSON.stringify(payload),signal}),
  retryLocalPermission:(jobId:string,id:string,payload:{request_key:string;expected_request_sha256:string},signal?:AbortSignal)=>apiRequest<LocalPermission>(`/api/v1/assistant/jobs/${jobId}/permissions/${id}/retry`,{method:"POST",body:JSON.stringify(payload),signal}),
  resumeJob:(jobId:string,requestKey:string,signal?:AbortSignal)=>apiRequest<{id?:string;job_id?:string;state:string}>(`/api/v1/assistant/jobs/${jobId}/resume`,{method:"POST",body:JSON.stringify({request_key:requestKey}),signal}),
  reportHref:(id:string,format:"markdown"|"csv"|"json")=>`/api/v1/assistant/jobs/${encodeURIComponent(id)}/report?format=${format}`,
};

export type LocalPermission={id:string;job_id:string;request_sha256:string;state:"pending"|"approved"|"denied"|"expired"|"completed"|"cancelled";arguments:{operation:string;path?:string;offset:number;limit:number;query?:string};operation:string;path?:string;purpose:string;share_notice:string;allowed_scopes:Array<"file"|"directory"|"all"|"deny">;scope_targets?:Partial<Record<"file"|"directory"|"all",string>>;approved_scope?:"file"|"directory"|"all"|null;scope_root?:string|null;created_at:string;expires_at:string;decided_at?:string|null;completed_at?:string|null;previous_request_id?:string|null};
export type LocalPermissions={job_id:string;state:string;paired:boolean;items:LocalPermission[];active_elapsed_ms:number;read_count:number;rounds:number;waiting_timeout_seconds:number};
export type AssistantReference={kind:"assets"|"stations"|"executions";id:string;label:string;detail?:string};
export type JobPayload={request_key:string;thread_id?:string;message:string;context:PageContext;attachments?:string[];after_job_id?:string};
export type JobEvent={seq:number;kind:"text"|"tool"|"state"|"input";data:{text?:string;round?:number;tool?:string;status?:string;state?:string};created_at:string};
export type Feedback={id:string;job_id:string;rating:"helpful"|"issue";note:string;created_at?:string;question?:string;answer?:string};
export type SharedFaq={id:string;question:string;answer:string;version?:number;published_at?:string;source_feedback_id?:string;revision?:number};
export type RepairProposal={id:string;path:string;kind:"config"|"code";reason:string;original:string;replacement:string;expected_sha256:string;proposal_sha256:string;status?:string;state?:string;diff?:string;receipt?:unknown;result?:unknown};
export type PageContext={view:string;kind:"MODULE"|"WHOLE_MACHINE";object_id?:string;archive_id?:string;selection_empty?:boolean;metrics?:string[];subjects?:string[];start?:number;end?:number;stage?:string;cycle?:string;target_part_code?:string;references?:AssistantReference[]};
export type AssistantCard={type:string;title:string;source:string;updated_at:string;unit?:string;range?:unknown;scope?:string;data:Record<string,unknown>;target?:{view:string;id:string|null}};
export type ChatHistory={messages:Array<{id:number;job_id:string;role:"user"|"assistant";body:string;context:PageContext;cards:AssistantCard[];attachments?:AssistantAttachment[];created_at:string}>;jobs:Array<{id:string;state:string}>};
export type ChatJob={id:string;thread_id:string;state:string;rounds:number;context:PageContext;result?:{presentation?:{status:string;conclusion:string;evidence:string[];next_actions:string[];unknown:string[]};reply:string;setup?:SetupOutcome;cards:AssistantCard[];completion?:"answered"|"partial"|"failed"|"awaiting_permission";termination_reason?:string;business_status?:"saved"|"needs_input"|"needs_adoption"|"answered"|"not_saved"};receipt?:SetupOutcome["receipt"]};

export type SetupOutcome = {
  status: "created" | "needs_input" | "needs_adoption";
  question: string;
  details?: unknown;
  unconfirmed_fields?: string[];
  operations?: Array<{ kind: string; alias: string; data: Record<string, unknown> }>;
  proposal_id?: string;
  proposal_hash?: string;
  receipt?: { request_key: string; replayed: boolean; objects: Record<string, Record<string, unknown>> };
};

export type AssistantAttachment={id:string;name:string;kind:"image"|"text"|"archive"|"binary";mime:string;sha256:string;size:number;expires_at:string};
