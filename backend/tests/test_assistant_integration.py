"""Real HTTP: nginx -> session/CSRF -> Pi -> SSE model fixture -> RLS tool.

Only the standalone isolated compose runner supplies these URLs and users.
"""
import os
import json
from pathlib import Path
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("ASSISTANT_QA_ADMIN_URL"), reason="standalone assistant isolation stack required")]
PASSWORD = "Synthetic-Assistant-Only-2026!"


@pytest.fixture(scope="module")
def environment():
    url = os.environ["ASSISTANT_QA_ADMIN_URL"]
    assert url.rsplit("/", 1)[-1] == "rp1_presence_assistant", "refuse any other database"
    assert os.environ["ASSISTANT_QA_HTTP_URL"] == "http://web:8080", "refuse non-isolated HTTP target"
    engine = create_engine(url)
    identifiers = os.environ["ASSISTANT_QA_PILOT_IDS"].split(",")
    users = {}
    with engine.begin() as connection:
        for index, (key, role) in enumerate((("admin", "SYSTEM_ADMIN"), ("first", "TEST_EXECUTOR"), ("second", "TEST_EXECUTOR"), ("viewer", "VIEWER"))):
            username = "assistant-qa-" + key
            user_id = connection.execute(text("INSERT INTO iam.app_user(public_id,username,display_name,role,principal_kind,must_change_password) VALUES (:public_id,:name,:name,:role,'HUMAN',false) RETURNING id"), {"public_id": identifiers[index], "name": username, "role": role}).scalar_one()
            connection.execute(text("INSERT INTO iam.user_credential(user_id,password_hash) VALUES (:id,:hash)"), {"id": user_id, "hash": PasswordHasher().hash(PASSWORD)})
            users[key] = username
    yield engine, users
    engine.dispose()


@contextmanager
def signed_in(username):
    with httpx.Client(base_url=os.environ["ASSISTANT_QA_HTTP_URL"], timeout=30, trust_env=False) as client:
        response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200, response.status_code
        client.headers["X-CSRF-Token"] = client.cookies["rp1_csrf"]
        yield client


@pytest.fixture(autouse=True)
def normal_model():
    with httpx.Client(trust_env=False) as client:
        assert client.post("http://model-mock:3100/control", json={"mode": "normal"}).status_code == 200


def probe(client):
    return client.post("/api/v1/assistant/probe", json={})


def heartbeat(client, label):
    response = client.put("/api/v1/hmi/presence/" + str(uuid4()), json={
        "installation_id": str(uuid4()), "station_name": label,
        "sequence": 1, "snapshot_age_seconds": 0,
        "plc": {"driver": "mock", "phase": "IDLE", "stale": False}, "runs": [],
    })
    assert response.status_code == 200, response.status_code


def test_full_same_origin_pi_sse_tool_round_trip_and_actual_user_scope(environment):
    engine, users = environment
    with signed_in(users["first"]) as first, signed_in(users["second"]) as second, signed_in(users["admin"]) as admin:
        heartbeat(first, "SYNTHETIC-FIRST")
        heartbeat(second, "SYNTHETIC-SECOND")
        for client, expected in ((first, {"SYNTHETIC-FIRST"}), (second, {"SYNTHETIC-SECOND"}), (admin, {"SYNTHETIC-FIRST", "SYNTHETIC-SECOND"})):
            response = probe(client)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["model"] == "deepseek-flash"
            assert data["verification_mode"] == "mock"
            evidence = data["evidence"][0]
            assert {item["station_name"] for item in evidence["items"]} == expected
            assert evidence["source"] == "/api/v1/hmi/stations" and evidence["as_of_at"]
            assert all(label in data["reply"] for label in expected)
            assert first.cookies["rp1_session"] not in response.text
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM integration.hmi_presence")).scalar_one() == 2
    stats = httpx.get("http://model-mock:3100/stats", trust_env=False).json()["requests"]
    assert len(stats) == 6
    for request in stats:
        assert request["model"] == "deepseek-flash"
        assert request["thinking"] == {"type": "disabled"}
        assert request["max_tokens"] == 512
        assert request["tool_names"] == ["list_hmi_stations"]
        assert request["tool_parameters"][0]["additionalProperties"] is False
        assert not request["contains_rp1_credentials"]


