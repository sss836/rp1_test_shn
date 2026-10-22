import io
import json
import os
from uuid import uuid4
import httpx
import pytest
from PIL import Image,ImageDraw,ImageFont
from sqlalchemy import text
from tests.test_assistant_integration import environment,signed_in
from tests.test_assistant_runtime_live import wait

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.getenv("ASSISTANT_QA_ADMIN_URL"),reason="isolated stack required")]

@pytest.fixture(autouse=True)
def reset_finance(environment):
    engine,_=environment
    with engine.begin() as c:
        c.execute(text("DELETE FROM integration.assistant_model_call"))
        c.execute(text("DELETE FROM integration.assistant_budget_day"))
        c.execute(text("UPDATE integration.assistant_finance SET blocked=false,reason=NULL"))
    if not os.getenv("ASSISTANT_QA_LIVE"):
        httpx.post("http://model-mock:3100/control",json={"mode":"normal","tool":"get_runtime_summary","arguments":{"part":"上肢"}},trust_env=False)

def ask(client,message,attachment=None):
    payload={"request_key":str(uuid4()),"message":message,"context":{"view":"archives","kind":"MODULE"}}
    if attachment:payload["attachments"]=[attachment]
    response=client.post("/api/v1/assistant/jobs",json=payload)
    assert response.status_code==202,response.text
    result=wait(client,response.json()["data"]["id"])
    from pathlib import Path
    Path("/reports/engineering-"+result["id"]+".json").write_text(json.dumps({"question":message,"job":result},ensure_ascii=False,default=str))
    return result

def test_account_attachment_real_http_and_snapshot(environment):
    _,users=environment
    with signed_in(users["first"]) as first,signed_in(users["second"]) as other:
        response=first.post("/api/v1/assistant/attachments",headers={"Content-Type":"text/plain","X-Filename":"synthetic-error.log"},content=b"synthetic diagnostic trace")
        assert response.status_code==201,response.text
        identifier=response.json()["data"]["id"]
        assert other.get("/api/v1/assistant/attachments/"+identifier).status_code==404
        result=ask(first,"请分析所附合成日志",identifier)
        assert result["state"]=="COMPLETED",result
        history=first.get("/api/v1/assistant/threads/"+result["thread_id"]).json()["data"]
        assert history["messages"][0]["attachments"][0]["id"]==identifier
        assert first.delete("/api/v1/assistant/attachments/"+identifier).status_code==204
        assert first.get("/api/v1/assistant/attachments/"+identifier).status_code==404

@pytest.mark.skipif(not os.getenv("ASSISTANT_QA_LIVE"),reason="official-model isolated evidence")
def test_official_linux_and_source_and_native_image(environment):
    _,users=environment
    with signed_in(users["first"]) as client:
        general=ask(client,"Linux如何查看哪个进程监听8080端口？只说明命令及含义，不执行。")
        assert general["state"]=="COMPLETED",general
        reply=general["result"]["reply"].lower()
        assert any(x in reply for x in ("ss ","lsof","netstat")) and "不属于" not in reply
        source=ask(client,"请实际搜索项目源码中的 assistant_task_seconds，读取相关上下文，说明超时限制设置在哪个文件，并引用源码快照版本。")
        assert source["state"]=="COMPLETED",source
        assert any(c["type"]=="source_evidence" and c["data"].get("version") for c in source["result"]["cards"]),source
        image=Image.new("RGB",(850,210),"white");draw=ImageDraw.Draw(image)
        font=ImageFont.load_default(size=28)
        draw.text((20,30),"RP1 SYNTHETIC ERROR 7319",font=font,fill="red")
        draw.text((20,90),"assistant_task_seconds",font=font,fill="black")
        output=io.BytesIO();image.save(output,format="PNG")
        uploaded=client.post("/api/v1/assistant/attachments",headers={"Content-Type":"image/png","X-Filename":"synthetic.png"},content=output.getvalue())
        assert uploaded.status_code==201,uploaded.text
        visual=ask(client,"读出图片中的错误编号和字段名；再实际搜索该字段的项目源码，说明其用途。图片为合成测试，请勿声称现场已发生故障。",uploaded.json()["data"]["id"])
        assert visual["state"]=="COMPLETED",visual
        assert "7319" in visual["result"]["reply"],visual
        assert any(c["type"]=="source_evidence" for c in visual["result"]["cards"]),visual
        business=ask(client,"当前上肢实际运行了多少小时，哪个关节的力矩最大？请先查询已有数据；无数据时明确说明，不能因为页面没选曲线就拒答。")
        assert business["state"]=="COMPLETED",business
        assert any(c["type"]=="runtime_summary" for c in business["result"]["cards"]),business
        assert not any(x in business["result"]["reply"] for x in ("不属于我可执行","请提供metric","请提供subject"))

@pytest.mark.skipif(bool(os.getenv("ASSISTANT_QA_LIVE")),reason="synthetic request capture only")
def test_native_image_and_actual_question_reach_provider(environment):
    _,users=environment
    image=Image.new("RGB",(80,40),"white");output=io.BytesIO();image.save(output,format="PNG")
    with signed_in(users["first"]) as client:
        response=client.post("/api/v1/assistant/attachments",headers={"Content-Type":"image/png","X-Filename":"synthetic.png"},content=output.getvalue())
        result=ask(client,"查询上肢累计时长并阅读图片",response.json()["data"]["id"])
        assert result["state"]=="COMPLETED",result
        captured=httpx.get("http://model-mock:3100/stats",trust_env=False).json()["requests"]
        assert captured and all(r["has_actual_question"] and "user" in r["roles"] and r["image_count"]==1 for r in captured)
