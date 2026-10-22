"""User-grounded setup intents; model output alone never adopts parameters."""
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from threading import RLock
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, StrictBool
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.repositories.setup import SetupRepository
from app.schemas.setup import SetupBundle,CaseDefinition
from app.services.auth import AuthService
from app.services.assistant import grants, require_pilot, service_secret
from app.services.setup import apply_bundle, canonical, validate_bundle_shape


class SetupCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_key: UUID
    messages: list[str] = Field(min_length=1, max_length=12)


class AdoptProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_id: str = Field(min_length=32,max_length=128)
    proposal_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    publish_approved: StrictBool = False


@dataclass
class Intent:
    user_id: UUID
    session_id: UUID
    messages: list[str]
    request_key: UUID
    choices: dict
    outcome: dict | None = None
    job_id: UUID | None = None
    operation_kinds: dict | None = None
    input_hash: str | None = None


@dataclass
class Proposal:
    intent: Intent
    bundle: SetupBundle
    digest: str
    expires: float


intents: dict[str,Intent] = {}
proposals: dict[str,Proposal] = {}
lock = RLock()


def source_hash(intent):
    return intent.input_hash or hashlib.sha256(canonical(intent.messages).encode()).hexdigest()


def has_literal(value, source):
    value = str(value).strip()
    if not value: return True
    return re.search(r"(?<![A-Za-z0-9_.-])"+re.escape(value)+r"(?![A-Za-z0-9_.-])",source) is not None


def structured_parts(message):
    """Keep quoted business values out of the action/negation recognizer."""
    decoder=json.JSONDecoder();prose=[];values=[];position=0
    while position<len(message):
        if message[position] in '[{"':
            try:
                value,end=decoder.raw_decode(message[position:]);values.append(value);prose.append(" ");position+=end;continue
            except ValueError: pass
        if message[position]=="“":
            end=message.find("”",position+1)
            if end!=-1: prose.append(" ");position=end+1;continue
        prose.append(message[position]);position+=1
    return "".join(prose),values


def explicit_intent(messages):
    # Conservative command gate. Questions/negation are clarified, never writes.
    last=structured_parts(messages[-1])[0].strip()
    denied=r"(?:不要|先别|暂不|别).{0,4}(?:创建|新建|执行|关联|修改)|取消|撤销|停止建档|不执行|do not|don't"
    question=r"仅咨询|只问|能否|是否|怎么|如何|可以吗|[？?]"
    action=r"^(?:请\s*|请帮我\s*|帮我\s*)?(?:创建|新建|新增|建立|发布|修订|修改|更新|追加|关联|配置|开放|create\b|publish\b|revise\b)"
    if re.search(denied+"|"+question,last,re.I): return False
    if re.match(action,last,re.I): return True
    if not re.match(r"^(?:名称|编号|序列号|部位|类型|项目|计划|台架|设备|采样|步骤|终止|判据|配置|型号|产品|补充|改为|name\b|code\b|serial\b|\{|\[)",last,re.I): return False
    for previous in reversed(messages[:-1]):
        previous=structured_parts(previous)[0]
        if re.search(denied,previous,re.I): return False
        if re.match(action,previous.strip(),re.I) and not re.search(question,previous,re.I): return True
    return False


def command_messages(messages):
    """A fresh explicit command doesn't inherit another command's parameters.

    Field-only follow-ups retain their nearest preceding instruction. Explicit
    references may retain prior context, but future messages are never present.
    """
    for index in range(len(messages)-1,-1,-1):
        prose=structured_parts(messages[index])[0]
        if explicit_intent([messages[index]]) and not re.search(r'上述|刚才|前面|上一个|该(?:项目|计划|样品|用例|版本)|此(?:项目|计划|样品|用例|版本)',prose):
            return messages[index:]
    return messages


