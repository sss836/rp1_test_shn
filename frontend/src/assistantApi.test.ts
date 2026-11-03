// @vitest-environment jsdom
import { afterEach,expect,it,vi } from "vitest";
import { assistantApi,MAX_DIAGNOSTIC_ATTACHMENT_BYTES } from "./assistantApi";
import { apiRequest } from "./client";
vi.mock("./client",()=>({apiRequest:vi.fn()}));
afterEach(()=>vi.clearAllMocks());

it("uploads Unicode filenames via UTF-8 header and preserves opaque bytes regardless of extension",async()=>{
  const file=new File([new Uint8Array([0,1,255])],"现场日志与参数.tar.gz");
  const signal=new AbortController().signal;
  await assistantApi.uploadAttachment(file,signal);
  expect(apiRequest).toHaveBeenCalledWith("/api/v1/assistant/attachments",expect.objectContaining({
    method:"POST",body:file,signal,headers:{"Content-Type":"application/octet-stream","X-Filename-UTF8":encodeURIComponent(file.name),"X-Filename":encodeURIComponent(file.name)},
  }));
});

it("allows CSV, archives, and unknown filenames without an extension allowlist",async()=>{
  for(const name of ["measurements.csv","log.tar","log.tar.gz","bundle.zip","firmware.bin","no-extension"])
    await assistantApi.uploadAttachment(new File(["data"],name));
  expect(apiRequest).toHaveBeenCalledTimes(6);
});

it("rejects empty and over-32MiB files before issuing a request and accepts the exact boundary",async()=>{
  await expect(assistantApi.uploadAttachment(new File([],"empty.csv"))).rejects.toThrow("32MiB");
  const oversized=new File(["data"],"huge.zip");Object.defineProperty(oversized,"size",{value:MAX_DIAGNOSTIC_ATTACHMENT_BYTES+1});
  await expect(assistantApi.uploadAttachment(oversized)).rejects.toThrow("32MiB");
  expect(apiRequest).not.toHaveBeenCalled();
  const boundary=new File(["data"],"boundary.bin");Object.defineProperty(boundary,"size",{value:MAX_DIAGNOSTIC_ATTACHMENT_BYTES});
  await assistantApi.uploadAttachment(boundary);expect(apiRequest).toHaveBeenCalledOnce();
});

it("passes cancellation to attachment removal",async()=>{
  const signal=new AbortController().signal;await assistantApi.deleteAttachment("synthetic",signal);
  expect(apiRequest).toHaveBeenCalledWith("/api/v1/assistant/attachments/synthetic",{method:"DELETE",signal});
});
