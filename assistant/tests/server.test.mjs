import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildServer } from "../src/server.mjs";
import { loadConfig } from "../src/config.mjs";

test("server-only config fixes official model endpoint and rejects device/DB config", () => {
  const dir = mkdtempSync(join(tmpdir(), "rp1-pi-test-"));
  const secret = join(dir, "secret");
  writeFileSync(secret, "a".repeat(48));
  try {
    const env = { RP1_BRIDGE_SECRET_FILE: secret, DEEPSEEK_API_KEY_FILE: secret };
    assert.equal(loadConfig(env).modelUrl, "https://api.deepseek.com");
    assert.equal(loadConfig({...env,APP_ENV:'production'}).allowLegacy,false);
    assert.throws(() => loadConfig({ ...env, DATABASE_URL: "forbidden" }));
    assert.throws(() => loadConfig({ ...env, PLC_HOST: "forbidden" }));
    assert.throws(() => loadConfig({ ...env, PI_MODEL_MODE: "mock", APP_ENV: "production" }));
    assert.throws(() => loadConfig({ RP1_BRIDGE_SECRET_FILE: secret }));
    assert.equal(loadConfig({ ...env, PI_MODEL_MODE: "mock", APP_ENV: "test" }).modelUrl, "http://model-mock:3100/v1");
  } finally { rmSync(dir, { recursive: true }); }
});

test('production bridge closes legacy unmetered endpoints even with bridge authentication',async(t)=>{
  const secret='c'.repeat(48);
  const server=buildServer({bridgeSecret:secret,allowLegacy:false},async()=>{throw new Error('must not call model');});
  server.listen(0,'127.0.0.1');await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  for(const path of ['/internal/probe','/internal/setup']){
    const response=await fetch(`http://127.0.0.1:${server.address().port}`+path,{method:'POST',headers:{'X-RP1-Bridge-Secret':secret},body:'{}'});
    assert.equal(response.status,410);
  }
});

test("private server rejects unauthenticated, arbitrary input and oversized bodies", async (t) => {
  let calls = 0;
  const secret = "b".repeat(48);
  const server = buildServer({ bridgeSecret: secret }, async () => { calls++; return { reply: "synthetic" }; });
  server.listen(0, "127.0.0.1");
  await new Promise((resolve) => server.once("listening", resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}`;
  const send = (body, token = secret) => fetch(url + "/internal/probe", { method: "POST", headers: { "X-RP1-Bridge-Secret": token }, body: JSON.stringify(body) });
  assert.equal((await send({ tool_grant: "g".repeat(64) }, "forged")).status, 403);
  assert.equal((await send({ tool_grant: "g".repeat(64), prompt: "run shell" })).status, 422);
  assert.equal((await send({ tool_grant: "x".repeat(2000) })).status, 413);
  assert.equal((await fetch(url + "/tools/execute_sql")).status, 404);
  assert.equal((await send({ tool_grant: "g".repeat(64) })).status, 200);
  assert.equal(calls, 1);
});

test('private chat accepts bounded diagnostic previews and rejects invalid attachment blocks before model',async(t)=>{
  const secret='d'.repeat(48);const calls=[];
  const server=buildServer({bridgeSecret:secret},async()=>{},async(_config,body)=>{calls.push(body);return {reply:'isolated'};});
  server.listen(0,'127.0.0.1');await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const base={tool_grant:'g'.repeat(64),messages:[{role:'user',body:'分析附件'}],context:{},choices:{},timeout_ms:120000};
  const send=attachments=>fetch(`http://127.0.0.1:${server.address().port}/internal/chat`,{method:'POST',headers:{'X-RP1-Bridge-Secret':secret},body:JSON.stringify({...base,attachments})});
  assert.equal((await send([{type:'text',text:'资料预览；未读正文请按附件ID分块读取。'},{type:'image',mimeType:'image/png',data:'YWJj'}])).status,200);
  for(const items of [Array(4).fill({type:'text',text:'x'}),[{type:'text',text:'x'.repeat(25001)}],
    [{type:'image',mimeType:'image/png',data:'http://host/private'}],[{type:'toolResult',text:'execute'}]])assert.equal((await send(items)).status,422);
  assert.equal(calls.length,1);
});

test('two images, three images and mixed previews use six body fields without widening image block count',async(t)=>{
  const secret='e'.repeat(48);const calls=[];
  const server=buildServer({bridgeSecret:secret},async()=>{},async(_config,body)=>{calls.push(body);return {reply:'isolated'};});
  server.listen(0,'127.0.0.1');await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  for(const count of [2,3,1]){
    const previews=['图片来源metadata','CSV初始分块，剩余内容未读','包目录未读成员正文'];
    const body={tool_grant:'g'.repeat(64),messages:[{role:'user',body:'诊断资料'}],choices:{},
      context:{diagnostic_attachment_previews:previews},attachments:Array.from({length:count},()=>({type:'image',mimeType:'image/png',data:'YWJj'})),timeout_ms:120000};
    const response=await fetch(`http://127.0.0.1:${server.address().port}/internal/chat`,{method:'POST',headers:{'X-RP1-Bridge-Secret':secret},body:JSON.stringify(body)});
    assert.equal(response.status,200);assert.equal(Object.keys(calls.at(-1)).length,6);
    assert.equal(calls.at(-1).attachments.length,count);assert.deepEqual(calls.at(-1).context.diagnostic_attachment_previews,previews);
    if(count>=2){
      const legacy={...body,attachments:body.attachments.flatMap(image=>[image,{type:'text',text:'image provenance'}])};
      assert.equal((await fetch(`http://127.0.0.1:${server.address().port}/internal/chat`,{method:'POST',headers:{'X-RP1-Bridge-Secret':secret},body:JSON.stringify(legacy)})).status,422);
      assert.equal(calls.at(-1).attachments.length,count);
    }
  }
});
