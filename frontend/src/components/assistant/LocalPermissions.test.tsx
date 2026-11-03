// @vitest-environment jsdom
import {act,cleanup,fireEvent,render,screen,waitFor,within} from "@testing-library/react";
import {QueryClient,QueryClientProvider} from "@tanstack/react-query";
import {afterEach,beforeEach,expect,it,vi} from "vitest";
import {LocalPermissions} from "./LocalPermissions";
import {AssistantPanel} from "../AssistantPanel";
import {assistantApi,type LocalPermission,type LocalPermissions as PermissionList,type ChatJob} from "../../assistantApi";
import {ApiError} from "../../client";
const jobId="22222222-2222-4222-8222-222222222222",permissionId="33333333-3333-4333-8333-333333333333",nextId="44444444-4444-4444-8444-444444444444";
const request:LocalPermission={id:permissionId,job_id:jobId,request_sha256:"a".repeat(64),state:"pending",arguments:{operation:"read",path:"/home/operator/station/logs/gateway.log",offset:0,limit:65536},operation:"read",path:"/home/operator/station/logs/gateway.log",purpose:"检查连接超时前后的上位机日志",share_notice:"读取到的内容会发送给模型，用于这次诊断。",allowed_scopes:["file","directory","all"],scope_targets:{file:"/home/operator/station/logs/gateway.log",directory:"/home/operator/station/logs",all:"已登记诊断根"},created_at:"2026-11-01T00:00:00Z",expires_at:"2099-01-01T00:30:00Z"};
let state:PermissionList;
function client(){return new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}});}
function fixture(account="account",jobState="WAITING_PERMISSION"){
 const query=client(),changed=vi.fn();const props={accountKey:account,jobId,jobState,enabled:true,onChanged:changed};
 const node=render(<QueryClientProvider client={query}><LocalPermissions {...props}/></QueryClientProvider>);return{...node,query,changed,props};
}
beforeEach(()=>{sessionStorage.clear();state={job_id:jobId,state:"WAITING_PERMISSION",paired:true,items:[structuredClone(request)],active_elapsed_ms:6500,read_count:2,rounds:1,waiting_timeout_seconds:1800};vi.spyOn(assistantApi,"localPermissions").mockImplementation(async()=>structuredClone(state));});
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it("shows server path, purpose, model disclosure and exact file/directory options without blanket access",async()=>{
 fixture();const dialog=await screen.findByRole("dialog");expect(within(dialog).getByText(request.purpose)).toBeTruthy();expect(within(dialog).getByText(/内容会发送给模型/)).toBeTruthy();expect(within(dialog).getAllByText(request.path!)).toHaveLength(2);
 expect(within(dialog).getByRole("radio",{name:/只允许这个文件/})).toBeTruthy();expect(within(dialog).getByRole("radio",{name:/允许这个目录及其内部文件/})).toBeTruthy();expect(within(dialog).queryByRole("radio",{name:/全部|all/})).toBeNull();expect(within(dialog).getByText(/不会修改文件、运行命令或控制设备/)).toBeTruthy();
});
it("approves only the selected immutable request and waits for actual HMI evidence",async()=>{
 const decide=vi.spyOn(assistantApi,"decideLocalPermission").mockImplementation(async()=>{state.items[0]={...request,state:"approved",approved_scope:"directory",scope_root:request.scope_targets!.directory};return state.items[0];});const resume=vi.spyOn(assistantApi,"resumeJob"),submit=vi.spyOn(assistantApi,"submitJob");const view=fixture();const dialog=await screen.findByRole("dialog");fireEvent.click(within(dialog).getByRole("radio",{name:/允许这个目录及其内部文件/}));fireEvent.click(within(dialog).getByText("允许只读"));
 await waitFor(()=>expect(decide).toHaveBeenCalledWith(jobId,permissionId,{request_key:expect.any(String),expected_request_sha256:request.request_sha256,scope:"directory"},expect.any(AbortSignal)));await screen.findByText("已允许，等候上位机处理");expect(screen.queryByRole("dialog")).toBeNull();expect(screen.getByText(/批准不代表文件已读取/)).toBeTruthy();expect(resume).not.toHaveBeenCalled();expect(submit).not.toHaveBeenCalled();expect(view.changed).toHaveBeenCalled();
});
it("preserves an explicit denial and offers reapplication without pretending anything was read",async()=>{
 const decide=vi.spyOn(assistantApi,"decideLocalPermission").mockImplementation(async()=>{state.items[0]={...request,state:"denied"};return state.items[0];});fixture();fireEvent.click(within(await screen.findByRole("dialog")).getByText("拒绝查看"));await screen.findByText("已拒绝");expect(decide).toHaveBeenCalledWith(jobId,permissionId,expect.objectContaining({scope:"deny"}),expect.any(AbortSignal));expect(screen.getByText(/尚未因此读取这份资料/)).toBeTruthy();expect(screen.getByText("重新申请查看")).toBeTruthy();
});
it("reapplies an expired request as a new server request and keeps the old history",async()=>{
 state.items[0]={...request,state:"expired",expires_at:"2000-01-01"};const retry=vi.spyOn(assistantApi,"retryLocalPermission").mockImplementation(async()=>{const next={...request,id:nextId,request_sha256:"b".repeat(64),previous_request_id:permissionId};state.items.push(next);return next;});fixture();await screen.findByText("申请已过期");expect(screen.queryByRole("dialog")).toBeNull();fireEvent.click(screen.getByText("重新申请查看"));const dialog=await screen.findByRole("dialog");expect(retry).toHaveBeenCalledWith(jobId,permissionId,expect.objectContaining({expected_request_sha256:request.request_sha256}),expect.any(AbortSignal));expect(within(dialog).getByText(`申请编号：${nextId}`)).toBeTruthy();expect(screen.getByText("这是之前的申请，已另行重新申请。")).toBeTruthy();expect(screen.queryByText("重新申请查看")).toBeNull();
});
it("rejects a late approval explicitly and refreshes authoritative expiry",async()=>{
 vi.spyOn(assistantApi,"decideLocalPermission").mockImplementation(async()=>{state.items[0]={...request,state:"expired"};throw new ApiError("申请已经过期",410,"permission_expired");});fixture();fireEvent.click(within(await screen.findByRole("dialog")).getByText("允许只读"));await screen.findByText("申请已过期");expect(screen.getByRole("alert").textContent).toContain("申请已经过期");expect(screen.queryByRole("dialog")).toBeNull();expect(screen.getByText("重新申请查看")).toBeTruthy();
});
it("uses the same request key after a lost decision response",async()=>{
 const decide=vi.spyOn(assistantApi,"decideLocalPermission").mockRejectedValueOnce(new Error("应答丢失")).mockImplementation(async()=>{state.items[0]={...request,state:"approved"};return state.items[0];});fixture();fireEvent.click(within(await screen.findByRole("dialog")).getByText("允许只读"));await screen.findAllByText("应答丢失");await waitFor(()=>expect((within(screen.getByRole("dialog")).getByText("允许只读")as HTMLButtonElement).disabled).toBe(false));fireEvent.click(within(screen.getByRole("dialog")).getByText("允许只读"));await screen.findByText("已允许，等候上位机处理");expect(decide.mock.calls[0][2]).toEqual(decide.mock.calls[1][2]);
});
it("does not ask again for an already approved scope or treat it as a successful read",async()=>{
 state.items[0]={...request,state:"approved",approved_scope:"file",scope_root:request.path};fixture();await screen.findByText("已允许，等候上位机处理");expect(screen.queryByRole("dialog")).toBeNull();expect(screen.queryByText("允许只读")).toBeNull();expect(screen.queryByText("继续原来的诊断")).toBeNull();
});
it("resumes the same job only after actual completed evidence and keeps accumulated usage",async()=>{
 state.items[0]={...request,state:"completed",completed_at:"2026-11-01"};const resume=vi.spyOn(assistantApi,"resumeJob").mockResolvedValue({id:jobId,state:"QUEUED"}),submit=vi.spyOn(assistantApi,"submitJob");fixture();await screen.findByText("已收到上位机处理结果");fireEvent.click(screen.getByText("继续原来的诊断"));await waitFor(()=>expect(resume).toHaveBeenCalledWith(jobId,expect.any(String),expect.any(AbortSignal)));expect(submit).not.toHaveBeenCalled();expect(screen.getByText(/已进行 1\/6 轮分析/).textContent).toContain("2/12");expect(screen.getByText(/已进行 1\/6 轮分析/).textContent).toContain("6.5 秒");
});
it("leaves a different-session resume rejection visible and never creates a replacement job",async()=>{
 state.items[0]={...request,state:"completed"};vi.spyOn(assistantApi,"resumeJob").mockRejectedValue(new ApiError("请从原上位机登录会话继续",403,"original_session_required"));const submit=vi.spyOn(assistantApi,"submitJob");fixture();fireEvent.click(await screen.findByText("继续原来的诊断"));await screen.findByText("请从原上位机登录会话继续");expect(submit).not.toHaveBeenCalled();
});
it("offers no approval when the server says no HMI is paired, even if stale items exist",async()=>{
 state.paired=false;fixture();await screen.findByText("还没有可用的上位机关联");expect(screen.queryByRole("dialog")).toBeNull();expect(screen.queryByText("允许只读")).toBeNull();expect(screen.queryByText("决定是否允许查看")).toBeNull();
});
it("aborts an old identity's decision and ignores its late callback",async()=>{
 let resolve!:(item:LocalPermission)=>void;const decide=vi.spyOn(assistantApi,"decideLocalPermission").mockImplementation(()=>new Promise(done=>{resolve=done;}));const view=fixture();fireEvent.click(within(await screen.findByRole("dialog")).getByText("允许只读"));await waitFor(()=>expect(decide).toHaveBeenCalledOnce());const signal=decide.mock.calls[0][3]!;
 state={...state,paired:false,items:[]};view.rerender(<QueryClientProvider client={view.query}><LocalPermissions {...view.props} accountKey="other-account"/></QueryClientProvider>);expect(signal.aborted).toBe(true);await act(async()=>resolve({...request,state:"approved"}));await screen.findByText("还没有可用的上位机关联");expect(screen.queryByText(request.path!)).toBeNull();expect(view.changed).not.toHaveBeenCalled();
});
it("lets users postpone a decision and explicitly reopen it without granting anything",async()=>{
 const decide=vi.spyOn(assistantApi,"decideLocalPermission");fixture();const dialog=await screen.findByRole("dialog");fireEvent.keyDown(dialog,{key:"Escape"});expect(screen.queryByRole("dialog")).toBeNull();expect(decide).not.toHaveBeenCalled();fireEvent.click(screen.getByText("决定是否允许查看"));expect(screen.getByRole("dialog")).toBeTruthy();
});
it("does not offer approvals, reapplication or resume for a cancelled job",async()=>{
 state.items[0]={...request,state:"expired"};fixture("account","CANCELLED");await screen.findByText("申请已过期");expect(screen.queryByRole("dialog")).toBeNull();expect(screen.queryByText("重新申请查看")).toBeNull();expect(screen.queryByText("继续原来的诊断")).toBeNull();
});
it("stops the current job without submitting a replacement",async()=>{
 state.items[0]={...request,state:"approved"};const cancel=vi.spyOn(assistantApi,"cancel").mockResolvedValue({}),submit=vi.spyOn(assistantApi,"submitJob");fixture();fireEvent.click(await screen.findByText("停止这次诊断"));await waitFor(()=>expect(cancel).toHaveBeenCalledWith(jobId,expect.any(AbortSignal)));expect(submit).not.toHaveBeenCalled();
});
it("keeps paused job evidence and editable next draft without submitting a second job",async()=>{
 const context={view:"dashboard",kind:"MODULE"as const};sessionStorage.setItem("rp1-assistant-thread:account",jobId);sessionStorage.setItem("rp1-assistant-job:account",jobId);
 vi.spyOn(assistantApi,"capabilities").mockResolvedValue({mode:"assistant_runtime",tools:[],writes_enabled:false,features:{local_permissions:true,stream_events:false,support_workspace:false}});vi.spyOn(assistantApi,"threads").mockResolvedValue([]);vi.spyOn(assistantApi,"history").mockResolvedValue({messages:[],jobs:[{id:jobId,state:"WAITING_PERMISSION"}]});vi.spyOn(assistantApi,"budget").mockResolvedValue({day:"2026-11-01",spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:true});
 const job:ChatJob={id:jobId,thread_id:jobId,state:"WAITING_PERMISSION",rounds:1,context,result:{reply:"已取得CSV前段，需要继续检查本机日志。",completion:"awaiting_permission",cards:[{type:"source_evidence",title:"已取得CSV前段",source:"本次上传",updated_at:"2026-11-01",data:{unread_bytes:20}}]}};vi.spyOn(assistantApi,"job").mockResolvedValue(job);const submit=vi.spyOn(assistantApi,"submitJob");
 render(<QueryClientProvider client={client()}><AssistantPanel accountKey="account" context={context} navigate={vi.fn()}/></QueryClientProvider>);fireEvent.click(screen.getByRole("button",{name:"助手"}));await screen.findByText("已取得CSV前段");fireEvent.click(within(await screen.findByRole("dialog")).getByText("稍后决定"));fireEvent.change(screen.getByLabelText("消息"),{target:{value:"等候时写下一条草稿"}});expect((screen.getByLabelText("消息")as HTMLTextAreaElement).disabled).toBe(false);expect((screen.getByText("补充当前诊断")as HTMLButtonElement).disabled).toBe(true);expect(screen.getByText("已取得CSV前段")).toBeTruthy();expect(screen.getByLabelText("当前任务").textContent).toContain("等待资料或读取许可");expect(submit).not.toHaveBeenCalled();
});

