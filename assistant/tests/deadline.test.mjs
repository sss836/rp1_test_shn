import test from 'node:test';
import assert from 'node:assert/strict';
import OpenAI from 'openai';
import { modelTimeout } from '../src/chat.mjs';

test('SDK accepts fractional remaining time only after bounding to integer milliseconds', async()=>{
  let calls=0;
  const client=new OpenAI({apiKey:'synthetic-not-a-secret',maxRetries:0,fetch:async()=>{calls++;return new Response('{}',{headers:{'Content-Type':'application/json'}});}});
  await assert.rejects(client.chat.completions.create({model:'synthetic',messages:[]},{timeout:19863.25}),/integer/);
  assert.equal(calls,0);
  await client.chat.completions.create({model:'synthetic',messages:[]},{timeout:modelTimeout(19863.25)});
  assert.equal(calls,1);
  assert.equal(modelTimeout(60000),30000);
  assert.equal(modelTimeout(.25),1);
});