def publication_state(message):
    prose,values=structured_parts(message)
    def requested(value):
        if isinstance(value,list):
            states=[requested(item) for item in value]
            return True if True in states else False if False in states else None
        if not isinstance(value,dict): return None
        if value.get("kind")=="case_publish": return True
        if value.get("kind") in {"case","case_revision"}:
            data=value.get("data")
            return data.get("publish") if isinstance(data,dict) else None
        if "operations" in value: return requested(value["operations"])
        return value.get("publish")
    if re.search(r"(?:不要|先别|不|暂不).{0,8}发布|不发布|do not.*publish|don't.*publish|publish\s*[:=]?\s*false",prose,re.I): return False
    states=[requested(value) for value in values]
    if False in states: return False
    if True in states or re.search(r"(?:^|[，,。;；\s])(?:请|帮我|请帮我)?(?:创建并发布|立即发布|并发布|发布(?:该|此|测试用例|用例|版本)|publish\b)",prose,re.I): return True
    return None


def may_publish(messages):
    for message in reversed(messages):
        state=publication_state(message)
        if state is not None:return state
    return False


def set_context(session, actor):
    session.execute(text("SELECT iam.set_request_context(:user,:request,:reason)"), {"user":str(actor.public_id),"request":actor.request_id,"reason":"authenticated assistant setup"})
    session.execute(text("SELECT set_config('app.source_ip',coalesce(:ip,''),true)"),{"ip":actor.source_ip})


def matching_choices(session, messages):
    repo=SetupRepository(session)
    options=repo.options()
    cases=repo.catalog()
    raw={"program_id":options["programs"],"asset_id":options["assets"],"station_id":options["stations"],
         "configuration_id":[{**r,"code":r["fingerprint"]} for r in options["configurations"]],
         "campaign_id":repo.campaigns(),"case_id":cases,
         "test_case_version_id":[{**r,"id":r["version_id"]} for r in cases if r["version_id"]]}
    source="\n".join(messages)
    choices={key:[row for row in rows if any(has_literal(row.get(field,""),source) for field in ("id","code","name") if row.get(field))] for key,rows in raw.items()}
    versions=[]
    definitions={}
    for case in choices["case_id"]:
        history=repo.case_versions(case["id"])["versions"]
        versions += [{"id":str(row["id"]),"code":case["code"],"name":case["name"],"version":row["version"],"status":row["status"]} for row in history]
        if history: definitions[str(case["id"])]=history[0]
    choices["version_id"]=versions
    choices["case_definitions"]=definitions
    return json.loads(canonical(choices))


def resolve_identifier(value, field, kind, intent):
    if isinstance(value,str) and value.startswith("$"): return value
    source="\n".join(intent.messages)
    try:
        parsed=str(UUID(str(value)))
        if has_literal(parsed,source): return parsed
    except ValueError: parsed=None
    group=field
    if field=="id": group={"case_revision":"case_id","case_publish":"version_id","configuration":"asset_id","asset_update":"asset_id","program_update":"program_id","campaign_update":"campaign_id","station_update":"station_id","campaign_activate":"campaign_id"}.get(kind,"id")
    rows=intent.choices.get(group,[])
    selected=[row for row in rows if value in (row.get("id"),row.get("code"),row.get("name"))]
    if len(selected)!=1: raise ApiError(422,"object_ambiguous","对象未明确或有同名冲突，请提供唯一编号或UUID。",[field])
    row=selected[0]
    # A model may use a resolved UUID only when a unique user-supplied label backs it.
    labels=[label for label in (row.get("code"),row.get("name")) if label and has_literal(label,source)]
    unique=any(sum(label in (r.get("code"),r.get("name")) for r in rows)==1 for label in labels)
    if group=="version_id" and len([r for r in rows if r.get("code")==row.get("code")])>1:
        unique=unique and has_literal(row["version"],source)
    if not unique: raise ApiError(422,"object_ambiguous","匹配对象不唯一，请补充具体版本或UUID。",[field])
    return row["id"]


def user_json_values(messages):
    found=set()
    def visit(value):
        if isinstance(value,(dict,list)):
            found.add(canonical(value))
            for child in value.values() if isinstance(value,dict) else value: visit(child)
    decoder=json.JSONDecoder()
    for message in messages:
        for match in re.finditer(r"[\[{]",message):
            try: value,_=decoder.raw_decode(message[match.start():]); visit(value)
            except (ValueError,TypeError): pass
    return found


