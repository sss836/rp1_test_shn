"""Disposable PostgreSQL only. All CSVs, accounts and records are synthetic fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import get_settings
from app.main import app
from app.services.factory_robot_results import FactoryRobotResultStorage
from test_factory_robot_results import result_csv, result_row
from test_hmi_presence_integration import PASSWORD, environment, signed_in


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="requires disposable rp1_presence database")]


def heartbeat(source, sn="00001234", robot="qa-robot"):
    return {"protocol":"reliability_v1", "source_id": source, "source_kind":"real", "robots":[{
        "robot_id":robot,"serial_number":sn,"model":"24轴","station_name":"隔离工位",
        "online":True,"observed_at":datetime.now(timezone.utc).isoformat(),"data_fresh":True}]}


def upload(client, source, body, key=None, robot="qa-robot", kind="real", **changes):
    params = dict(source_id=source,robot_id=robot,source_kind=kind,filename="results.csv",size_bytes=len(body),
                  sha256=hashlib.sha256(body).hexdigest(),verified="true",completed="true",reason="隔离夹具人工核实并显式提交")
    params.update(changes)
    return client.post("/api/v1/factory/robot-result-archives", params=params,content=body,
                       headers={"Content-Type":"text/csv","Idempotency-Key":key or str(uuid4())})


def do_review(client, archive_id, decision="APPROVE", revision=1, key=None):
    return client.post(f"/api/v1/factory/robot-result-archives/{archive_id}/review",
        json={"decision":decision,"expected_revision":revision,"reason":"隔离工厂汇总审核理由"},
        headers={"Idempotency-Key":key or str(uuid4())})


def test_presence_binding_ttl_omissions_disabled_owner_and_no_poll_audit_growth(environment):
    engine, users = environment; source = "PC-"+uuid4().hex[:12]
    with signed_in(users["first"]) as owner, signed_in(users["second"]) as stranger, signed_in(users["admin"]) as admin:
        message = heartbeat(source)
        assert owner.post("/api/v1/factory/robots/presence",json=message).status_code==200
        assert stranger.post("/api/v1/factory/robots/presence",json=message).status_code==409
        for _ in range(100):
            response=owner.post("/api/v1/factory/robots/presence",json=message)
            assert response.status_code==200,response.text
        with engine.connect() as db:
            assert db.execute(text("SELECT count(*) FROM integration.factory_robot_presence WHERE source_id=:s"),{"s":source}).scalar_one()==1
            assert db.execute(text("SELECT count(*) FROM audit.change_log WHERE schema_name='integration' AND table_name='factory_robot_presence'")).scalar_one()==0
            assert db.execute(text("SELECT count(*) FROM audit.change_log WHERE table_name='robot_result_source' AND after_data->>'source_id'=:s"),{"s":source}).scalar_one()==1
        assert admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["online"]
        diagnostic=heartbeat(source);diagnostic["robots"][0]["data_fresh"]=False
        assert owner.post("/api/v1/factory/robots/presence",json=diagnostic).status_code==200
        snapshot=admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]
        assert snapshot["online"] and snapshot["data_fresh"] is False
        assert owner.post("/api/v1/factory/robots/presence",json=heartbeat(source,sn="different")).status_code==409
        model_change=heartbeat(source);model_change["robots"][0]["model"]="28轴"
        assert owner.post("/api/v1/factory/robots/presence",json=model_change).status_code==409
        unchanged=admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]
        assert unchanged["model"]=="24轴" and unchanged["serial_number"]=="00001234" and unchanged["online"]
        with engine.begin() as db:
            db.execute(text("UPDATE integration.factory_robot_presence SET last_seen_at=now()-interval '46 seconds' WHERE source_id=:s"),{"s":source})
        assert not admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["online"]
        assert owner.post("/api/v1/factory/robots/presence",json=message).status_code==200
        with engine.begin() as db: db.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:u"),{"u":users["first"]})
        assert not admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["online"]


def test_approval_versions_original_files_viewer_and_parent_child_non_duplication(environment):
    engine, users = environment; source="PC-"+uuid4().hex[:12]
    row=result_row(**{"上位机编号":source})
    child=result_row(**{"上位机编号":source,"测试项目编号":"SYS-001","记录层级":"子项","原始检查ID":"RP001"})
    body=result_csv(row,child); original_count=None
    with engine.connect() as db:
        original_count=db.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()
        original_runtime=db.execute(text("SELECT count(*) FROM test.runtime_interval")).scalar_one()
        original_exposure=db.execute(text("SELECT count(*) FROM reliability.exposure_assessment")).scalar_one()
        original_failures=db.execute(text("SELECT count(*) FROM reliability.interruption")).scalar_one()
    with signed_in(users["first"]) as owner, signed_in(users["viewer"]) as viewer, signed_in(users["admin"]) as admin:
        key=str(uuid4()); response=upload(owner,source,body,key); assert response.status_code==201,response.text
        item=response.json()["data"]; archive_id=item["id"]
        assert "rows" not in item and item["status"]=="PENDING" and item["formal_statistics_eligible"] is False
        assert upload(owner,source,body,key).json()["data"]["id"]==archive_id
        assert viewer.get(f"/api/v1/factory/robot-result-archives/{archive_id}").status_code==404
        assert admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["cases"]==[]
        result=do_review(owner,archive_id); assert result.status_code==200,result.text
        detail=result.json()["data"]; assert detail["status"]=="APPROVED" and detail["revision"]==2
        assert detail["rows"][0]["values"]==row and detail["rows"][0]["version"]==1
        assert viewer.get(f"/api/v1/factory/robot-result-archives/{archive_id}/file").content==body
        cases=viewer.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["cases"]
        assert len(cases)==1 and cases[0]["case_code"]=="SYS-012" and cases[0]["record_count"]==1
        changed=list(row); changed[13]="更新的人工核实结果"; changed[15]="不通过"
        revision=upload(owner,source,result_csv(changed)); assert revision.status_code==201,revision.text
        new_id=revision.json()["data"]["id"]; assert new_id!=archive_id
        new_detail=admin.get(f"/api/v1/factory/robot-result-archives/{new_id}").json()["data"]
        assert new_detail["rows"][0]["version"]==2
        assert owner.get(f"/api/v1/factory/robot-result-archives/{archive_id}/file").content==body
        assert admin.get("/api/v1/factory/robots",params={"source_id":source}).json()["data"]["items"][0]["cases"]==[]
        assert do_review(admin,new_id,"REJECT").status_code==200
        resubmitted=upload(owner,source,result_csv(changed)); assert resubmitted.status_code==201,resubmitted.text
        third=resubmitted.json()["data"]["id"]; assert third!=new_id
        assert admin.get(f"/api/v1/factory/robot-result-archives/{third}").json()["data"]["rows"][0]["version"]==3
        assert do_review(admin,third).status_code==200
        assert do_review(admin,third,"REJECT").status_code==409
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()==original_count
        assert db.execute(text("SELECT count(*) FROM test.runtime_interval")).scalar_one()==original_runtime
        assert db.execute(text("SELECT count(*) FROM reliability.exposure_assessment")).scalar_one()==original_exposure
        assert db.execute(text("SELECT count(*) FROM reliability.interruption")).scalar_one()==original_failures


def test_validation_simulation_file_corruption_and_submitter_disable(environment):
    engine, users=environment;source="PC-"+uuid4().hex[:12]
    with signed_in(users["first"]) as owner, signed_in(users["admin"]) as admin:
        body=result_csv(result_row(**{"上位机编号":source}))
        assert upload(owner,source,body,verified="false").status_code==422
        assert upload(owner,source,body,completed="false").status_code==422
        assert upload(owner,source,body,sha256="0"*64).status_code==422
        sim=result_csv(result_row(**{"上位机编号":source,"备注":"数据来源：simulated（模拟数据，不代表实机验收）"}))
        assert upload(owner,source,sim).status_code==422
        result=upload(owner,source,sim,kind="simulated"); assert result.status_code==201,result.text
        assert do_review(admin,result.json()["data"]["id"]).status_code==422
        real=upload(owner,source,body);assert real.status_code==201,real.text
        identity=real.json()["data"]["id"]
        raw=FactoryRobotResultStorage(get_settings().archive_root).path(identity)
        raw.chmod(0o640);raw.write_bytes(body+b"tampered")
        assert do_review(admin,identity).status_code==409
        assert owner.get(f"/api/v1/factory/robot-result-archives/{identity}/file").status_code==409
        raw.write_bytes(body);raw.chmod(0o440)
        with engine.begin() as db:db.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:u"),{"u":users["first"]})
        assert do_review(admin,identity).status_code==403


def test_concurrent_idempotency_and_source_ownership(environment):
    _,users=environment;source="PC-"+uuid4().hex[:12];body=result_csv(result_row(**{"上位机编号":source}));key=str(uuid4())
    def send():
        with signed_in(users["first"]) as owner:
            response=upload(owner,source,body,key);assert response.status_code==201,response.text
            return response.json()["data"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert len(set(pool.map(lambda _:send(),range(2))))==1
    with signed_in(users["second"]) as stranger:
        assert upload(stranger,source,body).status_code==409


def test_sql_role_cannot_rewrite_evidence_and_card_projection_is_bounded(environment):
    engine,users=environment;source="PC-"+uuid4().hex[:12];body=result_csv(result_row(**{"上位机编号":source}))
    with signed_in(users["first"]) as owner:
        response=upload(owner,source,body);assert response.status_code==201,response.text
        actor=owner.get("/api/v1/auth/me").json()["data"]["user"]["id"]
    application=create_engine(os.environ["DATABASE_URL"])
    with application.connect() as db:
        tx=db.begin();db.execute(text("SELECT iam.set_request_context(:id,'qa-readonly','immutable evidence check')"),{"id":actor})
        with pytest.raises(Exception):db.execute(text("UPDATE integration.robot_result_row SET verdict='通过' WHERE source_id=:s"),{"s":source})
        tx.rollback()
    application.dispose()
    with engine.connect() as db:
        definition=db.execute(text("SELECT pg_get_functiondef('integration.robot_result_case_cards(jsonb)'::regprocedure)")).scalar_one()
        assert 'jsonb_to_recordset(p_keys)' in definition
        assert 'cell_values' not in definition and 'latest.*' not in definition
        assert 'GROUP BY' in definition and 'statement_timeout' in definition


def test_reviewer_can_review_but_cannot_upload_or_heartbeat(environment):
    engine,users=environment
    with engine.begin() as db: db.execute(text("UPDATE iam.app_user SET role='FACTORY_REVIEWER' WHERE username=:u"),{"u":users["second"]})
    source="PC-"+uuid4().hex[:12];body=result_csv(result_row(**{"上位机编号":source}))
    with signed_in(users["first"]) as owner, signed_in(users["second"]) as reviewer:
        response=upload(owner,source,body);assert response.status_code==201,response.text
        assert reviewer.post("/api/v1/factory/robots/presence",json=heartbeat(source)).status_code==403
        assert upload(reviewer,source,body).status_code==403
        result=do_review(reviewer,response.json()["data"]["id"]);assert result.status_code==200,result.text


def test_non_page_long_history_stays_in_sql_and_cards_only_return_light_projection(environment):
    engine,users=environment
    own_source="PC-"+uuid4().hex[:12];other_source="PC-"+uuid4().hex[:12]
    with signed_in(users["first"]) as owner:
        own=upload(owner,own_source,result_csv(result_row(**{"上位机编号":own_source}))).json()["data"]
        assert do_review(owner,own["id"]).status_code==200
        unrelated=result_csv(*[result_row(**{"上位机编号":other_source,"样品编号":"UNRELATED-SN","实际结果":"large-evidence-"+"x"*4000}) for _ in range(250)])
        other=upload(owner,other_source,unrelated);assert other.status_code==201,other.text
        assert do_review(owner,other.json()["data"]["id"]).status_code==200
        response=owner.get("/api/v1/factory/robots",params={"source_id":own_source})
        assert response.status_code==200 and len(response.content)<10000
        card=response.json()["data"]["items"][0]
        assert card["source_id"]==own_source and len(card["cases"])==1 and card["cases"][0]["record_count"]==1
        actor=owner.get("/api/v1/auth/me").json()["data"]["user"]["id"]
    with engine.begin() as db:
        db.execute(text("SELECT iam.set_request_context(:id,'qa-cards','bounded visible keys projection')"),{"id":actor})
        projection=db.execute(text("SELECT * FROM integration.robot_result_case_cards(CAST(:keys AS jsonb))"),
            {"keys":__import__('json').dumps([{"source_id":own_source,"robot_id":"qa-robot","serial_number":"00001234"}])})
        assert "cell_values" not in projection.keys() and "row_sha256" not in projection.keys()
        rows=projection.mappings().all();assert len(rows)==1 and rows[0]["record_count"]==1
