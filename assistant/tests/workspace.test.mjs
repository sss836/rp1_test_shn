import test from 'node:test';
import assert from 'node:assert/strict';
import {displayPrefix,workspaceTools} from '../src/workspace-tools.mjs';
import {runChat} from '../src/chat.mjs';
import {buildServer} from '../src/server.mjs';

test('display prefix withholds fragmented credentials and excludes known credentials at completion',()=>{
  const secret='synthetic-secret-'+ 'a'.repeat(64),prefix='safe '.repeat(100);
  for(let length=1;length<secret.length;length++){
    const value=displayPrefix(prefix+secret.slice(0,length),[secret]);
    assert.ok(!value.includes('synthetic-secret-'));
  }
  assert.equal(displayPrefix(prefix+secret+' trailing safe text',[secret],true),null);
  assert.equal(displayPrefix('Authorization: Bearer fictional-token',[],true),'[授权信息已隐藏]');
});

test('repair tools propose differences only and require original bytes plus hash',()=>{
  assert.deepEqual(workspaceTools.map(x=>x.name),['search_shared_faq','propose_local_repair']);
  const schema=workspaceTools[1].parameters;
  assert.equal(schema.additionalProperties,false);
  for(const key of ['original','replacement','expected_sha256','reason','kind','path'])assert.ok(schema.required.includes(key));
  assert.ok(!JSON.stringify(workspaceTools).includes('execute_shell'));
});

function completion(text){
  const event=(delta,finish_reason=null)=>'data: '+JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:'deepseek-flash',choices:[{index:0,delta,finish_reason}]})+'\n\n';
  return new Response(event({role:'assistant'})+event({reasoning_content:'private reasoning never display'})+
    event({content:text})+event({},'stop')+'data: '+JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:40,prompt_cache_miss_tokens:60,total_tokens:120}})+'\n\ndata: [DONE]\n\n',
    {headers:{'Content-Type':'text/event-stream'}});
}

test('diagnostic steering enters next turn within original round budget and streams no reasoning',async(t)=>{
  const previous=globalThis.fetch; t.after(()=>{globalThis.fetch=previous;});
  const requests=[],events=[];let controls=0;
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/pending_input'))return new Response(JSON.stringify({data:{messages:controls++===0?[{id:'synthetic-input',text:'请先解释日志时间顺序'}]:[]}}));
    if(String(url).includes('/tools/'))return new Response(JSON.stringify({data:data.phase==='settle'?{accounted:true}:{}}));
    requests.push(data);return completion(requests.length===1?'证据分析。'.repeat(100):'补充已用于本轮诊断；没有执行任何写入。');
  };
  const body={tool_grant:'g'.repeat(40),messages:[{role:'user',body:'分析故障'}],context:{},choices:{},timeout_ms:120000};
  const frozen=JSON.stringify(body);
  const result=await runChat({apiKey:'synthetic-key',bridgeSecret:'synthetic-bridge',modelUrl:'http://fixture.invalid/v1',mode:'mock'},body,new AbortController().signal,event=>{events.push(event);});
  assert.equal(requests.length,2);assert.equal(result.rounds,2);
  assert.ok(JSON.stringify(requests[1].messages).includes('请先解释日志时间顺序'));
  assert.ok(JSON.stringify(requests[1].messages).includes('不增加写入授权'));
  assert.ok(events.some(e=>e.kind==='text'&&e.data.text.length>0));
  assert.ok(!JSON.stringify(events).includes('private reasoning'));
  assert.equal(JSON.stringify(body),frozen);
});

test('private NDJSON bridge sends progress before its final authoritative result',async(t)=>{
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const server=buildServer({bridgeSecret:'s'.repeat(48)},async()=>{},async(_config,_body,_signal,emit)=>{
    await emit({kind:'text',data:{text:'已开始读取合成资料',round:1}});await gate;return {reply:'结果',rounds:1};
  });
  server.listen(0,'127.0.0.1');await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>{release();return new Promise(resolve=>server.close(resolve));});
  const response=await fetch(`http://127.0.0.1:${server.address().port}/internal/chat`,{
    method:'POST',headers:{'X-RP1-Bridge-Secret':'s'.repeat(48),'Accept':'application/x-ndjson'},
    body:JSON.stringify({tool_grant:'g'.repeat(40),messages:[{role:'user',body:'test'}],choices:{},context:{},timeout_ms:120000}),
  });
  assert.match(response.headers.get('content-type'),/ndjson/);
  const reader=response.body.getReader();const first=await reader.read();
  assert.match(new TextDecoder().decode(first.value),/已开始读取/);
  release();let rest='';for(;;){const part=await reader.read();if(part.done)break;rest+=new TextDecoder().decode(part.value);}
  assert.equal(JSON.parse(rest.trim()).kind,'result');
});