def unadopted_fields(bundle, intent):
    source="\n".join(intent.messages)
    json_values=user_json_values(intent.messages)
    numeric_fields=set()
    for encoded in json_values:
        value=json.loads(encoded)
        if isinstance(value,dict):
            for field,number in value.items():
                if isinstance(number,(int,float,bool)): numeric_fields.add((field,canonical(number)))
    missing=[]
    def grounded(value):
        if isinstance(value,dict):
            if not value or canonical(value) in json_values: return True
            if set(value)<= {"description","steps"}: return all(grounded(v) for v in value.values())
            return False
        if isinstance(value,list): return not value or canonical(value) in json_values or all(isinstance(v,str) and grounded(v) for v in value)
        if isinstance(value,str): return has_literal(value,source)
        return has_literal(json.dumps(value),source)
    for op in bundle.operations:
        baseline=intent.choices.get("case_definitions",{}).get(str(op.data.get("id")),{}) if op.kind=="case_revision" else {}
        for field,value in op.data.items():
            if field=="reason" or field.endswith("_id") or field=="id": continue
            if field=="publish":
                if value is True and not may_publish(intent.messages): missing.append(op.alias+".publish")
                continue
            if field in baseline and canonical(value)==canonical(baseline[field]):continue
            if isinstance(value,(int,float,bool)):
                labels={"planned_runs":"(?:计划执行次数|计划次数|执行次数|planned_runs)"}
                explicit_number=field in labels and re.search(labels[field]+r"\s*[:：=]?\s*"+re.escape(str(value))+r"(?![0-9.])",source)
                if (field,canonical(value)) not in numeric_fields and not explicit_number: missing.append(op.alias+"."+field)
                continue
            synonyms={"MODULE":("模块",),"WHOLE_MACHINE":("整机",),"SHARED":("共享","共用")}
            if isinstance(value,str) and value in synonyms and any(v in source for v in synonyms[value]): continue
            if not grounded(value): missing.append(op.alias+"."+field)
        if op.kind=="case_publish" and not may_publish(intent.messages): missing.append(op.alias+".publish")
    return missing


def describe_error(exc):
    if isinstance(exc,ApiError): return {"status":"needs_input","question":exc.message,"code":exc.code,"details":exc.details}
    if isinstance(exc,ValidationError): return {"status":"needs_input","question":"请补齐或修正字段后再次明确提交整组建档。","details":[{"field":".".join(map(str,e["loc"])),"message":e["msg"]} for e in exc.errors()]}
    code=getattr(getattr(exc,"orig",None),"sqlstate",None)
    return {"status":"needs_input","question":"编号或关联发生冲突，请核对后重提；本组没有部分保存。" if code in {"23505","23503","23514","P0001"} else "该对象不可编辑或参数不正确，请核对权限及对象标识后重提。","code":"setup_rejected"}


def commit_intent(session,bundle,intent,adopted_hash=None,publish_approved=False):
    if intent.job_id and not adopted_hash:
        active=session.execute(text("SELECT id FROM integration.assistant_job WHERE id=:id AND user_id=iam.current_user_id() AND input_hash=:hash AND state='RUNNING' AND NOT cancel_requested AND deadline>clock_timestamp() FOR UPDATE"),{"id":intent.job_id,'hash':intent.input_hash}).first()
        if active is None: raise ApiError(409,"assistant_task_ended","任务已停止，未发起新的业务写入；请核对已有回执。")
    evidence={"channel":"assistant","source_hash":source_hash(intent),"adopted_proposal_hash":adopted_hash,"publish_approved":publish_approved or may_publish(intent.messages)}
    evidence["base_case_versions"]={str(op.data["id"]):op.data["base_version_id"] for op in bundle.operations if op.kind=="case_revision" and op.data.get("base_version_id")}
    result=apply_bundle(session,bundle,evidence)
    return {"status":"created","question":"已按真实回执保存整组对象。","receipt":result}


