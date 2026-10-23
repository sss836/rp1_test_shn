// @vitest-environment jsdom
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,it,expect,vi } from "vitest";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { AssistantPanel,cardHref } from "./AssistantPanel";
import { assistantApi,type PageContext,type ChatJob } from "../assistantApi";

const context:PageContext={view:"curves",kind:"MODULE",archive_id:"11111111-1111-4111-8111-111111111111",start:1,end:2,metrics:["pos_rad"]};
const identifier="22222222-2222-4222-8222-222222222222";
const complete:ChatJob={id:identifier,thread_id:identifier,state:"COMPLETED",rounds:2,context,result:{reply:"已取得实际数据",cards:[]}};
beforeEach(()=>{
  vi.spyOn(assistantApi,"threads").mockResolvedValue([]);
  vi.spyOn(assistantApi,"history").mockResolvedValue({messages:[],jobs:[]});
  vi.spyOn(assistantApi,"budget").mockResolvedValue({day:"2026-10-22",spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:true});
  vi.spyOn(assistantApi,"job").mockResolvedValue(complete);
  vi.spyOn(assistantApi,"submitJob").mockResolvedValue({id:identifier,thread_id:identifier,state:"QUEUED"});
});
afterEach(()=>{cleanup();vi.restoreAllMocks();});
it("labels partial answers with the actual stopping reason",async()=>{
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,state:"FAILED",rounds:6,result:{reply:"已取得实际数据",cards:[],completion:"partial",termination_reason:"round_limit"}});
  view();await send();
  expect(screen.getByLabelText("当前任务").textContent).toContain("部分答复 · 达到交互上限");
  expect(screen.queryByText(/模型链路中断/)).toBeNull();
});
function view(account="one",page=context){const client=new QueryClient({defaultOptions:{queries:{retry:false,gcTime:0}}});return render(<QueryClientProvider client={client}><AssistantPanel key={account} accountKey={account} context={page} navigate={vi.fn()}/></QueryClientProvider>);}
async function send(){fireEvent.click(screen.getByText("助手"));const input=screen.getByLabelText("消息");fireEvent.change(input,{target:{value:"解释当前选区"}});fireEvent.submit(input.closest("form")!);await screen.findByText("已取得实际数据");}

it("shows the page selection before send and freezes it into the submitted job",async()=>{
  view();await send();
  expect(screen.getByLabelText("发送前页面上下文").textContent).toContain("位置 · 1–2 s");
  expect(screen.getByLabelText("发送前页面上下文").textContent).toContain("pos_rad");
  expect(assistantApi.submitJob).toHaveBeenCalledWith(expect.objectContaining({context,message:"解释当前选区"}),expect.any(AbortSignal));
});

it("uses the same request and context after a lost submit response",async()=>{
  vi.mocked(assistantApi.submitJob).mockRejectedValueOnce(new Error("lost connection"));
  view();fireEvent.click(screen.getByText("助手"));const input=screen.getByLabelText("消息");fireEvent.change(input,{target:{value:"查询当前工位"}});fireEvent.submit(input.closest("form")!);
  fireEvent.click(await screen.findByText("使用原请求标识重试并核对回执"));
  await screen.findByText("已取得实际数据");
  expect(vi.mocked(assistantApi.submitJob).mock.calls[0][0]).toEqual(vi.mocked(assistantApi.submitJob).mock.calls[1][0]);
});

it("never adopts model proposals automatically and requires the publication checkbox",async()=>{
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,result:{reply:"请采用",cards:[],setup:{status:"needs_adoption",question:"请采用",proposal_id:"proposal",proposal_hash:"hash",operations:[{kind:"case",alias:"case",data:{publish:true}}]}}});
  const adopt=vi.spyOn(assistantApi,"adoptSetup").mockResolvedValue({status:"created",question:"已保存"});
  view();fireEvent.click(screen.getByText("助手"));const input=screen.getByLabelText("消息");fireEvent.change(input,{target:{value:"创建测试用例"}});fireEvent.submit(input.closest("form")!);
  const action=await screen.findByText("采用所示参数并执行");expect((action as HTMLButtonElement).disabled).toBe(true);expect(adopt).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("我明确要求发布所示版本"));fireEvent.click(action);await waitFor(()=>expect(adopt).toHaveBeenCalledWith("proposal","hash",true));
});

