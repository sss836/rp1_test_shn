"""Read-only bench overview contract against a disposable PostgreSQL database."""
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.main import app
from test_hmi_presence_integration import environment, heartbeat_payload, signed_in


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated database required")]


def test_registry_includes_unreported_and_disabled_parents_with_bounded_pagination(environment):
    engine, users = environment
    suffix = uuid4().hex[:10]
    created = []
    with signed_in(users["admin"]) as admin:
        for index in range(4):
            response = admin.post("/api/v1/setup/stations", json={
                "site_code": f"SITE-{suffix}-{index}", "site_name": f"Site {index}",
                "lab_code": "LAB", "lab_name": f"Lab {index}", "station_code": f"BENCH-{suffix}-{index}",
                "name": f"Registry bench {index}", "station_type": "MODULE", "reason": "Isolated registry test",
            })
            assert response.status_code == 201, response.text
            created.append(response.json()["data"]["id"])
    with engine.begin() as db:
        for index, table in enumerate(("test.station", "test.lab", "test.site"), start=1):
            if table == "test.station":
                db.execute(text("UPDATE test.station SET enabled=false WHERE public_id=:id"), {"id": created[index]})
            elif table == "test.lab":
                db.execute(text("UPDATE test.lab SET enabled=false WHERE id=(SELECT lab_id FROM test.station WHERE public_id=:id)"), {"id": created[index]})
            else:
                db.execute(text("UPDATE test.site SET enabled=false WHERE id=(SELECT l.site_id FROM test.lab l JOIN test.station s ON s.lab_id=l.id WHERE s.public_id=:id)"), {"id": created[index]})
        before = db.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()
    for key in ("admin", "first", "viewer"):
        with signed_in(users[key]) as client:
            items, cursor, seen = [], None, set()
            while True:
                response = client.get("/api/v1/setup/stations", params={"limit": 2, **({"cursor": cursor} if cursor else {})})
                assert response.status_code == 200, response.text
                page = response.json()["data"]
                assert len(page["items"]) <= 2
                items.extend(page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                assert cursor not in seen
                seen.add(cursor)
            assert len({item["id"] for item in items}) == len(items)
            found = {item["id"]: item for item in items}
            assert found[created[0]]["available"] is True
            assert found[created[1]]["enabled"] is False
            assert found[created[2]]["lab_enabled"] is False
            assert found[created[3]]["site_enabled"] is False
            assert all(found[item]["available"] is False for item in created[1:])
            assert found[created[0]]["name"] == "Registry bench 0"
            assert found[created[0]]["site_name"] == "Site 0"
            assert found[created[0]]["lab_name"] == "Lab 0"
            options = client.get("/api/v1/setup/options").json()["data"]["stations"]
            assert created[0] in {item["id"] for item in options}
            assert not set(created[1:]) & {item["id"] for item in options}
            assert client.get("/api/v1/setup/stations?limit=201").status_code == 422
            assert client.get("/api/v1/setup/stations?cursor=invalid").status_code == 422
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one() == before


def test_registry_does_not_expand_presence_visibility(environment):
    _, users = environment
    payload = heartbeat_payload()
    with signed_in(users["first"]) as owner, signed_in(users["second"]) as other, signed_in(users["admin"]) as admin:
        response = owner.put("/api/v1/hmi/presence/" + str(uuid4()), json=payload)
        assert response.status_code == 200, response.text
        assert other.get("/api/v1/setup/stations").status_code == 200
        assert payload["installation_id"] not in {item["installation_id"] for item in other.get("/api/v1/hmi/stations").json()["data"]["items"]}
        assert payload["installation_id"] in {item["installation_id"] for item in admin.get("/api/v1/hmi/stations").json()["data"]["items"]}
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/setup/stations").status_code == 401
