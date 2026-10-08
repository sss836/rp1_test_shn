import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="requires isolated HMI_TEST_ADMIN_URL")]
PASSWORD = "Isolated-Presence-Test-2026!"


@pytest.fixture
def environment():
    url = os.environ["HMI_TEST_ADMIN_URL"]
    if not url.rsplit("/", 1)[-1].startswith("rp1_presence"):
        pytest.fail("HMI integration tests require a dedicated rp1_presence database")
    engine = create_engine(url)
    users = {}
    password_hash = PasswordHasher().hash(PASSWORD)
    with engine.begin() as connection:
        for role, key in (("SYSTEM_ADMIN", "admin"), ("TEST_EXECUTOR", "first"), ("TEST_EXECUTOR", "second"), ("VIEWER", "viewer")):
            username = f"presence-{key}-{uuid4().hex[:12]}"
            user_id = connection.execute(text("INSERT INTO iam.app_user(username,display_name,role,principal_kind,must_change_password) VALUES (:name,:name,:role,'HUMAN',false) RETURNING id"), {"name": username, "role": role}).scalar_one()
            connection.execute(text("INSERT INTO iam.user_credential(user_id,password_hash) VALUES (:id,:hash)"), {"id": user_id, "hash": password_hash})
            users[key] = username
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    yield engine, users
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


@contextmanager
def signed_in(username):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = client.cookies["rp1_csrf"]
        yield client


def heartbeat_payload(installation=None):
    return {
        "installation_id": installation or str(uuid4()), "station_name": "Synthetic QA workstation",
        "bench_id": "QA-01", "sequence": 1, "snapshot_age_seconds": 0,
        "runs": [{"session_key": "left", "state": "running", "test_id": "QA-ONLY", "sample_id": "SYNTHETIC", "active_seconds": 123}],
        "plc": {"driver": "mock", "phase": "RUNNING", "stale": False},
    }


def find_station(client, installation):
    response = client.get("/api/v1/hmi/stations?limit=200")
    assert response.status_code == 200, response.text
    return next((item for item in response.json()["data"]["items"] if item["installation_id"] == installation), None)


def test_real_cookie_login_presence_visibility_lifecycle_and_no_test_writes(environment):
    engine, users = environment
    connection_id = str(uuid4())
    payload = heartbeat_payload()
    route = "/api/v1/hmi/presence/" + connection_id
    with engine.connect() as database:
        original_executions = database.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()
    with signed_in(users["first"]) as operator, signed_in(users["second"]) as stranger, signed_in(users["admin"]) as admin:
        sent = operator.put(route, json=payload)
        assert sent.status_code == 200, sent.text
        assert find_station(admin, payload["installation_id"])["state"] == "running"
        assert find_station(operator, payload["installation_id"])["operator_name"] == users["first"]
        assert find_station(stranger, payload["installation_id"]) is None
        assert stranger.put(route, json={**payload, "sequence": 2}).status_code == 409
        assert stranger.delete(route).status_code == 404
        assert operator.put(route, json=payload).status_code == 200
        assert operator.put(route, json={**payload, "sequence": 2, "runs": []}).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "powered"
        assert operator.put(route, json=payload).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "powered"
        with engine.begin() as database:
            database.execute(text("UPDATE integration.hmi_presence SET last_seen_at = now() - interval '46 seconds' WHERE connection_id=:id"), {"id": connection_id})
        assert find_station(admin, payload["installation_id"])["state"] == "offline"
        assert operator.put(route, json={**payload, "sequence": 3}).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "running"
        assert operator.delete(route).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "offline"
        assert operator.put(route, json={**payload, "sequence": 4}).status_code == 409
    with engine.connect() as database:
        assert database.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one() == original_executions


def test_authentication_csrf_viewer_and_disabled_account_fail_closed(environment):
    engine, users = environment
    payload = heartbeat_payload()
    route = "/api/v1/hmi/presence/" + str(uuid4())
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/hmi/stations").status_code == 401
    with signed_in(users["viewer"]) as viewer:
        assert viewer.put(route, json=payload).status_code == 403
    with signed_in(users["first"]) as operator, signed_in(users["admin"]) as admin:
        csrf = operator.headers.pop("X-CSRF-Token")
        assert operator.put(route, json=payload).status_code == 403
        operator.headers["X-CSRF-Token"] = csrf
        assert operator.put(route, json=payload).status_code == 200
        with engine.begin() as database:
            database.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:username"), {"username": users["first"]})
        assert operator.put(route, json={**payload, "sequence": 2}).status_code == 401
        assert find_station(admin, payload["installation_id"])["online"] is False


def test_new_login_supersedes_old_station_session_and_logout_is_immediate(environment):
    _, users = environment
    installation = str(uuid4())
    with signed_in(users["first"]) as first, signed_in(users["second"]) as second, signed_in(users["admin"]) as admin:
        old_route = "/api/v1/hmi/presence/" + str(uuid4())
        new_route = "/api/v1/hmi/presence/" + str(uuid4())
        assert first.put(old_route, json=heartbeat_payload(installation)).status_code == 200
        assert second.put(new_route, json=heartbeat_payload(installation)).status_code == 200
        assert first.put(old_route, json={**heartbeat_payload(installation), "sequence": 2}).status_code == 200
        assert find_station(admin, installation)["operator_name"] == users["second"]
        assert find_station(first, installation) is None
        assert second.post("/api/v1/auth/logout").status_code == 204
        assert find_station(admin, installation)["state"] == "offline"
