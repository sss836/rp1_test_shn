"""Milestone 3: actual session API + PostgreSQL permissions and atomic writes."""
import os
import json
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy import create_engine
from sqlalchemy.exc import DBAPIError
import httpx

from test_assistant_integration import environment, signed_in, normal_model

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("ASSISTANT_QA_ADMIN_URL"), reason="standalone isolated database required")]


def create(client, path, payload, status=201, key=None):
    response = client.post("/api/v1/setup/"+path, json={"reason":"isolated unified setup", **payload}, headers={"Idempotency-Key":key or str(uuid4())})
    assert response.status_code == status, response.text
    return response.json().get("data")


def sample(suffix="001"):
    return dict(asset_code="RP1.3-SLEG-"+suffix, asset_kind="MODULE", target_part_code="SLEG", product_family="SYNTHETIC", serial_number=str(uuid4()), display_name="合成样品", model="合成型号", configuration_fingerprint="合成配置")


def definition():
    return dict(procedure_spec={"steps":["合成步骤"]}, stage_definitions=[{"name":"合成阶段"}], equipment_requirements={"description":"合成设备"}, sampling_requirements={"description":"合成采样要求"}, termination_rules={"description":"由操作员停止"}, decision_rules={"mode":"RECORD_ONLY"})


def test_executor_can_create_and_manage_all_setup_with_atomic_receipt(environment):
    engine, users = environment
    with signed_in(users["first"]) as editor:
        key=str(uuid4())
        asset=create(editor,"assets",sample(),key=key)
        replay=create(editor,"assets",{**sample(),"serial_number":"different"},status=409,key=key)
        program=create(editor,"programs",dict(program_code="SYNTHETIC-PROGRAM",name="合成项目"))
        campaign=create(editor,"campaigns",dict(program_id=program["id"],campaign_code="SYNTHETIC-PLAN",name="合成计划",asset_kind="MODULE"))
        station=create(editor,"stations",dict(site_code="SYNTHETIC-SITE",site_name="合成站点",lab_code="SYNTHETIC-LAB",lab_name="合成实验室",station_code="SYNTHETIC-STATION",name="合成台架",station_type="MODULE"))
        case=create(editor,"cases",dict(case_code="SYNTHETIC-CASE",name="合成用例",asset_kind="MODULE",target_part_code="SLEG",**definition()))
        assert case["status"] == "DRAFT"
        create(editor,f"case-versions/{case['version_id']}/publish",{},status=200)
        context=create(editor,f"campaigns/{campaign['id']}/contexts",dict(asset_id=asset["id"],configuration_id=asset["configuration_id"],station_id=station["id"],test_case_version_id=case["version_id"],cycle_code="SYNTHETIC-CYCLE"))
        assert context["context"]["asset_id"] == asset["id"]
        assert editor.patch("/api/v1/setup/assets/"+asset["id"], json={"name":"合成新名称","reason":"creator management"}).status_code==200
        create(editor,f"assets/{asset['id']}/configurations",dict(fingerprint="合成第二配置"))
        assert any(p["id"]==campaign["id"] and p["can_edit"] for p in editor.get("/api/v1/setup/campaigns").json()["data"])
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()==0
        assert connection.execute(text("SELECT count(*) FROM integration.setup_receipt")).scalar_one()>=8


def group(prefix, suffix, key=None):
    def op(kind,alias,**data): return dict(kind=kind,alias=alias,data={"reason":"synthetic atomic group",**data})
    return dict(request_key=key or str(uuid4()),operations=[
        op("program","program",program_code=prefix,name=prefix),
        op("campaign","campaign",program_id="$program.id",campaign_code=prefix,name=prefix,asset_kind="MODULE"),
        op("asset","asset",**sample(suffix)),
        op("station","station",site_code=prefix,site_name=prefix,lab_code=prefix,lab_name=prefix,station_code=prefix,name=prefix,station_type="MODULE"),
        op("case","case",case_code=prefix,name=prefix,asset_kind="MODULE",target_part_code="SLEG",publish=True,**definition()),
        op("context","context",campaign_id="$campaign.id",asset_id="$asset.id",configuration_id="$asset.configuration_id",test_case_version_id="$case.version_id",station_id="$station.id",cycle_code=prefix),
    ])