def apply_tool(token, operations):
    settings=get_settings()
    key=grants.key(token)
    with lock: intent=intents.get(key)
    if intent is None: raise ApiError(403,"assistant_grant_invalid","无此建档授权。")
    grant=grants.claim(token,scope="setup")
    try:
        with SessionLocal() as session:
            principal=AuthService(settings,SecurityRepository(session)).resolve(grant.cookie)
            if principal is None or principal.id!=intent.user_id or principal.session_id!=intent.session_id or principal.must_change_password:
                raise ApiError(401,"authentication_required","发起会话已失效。")
            require_pilot(settings,principal.id,principal.role,principal.kind)
            session.commit()
        normalized=[]
        for raw in operations:
            op={**raw,"data":dict(raw.get("data",{}))}
            for field,value in list(op["data"].items()):
                if field=="base_version_id" and op["kind"]=="case_revision": continue
                if field=="id" or field.endswith("_id"):
                    op["data"][field]=resolve_identifier(value,field,op["kind"],intent)
            if op["kind"]=="case_revision":
                baseline=intent.choices.get("case_definitions",{}).get(str(op["data"].get("id")))
                if baseline:
                    if op["data"].get("base_version_id") not in (None,baseline["id"]): raise ApiError(409,"case_version_conflict","指定基准不是当前版本，请刷新核对后修订。")
                    retained={name:baseline[name] for name in CaseDefinition.model_fields if name in baseline and name not in {"reason","publish"}}
                    op["data"]={**retained,**op["data"],"base_version_id":baseline["id"]}
            op["data"]["reason"]="authenticated assistant setup"
            normalized.append(op)
        bundle=SetupBundle(request_key=intent.request_key,operations=normalized)
        intent.operation_kinds={op.alias:op.kind for op in bundle.operations}
        validate_bundle_shape(bundle)
        missing=unadopted_fields(bundle,intent)
        if missing:
            proposal_id=secrets.token_urlsafe(36)
            digest=hashlib.sha256(canonical(bundle.model_dump(mode="json")).encode()).hexdigest()
            with lock:
                now=time.monotonic()
                for expired in [k for k,v in proposals.items() if v.expires<=now]: proposals.pop(expired,None)
                if len(proposals)>=100: raise ApiError(429,"proposal_busy","待采用建议已满，请稍后重试。")
                proposals[proposal_id]=Proposal(intent,bundle,digest,now+300)
            intent.outcome={"status":"needs_adoption","question":"以下字段没有找到用户原始输入依据，请补充真实值，或明确采用这份建议后再创建。","unconfirmed_fields":missing,"proposal_id":proposal_id,"proposal_hash":digest,"operations":bundle.model_dump(mode="json")["operations"]}
        else:
            with SessionLocal() as session:
                # Resolve again immediately before writing, after any preparation.
                principal=AuthService(settings,SecurityRepository(session)).resolve(grant.cookie)
                if principal is None or principal.must_change_password: raise ApiError(401,"authentication_required","发起会话已失效。")
                require_pilot(settings,principal.id,principal.role,principal.kind)
                session.execute(text("SELECT iam.set_request_context(:user,:request,:reason)"),{"user":str(principal.id),"request":grant.request_id,"reason":"authenticated assistant setup"})
                session.execute(text("SELECT set_config('app.source_ip',coalesce(:ip,''),true)"),{"ip":grant.source_ip})
                intent.outcome=commit_intent(session,bundle,intent)
                session.commit()
    except (ApiError,ValidationError,DBAPIError,ValueError,KeyError,TypeError) as exc:
        intent.outcome=describe_error(exc)
    return intent.outcome


