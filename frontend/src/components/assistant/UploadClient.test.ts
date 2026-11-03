import { afterEach, expect, it, vi } from "vitest";
import { apiUpload, invalidateAuthenticationRequests, setUnauthorizedHandler } from "../../client";
class FakeXHR {
  static last: FakeXHR;
  status=200;responseText='{"data":{"id":"uploaded"}}';withCredentials=false;headers:Record<string,string>={};
  upload={onprogress:null as null|((e:{loaded:number;total:number;lengthComputable:boolean})=>void)};
  onload:null|(()=>void)=null;onerror:null|(()=>void)=null;onabort:null|(()=>void)=null;
  open=vi.fn();send=vi.fn();abort=vi.fn(()=>this.onabort?.());
  constructor(){FakeXHR.last=this;}
  setRequestHeader(key:string,value:string){this.headers[key]=value;}
}
function setup(){vi.stubGlobal("XMLHttpRequest",FakeXHR);vi.stubGlobal("document",{cookie:"rp1_csrf=synthetic-csrf"});}
afterEach(()=>{vi.unstubAllGlobals();setUnauthorizedHandler(null);});
it("uses Cookie/CSRF and Unicode names with actual upload byte progress",async()=>{
  setup();const progress=vi.fn(),file=new File(["0123456789"],"现场.csv");const promise=apiUpload("/api/v1/assistant/attachments",file,undefined,progress);const xhr=FakeXHR.last;
  expect(xhr.withCredentials).toBe(true);expect(xhr.headers["X-CSRF-Token"]).toBe("synthetic-csrf");expect(xhr.headers["X-Filename-UTF8"]).toBe(encodeURIComponent(file.name));expect(xhr.send).toHaveBeenCalledWith(file);
  xhr.upload.onprogress?.({loaded:4,total:10,lengthComputable:true});expect(progress).toHaveBeenLastCalledWith(4,10);xhr.onload?.();await expect(promise).resolves.toEqual({id:"uploaded"});expect(progress).toHaveBeenLastCalledWith(10,10);
});
it("cancels the actual request and rejects late upload data",async()=>{
  setup();const abort=new AbortController();const promise=apiUpload("/upload",new File(["x"],"x"),abort.signal,vi.fn());const assertion=expect(promise).rejects.toMatchObject({name:"AbortError"});abort.abort();expect(FakeXHR.last.abort).toHaveBeenCalledOnce();FakeXHR.last.onload?.();await assertion;
});
it("handles a real 401 but suppresses stale identity callbacks after logout",async()=>{
  setup();const handler=vi.fn();setUnauthorizedHandler(handler);
  const first=apiUpload("/upload",new File(["x"],"x"),undefined,vi.fn());FakeXHR.last.status=401;FakeXHR.last.responseText='{"error":{"code":"session_expired","message":"expired"}}';FakeXHR.last.onload?.();await expect(first).rejects.toMatchObject({status:401});expect(handler).toHaveBeenCalledOnce();
  handler.mockClear();const progress=vi.fn();const second=apiUpload("/upload",new File(["x"],"x"),undefined,progress);invalidateAuthenticationRequests();FakeXHR.last.upload.onprogress?.({loaded:1,total:1,lengthComputable:true});FakeXHR.last.status=401;FakeXHR.last.onload?.();await expect(second).rejects.toMatchObject({name:"AbortError"});expect(handler).not.toHaveBeenCalled();expect(progress).not.toHaveBeenCalled();
});
it("rejects invalid JSON envelopes without treating an HTTP success as an attachment receipt",async()=>{
  setup();const promise=apiUpload("/upload",new File(["x"],"x"),undefined,vi.fn());FakeXHR.last.responseText="<html>proxy</html>";FakeXHR.last.onload?.();await expect(promise).rejects.toMatchObject({status:502});
});