def test_login_csrf_viewer_forged_user_and_private_routes(environment):
    _, users = environment
    with httpx.Client(base_url=os.environ["ASSISTANT_QA_HTTP_URL"], trust_env=False) as anonymous:
        assert probe(anonymous).status_code == 401
        assert anonymous.get("/api/v1/assistant/capabilities", headers={"X-User-Public-Id": os.environ["ASSISTANT_QA_PILOT_IDS"].split(",")[0]}).status_code == 401
        assert anonymous.post("/api/v1/assistant/tools/list_hmi_stations", json={}).status_code == 404
    with signed_in(users["viewer"]) as viewer, signed_in(users["first"]) as first:
        assert probe(viewer).status_code == 403
        csrf = first.headers.pop("X-CSRF-Token")
        assert probe(first).status_code == 403
        first.headers["X-CSRF-Token"] = csrf
        assert first.post("/api/v1/assistant/probe", json={"user_id": "forged"}).status_code == 422
        assert first.get("/api/v1/assistant/capabilities", headers={"Authorization": "Bearer service"}).status_code == 403
    with httpx.Client(trust_env=False) as internal:
        assert internal.post("http://api:8000/api/v1/assistant/tools/list_hmi_stations", headers={"X-RP1-Tool-Grant": "f" * 64}, json={}).status_code == 403
        assert internal.post("http://pi:3000/internal/probe", json={"tool_grant": "f" * 64}).status_code == 403
        assert internal.post("http://api:8000/api/v1/assistant/tools/execute_sql", json={}).status_code == 404


def test_disabled_account_and_logged_out_session_rejected(environment):
    engine, users = environment
    with signed_in(users["first"]) as first:
        with engine.begin() as connection:
            connection.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:name"), {"name": users["first"]})
        try: assert probe(first).status_code == 401
        finally:
            with engine.begin() as connection:
                connection.execute(text("UPDATE iam.app_user SET enabled=true WHERE username=:name"), {"name": users["first"]})
    with signed_in(users["first"]) as first:
        cookie = first.cookies["rp1_session"]
        assert first.post("/api/v1/auth/logout").status_code == 204
        first.cookies.set("rp1_session", cookie)
        assert probe(first).status_code == 401


def test_callback_rechecks_revocation_during_model_wait_without_holding_transaction(environment):
    engine, users = environment
    httpx.post("http://model-mock:3100/control", json={"mode": "pause_before_tool"}, trust_env=False)
    with signed_in(users["first"]) as first, ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(probe, first)
        deadline = time.monotonic() + 5
        while not httpx.get("http://model-mock:3100/stats", trust_env=False).json()["requests"]:
            assert time.monotonic() < deadline
            time.sleep(.02)
        with engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='rp1_app' AND state='idle in transaction'")).scalar_one() == 0
            connection.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:name"), {"name": users["first"]})
        try:
            assert future.result(timeout=8).status_code == 502
        finally:
            with engine.begin() as connection:
                connection.execute(text("UPDATE iam.app_user SET enabled=true WHERE username=:name"), {"name": users["first"]})


@pytest.mark.parametrize("mode", ["limited", "disconnect", "unknown_tool", "extra_args", "repeat", "no_tool", "slow"])
def test_failures_are_bounded_redacted_and_do_not_break_business(environment, mode):
    _, users = environment
    httpx.post("http://model-mock:3100/control", json={"mode": mode}, trust_env=False)
    with signed_in(users["admin"]) as admin:
        response = probe(admin)
        assert response.status_code in {502, 504}, (mode, response.text)
        assert "synthetic sensitive" not in response.text
        assert admin.get("/readyz").status_code == 200
        assert admin.get("/api/v1/setup/catalog").status_code == 200
        assert admin.get("/api/v1/hmi/stations").status_code == 200
        assert len(httpx.get("http://model-mock:3100/stats", trust_env=False).json()["requests"]) <= 2
        httpx.post("http://model-mock:3100/control", json={"mode": "normal"}, trust_env=False)
        assert probe(admin).status_code == 200


@pytest.mark.skipif(os.getenv("ASSISTANT_QA_LIVE") != "1", reason="requires explicit server key file and live isolation override")
def test_live_official_probe(environment):
    engine, users = environment
    with signed_in(users["first"]) as operator:
        heartbeat(operator, "SYNTHETIC-LIVE-PROBE")
        response = probe(operator)
        assert response.status_code == 200, response.status_code
        data = response.json()["data"]
        assert data["verification_mode"] == "live"
        assert data["model"] == "deepseek-flash"
        assert [item["station_name"] for item in data["evidence"][0]["items"]] == ["SYNTHETIC-LIVE-PROBE"]
        Path("/reports/live-receipt.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one() == 0