def bundle(client,payload,status=201):
    response=client.post("/api/v1/setup/bundle",json=payload)
    assert response.status_code==status,response.text
    return response.json().get("data")


@pytest.mark.parametrize("role,suffix",[("admin","010"),("first","011")])
def test_complete_group_and_identical_retry_returns_real_receipt(environment,role,suffix):
    engine,users=environment
    value=group("GROUP-"+role,suffix)
    with signed_in(users[role]) as client:
        first=bundle(client,value)
        second=bundle(client,value)
        assert first["objects"]==second["objects"] and second["replayed"]
        response=client.get("/api/v1/setup/receipts/"+value["request_key"])
        assert response.json()["data"]["objects"]==first["objects"]
        conflict=json.loads(json.dumps(value)); conflict["operations"][0]["data"]["name"]="changed"
        bundle(client,conflict,409)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM integration.setup_receipt WHERE request_key=:key"),{"key":value["request_key"]}).scalar_one()==1
        assert connection.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()==0


def test_same_key_concurrent_submissions_commit_once(environment):
    engine,users=environment
    value=group("CONCURRENT-SAME","012")
    with signed_in(users["first"]) as first,signed_in(users["first"]) as second,ThreadPoolExecutor(max_workers=2) as executor:
        futures=[executor.submit(bundle,client,value) for client in (first,second)]
        results=[future.result(timeout=15) for future in futures]
        assert results[0]["objects"]==results[1]["objects"]
        assert sum(result["replayed"] for result in results)==1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM integration.setup_receipt WHERE request_key=:key"),{"key":value["request_key"]}).scalar_one()==1


def test_late_group_conflict_rolls_back_every_created_row_and_can_resubmit(environment):
    engine,users=environment
    with signed_in(users["first"]) as client:
        bundle(client,group("ROLLBACK-EXISTING","013"))
        failing=group("ROLLBACK-FAILED","014")
        failing["operations"][-1]["data"]["cycle_code"]="ROLLBACK-EXISTING"
        bundle(client,failing,409)
        with engine.connect() as connection:
            for table,column in (("test.test_program","program_code"),("test.station","station_code"),("test.site","site_code"),("catalog.test_case","case_code")):
                assert connection.execute(text(f"SELECT count(*) FROM {table} WHERE {column}='ROLLBACK-FAILED'")).scalar_one()==0
            assert connection.execute(text("SELECT count(*) FROM test.asset WHERE asset_code='RP1.3-SLEG-014'")).scalar_one()==0
            assert connection.execute(text("SELECT count(*) FROM integration.setup_receipt WHERE request_key=:key"),{"key":failing["request_key"]}).scalar_one()==0
        failing["operations"][-1]["data"]["cycle_code"]="ROLLBACK-CORRECTED"
        bundle(client,failing)


def test_foreign_plan_requires_edit_even_with_own_sample(environment):
    _,users=environment
    with signed_in(users["first"]) as owner,signed_in(users["second"]) as other,signed_in(users["admin"]) as admin:
        first=bundle(owner,group("OWNER-PLAN","015"))["objects"]
        other_asset=create(other,"assets",sample("016"))
        context=dict(asset_id=other_asset["id"],configuration_id=other_asset["configuration_id"],test_case_version_id=first["case"]["version_id"],station_id=first["station"]["id"],cycle_code="OTHER-CONTEXT")
        response=other.post(f"/api/v1/setup/campaigns/{first['campaign']['id']}/contexts",json={"reason":"denied foreign plan",**context})
        assert response.status_code in {403,404},response.text
        assert other.patch("/api/v1/setup/assets/"+first["asset"]["id"],json={"name":"forged","reason":"forbidden"}).status_code in {403,404}
        assert other.patch("/api/v1/setup/programs/"+first["program"]["id"],json={"name":"forged","reason":"forbidden"}).status_code in {403,404}
        assert other.post("/api/v1/setup/cases/"+first["case"]["id"]+"/versions",json={"reason":"forbidden"},headers={"Idempotency-Key":str(uuid4())}).status_code in {403,404}
        user_id=os.environ["ASSISTANT_QA_PILOT_IDS"].split(",")[2]
        grant=admin.post(f"/api/v1/admin/users/{user_id}/campaign-grants",json={"campaign_id":first["campaign"]["id"],"access_level":"EDIT","reason":"explicit synthetic grant"})
        assert grant.status_code==200,grant.text
        create(other,f"campaigns/{first['campaign']['id']}/contexts",context)


