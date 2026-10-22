// Synthetic OpenAI-compatible SSE fixture. Never included in the Pi image.
import { createServer } from "node:http";
let mode = "normal";
let requests = [];
let setupOperations = [];
let chatTool="list_hmi_stations",chatArguments={};
const server = createServer(async (req, res) => {
  let raw = "";
  for await (const chunk of req) raw += chunk;
  if (req.url === "/control" && req.method === "POST") {
    const control = JSON.parse(raw);
    mode = control.mode;
    setupOperations = control.operations ?? [];
    chatTool=control.tool??"list_hmi_stations";chatArguments=control.arguments??{};
    requests = [];
    res.end("{}"); return;
  }
  if (req.url === "/stats") {
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ requests })); return;
  }
  if (req.url !== "/v1/chat/completions") { res.writeHead(404); res.end(); return; }
  const body = JSON.parse(raw);
  requests.push({
    model: body.model, max_tokens: body.max_tokens, thinking: body.thinking,
    tool_names: body.tools.map((item) => item.function.name),
    tool_parameters: body.tools.map((item) => item.function.parameters),
    roles: body.messages.map((item) => item.role),
    contains_rp1_credentials: /tool_grant|rp1_session|rp1_csrf|X-RP1-Tool-Grant|X-RP1-Bridge-Secret/i.test(raw),
  });
  if (mode === "limited") { res.writeHead(429); res.end('{"error":{"message":"synthetic sensitive provider detail"}}'); return; }
  if (mode === "disconnect") { req.socket.destroy(); return; }
  if (mode === "pause_before_tool") await new Promise((resolve) => setTimeout(resolve, 1500));
  if (mode === "slow") await new Promise((resolve) => setTimeout(resolve, 22000));
  const toolResult = body.messages.findLast((item) => item.role === "tool");
  const isSetup=body.tools.some((item)=>item.function.name==="apply_setup_bundle");
  const isChat=body.tools.some((item)=>item.function.name==="get_test_progress");
  if(isSetup && mode==="lose_final" && toolResult){req.socket.destroy();return;}
  res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
  function event(delta, finish_reason = null) {
    res.write(`data: ${JSON.stringify({ id: "synthetic-completion", object: "chat.completion.chunk", created: 1, model: "deepseek-flash", choices: [{ index: 0, delta, finish_reason }] })}\n\n`);
  }
  event({ role: "assistant" });
  if (mode === "no_tool") {
    event({ content: "未调用工具。" }); event({}, "stop");
  } else if (!toolResult || mode === "repeat") {
    event({ tool_calls: [{ index: 0, id: "synthetic-tool-call-" + requests.length, type: "function", function: {
      name: mode === "unknown_tool" ? "execute_sql" : isChat ? chatTool : isSetup ? "apply_setup_bundle" : "list_hmi_stations",
      arguments: mode === "extra_args" ? '{"user_id":"forged-admin"}' : (isChat?chatTool==="apply_setup_bundle":isSetup) ? JSON.stringify({operations:setupOperations}) : isChat?JSON.stringify(chatArguments):"{}",
    } }] });
    event({}, "tool_calls");
  } else {
    let data;
    try { data = JSON.parse(toolResult.content); } catch { data = null; }
    if(isChat) {event({content:data?.question??`已查询实际工具回执，来源${data?.source??"未提供"}，更新时间${data?.updated_at??"未提供"}。`});}
    else if(isSetup) {event({content:data?.question??"请补充建档字段。"});}
    else if (!data?.items) { event({ content: "工具拒绝执行。" }); }
    else { event({ content: `当前可见${data.items.length}个工位：${data.items.map((item) => item.station_name).join("、")}；更新时间${data.as_of_at}。此为合成模型响应。` }); }
    event({}, "stop");
  }
  if(mode!=="no_usage")res.write(`data: ${JSON.stringify({id:"synthetic-completion",model:"deepseek-flash",choices:[],usage:{prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:40,prompt_cache_miss_tokens:60,total_tokens:120}})}\n\n`);
  res.end("data: [DONE]\n\n");
});
server.listen(3100, "0.0.0.0");
