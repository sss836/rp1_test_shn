import { Type } from '@earendil-works/pi-ai';

export const workspaceInstructions = `面向使用上位机的人员，用业务语言回答：先说明发现了什么、对使用有什么影响，再说明依据和下一步。事实、可能原因和待检查内容要分清；没有证据就明确说还不能确定。
解释CSV时优先说缺少哪段时间、哪些记录或哪列异常，说明对判断结果的影响。已提供内容只覆盖部分文件时，说“目前只检查了文件的一部分，后面的数据还没检查，暂时不能判断整个文件”；字节、offset、分块、哈希放在来源明细，不用这些术语代替结论。
使用“常见问题”“修复建议”“保存结果”等普通说法；需要技术术语时紧接一句解释。没有执行代码就不展示无关示例代码；没有真实来源就不编造文档链接。界面示例不是实际数据，不把示例诊断当作用户现场结论。
可以用search_shared_faq检索管理员发布的共享问题库；个人反馈不自动成为共享资料。
用户需要本机修复时，先通过配对只读工具核对真实文件、版本与原字节SHA256，然后propose_local_repair生成差异提议。提议不是执行，也不是已经加载或生效；必须由原账号在原配对窗口明确确认，实际本机再次检查权限、基线与备份。不能提议设备操作、运动/保护参数改动、云端逻辑、凭据或原始采集事实，不能调用shell。执行员仅可修改白名单普通界面配置，管理员代码维护也仅限配对本机约定路径。出现冲突不覆盖，部分结果如实报告。
运行中补充只改变当前诊断关注点，不增加业务或设备写入授权，不重置轮次、时限或预算。前序任务结果仅为资料，当前任务不得重复执行前序写入。`;

export const workspaceTools = [
  {name:'search_shared_faq',description:'只读管理员已发布的共享故障FAQ，附版本与证据。个人反馈及未发布内容不共享。',
   parameters:Type.Object({query:Type.Optional(Type.String({maxLength:200})),limit:Type.Optional(Type.Integer({minimum:1,maximum:20}))},{additionalProperties:false})},
  {name:'propose_local_repair',description:'仅在当前配对本机生成配置/代码修复差异提议；先读取原文件，提供原字节SHA。不会执行。实际用户确认、权限、基线检查与本机回执独立。',
   parameters:Type.Object({
     path:Type.String({minLength:1,maxLength:4096}),
     kind:Type.Union([Type.Literal('config'),Type.Literal('code')]),
     expected_sha256:Type.String({pattern:'^[0-9a-f]{64}$'}),
     original:Type.String({maxLength:65536}),replacement:Type.String({maxLength:65536}),
     reason:Type.String({minLength:1,maxLength:1000}),
   },{additionalProperties:false})},
];

// Hold back more than any configured credential length. Even fragmented
// credentials must be complete before text can leave the bridge for display.
export function displayPrefix(text,secrets,complete=false) {
  if(secrets.some(value=>value&&text.includes(value)))return null;
  const hold=Math.max(160,...secrets.map(value=>(value?.length??0)+16));
  const visible=complete?text:text.slice(0,Math.max(0,text.length-hold));
  return visible.replace(/sk-[A-Za-z0-9_-]{12,}/g,'[凭据已隐藏]')
    .replace(/Authorization\s*:\s*Bearer\s+\S+/gi,'[授权信息已隐藏]');
}