@pytest.mark.parametrize("path,payload",[
    ("assets",sample("017")),("programs",dict(program_code="VIEWER",name="VIEWER")),
    ("stations",dict(site_code="VIEWER",site_name="VIEWER",lab_code="VIEWER",lab_name="VIEWER",station_code="VIEWER",name="VIEWER",station_type="MODULE")),
    ("cases",dict(case_code="VIEWER",name="VIEWER",asset_kind="MODULE",target_part_code="SLEG")),
])
def test_viewer_cannot_use_new_ordinary_creation_permissions(environment,path,payload):
    _,users=environment
    with signed_in(users["viewer"]) as viewer: create(viewer,path,payload,status=403)


def test_viewer_cannot_call_group_or_publish_and_service_cannot_use_setup(environment):
    _,users=environment
    with signed_in(users["viewer"]) as viewer:
        bundle(viewer,group("VIEWER-GROUP","018"),403)
        assert viewer.post("/api/v1/setup/case-versions/"+str(uuid4())+"/publish",json={"reason":"forbidden"}).status_code==403
        assert viewer.post("/api/v1/setup/programs",json={"program_code":"SERVICE","name":"SERVICE","reason":"forbidden"},headers={"Authorization":"Bearer fake-service"}).status_code==403


def test_published_definition_is_frozen_and_revision_keeps_original_context(environment):
    engine,users=environment
    with signed_in(users["first"]) as editor:
        original=bundle(editor,group("VERSION-FROZEN","019"))["objects"]
        before=editor.get(f"/api/v1/setup/cases/{original['case']['id']}/versions").json()["data"]["versions"][0]
        revised=create(editor,f"cases/{original['case']['id']}/versions",{**definition(),"decision_rules":{"mode":"TEXT_ONLY","text":"由人工核对现象"},"publish":True})
        assert revised["version_id"]!=original["case"]["version_id"]
        versions=editor.get(f"/api/v1/setup/cases/{original['case']['id']}/versions").json()["data"]["versions"]
        assert versions[1]==before
        context=editor.get(f"/api/v1/setup/campaigns/{original['campaign']['id']}/contexts").json()["data"][0]
        assert context["test_case_version_id"]==original["case"]["version_id"]
    app_engine=create_engine(os.environ["DATABASE_URL"])
    with pytest.raises(DBAPIError):
        with app_engine.begin() as connection:
            connection.execute(text("SELECT iam.set_request_context(:user,'immutable-case-check','synthetic')"),{"user":os.environ["ASSISTANT_QA_PILOT_IDS"].split(",")[1]})
            connection.execute(text("UPDATE catalog.test_case_version SET procedure_spec='{}' WHERE public_id=:id"),{"id":original["case"]["version_id"]})
    app_engine.dispose()
    with engine.connect() as connection:
        row=connection.execute(text("SELECT decision_rules,sampling_requirements,public_id AS id FROM catalog.test_case_version WHERE public_id=:id"),{"id":revised["version_id"]}).mappings().one()
        from app.services.archive_analysis import published_acceptance
        _,status,_=published_acceptance(dict(row))
        assert status=="UNCONFIGURED"


@pytest.mark.parametrize("missing",["procedure_spec","equipment_requirements","sampling_requirements","termination_rules","decision_rules"])
def test_incomplete_publish_is_rejected_without_mutating_draft(environment,missing):
    engine,users=environment
    with signed_in(users["first"]) as editor:
        values=definition();values[missing]={}
        if missing=="procedure_spec": values["stage_definitions"]=[]
        case=create(editor,"cases",dict(case_code="INCOMPLETE-"+missing,name=missing,asset_kind="MODULE",target_part_code="SLEG",**values))
        create(editor,f"case-versions/{case['version_id']}/publish",{},status=422)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT status FROM catalog.test_case_version WHERE public_id=:id"),{"id":case["version_id"]}).scalar_one()=="DRAFT"


