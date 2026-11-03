"""CNY conservative upper-bound accounting; missing usage fails closed."""
from datetime import datetime,timezone
from decimal import Decimal,ROUND_CEILING
from sqlalchemy import text
from app.core.errors import ApiError
from app.services.setup import canonical

RATES={"input":"2","cache_hit":"0.04","output":"8","currency":"CNY","basis":"peak_upper_bound","verified":"2026-10-22","source":"https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"}
MAX_INPUT=1_000_000
MAX_OUTPUT=4096
RESERVATION=(Decimal(MAX_INPUT)*Decimal(RATES["input"])+Decimal(MAX_OUTPUT)*Decimal(RATES["output"]))/1_000_000


def reliable_prices(settings):
    if settings.assistant_model_provider == 'tokenhub':
        from app.services.tokenhub import rates_for
        rates_for(settings)
        return
    if settings.assistant_model_provider != 'deepseek':
        raise ApiError(503, 'assistant_provider_unverified', '模型供应商尚未核验。')
    try:
        expiry=datetime.fromisoformat(settings.assistant_price_valid_until)
        valid=expiry.tzinfo is not None and expiry>datetime.now(timezone.utc)
    except ValueError: valid=False
    if not valid: raise ApiError(503,"assistant_pricing_unverified","计费配置已过核验期限，已停止新增模型调用，请核对服务端费率。")


def usage_charge(raw,rates=RATES):
    if not isinstance(raw,dict): return None
    if rates.get('provider') == 'tokenhub':
        fields = [raw.get(k) for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')]
        if any(type(v) is not int or v < 0 for v in fields): return None
        prompt, output, total = fields
        if prompt <= 0 or prompt > rates['max_input_tokens'] or output > rates['max_output_tokens'] or prompt + output != total: return None
        # Cache data is optional. Without reliable details charge all input at
        # the reviewed uncached rate, a documented conservative upper bound.
        details = raw.get('prompt_tokens_details', {})
        hit = details.get('cached_tokens', 0) if isinstance(details, dict) else None
        if type(hit) is not int or not 0 <= hit <= prompt: return None
        amount = (Decimal(prompt-hit)*Decimal(str(rates['input'])) + Decimal(hit)*Decimal(str(rates['cache_hit'])) + Decimal(output)*Decimal(str(rates['output'])))/1_000_000
        return amount.quantize(Decimal('0.00000001'), rounding=ROUND_CEILING)
    fields=[raw.get(k) for k in ("prompt_tokens","completion_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens","total_tokens")]
    if any(type(v) is not int or v<0 for v in fields): return None
    prompt,output,hit,miss,total=fields
    if prompt<=0 or prompt>MAX_INPUT or output>MAX_OUTPUT or hit+miss!=prompt or prompt+output!=total: return None
    amount=(Decimal(hit)*Decimal(rates["cache_hit"])+Decimal(miss)*Decimal(rates["input"])+Decimal(output)*Decimal(rates["output"]))/1_000_000
    return amount.quantize(Decimal('0.00000001'),rounding=ROUND_CEILING)


def reserve(session,job,sequence,settings,model=None):
    reliable_prices(settings)
    rates = RATES
    max_input, max_output, amount = MAX_INPUT, MAX_OUTPUT, RESERVATION
    if settings.assistant_model_provider == 'tokenhub':
        from app.services.tokenhub import rates_for
        rates = rates_for(settings, model)
        max_input, max_output = rates['max_input_tokens'], rates['max_output_tokens']
        amount = (Decimal(max_input)*Decimal(str(rates['input'])) + Decimal(max_output)*Decimal(str(rates['output'])))/1_000_000
        amount = max(Decimal('0.00000001'), amount.quantize(Decimal('0.00000001'), rounding=ROUND_CEILING))
    session.execute(text("SELECT integration.assistant_reserve(:job,:seq,:amount,CAST(:rates AS jsonb))"),{"job":job,"seq":sequence,"amount":amount,"rates":canonical(rates)})
    return {"reserved_cny":str(amount),"max_input_tokens":max_input,"max_output_tokens":max_output,
            **({'profile_sha256': rates['profile_sha256']} if 'profile_sha256' in rates else {})}


def settle(session,job,sequence,usage):
    rates=session.execute(text("SELECT rates FROM integration.assistant_model_call WHERE job_id=:job AND sequence=:seq"),{"job":job,"seq":sequence}).scalar_one()
    amount=usage_charge(usage,rates)
    session.execute(text("SELECT integration.assistant_settle(:job,:seq,:amount,CAST(:usage AS jsonb))"),{"job":job,"seq":sequence,"amount":amount,"usage":canonical(usage) if amount is not None else None})
    return {"accounted":amount is not None,"charged_upper_bound_cny":str(amount) if amount is not None else None}


def budget(session):
    row=session.execute(text("""SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date AS day,
     coalesce(b.spent,0) AS spent_cny,coalesce(b.reserved,0) AS reserved_cny,f.blocked,f.reason
     FROM integration.assistant_finance f LEFT JOIN integration.assistant_budget_day b ON b.day=(now() AT TIME ZONE 'Asia/Shanghai')::date WHERE f.singleton""")).mappings().one()
    return {**dict(row),"limit_cny":100,"basis":"按已核验高峰费率和实际Token用量计算的保守上界，不是供应商账单","reset_timezone":"Asia/Shanghai"}
