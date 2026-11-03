import { Type } from "@earendil-works/pi-ai";

export const localDiagnosticInstructions = `read_local_diagnostic是独立的本机只读诊断工具。只读取当前任务原始HMI上下文配对的实例、窗口和登录账号，机器身份由后端确定，不能由模型指定机器ID、其他账号或任意回调URL。网页任务没有本机配对上下文时应明确“本次无法读取本机”，不能假装读过。
先用capabilities核对可读范围；list/read/search读取受授权路径的普通文件，不执行内容、shell或设备命令。用户授权本文件/本目录/本次可读范围后仍受系统实际权限限制，不自动提权。permission/denied/timeout/unavailable/unknown以及未读范围必须如实说明；预算不足或尚待用户授权时先答已核实部分，不能把申请授权写成已读成功。
execution_summary才用于本机历史记录数、判定分布和失败原因统计，查询实时工位在线快照不能当成本机历史记录数。原始事实只读，本机统计不自动归档、审批或进入平台正式统计。引用路径、SHA256、来源版本和观察时间，区分完整读取、分块、跳过和未知；连接在线不代表资料已读或当前设备安全。`;

export const localDiagnosticTool={
  name:"read_local_diagnostic",
  description:"只读当前任务配对的本机：capabilities查询授权范围，list列目录，read按字节分块，search字面关键词，execution_summary查本机历史。无本机上下文/无权限/超时须说明，不是设备操作。",
  parameters:Type.Object({
    operation:Type.Union([Type.Literal("capabilities"),Type.Literal("list"),Type.Literal("read"),Type.Literal("search"),Type.Literal("execution_summary"),Type.Literal("discover"),Type.Literal("csv_diagnosis"),Type.Literal("log_diagnosis")]),
    path:Type.Optional(Type.Union([Type.String({maxLength:4096}),Type.Null()])),
    offset:Type.Optional(Type.Integer({minimum:0,maximum:Number.MAX_SAFE_INTEGER})),
    limit:Type.Optional(Type.Integer({minimum:1,maximum:65536})),
    query:Type.Optional(Type.Union([Type.String({maxLength:512}),Type.Null()])),
  },{additionalProperties:false}),
};

export function localDiagnosticToolResult(data,args,budget) {
  const text=JSON.stringify({...data,read_budget:budget});
  if(Buffer.byteLength(text,"utf8")<=128*1024)return text;
  return JSON.stringify({source:"paired_hmi_live_read",operation:args.operation,path:args.path??null,
    status:"tool_output_limit",eof:false,truncated:true,read_scope:null,
    unread:{reason:"本机诊断结果超过128KiB，本次内容未提供给模型；请缩小limit或路径范围补读，不得声称已读。"},
    read_budget:budget});
}