@pytest.mark.parametrize("rules",[
    {"mode":"AUTOMATIC","archive_acceptance":{}},
    {"mode":"AUTOMATIC","archive_acceptance":{"max_torque_abs_nm":"50"}},
    {"mode":"AUTOMATIC","archive_acceptance":{"max_torque_abs_nm":True}},
    {"mode":"TEXT_ONLY","text":"人工判定","archive_acceptance":{"max_torque_abs_nm":50}},
    {"mode":"RECORD_ONLY","archive_acceptance":{"required_cycles":1}},
])
def test_unsupported_or_contradictory_rules_never_publish(environment,rules):
    _,users=environment
    with signed_in(users["admin"]) as admin:
        create(admin,"cases",dict(case_code="BAD-RULE-"+uuid4().hex[:8],name="bad",asset_kind="MODULE",target_part_code="SLEG",**{**definition(),"decision_rules":rules},publish=True),status=422)


def model_operations(operations,mode="normal"):
    assert httpx.post("http://model-mock:3100/control",json={"mode":mode,"operations":operations},trust_env=False).status_code==200


def command(client,messages,key=None):
    response=client.post("/api/v1/assistant/setup/commands",json={"request_key":key or str(uuid4()),"messages":messages})
    assert response.status_code==200,response.text
    return response.json()["data"]


