// @vitest-environment jsdom
import { cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,it,expect,vi } from "vitest";
import { QueryClient,QueryClientProvider } from "@tanstack/react-query";
import { SetupDialogue } from "./SetupDialogue";
import { assistantApi } from "../assistantApi";

function view(){return render(<QueryClientProvider client={new QueryClient()}><SetupDialogue/></QueryClientProvider>);}
afterEach(()=>{cleanup();vi.restoreAllMocks();});

it("does not auto-adopt model suggestions and requires an explicit publication action",async()=>{
  vi.spyOn(assistantApi,"setupCommand").mockResolvedValue({status:"needs_adoption",question:"请核对建议",proposal_id:"proposal",proposal_hash:"hash",unconfirmed_fields:["case.publish"],operations:[{kind:"case",alias:"case",data:{publish:true}}]});
  const adopt=vi.spyOn(assistantApi,"adoptSetup").mockResolvedValue({status:"created",question:"已保存"});
  view();
  const input=screen.getByLabelText("建档指令");
  fireEvent.change(input,{target:{value:"创建用例"}});fireEvent.submit(input.closest("form")!);
  const button=await screen.findByText("采用所示参数并执行整组建档");
  expect(adopt).not.toHaveBeenCalled();expect((button as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText("我明确要求发布所示用例版本"));fireEvent.click(button);
  await waitFor(()=>expect(adopt).toHaveBeenCalledWith("proposal","hash",true));
  await screen.findByText("已保存");
});

it("retries a disconnected command with the same messages and idempotency key",async()=>{
  const command=vi.spyOn(assistantApi,"setupCommand").mockRejectedValueOnce(new Error("network lost")).mockResolvedValue({status:"needs_input",question:"请补充名称"});
  view();const input=screen.getByLabelText("建档指令");
  fireEvent.change(input,{target:{value:"创建项目编号ABC"}});fireEvent.submit(input.closest("form")!);
  fireEvent.click(await screen.findByText("使用原请求标识重试"));
  await screen.findByText("请补充名称");
  expect(command.mock.calls[1]).toEqual(command.mock.calls[0]);
  expect(screen.queryByText("采用所示参数并执行整组建档")).toBeNull();
});
