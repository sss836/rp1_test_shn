import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {loadTokenHub,tokenHubModel} from '../src/tokenhub.mjs';
import {runChat} from '../src/chat.mjs';

export const profile={region:'guangzhou',base_url:'https://tokenhub.tencentmaas.com/v1',key_type:'pay_as_you_go',currency:'CNY',revision:'SYNTHETIC-NOT-ACCOUNT-EVIDENCE',account_evidence:'synthetic fixture only',price_source:'https://example.invalid/synthetic',verified_at:'2026-10-01T00:00:00+08:00',valid_until:'2099-01-01T00:00:00+08:00',text_model:'fixture-text',vision_model:'fixture-vision',models:{'fixture-text':{tools:true,vision:false,input:'2',cache_hit:'1',output:'8',max_input_tokens:32000,max_output_tokens:4096},'fixture-vision':{tools:true,vision:true,input:'4',cache_hit:'2',output:'12',max_input_tokens:64000,max_output_tokens:4096}}};
test('profile rejects wrong region, subscription, unverified capability and expiry',t=>{
  const dir=mkdtempSync(join(tmpdir(),'rp1-tokenhub-'));t.after(()=>rmSync(dir,{recursive:true}));const path=join(dir,'profile.json');
  writeFileSync(path,JSON.stringify(profile));const p=loadTokenHub(path);
  assert.equal(tokenHubModel(p,false).id,'fixture-text');assert.equal(tokenHubModel(p,true).id,'fixture-vision');
  for(const change of [{region:'unknown'},{key_type:'token_plan'},{valid_until:'2000-01-01T00:00:00Z'},{vision_model:'missing'}]){
    writeFileSync(path,JSON.stringify({...profile,...change}));assert.throws(()=>loadTokenHub(path));
  }
});

test('enterprise key public budget needs an explicit owner decision, never an invoice claim',t=>{
  const dir=mkdtempSync(join(tmpdir(),'rp1-tokenhub-'));t.after(()=>rmSync(dir,{recursive:true}));
  const path=join(dir,'profile.json');
  const approved={...profile,key_type:'enterprise_api_key',billing_basis:'published_peak_upper_bound',billing_authorization:'synthetic owner-approved public-price budget'};
  writeFileSync(path,JSON.stringify(approved));
  assert.equal(loadTokenHub(path).key_type,'enterprise_api_key');
  for(const change of [{billing_authorization:''},{billing_basis:'unmetered'},{key_type:'token_plan'}]){
    writeFileSync(path,JSON.stringify({...approved,...change}));assert.throws(()=>loadTokenHub(path));
  }
});

for(const hasImage of [false,true])test('TokenHub streams text/image, calls tools and meters selected model '+hasImage,async t=>{
  const original=globalThis.fetch;t.after(()=>globalThis.fetch=original);
  const requests=[],callbacks=[];let calls=0;
  const p={...profile,profile_sha256:'synthetic-profile-hash'};
  globalThis.fetch=async(url,init)=>{
    const data=JSON.parse(init.body);
    if(String(url).includes('/tools/')){
      callbacks.push(data);
      return Response.json({data:data.phase==='reserve'?{profile_sha256:p.profile_sha256}:data.phase==='settle'?{accounted:true}:{source:'synthetic-station',data:{items:[]}}});
    }
    requests.push(data);const tool=++calls===1;
    const item=(delta,finish_reason=null)=>'data: '+JSON.stringify({id:'fixture',model:data.model,choices:[{index:0,delta,finish_reason}]})+'\n\n';
    return new Response(item(tool?{tool_calls:[{index:0,id:'fixture-call',type:'function',function:{name:'list_hmi_stations',arguments:'{}'}}]}:{content:'合成证据已核对。'})+item({},tool?'tool_calls':'stop')+'data: '+JSON.stringify({choices:[],usage:{prompt_tokens:100,completion_tokens:20,total_tokens:120}})+'\n\ndata: [DONE]\n\n',{headers:{'Content-Type':'text/event-stream'}});
  };
  const result=await runChat({apiKey:'fixture-secret',bridgeSecret:'fixture-bridge',modelUrl:profile.base_url,tokenhub:p,mode:'mock'},
    {tool_grant:'fixture-grant',messages:[{body:'查询工位'}],context:{},choices:{},timeout_ms:10000,attachments:hasImage?[{type:'image',mimeType:'image/png',data:'aGVsbG8='}]:[]},new AbortController().signal);
  assert.equal(result.rounds,2);assert.match(result.reply,/合成/);
  assert.ok(requests.every(r=>r.model===(hasImage?'fixture-vision':'fixture-text')&&r.stream_options.include_usage===true&&r.thinking===undefined));
  assert.equal(callbacks.filter(c=>c.phase==='reserve').length,2);
  assert.equal(callbacks.filter(c=>c.phase==='settle').length,2);
  assert.equal(callbacks.filter(c=>c.tool==='list_hmi_stations').length,1);
  assert.equal(requests[0].messages.some(m=>Array.isArray(m.content)&&m.content.some(c=>c.type==='image_url')),hasImage);
});