def test_dialogue_pi_commits_grounded_fields_and_returns_receipt_after_lost_final_reply(environment):
    engine,users=environment
    operations=[dict(kind="program",alias="project",data={"program_code":"DIALOGUE-DIRECT","name":"真实合成项目"})]
    model_operations(operations,"lose_final")
    key=str(uuid4());messages=["创建项目，编号 DIALOGUE-DIRECT，名称真实合成项目"]
    with signed_in(users["first"]) as editor:
        first=command(editor,messages,key)
        assert first["status"]=="created",first
        replay=command(editor,messages,key)
        assert replay["receipt"]["replayed"] and replay["receipt"]["objects"]==first["receipt"]["objects"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_program WHERE program_code='DIALOGUE-DIRECT'")).scalar_one()==1


def test_ai_suggestion_requires_real_user_adoption_and_cannot_cross_sessions(environment):
    engine,users=environment
    ops=[dict(kind="program",alias="project",data={"program_code":"DIALOGUE-PROPOSED","name":"模型建议名称"})]
    model_operations(ops)
    with signed_in(users["first"]) as first,signed_in(users["second"]) as stranger:
        outcome=command(first,["创建项目，编号 DIALOGUE-PROPOSED"])
        assert outcome["status"]=="needs_adoption",outcome
        payload={"proposal_id":outcome["proposal_id"],"proposal_hash":outcome["proposal_hash"]}
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM test.test_program WHERE program_code='DIALOGUE-PROPOSED'")).scalar_one()==0
        assert stranger.post("/api/v1/assistant/setup/adopt",json=payload).status_code==403
        bad={**payload,"proposal_hash":"0"*64}
        assert first.post("/api/v1/assistant/setup/adopt",json=bad).status_code==403
        result=first.post("/api/v1/assistant/setup/adopt",json=payload)
        assert result.status_code==200,result.text
        assert result.json()["data"]["status"]=="created"
        assert first.post("/api/v1/assistant/setup/adopt",json=payload).json()["data"]["receipt"]["replayed"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT authorization_evidence->>'adopted_proposal_hash' FROM integration.setup_receipt WHERE request_key=:key"),{"key":result.json()["data"]["receipt"]["request_key"]}).scalar_one()==outcome["proposal_hash"]


@pytest.mark.parametrize("message",["可以创建项目吗？","不要创建项目 FORBIDDEN","创建项目 FORBIDDEN 可以吗？","如何创建项目"])
def test_question_and_negative_commands_never_dispatch_model_or_write(environment,message):
    engine,users=environment
    model_operations([dict(kind="program",alias="project",data={"program_code":"FORBIDDEN","name":"FORBIDDEN"})])
    with signed_in(users["first"]) as editor: assert command(editor,[message])["status"]=="needs_input"
    assert httpx.get("http://model-mock:3100/stats",trust_env=False).json()["requests"]==[]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_program WHERE program_code='FORBIDDEN'")).scalar_one()==0


def test_missing_fields_and_duplicate_codes_prompt_without_partial_writes(environment):
    engine,users=environment
    with signed_in(users["first"]) as editor:
        model_operations([dict(kind="asset",alias="sample",data={"asset_code":"RP1.3-SLEG-020"})])
        outcome=command(editor,["创建模块样品，编号RP1.3-SLEG-020"])
        assert outcome["status"]=="needs_input",outcome
        model_operations([dict(kind="program",alias="project",data={"program_code":"SYNTHETIC-PROGRAM","name":"合成项目"})])
        assert command(editor,["创建项目 SYNTHETIC-PROGRAM 名称合成项目"])["status"]=="needs_input"
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.asset WHERE asset_code='RP1.3-SLEG-020'")).scalar_one()==0


def test_ai_cannot_publish_without_explicit_user_publication_action(environment):
    _,users=environment
    value=dict(case_code="PROPOSED-PUBLICATION",name="提议发布用例",asset_kind="MODULE",target_part_code="SLEG",**definition(),publish=False)
    ops=[dict(kind="case",alias="case",data={**value,"publish":True})]
    model_operations(ops)
    with signed_in(users["first"]) as editor:
        outcome=command(editor,["创建用例，内容 "+json.dumps(value,ensure_ascii=False)])
        assert outcome["status"]=="needs_adoption",outcome
        assert "case.publish" in outcome["unconfirmed_fields"]
        payload={"proposal_id":outcome["proposal_id"],"proposal_hash":outcome["proposal_hash"]}
        assert editor.post("/api/v1/assistant/setup/adopt",json=payload).status_code==422
        response=editor.post("/api/v1/assistant/setup/adopt",json={**payload,"publish_approved":True})
        assert response.status_code==200,response.text
        assert response.json()["data"]["receipt"]["objects"]["case"]["status"]=="PUBLISHED"


def test_dialogue_group_is_atomic_through_pi_and_full_user_json(environment):
    _,users=environment
    value=group("DIALOGUE-FULL","021")
    model_operations(value["operations"])
    with signed_in(users["admin"]) as admin:
        result=command(admin,["创建并发布整组对象，按以下真实字段："+json.dumps(value["operations"],ensure_ascii=False)])
        assert result["status"]=="created",result
        assert len(result["receipt"]["objects"])==6


@pytest.mark.parametrize("flag",["true","yes",1])
def test_model_cannot_bypass_publication_action_with_coerced_boolean(environment,flag):
    engine,users=environment
    value=dict(case_code="STRICT-PUBLISH-"+str(flag),name="合成用例",asset_kind="MODULE",target_part_code="SLEG",**definition())
    model_operations([dict(kind="case",alias="case",data={**value,"publish":flag})])
    with signed_in(users["first"]) as editor:
        result=command(editor,["创建用例，内容 "+json.dumps(value,ensure_ascii=False)])
        assert result["status"]=="needs_input",result
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM catalog.test_case WHERE case_code=:code"),{"code":value["case_code"]}).scalar_one()==0


def test_dialogue_follow_up_supplies_missing_real_field_without_second_confirmation(environment):
    _,users=environment
    model_operations([dict(kind="program",alias="program",data={"program_code":"FOLLOW-UP"})])
    with signed_in(users["first"]) as editor:
        assert command(editor,["创建项目编号 FOLLOW-UP"])["status"]=="needs_input"
        model_operations([dict(kind="program",alias="program",data={"program_code":"FOLLOW-UP","name":"补充名称"})])
        result=command(editor,["创建项目编号 FOLLOW-UP","名称补充名称"])
        assert result["status"]=="created",result


def test_ambiguous_existing_object_is_asked_before_selection(environment):
    engine,users=environment
    with signed_in(users["first"]) as editor:
        plans=[]
        for number in range(2):
            project=create(editor,"programs",dict(program_code="AMBIGUOUS-PROJECT-"+str(number),name="分属项目"+str(number)))
            plans.append(create(editor,"campaigns",dict(program_id=project["id"],campaign_code="AMBIGUOUS-PLAN",name="同名计划",asset_kind="MODULE")))
        model_operations([dict(kind="campaign_activate",alias="plan",data={"id":plans[0]["id"]})])
        result=command(editor,["开放计划 AMBIGUOUS-PLAN 同名计划"])
        assert result["status"]=="needs_input" and result["code"]=="object_ambiguous",result
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM test.test_campaign WHERE campaign_code='AMBIGUOUS-PLAN' AND status='PLANNED'")).scalar_one()==2
        model_operations([dict(kind="campaign_activate",alias="plan",data={"id":plans[0]["id"]})])
        result=command(editor,["开放计划 AMBIGUOUS-PLAN 同名计划","开放计划 UUID "+plans[0]["id"]])
        assert result["status"]=="created",result


def test_disabled_during_model_wait_cannot_commit_and_wait_has_no_open_transaction(environment):
    engine,users=environment
    model_operations([dict(kind="program",alias="project",data={"program_code":"DISABLED-WAIT","name":"等待期间停用"})],"pause_before_tool")
    with signed_in(users["first"]) as editor,ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(command,editor,["创建项目编号 DISABLED-WAIT 名称等待期间停用"])
        deadline=time.monotonic()+5
        while not httpx.get("http://model-mock:3100/stats",trust_env=False).json()["requests"]:
            assert time.monotonic()<deadline
            time.sleep(.02)
        with engine.begin() as connection:
            assert connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='rp1_app' AND state='idle in transaction'")).scalar_one()==0
            connection.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:name"),{"name":users["first"]})
        try:
            assert future.result(timeout=10)["status"]=="needs_input"
        finally:
            with engine.begin() as connection:
                connection.execute(text("UPDATE iam.app_user SET enabled=true WHERE username=:name"),{"name":users["first"]})
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM test.test_program WHERE program_code='DISABLED-WAIT'")).scalar_one()==0


def test_append_without_retry_key_is_refused(environment):
    _,users=environment
    with signed_in(users["first"]) as editor:
        case=create(editor,"cases",dict(case_code="KEY-REQUIRED",name="请求标识检查",asset_kind="MODULE",target_part_code="SLEG"))
        assert editor.post(f"/api/v1/setup/cases/{case['id']}/versions",json={"reason":"missing retry key"}).status_code==422


def test_dialogue_revision_retains_verified_fields_and_stale_base_rejects(environment):
    _,users=environment
    with signed_in(users["first"]) as editor:
        fields={**definition(),"metric_requirements":[{"metric":"合成扭矩"}],"fault_rules":{"description":"合成故障登记规则"},"source_references":[{"reference":"synthetic-source"}],"deviation_notes":"原始偏离说明"}
        case=create(editor,"cases",dict(case_code="RETAINED-CASE",name="保留字段用例",asset_kind="MODULE",target_part_code="SLEG",publish=True,**fields))
        model_operations([dict(kind="case_revision",alias="case",data={"id":case["id"],"sampling_requirements":{"description":"改用2Hz记录"}})])
        result=command(editor,["修订用例 RETAINED-CASE，采样要求改用2Hz记录"])
        assert result["status"]=="created",result
        rows=editor.get(f"/api/v1/setup/cases/{case['id']}/versions").json()["data"]["versions"]
        assert rows[0]["status"]=="DRAFT" and rows[1]["status"]=="PUBLISHED"
        for name in ("metric_requirements","fault_rules","source_references","deviation_notes","equipment_requirements","procedure_spec"):
            assert rows[0][name]==rows[1][name]==fields[name]
        assert rows[0]["sampling_requirements"]=={"description":"改用2Hz记录"}
        create(editor,f"cases/{case['id']}/versions",dict(**fields,base_version_id=case["version_id"]),status=409)