it("restores account history and blocks sending while pricing is unverified",async()=>{
  vi.mocked(assistantApi.threads).mockResolvedValue([{id:identifier,updated_at:"2026-10-22"}]);
  vi.mocked(assistantApi.history).mockResolvedValue({messages:[{id:1,job_id:identifier,role:"user",body:"本账号的历史",context,cards:[],created_at:"2026-10-22"}],jobs:[]});
  vi.mocked(assistantApi.budget).mockResolvedValue({day:"2026-10-22",spent_cny:0,reserved_cny:0,limit_cny:100,blocked:false,pricing_verified:false});
  view();fireEvent.click(screen.getByText("助手"));fireEvent.click(screen.getByLabelText("查看历史会话"));await screen.findByRole("option",{name:/22222222/});fireEvent.change(screen.getByLabelText("历史会话"),{target:{value:identifier}});
  await screen.findByText("本账号的历史");fireEvent.change(screen.getByLabelText("消息"),{target:{value:"查询"}});
  expect((screen.getByRole("button",{name:"发送"}) as HTMLButtonElement).disabled).toBe(true);
});

it("prefills a suggested question without sending and keeps one copy of the saved reply",async()=>{
  vi.mocked(assistantApi.history).mockResolvedValue({messages:[{id:1,job_id:identifier,role:"assistant",body:"已取得实际数据",context,cards:[],created_at:"2026-10-22"}],jobs:[]});
  view();fireEvent.click(screen.getByText("助手"));fireEvent.click(screen.getByRole("button",{name:/解释当前选区/}));
  expect((screen.getByLabelText("消息") as HTMLTextAreaElement).value).toBe("解释当前选区");
  expect(assistantApi.submitJob).not.toHaveBeenCalled();
  fireEvent.submit(screen.getByLabelText("消息").closest("form")!);
  await screen.findByText("已取得实际数据");
  expect(screen.getAllByText("已取得实际数据")).toHaveLength(1);
});

it("clears the old account view on remount and only builds allowlisted object links",async()=>{
  const client=new QueryClient();const props={context,navigate:vi.fn()};
  const result=render(<QueryClientProvider client={client}><AssistantPanel key="one" accountKey="one" {...props}/></QueryClientProvider>);
  fireEvent.click(screen.getByText("助手"));fireEvent.change(screen.getByLabelText("消息"),{target:{value:"前一个账号内容"}});
  result.rerender(<QueryClientProvider client={client}><AssistantPanel key="two" accountKey="two" {...props}/></QueryClientProvider>);
  fireEvent.click(screen.getByText("助手"));expect((screen.getByLabelText("消息") as HTMLTextAreaElement).value).toBe("");
  expect(cardHref({type:"saved",title:"x",source:"x",updated_at:"x",data:{},target:{view:"javascript:alert(1)",id:null}},context)).toBeNull();
  expect(cardHref({type:"saved",title:"x",source:"x",updated_at:"x",data:{},target:{view:"archive",id:identifier}},context)).toContain("view=archive");
});

it("shows missing-input business state instead of claiming a write completed",async()=>{
  vi.mocked(assistantApi.job).mockResolvedValue({...complete,result:{reply:"尚未保存，请补充名称",cards:[],business_status:"needs_input"}});
  view();fireEvent.click(screen.getByText("助手"));fireEvent.change(screen.getByLabelText("消息"),{target:{value:"创建项目"}});fireEvent.submit(screen.getByLabelText("消息").closest("form")!);
  await screen.findByText("尚未保存，请补充名称");
  expect(screen.getByLabelText("当前任务").textContent).toContain("待补充，尚未保存");
  expect(screen.getByLabelText("当前任务").textContent).not.toContain("已完成");
});


it("uploads an image and submits its immutable identifier without requiring text",async()=>{
  vi.spyOn(assistantApi,"uploadAttachment").mockResolvedValue({id:identifier,name:"error.png",kind:"image",mime:"image/png",sha256:"synthetic",size:100,expires_at:"2026-11-21"});
  const result=view();fireEvent.click(screen.getByText("助手"));
  fireEvent.change(result.container.querySelector('input[type="file"]')!,{target:{files:[new File(["synthetic"],"error.png",{type:"image/png"})]}});
  await screen.findByAltText("error.png");
  fireEvent.submit(screen.getByLabelText("消息").closest("form")!);
  await screen.findByText("已取得实际数据");
  expect(assistantApi.submitJob).toHaveBeenCalledWith(expect.objectContaining({attachments:[identifier],message:expect.stringContaining("分析")}),expect.any(AbortSignal));
});
