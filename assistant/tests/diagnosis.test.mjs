import test from 'node:test';
import assert from 'node:assert/strict';
import {runChat} from '../src/chat.mjs';
import {needsDiagnosis} from '../src/diagnosis.mjs';

const body={tool_grant:'synthetic-grant-only',messages:[{role:'user',body:'检查CSV缺失和连接超时的具体原因'}],
  context:{hmi:{installation_id:'synthetic',window_id:'synthetic'}},choices:{},timeout_ms:120000,
  resume:{rounds_used:0,reads_used:0,writes_used:0,active_elapsed_ms:0,evidence:[],permissions:[]}};
const config={apiKey:'synthetic-key',bridgeSecret:'synthetic-bridge',modelUrl:'http://fixture.invalid/v1',mode:'mock'};
const card=(data)=>({type:'diagnosis_preparation',data});
function stream(reply){
  const chunk=(delta,finish_reason=null)=>'data: '+JSON.stringify({id:'fixture',object:'chat.completion.chunk',created:1,model:'deepseek-flash',choices:[{index:0,delta,finish_reason}]})+'\n\n';
  return new Response(chunk({role:'assistant'})+chunk({content:reply})+chunk({},'stop')+
    'data: '+JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,total_tokens:120}})+'\n\ndata: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}});
}

test('automatic diagnosis uses trusted task input, not generic page status',()=>{
  assert.equal(needsDiagnosis(body),true);
  assert.equal(needsDiagnosis({...body,messages:[{role:'user',body:'查询工位'}]}),false);
  assert.equal(needsDiagnosis({...body,resume:undefined}),false);
  assert.equal(needsDiagnosis({...body,context:{},attachments:[{type:'text',text:'文件=motor.csv；初始预览'}]}),true);
  assert.equal(needsDiagnosis({...body,messages:[{role:'user',body:'检查这个文件'}],context:{diagnostic_attachment_inventory:[{id:'frozen',name:'motor.csv',kind:'text'}]},attachments:[]}),true);
});

test('permission pause ends before model billing and keeps counters from previous phase',async(t)=>{
  const previous=fetch;const calls=[];t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    calls.push({url,data:JSON.parse(init.body)});
    assert.match(url,/\/tools\/chat_read$/);
    return Response.json({data:card({phase:'awaiting_permission',pending_permissions:[{id:'synthetic'}],evidence_complete:false})});
  };
  const result=await runChat(config,{...body,resume:{...body.resume,rounds_used:2,reads_used:3}},new AbortController().signal);
  assert.equal(result.completion,'awaiting_permission');assert.equal(result.rounds,2);assert.equal(result.reads,4);
  assert.equal(calls.length,1);assert.equal(calls[0].data.tool,'prepare_diagnosis');
});

test('all available stages are collected before a model can prematurely answer',async(t)=>{
  const previous=fetch;let steps=0;const requests=[];const reserves=[];t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(url.includes('/tools/')){
      if(data.tool==='prepare_diagnosis'){
        steps++;
        return Response.json({data:card(steps<3?{continuation:true,evidence_complete:false}:{continuation:false,evidence_complete:true,
          evidence:[{source:'$STATE/measurements.csv',tail_gap_row:15001},{source:'$STATE/communication.log',underlying_error:'ConnectionRefusedError'}]})});
      }
      if(data.phase==='reserve')reserves.push(data.sequence);
      return Response.json({data:data.phase==='settle'?{accounted:true}:{messages:[]}});
    }
    assert.equal(steps,3);requests.push(data);
    assert.match(JSON.stringify(data.messages),/15001/);assert.match(JSON.stringify(data.messages),/ConnectionRefusedError/);
    return stream('已检查完整CSV和日志：尾部有间隔异常；日志报告目标拒绝连接。是否同一原因仍需核对采集与通信调用链。');
  };
  const result=await runChat(config,body,new AbortController().signal);
  assert.equal(result.completion,'answered');assert.equal(result.reads,3);assert.deepEqual(reserves,[1]);assert.equal(requests.length,1);
});

test('resume at read limit explains existing evidence without resetting counters',async(t)=>{
  const previous=fetch;const reserves=[];let reads=0;t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(url.includes('/tools/')){
      if(data.tool)reads++;
      if(data.phase==='reserve')reserves.push(data.sequence);
      return Response.json({data:data.phase==='settle'?{accounted:true}:{messages:[]}});
    }
    assert.equal(data.tool_choice,'none');assert.equal(data.tools,undefined);
    return stream('只整理已有证据，未读范围仍待核实。');
  };
  const result=await runChat(config,{...body,resume:{...body.resume,rounds_used:4,reads_used:12,evidence:[{source:'synthetic-completed-read'}]}},new AbortController().signal);
  assert.equal(result.reads,12);assert.equal(reads,0);assert.deepEqual(reserves,[5]);
});

test('six used model rounds produce honest partial without another model request',async(t)=>{
  const previous=fetch;t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async()=>{throw new Error('no additional requests allowed');};
  const result=await runChat(config,{...body,resume:{...body.resume,rounds_used:6,reads_used:12}},new AbortController().signal);
  assert.equal(result.rounds,6);assert.equal(result.reads,12);assert.equal(result.completion,'partial');
});

test('missing logs remain partial instead of reporting complete evidence',async(t)=>{
  const previous=fetch;t.after(()=>{globalThis.fetch=previous;});
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(url.includes('/tools/'))return Response.json({data:data.tool?card({continuation:false,evidence_complete:false,issues:[{reason:'OS拒绝读取'}]}):data.phase==='settle'?{accounted:true}:{messages:[]}});
    return stream('CSV异常已核实，操作系统拒绝读取日志，不能确定超时根因。');
  };
  const result=await runChat(config,body,new AbortController().signal);
  assert.equal(result.completion,'partial');assert.equal(result.termination_reason,'evidence_incomplete');
});
