import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";
import { pathToFileURL } from "node:url";
import { loadConfig } from "./config.mjs";
import { runProbe } from "./probe.mjs";
import { runSetup } from "./setup.mjs";
import { runChat } from "./chat.mjs";
import { safeFailure } from "./chat-errors.mjs";

function secretMatches(provided, expected) {
  const left = Buffer.from(typeof provided === "string" ? provided : "");
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function send(response, status, data) {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(data));
}

export function buildServer(config, probe = runProbe, chat = runChat) {
  let active = 0;
  const server = createServer(async (request, response) => {
    if (request.method === "GET" && request.url === "/healthz") return send(response, 200, { status: "ok" });
    const isSetup=request.url === "/internal/setup";
    const isChat=request.url === "/internal/chat";
    if (request.method !== "POST" || (!isChat && !isSetup && request.url !== "/internal/probe")) return send(response, 404, { error: "not_found" });
    if (!secretMatches(request.headers["x-rp1-bridge-secret"], config.bridgeSecret)) return send(response, 403, { error: "forbidden" });
    if (!isChat && config.allowLegacy === false) return send(response,410,{error:"legacy_closed"});
    if (active >= 2) return send(response, 429, { error: "probe_busy" });
    active++;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), isChat?120000:20000);
    const onClose = () => { if (!response.writableEnded) controller.abort(); };
    response.on("close", onClose);
    try {
      const chunks = [];
      let bytes = 0;
      for await (const chunk of request) {
        bytes += chunk.length;
        if (bytes > (isChat?18000000:isSetup ? 131072 : 1024)) return send(response, 413, { error: "body_too_large" });
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
      if (!body || typeof body !== "object" || Array.isArray(body) || (isChat?![5,6].includes(Object.keys(body).length):Object.keys(body).length !== (isSetup ? 3 : 1)) || typeof body.tool_grant !== "string" || !/^[A-Za-z0-9_-]{32,128}$/.test(body.tool_grant)) {
        return send(response, 422, { error: "invalid_probe" });
      }
      if(isSetup && (!Array.isArray(body.messages)||body.messages.length>12||!body.messages.every((m)=>typeof m==="string"&&m.length<=8000)||!body.choices||typeof body.choices!=="object"))return send(response,422,{error:"invalid_setup"});
      if(isChat&&(!Array.isArray(body.messages)||body.messages.length>20||!body.context||!body.choices||!Number.isInteger(body.timeout_ms)||body.timeout_ms<1000||body.timeout_ms>120000))return send(response,422,{error:"invalid_chat"});
      if(isChat&&body.attachments!==undefined&&(!Array.isArray(body.attachments)||body.attachments.length>3||body.attachments.some(a=>!a||!["text","image"].includes(a.type)||(a.type==="image"&&(!["image/png","image/jpeg"].includes(a.mimeType)||typeof a.data!=="string"||a.data.length>5700000))||(a.type==="text"&&(typeof a.text!=="string"||a.text.length>25000)))))return send(response,422,{error:"invalid_attachment"});
      send(response, 200, isChat?await chat(config,body,controller.signal):isSetup ? await runSetup(config, body, controller.signal) : await probe(config, body.tool_grant, controller.signal));
    } catch(error) {
      // Never return or log provider errors, prompts, tokens or raw payloads.
      if(isChat){const failure=safeFailure(error,controller.signal.aborted);console.log(JSON.stringify({event:"assistant_request_failed",reason:failure.body.error}));send(response,failure.status,failure.body);}
      else send(response, controller.signal.aborted ? 504 : 502, { error: "probe_failed" });
    } finally {
      active--;
      clearTimeout(timeout);
      response.removeListener("close", onClose);
    }
  });
  server.requestTimeout = 125000;
  server.headersTimeout = 5000;
  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    buildServer(loadConfig()).listen(3000, "0.0.0.0", () => console.log("rp1-pi-bridge ready"));
  } catch {
    console.error("rp1-pi-bridge configuration invalid; check server secret files and isolation settings");
    process.exitCode = 1;
  }
}
