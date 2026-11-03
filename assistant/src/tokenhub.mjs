import {readFileSync} from 'node:fs';
import {createHash} from 'node:crypto';

export function loadTokenHub(path) {
  const raw=readFileSync(path),p=JSON.parse(raw);
  const urls={guangzhou:'https://tokenhub.tencentmaas.com/v1',singapore:'https://tokenhub-intl.tencentmaas.com/v1'};
  const publicBudget=p.key_type==='enterprise_api_key'&&p.billing_basis==='published_peak_upper_bound'
    &&typeof p.billing_authorization==='string'&&p.billing_authorization.trim();
  const valid=urls[p.region]===p.base_url&&(p.key_type==='pay_as_you_go'||publicBudget)&&p.currency==='CNY'
    &&p.revision&&p.account_evidence&&p.price_source?.startsWith('https://')
    &&Number.isFinite(Date.parse(p.verified_at))&&Date.parse(p.valid_until)>Date.now()
    &&p.models?.[p.text_model]&&(!p.vision_model||p.models[p.vision_model]?.vision===true);
  if(!valid)throw new Error('TokenHub profile unverified');
  for(const [name,m] of Object.entries(p.models)){
    if(!name||name.length>200||m.tools!==true||typeof m.vision!=='boolean'
      ||!Number.isInteger(m.max_input_tokens)||m.max_input_tokens<=0||m.max_input_tokens>2000000
      ||!Number.isInteger(m.max_output_tokens)||m.max_output_tokens<=0||m.max_output_tokens>4096
      ||['input','output','cache_hit'].some(k=>!Number.isFinite(Number(m[k]))||Number(m[k])<0||Number(m[k])>10000)
      ||Number(m.cache_hit)>Number(m.input))throw new Error('TokenHub model unverified');
  }
  return {...p,profile_sha256:createHash('sha256').update(raw).digest('hex')};
}

export function tokenHubModel(profile,hasImage) {
  const id=hasImage?profile.vision_model:profile.text_model,m=profile.models[id];
  if(!m||hasImage&&!m.vision)throw new Error('TokenHub vision model not enabled');
  return {id,name:id,provider:'tokenhub',api:'openai-completions',baseUrl:profile.base_url,
    reasoning:false,input:m.vision?['text','image']:['text'],
    cost:{input:Number(m.input),output:Number(m.output),cacheRead:Number(m.cache_hit),cacheWrite:Number(m.input)},
    contextWindow:m.max_input_tokens,maxTokens:m.max_output_tokens,
    compat:{supportsDeveloperRole:false,supportsStore:false,supportsReasoningEffort:false,
      supportsUsageInStreaming:true,supportsStrictMode:false,maxTokensField:'max_tokens'}};
}
