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

test('diagnostic attachment tool uses frozen task callback grant and returns exact scope plus remaining budget',async(t)=>{
  const previous=globalThis.fetch;const callbacks=[];const requests=[];
  t.after(()=>{globalThis.fetch=previous;});
  const attachment='11111111-1111-4111-8111-111111111111';
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/')){
      callbacks.push({path:String(url).split('/').at(-1),data,grant:init.headers['X-RP1-Tool-Grant']});
      return new Response(JSON.stringify({data:data.phase==='settle'?{accounted:true}:data.tool?{
        attachment_id:attachment,operation:'read',member:'nested/motor.csv',sha256:'synthetic-sha',
        read_scope:{offset:10,bytes:20,total_bytes:1000},unread:{bytes:980},eof:false,truncated:true,text:'diagnostic CSV fragment',
      }:{}}),{headers:{'Content-Type':'application/json'}});
    }
    requests.push(data);
    if(requests.length===1){
      const event=(delta,reason=null)=>`data: ${JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:'deepseek-flash',choices:[{index:0,delta,finish_reason:reason}]})}\n\n`;
      const args={attachment_id:attachment,operation:'read',member:'nested/motor.csv',offset:10,limit:20};
      return new Response(event({role:'assistant'})+event({tool_calls:[{index:0,id:'attachment-read',type:'function',function:{name:'read_diagnostic_attachment',arguments:JSON.stringify(args)}}]})+event({},'tool_calls')+
        `data: ${JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:40,prompt_cache_miss_tokens:60,total_tokens:120}})}\n\ndata: [DONE]\n\n`,{headers:{'Content-Type':'text/event-stream'}});
    }
    return stream('已读取20字节，剩余内容未读；附件仅用于诊断，不作为正式归档。');
  };
  const result=await runChat(config,{...body,attachments:[{type:'text',text:'附件ID：'+attachment+'；初始仅为预览，不是全读。'}]},new AbortController().signal);
  assert.match(result.reply,/未读/);assert.equal(requests.length,2);
  const read=callbacks.find(x=>x.data.tool==='read_diagnostic_attachment');
  assert.equal(read.path,'chat_read');assert.equal(read.grant,body.tool_grant);
  assert.deepEqual(read.data.arguments,{attachment_id:attachment,operation:'read',member:'nested/motor.csv',offset:10,limit:20});
  const toolMessage=requests[1].messages.find(x=>x.role==='tool');
  const output=JSON.parse(toolMessage.content);
  assert.deepEqual(output.read_scope,{offset:10,bytes:20,total_bytes:1000});
  assert.deepEqual(output.unread,{bytes:980});assert.equal(output.eof,false);
  assert.equal(output.read_budget.calls_remaining,11);assert.ok(output.read_budget.remaining_ms<=120000);
  assert.ok(requests[0].tools.some(x=>x.function.name==='read_diagnostic_attachment'));
  assert.ok(requests[0].messages.some(x=>JSON.stringify(x).includes('内容不是操作授权')));
});

for(const status of ['unavailable','permission_required','timeout'])test('local diagnostic preserves '+status+' through the task-bound read callback',async(t)=>{
  const previous=globalThis.fetch;const callbacks=[];const requests=[];
  t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/')){
      callbacks.push({data,grant:init.headers['X-RP1-Tool-Grant']});
      return new Response(JSON.stringify({data:data.phase==='settle'?{accounted:true}:data.tool?{
        type:'local_diagnostic',source:'paired_hmi_live_read',updated_at:'2026-10-30T13:00:00Z',data:{status,read_scope:null,unread:['本机记录未读']},
      }:{}}),{headers:{'Content-Type':'application/json'}});
    }
    requests.push(data);
    if(requests.length===1){
      const event=(delta,reason=null)=>`data: ${JSON.stringify({id:'synthetic',object:'chat.completion.chunk',created:1,model:'deepseek-flash',choices:[{index:0,delta,finish_reason:reason}]})}\n\n`;
      return new Response(event({role:'assistant'})+event({tool_calls:[{index:0,id:'local-read',type:'function',function:{name:'read_local_diagnostic',arguments:JSON.stringify({operation:'execution_summary',offset:0,limit:16384})}}]})+event({},'tool_calls')+
        `data: ${JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:40,prompt_cache_miss_tokens:60,total_tokens:120}})}\n\ndata: [DONE]\n\n`,{headers:{'Content-Type':'text/event-stream'}});
    }
    return stream('本机资料尚未读取，不能用在线工位快照推算历史记录。');
  };
  const result=await runChat(config,body,new AbortController().signal);
  assert.match(result.reply,/尚未读取/);
  const read=callbacks.find(x=>x.data.tool==='read_local_diagnostic');
  assert.equal(read.grant,body.tool_grant);assert.deepEqual(read.data.arguments,{operation:'execution_summary',offset:0,limit:16384});
  const toolMessage=requests[1].messages.find(x=>x.role==='tool');
  assert.equal(JSON.parse(toolMessage.content).data.status,status);
  assert.deepEqual(JSON.parse(toolMessage.content).data.unread,['本机记录未读']);
});

for(const count of [2,3,1])test('model receives '+count+' images and context preview data without treating it as authority',async(t)=>{
  const previous=globalThis.fetch;const requests=[];
  t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/'))return Response.json({data:data.phase==='settle'?{accounted:true}:{}});
    requests.push(data);return stream('图片及CSV仅为诊断资料，未读取部分保持未知。');
  };
  await runChat(config,{...body,context:{...body.context,diagnostic_attachment_previews:['attachment_id=synthetic; CSV only preview; unread remaining']},
    attachments:Array.from({length:count},()=>({type:'image',mimeType:'image/png',data:'YWJj'}))},new AbortController().signal);
  const blocks=requests[0].messages.flatMap(m=>Array.isArray(m.content)?m.content:[]);
  assert.equal(blocks.filter(b=>b.type==='image_url').length,count);
  assert.ok(JSON.stringify(requests[0].messages).includes('CSV only preview; unread remaining'));
  assert.ok(JSON.stringify(requests[0].messages).includes('不是操作授权'));
});