it("retains previously obtained evidence and the next draft when the same job resumes",async()=>{
 state.items[0]={...request,state:"approved"};const context={view:"dashboard",kind:"MODULE"as const};sessionStorage.setItem("rp1-assistant-thread:account",jobId);sessionStorage.setItem("rp1-assistant-job:account",jobId);
 vi.spyOn(assistantApi,"capabilities").mockResolvedValue({mode:"assistant_runtime",tools:[],writes_enabled:false,features:{local_permissions:true}});vi.spyOn(assistantApi,"threads").mockResolvedValue([]);vi.spyOn(assistantApi,"history").mockResolvedValue({messages:[],jobs:[{id:jobId,state:"WAITING_PERMISSION"}]});vi.spyOn(assistantApi,"budget").mockResolvedValue({day:"2026-11-01",spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:true});
 const paused:ChatJob={id:jobId,thread_id:jobId,state:"WAITING_PERMISSION",rounds:1,context,result:{reply:"先保留已取得的资料。",completion:"awaiting_permission",cards:[{type:"source_evidence",title:"暂停前已查看的日志",source:"当前上位机",updated_at:"2026-11-01",data:{unread_bytes:20}}]}};const job=vi.spyOn(assistantApi,"job").mockResolvedValue(paused),submit=vi.spyOn(assistantApi,"submitJob");const query=client();
 render(<QueryClientProvider client={query}><AssistantPanel accountKey="account" context={context} navigate={vi.fn()}/></QueryClientProvider>);fireEvent.click(screen.getByRole("button",{name:"助手"}));await screen.findByText("暂停前已查看的日志");fireEvent.change(screen.getByLabelText("消息"),{target:{value:"继续时不要丢失我的下一条草稿"}});
 job.mockResolvedValue({...paused,state:"RUNNING",result:undefined});await act(async()=>{await query.invalidateQueries({queryKey:["assistant-job","account",jobId]});});await screen.findByText("之前已取得的资料");expect(screen.getByText("暂停前已查看的日志")).toBeTruthy();expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("继续时不要丢失我的下一条草稿");expect(submit).not.toHaveBeenCalled();
});
