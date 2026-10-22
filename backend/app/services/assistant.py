"""Short lived, single-worker grants for the isolated Pi connection probe.

The browser cookie stays in this process. Pi receives a random capability for
one fixed read; every use re-resolves the original browser session in PostgreSQL.
No transaction is held while waiting for Pi or the external model.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text

from app.core.auth import RequestActor
from app.core.config import Settings
from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.repositories.hmi_presence import HmiPresenceRepository
from app.repositories.security import SecurityRepository
from app.services.auth import AuthService

logger = logging.getLogger("rp1.assistant")
TOOL_NAME = "list_hmi_stations"


def require_isolation(settings: Settings) -> None:
    if not settings.assistant_enabled:
        raise ApiError(404, "assistant_disabled", "助手尚未启用。")
    if settings.app_env.lower() not in {"test", "testing", "development", "dev"}:
        if not settings.assistant_production_enabled:
            raise ApiError(503, "assistant_isolation_only", "助手站点运行模式尚未启用。")
        if not settings.assistant_runtime_enabled or settings.assistant_legacy_probes or not settings.assistant_pilot_user_ids or not settings.secure_auth_cookie:
            raise ApiError(503, "assistant_production_config", "助手站点配置不完整：需要任务计费、指定试点账号、安全Cookie并关闭旧探针。")
    if settings.auth_mode != "session":
        raise ApiError(503, "assistant_session_required", "助手需要真实账号会话。")


def require_pilot(settings: Settings, user_id: UUID, role: str, kind: str) -> None:
    require_isolation(settings)
    if kind != "HUMAN" or role not in {"SYSTEM_ADMIN", "TEST_EXECUTOR"} or user_id not in settings.assistant_pilot_user_ids:
        raise ApiError(403, "assistant_pilot_required", "当前账号未获助手试点授权。")


def service_secret(settings: Settings) -> str:
    try:
        value = Path(settings.assistant_service_secret_file or "").read_text().strip()
        if len(value) >= 32:
            return value
    except (OSError, ValueError):
        pass
    raise ApiError(503, "assistant_not_configured", "助手服务端连接尚未配置。")


@dataclass
class ToolGrant:
    cookie: str = field(repr=False)
    user_id: UUID
    session_id: UUID
    request_id: str
    source_ip: str | None
    expires: float
    scope: str = "probe"
    calls: int = 0
    evidence: list[dict] = field(default_factory=list)


class GrantRegistry:
    """Intentionally local: run the M2 API with one worker, no durable tasks."""

    def __init__(self):
        self._items: dict[str, ToolGrant] = {}
        self._lock = RLock()

    @staticmethod
    def key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, cookie: str, actor: RequestActor, scope: str = "probe", ttl: int = 30) -> tuple[str, ToolGrant]:
        if not cookie or actor.session_public_id is None or actor.session_type != "BROWSER":
            raise ApiError(403, "assistant_session_required", "助手需要真实浏览器登录会话。")
        with self._lock:
            now = time.monotonic()
            self._items = {key: grant for key, grant in self._items.items() if grant.expires > now}
            if len(self._items) >= 2:
                raise ApiError(429, "assistant_probe_busy", "隔离验证并发已满，请稍后重试。")
            token = secrets.token_urlsafe(48)
            grant = ToolGrant(cookie, actor.public_id, actor.session_public_id, str(uuid4()), actor.source_ip, now + min(ttl,120))
            grant.scope = scope
            self._items[self.key(token)] = grant
            return token, grant

    def claim(self, token: str, scope: str = "probe") -> ToolGrant:
        with self._lock:
            grant = self._items.get(self.key(token))
            if grant is None or grant.scope != scope or grant.expires <= time.monotonic() or grant.calls >= 1:
                raise ApiError(403, "assistant_grant_invalid", "工具授权无效、已使用或已到期。")
            grant.calls += 1
            return grant

    def revoke(self, token: str) -> None:
        with self._lock:
            self._items.pop(self.key(token), None)

    def access(self, token: str) -> ToolGrant:
        with self._lock:
            grant=self._items.get(self.key(token))
            if grant is None or grant.expires<=time.monotonic():
                raise ApiError(403,"assistant_grant_invalid","任务授权失效或已过期。")
            return grant


grants = GrantRegistry()


def read_stations(token: str, settings: Settings) -> dict:
    require_isolation(settings)
    grant = grants.claim(token)
    with SessionLocal() as session:
        principal = AuthService(settings, SecurityRepository(session)).resolve(grant.cookie)
        if principal is None or principal.id != grant.user_id or principal.session_id != grant.session_id or principal.must_change_password:
            raise ApiError(401, "authentication_required", "发起账号会话已失效。")
        require_pilot(settings, principal.id, principal.role, principal.kind)
        session.execute(text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"), {
            "user_id": str(principal.id), "request_id": grant.request_id, "reason": "assistant isolated read probe",
        })
        session.execute(text("SELECT set_config('app.source_ip', coalesce(:source_ip, ''), true)"), {"source_ip": grant.source_ip})
        result = HmiPresenceRepository(session).list_stations(cursor=None, limit=20)
        # Use a fixed projection: no cookie, grant, internal URLs or credentials.
        evidence = {
            "source": "/api/v1/hmi/stations", "as_of_at": result.as_of_at.isoformat(),
            "next_cursor": str(result.next_cursor) if result.next_cursor else None,
            "items": [{
                "installation_id": str(item.installation_id), "station_name": item.station_name,
                "state": item.state, "online": item.online,
                "last_seen_at": item.last_seen_at.isoformat(),
            } for item in result.items],
        }
        session.commit()
    grant.evidence.append(evidence)
    logger.info("assistant_tool request_id=%s actor=%s tool=%s result=ok", grant.request_id, grant.user_id, TOOL_NAME)
    return evidence


async def run_probe(cookie: str, actor: RequestActor, settings: Settings) -> dict:
    require_pilot(settings, actor.public_id, actor.role, actor.principal_kind)
    secret = service_secret(settings)
    token, grant = grants.issue(cookie, actor)
    try:
        async with httpx.AsyncClient(timeout=25, trust_env=False, follow_redirects=False) as client:
            response = await client.post(settings.assistant_pi_url.rstrip("/") + "/internal/probe", json={"tool_grant": token}, headers={"X-RP1-Bridge-Secret": secret})
        if response.status_code != 200:
            raise ApiError(502, "assistant_upstream_failed", "Pi或模型调用失败，请核对服务端配置和隔离日志。")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("model_mode") not in {"live", "mock"} or not isinstance(payload.get("reply"), str) or not payload["reply"] or len(payload["reply"]) > 16000 or len(grant.evidence) != 1:
            raise ValueError("invalid probe receipt")
        # Defense in depth: these values must never be returned by the model.
        if any(value and value in payload["reply"] for value in (token, cookie, secret)):
            raise ValueError("credential in upstream response")
        return {"request_id": grant.request_id, "reply": payload["reply"], "model": "deepseek-flash", "verification_mode": payload["model_mode"], "evidence": grant.evidence}
    except httpx.TimeoutException as exc:
        raise ApiError(504, "assistant_timeout", "隔离链路超时；本工具仅查询，可重新发起验证。") from exc
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise ApiError(502, "assistant_upstream_failed", "隔离链路返回无效回执。") from exc
    finally:
        grants.revoke(token)
        logger.info("assistant_probe_finished request_id=%s actor=%s tool_receipts=%s", grant.request_id, grant.user_id, len(grant.evidence))
