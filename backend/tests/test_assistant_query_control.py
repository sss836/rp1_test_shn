from types import SimpleNamespace
from unittest.mock import patch
import pytest
from app.core.errors import ApiError
from app.services import assistant_business as business
from app.services.assistant_chat import ChatRuntime
from app.services.assistant_outcomes import failure_reason,partial_reply


def catalog():
    return [dict(code=f"REL-{part}-{n}",name="合成用例",target_part_code=part,asset_kind=kind,
                 enabled=enabled,version_id=version,procedure_spec={"long":"steps"})
            for part,n,kind,enabled,version in [
                ("ARM",1,"MODULE",True,"v1"),("UPPER",1,"MODULE",True,"v2"),
                ("UPPER",2,"MODULE",True,"v3"),("UPPER",3,"MODULE",False,None),
                ("SYS",1,"WHOLE_MACHINE",True,"v4")]]


@pytest.mark.parametrize("value",["upper_body","UPPER","上肢"])
def test_alias_and_case_filter_apply_before_pagination(value):
    with patch.object(business,"SetupRepository",lambda _:SimpleNamespace(catalog=catalog)),patch.object(business,"ReadModelRepository"):
        first=business.search(None,{"resource":"cases","query":value,"published_only":True,"enabled":True,"limit":1})
        second=business.search(None,{"resource":"cases","target_part_code":value,"published_only":True,"enabled":True,"limit":1,"cursor":first["next_cursor"]})
    assert first["total"]==2 and first["applied_filters"]["target_part_code"]=="UPPER"
    assert first["items"][0]["code"]!=second["items"][0]["code"] and second["next_cursor"] is None
    assert all(x["target_part_code"]=="UPPER" and "procedure_spec" not in x for x in first["items"]+second["items"])


def test_case_kind_and_conflicting_or_unsupported_filters():
    with patch.object(business,"SetupRepository",lambda _:SimpleNamespace(catalog=catalog)),patch.object(business,"ReadModelRepository"):
        assert business.search(None,{"resource":"cases","asset_kind":"WHOLE_MACHINE"})["total"]==1
        with pytest.raises(ApiError,match="检索词与指定部位"):
            business.search(None,{"resource":"cases","query":"upper_body","target_part_code":"ARM"})
        with pytest.raises(ApiError):business.search(None,{"resource":"cases","execution_id":"not-supported"})
        with pytest.raises(ApiError):business.search(None,{"resource":"cases","cursor":"-1"})
        assert business.search(None,{"resource":"cases","target_part_code":"MISSING"})["items"]==[]


def test_cached_catalog_revalidates_authority_and_retains_one_card(monkeypatch):
    from app.services import assistant_chat as chat
    runtime=ChatRuntime();item={"id":"synthetic-job","reads":0,"cards":[],"context":{},"actor":None}
    authorized=[];calls=[]
    def authorize(token):authorized.append(token);return item
    runtime.authorize=authorize
    class Scope:
        def __enter__(self):return None
        def __exit__(self,*_):pass
    monkeypatch.setattr(chat,"actor_session",lambda actor:Scope())
    def query(*args):calls.append(args);return {"type":"business_search","title":"合成目录","source":"/api/v1/cases","data":{"items":[],"next_cursor":"20"}}
    monkeypatch.setattr(chat,"query",query)
    first=runtime.read("grant",SimpleNamespace(tool="search_business",arguments={"resource":"cases","query":"upper_body"}))
    replay=runtime.read("grant",SimpleNamespace(tool="search_business",arguments={"target_part_code":"UPPER","resource":"cases"}))
    assert len(authorized)==2 and len(calls)==1 and len(item["cards"])==1 and item["reads"]==1
    assert replay["retrieval"]["reused"] and replay["data"]==first["data"]
    runtime.authorize=lambda token:(_ for _ in ()).throw(ApiError(401,"expired","expired"))
    with pytest.raises(ApiError):runtime.read("grant",SimpleNamespace(tool="search_business",arguments={"resource":"cases","query":"upper_body"}))


def test_live_reads_are_not_reused(monkeypatch):
    from app.services import assistant_chat as chat
    runtime=ChatRuntime();item={"id":"job","reads":0,"cards":[],"context":{},"actor":None}
    runtime.authorize=lambda token:item
    class Scope:
        def __enter__(self):return None
        def __exit__(self,*_):pass
    monkeypatch.setattr(chat,"actor_session",lambda actor:Scope())
    calls=[]
    monkeypatch.setattr(chat,"query",lambda *_:calls.append(1) or {"type":"stations","data":{"items":[]}})
    for _ in range(2):runtime.read("grant",SimpleNamespace(tool="list_hmi_stations",arguments={}))
    assert len(calls)==2 and item["reads"]==2


@pytest.mark.parametrize("code,status",[("round_limit",422),("provider_network",502),("provider_timeout",504),("billing_unverified",503),("auth_expired",401)])
def test_bounded_failure_and_partial_evidence(code,status):
    assert failure_reason(status,{"error":code})==code
    text=partial_reply(code,[{"type":"business_search","source":"/api/v1/cases","data":{"items":[{}],"total":10,"applied_filters":{"target_part_code":"UPPER"}}}])
    assert "匹配10条" in text and "UPPER" in text and "不会自动撤销" in text
    assert failure_reason(502,{"error":{"message":"private upstream payload"}})=="tool_failed"
