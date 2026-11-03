import pytest
from app.core.errors import ApiError
from app.schemas.setup import CaseDefinition
from app.services.setup import validate_definition
from app.services.setup_assistant import may_publish,explicit_intent
from app.schemas.setup import AssetCreate
from pydantic import ValidationError


@pytest.mark.parametrize("message,allowed",[
    ('创建用例，内容 {"name":"提议发布用例","publish":false}',False),
    ('创建用例，内容 {"notes":"立即发布版本","publish":false}',False),
    ('创建用例，内容 {"parameter_manifest":{"publish":true}}',False),
    ('创建用例，内容 {"publish":true}',True),
    ('创建并发布用例，编号 CASE-1',True),
    ('创建用例 CASE-1，但不要发布',False),
    ('请发布该版本 CASE-1',True),
    ('创建用例，内容 [{"kind":"case","data":{"publish":false}}]',False),
    ('创建用例，内容 [{"kind":"case","data":{"publish":true}}]',True),
])
def test_publication_requires_action_not_quoted_names_or_false_flags(message,allowed):
    assert may_publish([message]) is allowed


def test_empty_descriptions_and_empty_steps_do_not_meet_publication_completeness():
    with pytest.raises(ApiError):
        validate_definition(CaseDefinition(reason="test",publish=True,procedure_spec={"steps":[]},stage_definitions=[],equipment_requirements={"description":" "},sampling_requirements={"description":" "},termination_rules={"description":" "},decision_rules={"mode":"RECORD_ONLY"}))


def test_follow_up_keeps_original_explicit_action_but_cancel_revokes_it():
    assert explicit_intent(["创建项目编号P1","名称合成项目"])
    assert not explicit_intent(["创建项目编号P1","取消建档","名称合成项目"])
    assert not explicit_intent(["创建项目编号P1","名称是什么？"])
    assert may_publish(["创建并发布用例C1","采样要求1Hz"])
    assert not may_publish(["创建并发布用例C1","先不要发布"])
    assert explicit_intent(['创建项目，内容 {"name":"如何创建？不要创建"}'])
    assert not may_publish(['创建用例名称“立即发布用例”，仅存草稿'])


def test_sample_number_derives_identity_without_metadata_or_purpose():
    value = AssetCreate(asset_code="RP1.3-ARM-004", auto_number=False, creation_scenario="RND_TEST")
    assert (value.product_family, value.target_part_code, value.asset_kind) == ("RP1.3", "ARM", "MODULE")
    assert value.serial_number is value.model is value.configuration_fingerprint is None
    with pytest.raises(ValidationError):
        AssetCreate(asset_code="RP1.3-ARM-004")
    with pytest.raises(ValidationError):
        AssetCreate(asset_code="RP1.3-ARM-004", auto_number=False, creation_scenario="RND_TEST", sample_purpose="TEST")
    with pytest.raises(ValidationError):
        AssetCreate(asset_code="RP1.3-ARM-004", auto_number=False, creation_scenario="RND_TEST", product_family="wrong")


@pytest.mark.parametrize("answer,expected",[("向下编号是，创建场景研发测试",True),("向下编号：否",False),("是，研发测试",True),("不顺延编号",False)])
def test_numbering_answers_are_grounded_without_adopting_generated_fields(answer,expected):
    from types import SimpleNamespace
    from app.schemas.setup import SetupBundle, SetupOperation
    from app.services.setup_assistant import unadopted_fields, numbering_choice
    messages=["创建样品 RP1.3-ARM-004",answer]
    assert numbering_choice(messages) is expected
    assert explicit_intent(messages)
    op=SetupOperation(kind="asset",alias="asset",data={"asset_code":"RP1.3-ARM-004","auto_number":expected,"creation_scenario":"RND_TEST"})
    intent=SimpleNamespace(messages=messages+["创建场景研发测试"],choices={})
    assert unadopted_fields(SetupBundle(request_key="00000000-0000-4000-8000-000000000001",operations=[op]),intent)==[]
    op.data["auto_number"]=not expected
    assert "asset.auto_number" in unadopted_fields(SetupBundle(request_key="00000000-0000-4000-8000-000000000001",operations=[op]),intent)
