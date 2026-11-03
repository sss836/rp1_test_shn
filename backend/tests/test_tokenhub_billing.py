import json
from decimal import Decimal
from types import SimpleNamespace
from pathlib import Path
import pytest
from app.services.tokenhub import load_profile, rates_for
from app.services.assistant_billing import usage_charge, reliable_prices
from app.core.errors import ApiError

ROOT = Path('/workspace') if Path('/workspace/assistant/tests/fixtures/tokenhub-synthetic.json').is_file() else Path(__file__).resolve().parents[2]
PROFILE = ROOT/'assistant/tests/fixtures/tokenhub-synthetic.json'


def test_tokenhub_usage_cache_missing_inconsistent_and_bounds():
    rates = dict(provider='tokenhub', input='4', cache_hit='2', output='12', max_input_tokens=64000, max_output_tokens=4096)
    usage = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120)
    assert usage_charge(usage, rates) == Decimal('0.00064000')
    assert usage_charge({**usage, 'prompt_tokens_details': {'cached_tokens': 40}}, rates) == Decimal('0.00056000')
    for change in ({'total_tokens': 121}, {'prompt_tokens': True}, {'prompt_tokens_details': {'cached_tokens': 101}}, {'completion_tokens': 4097}):
        assert usage_charge({**usage, **change}, rates) is None
    assert usage_charge(None, rates) is None


def test_profile_fails_closed_without_account_and_price_evidence(tmp_path):
    original = json.loads(PROFILE.read_text())
    path = tmp_path/'profile.json'
    path.write_text(json.dumps(original))
    p = load_profile(path)
    assert len(p['profile_sha256']) == 64
    settings = SimpleNamespace(assistant_model_provider='tokenhub', assistant_tokenhub_profile_file=str(path))
    assert rates_for(settings, 'fixture-vision')['model'] == 'fixture-vision'
    reliable_prices(settings)
    with pytest.raises(ApiError): rates_for(settings, 'unauthorized-model')
    for change in ({'region': 'unknown'}, {'key_type': 'token_plan'}, {'valid_until': '2000-01-01T00:00:00Z'}, {'account_evidence': ''}):
        path.write_text(json.dumps({**original, **change}))
        with pytest.raises(ApiError): reliable_prices(settings)


def test_production_timeout_default_and_ceiling_are_120_seconds(monkeypatch):
    from app.core.config import Settings
    from pydantic import ValidationError
    monkeypatch.delenv('ASSISTANT_TASK_SECONDS', raising=False)
    assert Settings(_env_file=None).assistant_task_seconds == 120
    with pytest.raises(ValidationError): Settings(_env_file=None, assistant_task_seconds=121)


def test_enterprise_key_uses_explicit_public_budget_without_contract_gate(tmp_path):
    profile = {**json.loads(PROFILE.read_text()), 'key_type': 'enterprise_api_key',
               'billing_basis': 'published_peak_upper_bound',
               'billing_authorization': 'synthetic owner instruction: use public rates, no enterprise bill gate'}
    path = tmp_path/'profile.json'
    path.write_text(json.dumps(profile))
    settings = SimpleNamespace(assistant_model_provider='tokenhub', assistant_tokenhub_profile_file=str(path))
    rates = rates_for(settings)
    assert rates['basis'] == 'published_peak_upper_bound' and rates['supplier_invoice_verified'] is False
    reliable_prices(settings)
    for change in ({'billing_authorization': ''}, {'billing_basis': 'unmetered'}, {'key_type': 'token_plan'}, {'valid_until': '2000-01-01T00:00:00Z'}):
        path.write_text(json.dumps({**profile, **change}))
        with pytest.raises(ApiError): reliable_prices(settings)
