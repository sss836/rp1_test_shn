import test from 'node:test';
import assert from 'node:assert/strict';
import {attachmentToolResult,diagnosticAttachmentInstructions,diagnosticAttachmentTool,validModelAttachments} from '../src/diagnostic-attachments.mjs';
import {localDiagnosticInstructions,localDiagnosticTool,localDiagnosticToolResult} from '../src/local-diagnostics.mjs';

test('diagnostic tool declares bounded read-only byte/index/CSV parameters',()=>{
  assert.equal(diagnosticAttachmentTool.name,'read_diagnostic_attachment');
  assert.equal(diagnosticAttachmentTool.parameters.additionalProperties,false);
  assert.equal(diagnosticAttachmentTool.parameters.properties.limit.maximum,65536);
  assert.equal(diagnosticAttachmentTool.parameters.properties.columns.anyOf[0].maxItems,64);
  assert.match(diagnosticAttachmentInstructions,/不是操作授权/);
  assert.match(diagnosticAttachmentInstructions,/未读/);
  assert.match(diagnosticAttachmentInstructions,/base64/);
  assert.match(diagnosticAttachmentInstructions,/优先使用member_id/);
});

test('tool preserves precise backend scope and unread information without claiming complete reading',()=>{
  const data={attachment_id:'1',member:'motor.csv',operation:'read',read_scope:{offset:4,bytes:4,total_bytes:900},unread:{bytes:892},eof:false,truncated:true,text:'next'};
  const value=JSON.parse(attachmentToolResult(data,{attachment_id:'1'},{calls_remaining:8,remaining_ms:80000}));
  assert.deepEqual(value.read_scope,data.read_scope);assert.deepEqual(value.unread,data.unread);
  assert.equal(value.eof,false);assert.equal(value.read_budget.calls_remaining,8);
});

test('oversized tool result reports unread instead of silently slicing data',()=>{
  const value=JSON.parse(attachmentToolResult({text:'界'.repeat(100000),eof:true},{attachment_id:'1',member:'large.txt'},{calls_remaining:1}));
  assert.equal(value.status,'tool_output_limit');assert.equal(value.eof,false);
  assert.equal(value.truncated,true);assert.equal(value.read_scope,null);assert.equal(value.text,undefined);
  assert.match(value.unread.reason,/未提供给模型/);assert.ok(Buffer.byteLength(JSON.stringify(value))<4096);
});

test('initial model attachments retain images and small text but enforce three files and shared preview bound',()=>{
  const image={type:'image',mimeType:'image/png',data:'YWJj'};
  assert.equal(validModelAttachments(undefined),true);
  assert.equal(validModelAttachments([{type:'text',text:'partial preview'},image]),true);
  assert.equal(validModelAttachments([image,image,image,image]),false);
  assert.equal(validModelAttachments([{type:'text',text:'x'.repeat(25001)}]),false);
  assert.equal(validModelAttachments([{type:'text',text:'x'.repeat(20000)},{type:'text',text:'x'.repeat(10000)}]),false);
});

test('initial content cannot inject arbitrary agent blocks, file URLs or non-base64 images',()=>{
  for(const item of [{type:'file',path:'/etc/passwd'},{type:'text',text:'data',role:'system'},
    {type:'image',mimeType:'image/svg+xml',data:'YWJj'},{type:'image',mimeType:'image/png',data:'data:image/png;base64,YWJj'},
    {type:'image',mimeType:'image/png',data:''},null,[]])assert.equal(validModelAttachments([item]),false);
});

test('local diagnostic tool does not accept model supplied machine identity and reports permission/unknown boundaries',()=>{
  assert.equal(localDiagnosticTool.name,'read_local_diagnostic');
  assert.equal(localDiagnosticTool.parameters.additionalProperties,false);
  assert.equal(localDiagnosticTool.parameters.properties.limit.maximum,65536);
  for(const key of ['installation_id','window_id','user_id','url'])assert.equal(localDiagnosticTool.parameters.properties[key],undefined);
  assert.match(localDiagnosticInstructions,/permission\/denied\/timeout\/unavailable\/unknown/);
  assert.match(localDiagnosticInstructions,/在线快照不能当成本机历史记录数/);
  const result=JSON.parse(localDiagnosticToolResult({data:{status:'permission_required',unread:['/outside']}},{operation:'read',path:'/outside'},{calls_remaining:2}));
  assert.equal(result.data.status,'permission_required');assert.deepEqual(result.data.unread,['/outside']);
  assert.equal(result.read_budget.calls_remaining,2);
});

test('local diagnostic oversized response is explicit unread and retains no misleading partial content',()=>{
  const value=JSON.parse(localDiagnosticToolResult({data:{text:'x'.repeat(150000)}},{operation:'read',path:'/log'},{remaining_ms:1000}));
  assert.equal(value.status,'tool_output_limit');assert.equal(value.eof,false);assert.equal(value.data,undefined);
  assert.match(value.unread.reason,/未提供给模型/);
});
