from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import app.api.routes.assistant as routes
import app.core.auth as auth
import app.core.db as db
import app.services.assistant as bridge
from app.core.config import Settings
from app.core.errors import ApiError
from app.schemas.security import Principal
from app.services.auth import AuthService


def actor():
    return auth.RequestActor(uuid4(), "test", "", "127.0.0.1", "synthetic", "synthetic", "TEST_EXECUTOR", "HUMAN", uuid4(), "BROWSER", datetime.now(timezone.utc) + timedelta(hours=1), False)


class FakeSession:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def commit(self): pass


def test_isolation_disabled_production_and_development_identity_fail_closed():
    for settings, code in [
        (Settings(_env_file=None, assistant_enabled=False), "assistant_disabled"),
        (Settings(_env_file=None, app_env="production", service_api_key_hmac_secret="x" * 48, assistant_enabled=True), "assistant_isolation_only"),
        (Settings(_env_file=None, assistant_enabled=True, auth_mode="development"), "assistant_session_required"),
    ]:
        with pytest.raises(ApiError) as error:
            bridge.require_isolation(settings)
        assert error.value.code == code


def test_production_requires_explicit_mode_metered_runtime_and_pilot_list():
    good=Settings(_env_file=None,app_env='production',service_api_key_hmac_secret='x'*48,assistant_enabled=True,assistant_production_enabled=True,assistant_runtime_enabled=True,assistant_legacy_probes=False,auth_cookie_secure=True,assistant_pilot_user_ids=[uuid4()])
    bridge.require_isolation(good)
    for update in ({'assistant_legacy_probes':True},{'assistant_runtime_enabled':False},{'assistant_pilot_user_ids':[]},{'auth_cookie_secure':False}):
        with pytest.raises(ApiError):bridge.require_isolation(good.model_copy(update=update))


def test_grant_scope_one_use_revocation_expiry_and_capacity(monkeypatch):
    registry = bridge.GrantRegistry()
    who = actor()
    first, _ = registry.issue("private-browser-cookie", who)
    second, _ = registry.issue("second-cookie", who)
    with pytest.raises(ApiError) as error: registry.issue("third-cookie", who)
    assert error.value.status_code == 429
    claimed = registry.claim(first)
    assert "private-browser-cookie" not in repr(claimed)
    with pytest.raises(ApiError): registry.claim(first)
    registry.revoke(second)
    with pytest.raises(ApiError): registry.claim(second)
    expired, grant = registry.issue("cookie", who)
    grant.expires = 0
    with pytest.raises(ApiError): registry.claim(expired)
    with pytest.raises(ApiError): registry.claim("forged")


@pytest.mark.parametrize("revoked", [True, False])
def test_callback_rechecks_original_session_and_current_pilot_rights(monkeypatch, revoked):
    who = actor()
    settings = Settings(_env_file=None, assistant_enabled=True, assistant_pilot_user_ids=[who.public_id])
    registry = bridge.GrantRegistry()
    token, _ = registry.issue("cookie", who)
    monkeypatch.setattr(bridge, "grants", registry)
    monkeypatch.setattr(bridge, "SessionLocal", FakeSession)
    monkeypatch.setattr(AuthService, "resolve", lambda *_: None if revoked else SimpleNamespace(id=who.public_id, session_id=who.session_public_id, must_change_password=False, role="VIEWER", kind="HUMAN"))
    with pytest.raises(ApiError) as error: bridge.read_stations(token, settings)
    assert error.value.status_code == (401 if revoked else 403)


def test_same_origin_entry_reuses_cookie_csrf_and_rejects_identity_headers(monkeypatch):
    who = actor()
    settings = Settings(_env_file=None, assistant_enabled=True, assistant_pilot_user_ids=[who.public_id])
    principal = Principal(id=who.public_id, username="synthetic", display_name="synthetic", role="TEST_EXECUTOR", kind="HUMAN", session_id=who.session_public_id, session_type="BROWSER", expires_at=who.expires_at, must_change_password=False, csrf_token_hash=AuthService.secret_hash("csrf"))
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    monkeypatch.setattr(db, "SessionLocal", FakeSession)
    monkeypatch.setattr(AuthService, "resolve", lambda *_: principal)
    app = FastAPI()
    app.include_router(routes.router)
    @app.exception_handler(ApiError)
    async def error_handler(_: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})
    with TestClient(app) as client:
        assert client.get("/api/v1/assistant/capabilities", headers={"X-User-Public-Id": str(who.public_id)}).status_code == 401
        client.cookies.set("rp1_session", "cookie")
        assert client.get("/api/v1/assistant/capabilities").status_code == 200
        assert client.post("/api/v1/assistant/probe", json={}).json()["code"] == "csrf_invalid"
        assert client.get("/api/v1/assistant/capabilities", headers={"Authorization": "Bearer service"}).status_code == 403
        client.cookies.set("rp1_csrf", "csrf")
        client.headers["X-CSRF-Token"] = "csrf"
        assert client.post("/api/v1/assistant/probe", json={"user_id": str(who.public_id)}).status_code == 422
        assert client.post("/api/v1/assistant/probe", json={}).json()["code"] == "assistant_not_configured"
