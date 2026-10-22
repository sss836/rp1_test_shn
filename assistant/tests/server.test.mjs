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
