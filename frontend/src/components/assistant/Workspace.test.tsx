// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AssistantPanel } from "../AssistantPanel";
import { assistantApi, type AssistantAttachment, type ChatJob, type PageContext } from "../../assistantApi";
import { Markdown } from "./Markdown";
import { SupportWorkspace } from "./SupportWorkspace";
import { Evidence } from "./Evidence";
const id="22222222-2222-4222-8222-222222222222", second="33333333-3333-4333-8333-333333333333";
const context:PageContext={view:"dashboard",kind:"MODULE"};
const complete:ChatJob={id,thread_id:id,state:"COMPLETED",rounds:1,context,result:{reply:"已取得结果",cards:[]}};
const attachment=(name:string):AssistantAttachment=>({id:name,name,kind:name.endsWith("png")?"image":"text",mime:"text/plain",sha256:name,size:16,expires_at:"2099-01-01"});
function client(){return new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}});}
function panel(){return render(<QueryClientProvider client={client()}><AssistantPanel accountKey="workspace-user" context={context} navigate={vi.fn()}/></QueryClientProvider>);}
function open(){fireEvent.click(screen.getByRole("button",{name:"助手"}));}
async function send(text="分析本次诊断"){fireEvent.change(screen.getByLabelText("消息"),{target:{value:text}});fireEvent.submit(screen.getByLabelText("消息").closest("form")!);await waitFor(()=>expect(assistantApi.submitJob).toHaveBeenCalled());}
beforeEach(()=>{
  sessionStorage.clear();
  vi.spyOn(assistantApi,"capabilities").mockResolvedValue({mode:"assistant_runtime",writes_enabled:false,tools:[],features:{stream_events:false,support_workspace:false}});
  vi.spyOn(assistantApi,"threads").mockResolvedValue([]);vi.spyOn(assistantApi,"history").mockResolvedValue({messages:[],jobs:[]});
  vi.spyOn(assistantApi,"budget").mockResolvedValue({day:"2026-10-31",spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:true});
  vi.spyOn(assistantApi,"job").mockResolvedValue(complete);
  vi.spyOn(assistantApi,"submitJob").mockResolvedValue({id,thread_id:id,state:"QUEUED"});
});
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it("deduplicates the same bytes across file selection, paste and drop",async()=>{
  const upload=vi.spyOn(assistantApi,"uploadAttachment").mockImplementation(async f=>attachment(f.name));
  panel();open();const file=new File(["same immutable bytes"],"one.csv");
  fireEvent.change(screen.getByLabelText("选择诊断附件"),{target:{files:[file]}});await screen.findByText("上传完成",{exact:false});
  fireEvent.paste(screen.getByLabelText("消息"),{clipboardData:{files:[new File(["same immutable bytes"],"pasted.csv")]}});
  fireEvent.drop(screen.getByLabelText("消息").closest(".assistant-draft")!,{dataTransfer:{files:[file]}});
  await act(async()=>{await new Promise(resolve=>setTimeout(resolve,30));});expect(upload).toHaveBeenCalledOnce();
  expect(screen.getAllByText("one.csv")).toHaveLength(1);
});
it("shows actual byte progress and permits an independent draft while uploading",async()=>{
  let resolve!:(a:AssistantAttachment)=>void;let progress!:(loaded:number,total:number)=>void;
  vi.spyOn(assistantApi,"uploadAttachment").mockImplementation((_file,_signal,onProgress)=>{progress=onProgress!;return new Promise(done=>{resolve=done;});});
  panel();open();fireEvent.change(screen.getByLabelText("选择诊断附件"),{target:{files:[new File(["0123456789"],"slow.csv")]}});
  await screen.findByRole("progressbar");act(()=>progress(5,10));expect((screen.getByRole("progressbar")as HTMLProgressElement).value).toBe(5);
  fireEvent.change(screen.getByLabelText("消息"),{target:{value:"上传时继续写下一条"}});expect((screen.getByLabelText("消息")as HTMLTextAreaElement).disabled).toBe(false);
  expect((screen.getByRole("button",{name:"发送"})as HTMLButtonElement).disabled).toBe(true);
  await act(async()=>resolve(attachment("slow.csv")));expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("上传时继续写下一条");
});
it("keeps the next draft entered while the original submit response is in flight",async()=>{
  let resolve!:(data:{id:string;thread_id:string;state:string})=>void;
  vi.mocked(assistantApi.submitJob).mockImplementation(()=>new Promise(done=>{resolve=done;}));panel();open();await send("冻结的本轮输入");
  fireEvent.change(screen.getByLabelText("消息"),{target:{value:"发送等待中另写草稿"}});
  await act(async()=>resolve({id,thread_id:id,state:"QUEUED"}));await screen.findByText("已取得结果");
  expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("发送等待中另写草稿");
  expect(assistantApi.submitJob).toHaveBeenCalledWith(expect.objectContaining({message:"冻结的本轮输入"}),expect.any(AbortSignal));
});
it("keeps per-thread drafts when switching and restores them after remount",async()=>{
  vi.mocked(assistantApi.threads).mockResolvedValue([{id,title:"电机温度",updated_at:"2026-10-31"},{id:second,title:"工位通信",updated_at:"2026-10-31"}]);
  const view=panel();open();fireEvent.click(screen.getByLabelText("查看历史会话"));await screen.findByRole("option",{name:/电机温度/});
  fireEvent.change(screen.getByLabelText("历史会话"),{target:{value:id}});fireEvent.change(screen.getByLabelText("消息"),{target:{value:"温度草稿"}});
  fireEvent.change(screen.getByLabelText("历史会话"),{target:{value:second}});fireEvent.change(screen.getByLabelText("消息"),{target:{value:"通信草稿"}});
  fireEvent.change(screen.getByLabelText("历史会话"),{target:{value:id}});expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("温度草稿");
  view.unmount();panel();open();expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("温度草稿");
});
it("queries readable thread titles, renames and explicitly confirms deletion",async()=>{
  vi.mocked(assistantApi.threads).mockResolvedValue([{id,title:"电机温度问题",updated_at:"2026-10-31"}]);
  const rename=vi.spyOn(assistantApi,"renameThread").mockResolvedValue({});const deletion=vi.spyOn(assistantApi,"deleteThread").mockResolvedValue({});
  panel();open();fireEvent.click(screen.getByLabelText("查看历史会话"));await screen.findByRole("option",{name:/电机温度问题/});
  fireEvent.change(screen.getByLabelText("搜索会话"),{target:{value:"温度"}});await waitFor(()=>expect(assistantApi.threads).toHaveBeenCalledWith(expect.any(AbortSignal),"温度"));await screen.findByRole("option",{name:/电机温度问题/});
  fireEvent.change(screen.getByLabelText("历史会话"),{target:{value:id}});fireEvent.click(screen.getByRole("button",{name:"重命名"}));fireEvent.change(screen.getByLabelText("会话名称"),{target:{value:"温度复核"}});fireEvent.click(screen.getByText("保存名称"));await waitFor(()=>expect(rename).toHaveBeenCalledWith(id,"温度复核"));
  fireEvent.click(screen.getByText("删除会话"));expect(deletion).not.toHaveBeenCalled();fireEvent.click(screen.getByText("确认删除"));await waitFor(()=>expect(deletion).toHaveBeenCalledWith(id));
});
it("freezes selected visible references into the submitted context",async()=>{
  const refs=vi.spyOn(assistantApi,"references").mockResolvedValue({items:[{kind:"assets",id:second,label:"左腿模块",detail:"本人可见样品"}]});
  panel();open();fireEvent.click(screen.getByLabelText("引用对象"));fireEvent.change(screen.getByLabelText("搜索引用对象"),{target:{value:"左腿"}});
  fireEvent.click(await screen.findByRole("button",{name:/左腿模块/}));expect(refs).toHaveBeenCalledWith("assets","左腿",expect.any(AbortSignal));
  expect(screen.getByLabelText("本次引用对象").textContent).toContain("左腿模块");await send("分析这个样品");
  expect(assistantApi.submitJob).toHaveBeenCalledWith(expect.objectContaining({context:{...context,references:[{kind:"assets",id:second,label:"左腿模块"}]}}),expect.any(AbortSignal));
});
it("distinguishes steering from a frozen next job and keeps the current draft through cancellation",async()=>{
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,state:"RUNNING",result:undefined});
  const steer=vi.spyOn(assistantApi,"steer").mockResolvedValue({accepted:true});const cancel=vi.spyOn(assistantApi,"cancel").mockResolvedValue({});
  vi.mocked(assistantApi.submitJob).mockResolvedValueOnce({id,thread_id:id,state:"QUEUED"}).mockResolvedValueOnce({id:second,thread_id:id,state:"QUEUED"});
  panel();open();await send();await screen.findByRole("button",{name:"补充当前诊断"});
  fireEvent.change(screen.getByLabelText("消息"),{target:{value:"错误发生在昨晚"}});fireEvent.click(screen.getByRole("button",{name:"补充当前诊断"}));await waitFor(()=>expect(steer).toHaveBeenCalledWith(id,"错误发生在昨晚",expect.any(String),expect.any(AbortSignal)));
  await waitFor(()=>expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe(""));
  fireEvent.change(screen.getByLabelText("消息"),{target:{value:"下一条独立查询"}});fireEvent.click(screen.getByRole("button",{name:"下一条排队"}));await waitFor(()=>expect(assistantApi.submitJob).toHaveBeenCalledTimes(2));
  expect(vi.mocked(assistantApi.submitJob).mock.calls[1][0]).toMatchObject({message:"下一条独立查询",after_job_id:id,thread_id:id});
  await waitFor(()=>expect(JSON.parse(sessionStorage.getItem("rp1-assistant-queued:workspace-user")??"[]")).toEqual([second]));
  fireEvent.change(screen.getByLabelText("消息"),{target:{value:"停止不丢这段草稿"}});fireEvent.click(screen.getByRole("button",{name:"停止并核对回执"}));await waitFor(()=>expect(cancel).toHaveBeenCalledWith(id));expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("停止不丢这段草稿");
});
it("recovers incremental text from the last sequence without duplicating the prefix",async()=>{
  vi.mocked(assistantApi.capabilities).mockResolvedValue({mode:"assistant_runtime",writes_enabled:false,tools:[],features:{stream_events:true}});
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,state:"RUNNING",result:undefined});
  const events=vi.spyOn(assistantApi,"events").mockResolvedValueOnce({items:[{seq:1,kind:"text",data:{text:"正在读取",round:1},created_at:"2026-10-31"}],next_seq:1,state:"RUNNING"}).mockResolvedValueOnce({items:[{seq:2,kind:"text",data:{text:"正在读取完整日志",round:1},created_at:"2026-10-31"}],next_seq:2,state:"RUNNING"}).mockResolvedValue({items:[],next_seq:2,state:"COMPLETED"});
  panel();open();await send();await screen.findByText("正在读取完整日志");expect(screen.getAllByText("正在读取完整日志")).toHaveLength(1);expect(screen.queryByText("正在读取")).toBeNull();
  expect(events).toHaveBeenCalledWith(id,1,expect.any(AbortSignal));expect(assistantApi.submitJob).toHaveBeenCalledOnce();
});
it("renders Markdown tables/code with safe links and never executes raw HTML",async()=>{
  const write=vi.fn().mockResolvedValue(undefined);Object.defineProperty(navigator,"clipboard",{value:{writeText:write},configurable:true});
  const view=render(<Markdown text={'# 复核\n\n| 来源 | 状态 |\n|---|---|\n| 日志 | 完整 |\n\n```python\nprint("ok")\n```\n\n[资料](https://example.org/source) [危险](javascript:alert(1))\n<img src=x onerror=alert(1)>'}/>);
  expect(screen.getByRole("table")).toBeTruthy();expect(screen.getByRole("link",{name:"资料"}).getAttribute("rel")).toContain("noopener");expect(screen.queryByRole("link",{name:"危险"})).toBeNull();expect(view.container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByText("复制代码"));await waitFor(()=>expect(write).toHaveBeenCalledWith('print("ok")'));
});
it("makes partial evidence and original hashes readable without opening raw JSON",()=>{
  render(<Evidence card={{type:"attachment_coverage",title:"范围",source:"冻结附件",updated_at:"2026-10-31",data:{attachments:[{name:"long.csv",source_sha256:"originalhash",members_total:2,members_with_evidence:1,members_without_content_evidence:1,provided:[{member:"data.csv",size:100,provided_byte_ranges:[[0,20]],unprovided_byte_ranges:[[20,100]]}]}]}}}/>);
  expect(screen.getByText("尚未查看：1 个文件的内容")).toBeTruthy();expect(screen.getByText("只检查了部分数据，尚不能判断整个文件。")).toBeTruthy();
  const hash=screen.getByText("originalhash");expect(hash.closest("details")).not.toBeNull();expect(hash.closest("details")?.open).toBe(false);
  expect(screen.getByText("[[20,100]]").closest("details")).toBe(hash.closest("details"));
});
it.each(["文件后面的记录尚未检查",17])("keeps unread warnings visible for different tool receipts: %s",unread=>{
  render(<Evidence card={{type:"source_evidence",title:"检查范围",source:"本机文件",updated_at:"2026-11-01",data:{unread}}}/>);
  expect(screen.getByText("只检查了部分数据，尚不能判断整个文件。").closest("details")).toBeNull();
});
it("only publishes FAQ through the server capability and explicit shared feedback confirmation",async()=>{
  vi.spyOn(assistantApi,"faq").mockResolvedValue({items:[]});const list=vi.spyOn(assistantApi,"feedbackList").mockResolvedValue({items:[{id:"feedback1",job_id:id,rating:"issue",note:"脱敏问题"}]});const publish=vi.spyOn(assistantApi,"publishFaq").mockResolvedValue({id:"faq1",question:"问题",answer:"方法",revision:1});
  const props={accountKey:"account",jobId:id,terminal:true,enabled:true,onError:vi.fn()};const view=render(<QueryClientProvider client={client()}><SupportWorkspace {...props} canPublish={false}/></QueryClientProvider>);fireEvent.click(screen.getByText("常见问题"));expect(screen.queryByText("管理员发布常见问题")).toBeNull();view.unmount();
  render(<QueryClientProvider client={client()}><SupportWorkspace {...props} canPublish/></QueryClientProvider>);fireEvent.click(screen.getByText("常见问题"));await waitFor(()=>expect(list).toHaveBeenCalledWith(expect.any(AbortSignal),true));fireEvent.click(screen.getByText("管理员发布常见问题"));await screen.findByRole("option",{name:"脱敏问题"});fireEvent.change(screen.getByLabelText("常见问题来源"),{target:{value:"feedback1"}});fireEvent.change(screen.getByLabelText("共享问题"),{target:{value:"问题"}});fireEvent.change(screen.getByLabelText("处理经验"),{target:{value:"方法"}});
  expect((screen.getByText("发布到常见问题")as HTMLButtonElement).disabled).toBe(true);expect(publish).not.toHaveBeenCalled();fireEvent.click(screen.getByLabelText("已去除敏感信息，核对依据和适用版本，确认发布以上内容"));fireEvent.click(screen.getByText("发布到常见问题"));await waitFor(()=>expect(publish).toHaveBeenCalledWith(expect.objectContaining({feedback_id:"feedback1",question:"问题",answer:"方法"})));
});
it("keeps private feedback opt-in separate and offers same-origin report artifacts",async()=>{
  const feedback=vi.spyOn(assistantApi,"feedback").mockResolvedValue({id:"feedback",job_id:id,rating:"issue",note:"错误"});
  render(<QueryClientProvider client={client()}><SupportWorkspace accountKey="account" jobId={id} terminal canPublish={false} enabled onError={vi.fn()}/></QueryClientProvider>);
  expect(screen.getByRole("link",{name:"CSV"}).getAttribute("href")).toBe(`/api/v1/assistant/jobs/${id}/report?format=csv`);
  fireEvent.click(screen.getByText("反馈问题"));fireEvent.change(screen.getByLabelText("哪里没解决？你做过哪些处理？（请勿填写个人信息或密钥）"),{target:{value:"错误"}});fireEvent.click(screen.getByText("记录这个问题"));await waitFor(()=>expect(feedback).toHaveBeenCalledWith(expect.objectContaining({job_id:id,note:"错误",share_for_faq:false})));
});
it("requires explicit repair diff approval and displays an actual conflict receipt",async()=>{
  const proposal={id:"repair",path:"config/ui.yaml",kind:"config"as const,reason:"兼容格式",original:"x: 1",replacement:"x: 2",expected_sha256:"baseline",proposal_sha256:"proposalhash",state:"proposed",diff:"-x: 1\n+x: 2"};
  vi.spyOn(assistantApi,"repairs").mockResolvedValueOnce({items:[proposal]}).mockResolvedValue({items:[{...proposal,state:"conflict",receipt:{status:"conflict",backup_created:false}}]});const confirm=vi.spyOn(assistantApi,"confirmRepair").mockResolvedValue({...proposal,state:"queued"});
  render(<QueryClientProvider client={client()}><SupportWorkspace accountKey="account" jobId={id} terminal canPublish={false} enabled onError={vi.fn()}/></QueryClientProvider>);fireEvent.click(screen.getByText("查看修复建议"));await screen.findByText("config/ui.yaml");expect(confirm).not.toHaveBeenCalled();expect((screen.getByText("确认并交给当前上位机处理")as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("我已看过修改前后的内容，确认执行这次本机修复"));fireEvent.click(screen.getByText("确认并交给当前上位机处理"));await waitFor(()=>expect(confirm).toHaveBeenCalledWith("repair","proposalhash"));await screen.findByText(/文件已被修改，本次没有覆盖/);expect(screen.getByText("本机处理结果")).toBeTruthy();expect(screen.queryByText("确认并交给当前上位机处理")).toBeNull();
});

it("revises an existing FAQ with the original ID and observed revision after explicit review",async()=>{
  vi.spyOn(assistantApi,"faq").mockResolvedValue({items:[{id:"faq-original",revision:2,question:"原问题",answer:"原经验",source_feedback_id:"source-feedback"}]});
  vi.spyOn(assistantApi,"feedbackList").mockResolvedValue({items:[{id:"source-feedback",job_id:id,rating:"issue",note:"可共享反馈"}]});
  const publish=vi.spyOn(assistantApi,"publishFaq").mockResolvedValue({id:"faq-original",revision:3,question:"原问题",answer:"修订经验"});
  render(<QueryClientProvider client={client()}><SupportWorkspace accountKey="account" jobId={id} terminal canPublish enabled onError={vi.fn()}/></QueryClientProvider>);fireEvent.click(screen.getByText("常见问题"));fireEvent.click(await screen.findByText("修改这条常见问题"));
  expect((screen.getByLabelText("共享问题")as HTMLInputElement).value).toBe("原问题");expect((screen.getByLabelText("处理经验")as HTMLTextAreaElement).value).toBe("原经验");expect((screen.getByText("发布修改")as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("处理经验"),{target:{value:"修订经验"}});fireEvent.click(screen.getByLabelText("已去除敏感信息，核对依据和适用版本，确认发布以上内容"));fireEvent.click(screen.getByText("发布修改"));await waitFor(()=>expect(publish).toHaveBeenCalledWith(expect.objectContaining({faq_id:"faq-original",expected_revision:2,feedback_id:"source-feedback",question:"原问题",answer:"修订经验"})));
});
it("keeps a newly uploading next attachment through first-thread assignment without reupload or abort",async()=>{
  let submitResolve!:(result:{id:string;thread_id:string;state:string})=>void,uploadResolve!:(result:AssistantAttachment)=>void;
  vi.mocked(assistantApi.submitJob).mockImplementationOnce(()=>new Promise(resolve=>{submitResolve=resolve;})).mockResolvedValue({id:second,thread_id:id,state:"QUEUED"});
  const upload=vi.spyOn(assistantApi,"uploadAttachment").mockImplementation(()=>new Promise(resolve=>{uploadResolve=resolve;}));
  panel();open();await send("第一条已冻结");fireEvent.change(screen.getByLabelText("选择诊断附件"),{target:{files:[new File(["next evidence"],"next.csv")]}});await screen.findByRole("progressbar");
  const signal=upload.mock.calls[0][1]!;await act(async()=>submitResolve({id,thread_id:id,state:"QUEUED"}));await screen.findByText("已取得结果");expect(signal.aborted).toBe(false);expect(upload).toHaveBeenCalledOnce();expect(screen.getByText("next.csv")).toBeTruthy();
  await act(async()=>uploadResolve(attachment("next.csv")));fireEvent.submit(screen.getByLabelText("消息").closest("form")!);await waitFor(()=>expect(assistantApi.submitJob).toHaveBeenCalledTimes(2));expect(vi.mocked(assistantApi.submitJob).mock.calls[1][0].attachments).toEqual(["next.csv"]);expect(upload).toHaveBeenCalledOnce();
});

it("labels unconsumed supplements and restores them into an existing draft without sending",async()=>{
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,result:{reply:"已取得结果",cards:[{type:"unprocessed_input",title:"补充未处理",source:"当前任务",updated_at:"2026-10-31",data:{summary:"补充未用于本次诊断，请作为下一条问题发送。",items:[{request_key:"supplement-key",message:"尚未处理的诊断补充"}]}}]}});
  panel();open();await send();await screen.findByText("补充未用于本次诊断");fireEvent.change(screen.getByLabelText("消息"),{target:{value:"已有下一条草稿"}});fireEvent.click(screen.getByRole("button",{name:"恢复到草稿"}));
  expect((screen.getByLabelText("消息")as HTMLTextAreaElement).value).toBe("已有下一条草稿\n\n尚未处理的诊断补充");expect(assistantApi.submitJob).toHaveBeenCalledOnce();
});
