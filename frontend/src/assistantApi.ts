import { apiRequest } from "./client";

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
  capabilities: (signal?: AbortSignal) => apiRequest<{ mode: "isolation_probe"|"assistant_runtime"; tools: string[]; writes_enabled: boolean }>("/api/v1/assistant/capabilities", { signal }),
  probe: (signal?: AbortSignal) => apiRequest<AssistantProbe>("/api/v1/assistant/probe", { method: "POST", body: "{}", signal }),
  setupCommand: (messages: string[], requestKey: string, signal?: AbortSignal) => apiRequest<SetupOutcome>("/api/v1/assistant/setup/commands", {method:"POST",body:JSON.stringify({messages,request_key:requestKey}),signal}),
  adoptSetup: (proposalId: string, proposalHash: string, publishApproved = false) => apiRequest<SetupOutcome>("/api/v1/assistant/setup/adopt", {method:"POST",body:JSON.stringify({proposal_id:proposalId,proposal_hash:proposalHash,publish_approved:publishApproved})}),
  threads:(signal?:AbortSignal)=>apiRequest<Array<{id:string;updated_at:string}>>("/api/v1/assistant/threads",{signal}),
  history:(id:string,signal?:AbortSignal)=>apiRequest<ChatHistory>(`/api/v1/assistant/threads/${id}`,{signal}),
  submitJob:(payload:{request_key:string;thread_id?:string;message:string;context:PageContext},signal?:AbortSignal)=>apiRequest<{id:string;thread_id:string;state:string}>("/api/v1/assistant/jobs",{method:"POST",body:JSON.stringify(payload),signal}),
  job:(id:string,signal?:AbortSignal)=>apiRequest<ChatJob>(`/api/v1/assistant/jobs/${id}`,{signal}),
  cancel:(id:string)=>apiRequest(`/api/v1/assistant/jobs/${id}/cancel`,{method:"POST",body:"{}"}),
  budget:(signal?:AbortSignal)=>apiRequest<{day:string;spent_cny:number;reserved_cny:number;limit_cny:number;blocked:boolean;pricing_verified:boolean}>("/api/v1/assistant/budget",{signal}),
};

export type PageContext={view:string;kind:"MODULE"|"WHOLE_MACHINE";object_id?:string;archive_id?:string;selection_empty?:boolean;metrics?:string[];subjects?:string[];start?:number;end?:number;stage?:string;cycle?:string};
export type AssistantCard={type:string;title:string;source:string;updated_at:string;unit?:string;range?:unknown;scope?:string;data:Record<string,unknown>;target?:{view:string;id:string|null}};
export type ChatHistory={messages:Array<{id:number;job_id:string;role:"user"|"assistant";body:string;context:PageContext;cards:AssistantCard[];created_at:string}>;jobs:Array<{id:string;state:string}>};
export type ChatJob={id:string;thread_id:string;state:string;rounds:number;context:PageContext;result?:{reply:string;setup?:SetupOutcome;cards:AssistantCard[];business_status?:"saved"|"needs_input"|"needs_adoption"|"answered"|"not_saved"};receipt?:SetupOutcome["receipt"]};

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
