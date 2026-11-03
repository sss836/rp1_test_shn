import {Type} from '@earendil-works/pi-ai';

export const diagnosisTool={name:'prepare_diagnosis',
  description:'主动补齐本任务CSV和原上位机日志证据；按持久进度补查后段和时序。待授权即暂停，不执行文件、修改或设备操作。',
  parameters:Type.Object({},{additionalProperties:false})};

export function needsDiagnosis(body){
  // New trusted backend sends resume even for phase zero. Older bridge callers
  // retain their explicit tool flow; no client-supplied attachment is a grant.
  if(!body.resume)return false;
  const question=body.messages.at(-1)?.body??'';
  const inventory=body.context?.diagnostic_attachment_inventory??[];
  const hasCsv=Array.isArray(inventory)&&inventory.some(a=>typeof a?.name==='string'&&a.name.toLowerCase().endsWith('.csv'));
  return hasCsv||Boolean(body.context?.hmi&&/CSV|缺失|漏采|超时|timeout|断连|连接|报错|异常|故障|排查|日志|diagnos|error|fail/i.test(question))||
    Boolean((body.attachments??[]).some(a=>a.type==='text'&&/文件=[^\n；]*\.csv[；\n]/i.test(a.text)));
}

export function permissionWaiting(card){
  const data=card?.data??{};
  return data.phase==='awaiting_permission'||Boolean(data.pending_permissions?.length);
}

export const diagnosisInstructions=`对本机故障或CSV问题，prepare_diagnosis先主动补齐当前允许读取的原始数据和日志。它的continuation=true表示还有可继续的取证阶段，不能仅把“再看剩余CSV/日志”当最终建议；在剩余只读调用/时间内继续查证。awaiting_permission表示持久暂停，请等待具体权限申请或本机回执，不能声称已读。evidence_complete与根因是否成立是不同问题：解释具体调用链、失败位置和日志中的底层错误；区分已确认事实与推断，缺少时区/采集配置/因果日志时说明具体缺口，不根据同时出现就断言因果。回答用通俗的“发现了什么、为什么、怎么处理、还缺什么”，精确数字和范围附来源。不要展示内部fixture/工具JSON或把示例当现场数据。`;
