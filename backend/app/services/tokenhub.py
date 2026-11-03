"""Reviewed server profile shared byte-for-byte with Pi. Never stores API keys."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from app.core.errors import ApiError

ENDPOINTS = {"guangzhou": "https://tokenhub.tencentmaas.com/v1",
             "singapore": "https://tokenhub-intl.tencentmaas.com/v1"}


def load_profile(path):
    try:
        raw = Path(path).read_bytes()
        p = json.loads(raw)
        assert p['region'] in ENDPOINTS and p['base_url'] == ENDPOINTS[p['region']]
        # An enterprise-issued key may use the standard API without exposing
        # its settlement contract. An explicit owner-approved public-price
        # budget is sufficient; it is never labelled a verified supplier bill.
        public_budget = (p.get('key_type') == 'enterprise_api_key'
                         and p.get('billing_basis') == 'published_peak_upper_bound'
                         and isinstance(p.get('billing_authorization'), str)
                         and bool(p['billing_authorization'].strip()))
        assert (p['key_type'] == 'pay_as_you_go' or public_budget) and p['currency'] == 'CNY'
        assert p['revision'] and p['account_evidence'] and p['price_source'].startswith('https://')
        assert datetime.fromisoformat(p['verified_at']).tzinfo is not None
        assert datetime.fromisoformat(p['valid_until']) > datetime.now(timezone.utc)
        assert p['models'] and p['text_model'] in p['models'] and p.get('vision_model') in (None, *p['models'])
        for name, model in p['models'].items():
            assert isinstance(name, str) and 0 < len(name) <= 200
            assert model['tools'] is True
            assert type(model['vision']) is bool
            assert type(model['max_input_tokens']) is int and 0 < model['max_input_tokens'] <= 2_000_000
            assert type(model['max_output_tokens']) is int and 0 < model['max_output_tokens'] <= 4096
            for key in ('input', 'output', 'cache_hit'):
                amount = Decimal(str(model[key]))
                assert amount.is_finite() and 0 <= amount <= 10000
            assert Decimal(str(model['cache_hit'])) <= Decimal(str(model['input']))
        if p.get('vision_model'):
            assert p['models'][p['vision_model']]['vision'] is True
        return {**p, 'profile_sha256': hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError, KeyError, AssertionError, ArithmeticError) as exc:
        raise ApiError(503, 'assistant_tokenhub_unverified', 'TokenHub账号、模型能力或费率未核验，已停止新增模型调用。') from exc


def rates_for(settings, model=None):
    p = load_profile(settings.assistant_tokenhub_profile_file)
    name = model or p['text_model']
    if name not in {p['text_model'], p.get('vision_model')}:
        raise ApiError(503, 'assistant_model_unverified', '模型未获当前TokenHub配置授权。')
    return {**p['models'][name], 'provider': 'tokenhub', 'model': name,
            **{key: p[key] for key in ('profile_sha256', 'region', 'key_type', 'currency', 'revision', 'price_source')},
            'basis': p.get('billing_basis', 'tokenhub_upper_bound'),
            'supplier_invoice_verified': False,
            **({'billing_authorization': p['billing_authorization']} if p.get('billing_authorization') else {})}
