import test from 'node:test';
import assert from 'node:assert/strict';
import {runChat} from '../src/chat.mjs';
import {ChatFailure,safeFailure} from '../src/chat-errors.mjs';

const body={tool_grant:'synthetic-grant',messages:[{role:'user',body:'为什么我的上位机没有upper_body的可选用例编号'}],context:{view:'home'},choices:{},timeout_ms:120000};
const config={apiKey:'synthetic-key',bridgeSecret:'synthetic-bridge',modelUrl:'http://fixture.invalid/v1',mode:'mock'};
function stream(content,tool=false,usage=true){
  const event=(delta,finish_reason=null)=>`data: ${JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:'deepseek-flash',choices:[{index:0,delta,finish_reason}]})}\n\n`;
  let text=event({role:'assistant'})+event(tool?{tool_calls:[{index:0,id:'tool-'+Math.random(),type:'function',function:{name:'list_hmi_stations',arguments:'{}'}}]}:{content})+event({},tool?'tool_calls':'stop');
  if(usage)text+=`data: ${JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:40,prompt_cache_miss_tokens:60,total_tokens:120}})}\n\n`;
  return new Response(text+'data: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}});
}
for(const mode of ['final','ignores_final','provider_http','provider_network','no_usage'])test('chat control '+mode,async(t)=>{
  const previous=globalThis.fetch;const requests=[];const callbacks=[];
  t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/')){
      callbacks.push({path:String(url).split('/').at(-1),...data});
      return new Response(JSON.stringify({data:data.phase==='settle'?{accounted:!!data.usage}:{source:'/api/v1/hmi/stations',data:{items:[]}}}),{headers:{'Content-Type':'application/json'}});
    }
    requests.push(data);
    if(mode==='provider_http')return new Response('private provider detail',{status:429});
    if(mode==='provider_network')throw new TypeError('private network detail');
    if(mode==='no_usage')return stream('不能计费',false,false);
    return stream('已取得目录及源码；现场版本仍待核对。',data.tool_choice!=='none'||mode==='ignores_final');
  };
  if(mode==='final'){
    const result=await runChat(config,body,new AbortController().signal);
    assert.equal(result.completion,'partial');assert.equal(result.termination_reason,'round_limit');
    assert.equal(requests.length,6);assert.equal(requests[5].tools,undefined);assert.equal(requests[5].tool_choice,'none');
    assert.equal(callbacks.filter(c=>c.path==='chat_read').length,5);
    assert.equal(callbacks.filter(c=>c.phase==='reserve').length,6);
    assert.equal(callbacks.filter(c=>c.phase==='settle').length,6);
  }else{
    const reason={ignores_final:'round_limit',provider_http:'provider_http',provider_network:'provider_network',no_usage:'billing_unverified'}[mode];
    await assert.rejects(runChat(config,body,new AbortController().signal),e=>e instanceof ChatFailure&&e.reason===reason);
    if(mode==='ignores_final'){assert.equal(requests.length,6);assert.equal(callbacks.filter(c=>c.path==='chat_read').length,5);}
  }
});

test('public failures never include arbitrary exception details',()=>{
  assert.deepEqual(safeFailure(new Error('private prompt/credential')),{status:502,body:{error:'tool_failed'}});
  assert.equal(safeFailure(new ChatFailure('round_limit')).body.error,'round_limit');
  assert.equal(safeFailure(new Error('private'),true).body.error,'deadline');
});