def adopt_proposal(payload,actor,session):
    require_pilot(get_settings(),actor.public_id,actor.role,actor.principal_kind)
    with lock: proposal=proposals.get(payload.proposal_id)
    if proposal is None or proposal.expires<=time.monotonic() or proposal.intent.user_id!=actor.public_id or proposal.intent.session_id!=actor.session_public_id or proposal.digest!=payload.proposal_hash:
        raise ApiError(403,"proposal_invalid","建议已过期、内容不一致或不属于当前会话。")
    if any(op.kind=="case_publish" or op.data.get("publish") is True for op in proposal.bundle.operations) and not (may_publish(proposal.intent.messages) or payload.publish_approved):
        raise ApiError(422,"publication_not_approved","采用建议不自动授权发布，请明确勾选发布所示版本。")
    outcome=commit_intent(session,proposal.bundle,proposal.intent,proposal.digest,payload.publish_approved)
    if proposal.intent.job_id:
        from app.services.assistant_chat import receipt,setup_cards
        outcome['receipt']=receipt(session,proposal.intent.job_id)
        cards=setup_cards(outcome)
        result={'reply':outcome['question'],'setup':outcome,'cards':cards,'business_status':'saved'}
        session.execute(text("UPDATE integration.assistant_job SET result=CAST(:result AS jsonb) WHERE id=:id"),{'id':proposal.intent.job_id,'result':canonical(result)})
        session.execute(text("""INSERT INTO integration.assistant_message(thread_id,user_id,job_id,role,body,cards)
        SELECT thread_id,user_id,id,'assistant',:body,CAST(:cards AS jsonb) FROM integration.assistant_job j WHERE id=:id
        AND NOT EXISTS(SELECT 1 FROM integration.assistant_message WHERE job_id=j.id AND role='assistant' AND body=:body)"""),{'id':proposal.intent.job_id,'body':outcome['question'],'cards':canonical(cards)})
    return outcome


async def run_command(payload,request,actor):
    settings=get_settings()
    require_pilot(settings,actor.public_id,actor.role,actor.principal_kind)
    if any(not message.strip() or len(message)>8000 for message in payload.messages) or sum(map(len,payload.messages))>24000:
        raise ApiError(422,"invalid_command","建档对话长度超出本项限制。")
    if not explicit_intent(payload.messages): return {"status":"needs_input","question":"请明确使用“创建/修订/发布/关联”等指令，并提供真实字段；咨询或否定指令不会保存对象。"}
    with SessionLocal() as session:
        set_context(session,actor)
        intent=Intent(actor.public_id,actor.session_public_id,payload.messages,payload.request_key,matching_choices(session,payload.messages))
        previous=session.execute(text("SELECT result,authorization_evidence FROM integration.setup_receipt WHERE user_id=iam.current_user_id() AND request_key=:key"),{"key":str(payload.request_key)}).mappings().first()
        if previous:
            if previous["authorization_evidence"].get("source_hash")!=source_hash(intent): raise ApiError(409,"idempotency_conflict","请求内容改变，请使用新的请求标识。")
            return {"status":"created","question":"已取得前次实际保存回执。","receipt":{**previous["result"],"replayed":True}}
        session.commit()
    secret=service_secret(settings)
    token,grant=grants.issue(request.cookies.get(settings.auth_cookie_name,""),actor,scope="setup")
    with lock: intents[grants.key(token)]=intent
    try:
        async with httpx.AsyncClient(timeout=25,trust_env=False,follow_redirects=False) as client:
            response=await client.post(settings.assistant_pi_url.rstrip("/")+"/internal/setup",json={"tool_grant":token,"messages":payload.messages,"choices":intent.choices},headers={"X-RP1-Bridge-Secret":secret})
        # Committed canonical receipts survive a failed final model response.
        if intent.outcome: return intent.outcome
        if response.status_code!=200: raise ApiError(502,"assistant_upstream_failed","建档模型未完成；可按原请求标识重试并先核对真实回执。")
        try:
            reply=response.json().get("reply","")
        except (ValueError,AttributeError): reply=""
        if not isinstance(reply,str) or not reply or len(reply)>16000 or any(v in reply for v in (token,secret,grant.cookie)):
            reply="尚未收到有效建档字段，请补充对象编号、名称、类型及必要上下文。"
        return {"status":"needs_input","question":reply}
    except httpx.HTTPError as exc:
        if intent.outcome: return intent.outcome
        raise ApiError(504,"assistant_timeout","建档链路超时，请先核对真实回执或使用原请求标识重试。") from exc
    finally:
        grants.revoke(token)
        with lock: intents.pop(grants.key(token),None)
