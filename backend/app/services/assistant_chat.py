"""Two FIFO workers; account-isolated durable history and authoritative receipts.

Cookies and tool capabilities only live in this one API process. After restart,
old pending work is interrupted rather than replayed under a replacement user.
"""
import asyncio
import hashlib
import json
import re
import logging
import time
from contextlib import contextmanager
from datetime import datetime,timezone
from uuid import UUID,uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from pydantic import ValidationError
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.services.auth import AuthService
from app.services.assistant import grants,require_pilot,service_secret
from app.services import setup_assistant as setup_flow
from app.services.assistant_queries import query
from app.services import assistant_billing as billing
from app.services.setup import canonical

TERMINAL={'COMPLETED','FAILED','TIMED_OUT','INTERRUPTED','CANCELLED'}
logger=logging.getLogger('rp1.assistant.tasks')


def require_runtime(actor):
    settings=get_settings();require_pilot(settings,actor.public_id,actor.role,actor.principal_kind)
    if not settings.assistant_runtime_enabled: raise ApiError(503,"assistant_runtime_disabled","助手任务入口尚未启用。")


@contextmanager
def actor_session(actor):
    with SessionLocal() as session:
        setup_flow.set_context(session,actor)
        session.execute(text("SELECT set_config('app.source_ip',coalesce(:ip,''),true)"),{"ip":actor.source_ip})
        yield session
        session.commit()


def own_job(session,identifier):
    row=session.execute(text("SELECT * FROM integration.assistant_job WHERE id=:id AND user_id=iam.current_user_id()"),{"id":identifier}).mappings().first()
    if row is None: raise ApiError(404,"assistant_job_not_found","任务不存在或不属于当前账号。")
    return dict(row)


def receipt(session,identifier):
    row=session.execute(text("SELECT result,request_payload FROM integration.setup_receipt WHERE request_key=:id AND user_id=iam.current_user_id()"),{"id":identifier}).mappings().first()
    return {**row['result'],'object_types':{op['alias']:op['kind'] for op in row['request_payload']['operations']}} if row else None


def setup_cards(outcome):
    if not outcome or not outcome.get('receipt'): return []
    mapping={'configuration':'asset','campaign':'planning','program':'planning','station':'benches','case':'test-case','asset':'asset','context':'planning'}
    cards=[]
    for alias,obj in outcome['receipt']['objects'].items():
        # Targets come from actual saved object types, never model-supplied URLs.
        kind=outcome['receipt'].get('object_types',{}).get(alias,alias)
        if kind.endswith('_update'):kind=kind.removesuffix('_update')
        if kind=='case_revision':kind='case'
        if kind=='case_publish':kind='case'
        view=mapping.get(kind,'planning')
        identifier=obj.get('id')
        if obj.get('case_id'):identifier=obj['case_id']
        if obj.get('context'): identifier=obj['context'].get('campaign_id');view='planning'
        cards.append({'type':'saved_object','title':'实际保存：'+alias,'source':'/api/v1/setup/receipts/'+outcome['receipt']['request_key'],
            'updated_at':datetime.now(timezone.utc).isoformat(),'data':obj,'target':{'view':view,'id':str(identifier) if identifier else None}})
    return cards


class ChatRuntime:
    def __init__(self):
        self.queue=None;self.workers=[];self.pending={};self.tokens={};self.running={};self.last_cleanup=0;self.maintenance_task=None

    async def start(self):
        if self.workers:return
        with SessionLocal.begin() as session:session.execute(text("SELECT integration.assistant_maintenance(true)"))
        self.queue=asyncio.Queue(maxsize=100)
        self.workers=[asyncio.create_task(self.consume()) for _ in range(2)]
        self.maintenance_task=asyncio.create_task(self.maintain())

    async def maintain(self):
        while True:
            await asyncio.sleep(60)
            with SessionLocal.begin() as session:session.execute(text("SELECT integration.assistant_maintenance(false)"))
            with setup_flow.lock:
                now=time.monotonic()
                for key in [k for k,v in setup_flow.proposals.items() if v.expires<=now]:setup_flow.proposals.pop(key,None)

    async def stop(self):
        tasks=[*self.workers,*self.running.values(),*([self.maintenance_task] if self.maintenance_task else [])]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.workers=[];self.pending.clear();self.tokens.clear();self.running.clear();self.queue=None

    async def submit(self,payload,request,actor):
        require_runtime(actor)
        if re.search(r"sk-[a-zA-Z0-9]{20,}|Authorization\s*:\s*Bearer",payload.message):
            raise ApiError(422,"assistant_secret_rejected","请勿在助手对话中发送密钥或登录凭据。")
        if self.queue is None: raise ApiError(503,"assistant_worker_unavailable","助手任务执行器未就绪。")
        if self.queue.full(): raise ApiError(429,"assistant_queue_full","站点等待队列已满，请稍后提交。")
        digest=hashlib.sha256(canonical(payload.model_dump(mode='json',exclude={'request_key'})).encode()).hexdigest()
        with actor_session(actor) as session:
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),{'key':'chat:'+str(payload.request_key)})
            previous=session.execute(text("SELECT id,request_hash,thread_id,state FROM integration.assistant_job WHERE id=:id"),{'id':payload.request_key}).mappings().first()
            if previous:
                if previous['request_hash']!=digest:raise ApiError(409,"idempotency_conflict","同一请求标识内容已改变，请换新请求标识。")
                return {**dict(previous),'replayed':True}
            thread=payload.thread_id
            if thread:
                if session.execute(text("SELECT id FROM integration.assistant_thread WHERE id=:id FOR UPDATE"),{'id':thread}).first() is None:raise ApiError(404,"assistant_thread_not_found","会话不存在或不属于当前账号。")
            else:
                thread=uuid4();session.execute(text("INSERT INTO integration.assistant_thread(id,user_id) VALUES(:id,iam.current_user_id())"),{'id':thread})
            session.execute(text("INSERT INTO integration.assistant_job(id,thread_id,user_id,request_hash,context) VALUES(:id,:thread,iam.current_user_id(),:hash,CAST(:context AS jsonb))"),{'id':payload.request_key,'thread':thread,'hash':digest,'context':canonical(payload.context.model_dump(mode='json',exclude_none=True))})
            message_id=session.execute(text("INSERT INTO integration.assistant_message(thread_id,user_id,job_id,role,body,context) VALUES(:thread,iam.current_user_id(),:job,'user',:body,CAST(:context AS jsonb)) RETURNING id"),{'thread':thread,'job':payload.request_key,'body':payload.message,'context':canonical(payload.context.model_dump(mode='json',exclude_none=True))}).scalar_one()
            # Freeze what actually exists at submission, under the thread lock.
            # Neither a later message nor a later tool result may enter this job.
            messages=[dict(x) for x in session.execute(text("SELECT id,role,body FROM integration.assistant_message WHERE thread_id=:thread AND id<=:boundary AND created_at>=now()-interval '30 days' ORDER BY id DESC LIMIT 20"),{'thread':thread,'boundary':message_id}).mappings()][::-1]
            authority=setup_flow.command_messages([m['body'] for m in messages if m['role']=='user'][-12:])
            choices=setup_flow.matching_choices(session,authority)
            snapshot={'messages':messages,'authorization_messages':authority,'choices':choices,'context':payload.context.model_dump(mode='json',exclude_none=True)}
            encoded=canonical(snapshot)
            session.execute(text("UPDATE integration.assistant_job SET input_message_id=:message,input_snapshot=CAST(:snapshot AS jsonb),input_hash=:hash WHERE id=:id"),{'id':payload.request_key,'message':message_id,'snapshot':encoded,'hash':hashlib.sha256(encoded.encode()).hexdigest()})
            session.execute(text("UPDATE integration.assistant_thread SET updated_at=now() WHERE id=:id"),{'id':thread})
        self.pending[str(payload.request_key)]=(actor,request.cookies.get(get_settings().auth_cookie_name,''))
        self.queue.put_nowait(str(payload.request_key))
        return {'id':payload.request_key,'thread_id':thread,'state':'QUEUED','replayed':False}

    async def consume(self):
        while True:
            identifier=await self.queue.get()
            pair=self.pending.pop(identifier,None)
            if pair:
                task=asyncio.create_task(self.run(identifier,*pair));self.running[identifier]=task
                try:await task
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():raise
                except Exception as exc:logger.error('assistant_task_metadata_failed job=%s kind=%s sqlstate=%s',identifier,type(exc).__name__,getattr(getattr(exc,'orig',None),'sqlstate',None))
                finally:self.running.pop(identifier,None)
            self.queue.task_done()
            if asyncio.current_task().cancelling():return

    async def run(self,identifier,actor,cookie):
        settings=get_settings();token=None;intent=None;state='FAILED';reply='助手未完成，请核对真实回执。';cards=[];outcome=None;started=time.monotonic()
        try:
            with actor_session(actor) as session:
                job=own_job(session,identifier)
                if job['state']!='QUEUED':
                    state=job['state'];reply='任务已取消，未发起模型。';return
                session.execute(text("UPDATE integration.assistant_job SET state='RUNNING',started_at=now(),deadline=now()+:seconds*interval '1 second' WHERE id=:id"),{'id':identifier,'seconds':settings.assistant_task_seconds})
                snapshot=job.get('input_snapshot')
                if not snapshot or hashlib.sha256(canonical(snapshot).encode()).hexdigest()!=job.get('input_hash'):
                    state='INTERRUPTED';reply='任务缺少可信输入快照，请重新提交；已有业务回执保留。';return
                messages=[{'role':m['role'],'body':m['body']} for m in snapshot['messages']]
                user_messages=snapshot.get('authorization_messages') or [m['body'] for m in messages if m['role']=='user'][-12:]
                choices=snapshot['choices']
            token,grant=grants.issue(cookie,actor,scope='setup',ttl=settings.assistant_task_seconds)
            intent=setup_flow.Intent(actor.public_id,actor.session_public_id,user_messages,UUID(identifier),choices,job_id=UUID(identifier),input_hash=job['input_hash'])
            with setup_flow.lock:setup_flow.intents[grants.key(token)]=intent
            self.tokens[grants.key(token)]={'id':identifier,'actor':actor,'context':job['context'],'cards':cards,'write_count':0,'reads':0}
            async with asyncio.timeout(max(.001,settings.assistant_task_seconds-(time.monotonic()-started))):
                async with httpx.AsyncClient(timeout=settings.assistant_task_seconds,trust_env=False,follow_redirects=False) as client:
                    response=await client.post(settings.assistant_pi_url.rstrip('/')+'/internal/chat',json={'tool_grant':token,'messages':messages,'choices':choices,'context':job['context'],'timeout_ms':settings.assistant_task_seconds*1000},headers={'X-RP1-Bridge-Secret':service_secret(settings)})
            outcome=intent.outcome
            if response.status_code!=200:
                state='TIMED_OUT' if response.status_code==504 else 'FAILED'
                reply='模型链路中断；已核对实际提交回执，不会自动撤销已保存对象。'
            else:
                data=response.json();reply=data.get('reply','')
                if not isinstance(reply,str) or not reply or len(reply)>16000 or any(v in reply for v in (token,cookie,service_secret(settings))):raise ValueError('invalid reply')
                state='COMPLETED'
        except (httpx.TimeoutException,asyncio.TimeoutError):state='TIMED_OUT';reply='任务达到时限，已停止新增调用并核对真实回执。'
        except asyncio.CancelledError:state='CANCELLED';reply='任务已停止，已提交结果保留；请核对真实回执。'
        except Exception as exc:
            # Provider payloads and credentials are deliberately not logged.
            logger.warning('assistant_task_failed job=%s elapsed_ms=%d kind=%s',identifier,int((time.monotonic()-started)*1000),type(exc).__name__)
            state='FAILED';reply='助手处理失败，已核对真实回执；既有业务可继续使用。'
        finally:
            if token:
                self.tokens.pop(grants.key(token),None);grants.revoke(token)
                with setup_flow.lock:setup_flow.intents.pop(grants.key(token),None)
            if intent and intent.outcome:outcome=intent.outcome
            if outcome and outcome.get('receipt'):
                if intent and intent.operation_kinds:outcome['receipt']['object_types']=intent.operation_kinds
                reply=outcome.get('question','已核对实际回执。')+' '+reply
            if outcome:cards+=setup_cards(outcome)
            if state=='COMPLETED' and intent and setup_flow.explicit_intent(intent.messages) and not outcome:
                outcome={'status':'needs_input','question':'本次未取得保存回执，尚未保存。请补充说明后重新提交，或使用普通建档页面。'}
                reply=outcome['question']
            business_status='saved' if outcome and outcome.get('receipt') else outcome.get('status') if outcome else 'answered' if state=='COMPLETED' else 'not_saved'
            result={'reply':reply,'setup':outcome,'cards':cards,'business_status':business_status}
            # Resolving the original login remains mandatory for tools, but final
            # metadata and receipt reconciliation must survive account revocation.
            with SessionLocal.begin() as session:
                session.execute(text("SELECT integration.assistant_finish(:id,:user,:state,CAST(:result AS jsonb))"),{'id':identifier,'user':str(actor.public_id),'state':state,'result':canonical(result)})
            with SessionLocal.begin() as session:
                session.execute(text("SELECT integration.assistant_abandon(:id)"),{'id':identifier})
                if time.monotonic()-self.last_cleanup>3600:
                    session.execute(text("SELECT integration.assistant_maintenance(false)"));self.last_cleanup=time.monotonic()

    def authorize(self,token,allow_terminal=False):
        grant=grants.access(token);item=self.tokens.get(grants.key(token))
        if not item:raise ApiError(403,"assistant_grant_invalid","无此助手任务授权。")
        with SessionLocal() as session:
            principal=AuthService(get_settings(),SecurityRepository(session)).resolve(grant.cookie)
            if principal is None or principal.id!=grant.user_id or principal.session_id!=grant.session_id or principal.must_change_password:raise ApiError(401,"authentication_required","原登录会话已失效。")
            require_pilot(get_settings(),principal.id,principal.role,principal.kind)
        with actor_session(item['actor']) as session:
            job=own_job(session,item['id'])
            if not allow_terminal and (job['state']!='RUNNING' or job['cancel_requested'] or not job['deadline'] or job['deadline']<=datetime.now(timezone.utc)):raise ApiError(409,"assistant_task_ended","任务已结束，不再发起新工具。")
        return item

    def read(self,token,payload):
        item=self.authorize(token);item['reads']+=1
        if item['reads']>12:raise ApiError(409,"assistant_tool_limit","单任务工具调用过多。")
        with actor_session(item['actor']) as session:card=query(session,payload.tool,payload.arguments,item['context'])
        item['cards'].append(json.loads(canonical(card)))
        return card

    def meter(self,token,payload):
        item=self.authorize(token,allow_terminal=payload.phase=='settle')
        with actor_session(item['actor']) as session:
            return billing.reserve(session,item['id'],payload.sequence,get_settings()) if payload.phase=='reserve' else billing.settle(session,item['id'],payload.sequence,payload.usage)

    def allow_write(self,token):
        item=self.authorize(token)
        with setup_flow.lock:intent=setup_flow.intents.get(grants.key(token))
        if intent is None or not setup_flow.explicit_intent(intent.messages):raise ApiError(422,"explicit_action_required","建档需要当前用户明确指令。")
        return item


runtime=ChatRuntime()


def list_threads(actor):
    require_runtime(actor)
    with actor_session(actor) as session:
        return [dict(x) for x in session.execute(text("SELECT id,created_at,updated_at FROM integration.assistant_thread WHERE updated_at>=now()-interval '30 days' ORDER BY updated_at DESC LIMIT 50")).mappings()]


def history(actor,thread):
    require_runtime(actor)
    with actor_session(actor) as session:
        if session.execute(text("SELECT id FROM integration.assistant_thread WHERE id=:id"),{'id':thread}).first() is None:raise ApiError(404,"assistant_thread_not_found","会话不存在或不可访问。")
        rows=[dict(x) for x in session.execute(text("SELECT id,job_id,role,body,context,cards,created_at FROM integration.assistant_message WHERE thread_id=:id AND created_at>=now()-interval '30 days' ORDER BY id DESC LIMIT 200"),{'id':thread}).mappings()][::-1]
        jobs=[dict(x) for x in session.execute(text("SELECT id,state,created_at FROM integration.assistant_job WHERE thread_id=:id AND created_at>=now()-interval '30 days' ORDER BY created_at DESC LIMIT 10"),{'id':thread}).mappings()]
        return {'messages':rows,'jobs':jobs}


def job_status(actor,identifier):
    require_runtime(actor)
    with actor_session(actor) as session:
        job=own_job(session,identifier)
        if (datetime.now(timezone.utc)-job['created_at']).total_seconds()>30*86400:
            job['result']=None;job['context']={}
        actual=receipt(session,identifier)
        if actual:
            job['receipt']=actual
            old=job.get('result') or {}
            job['result']={**old,'reply':old.get('reply') or '已取得真实提交回执。','setup':{'status':'created','receipt':actual},'cards':[c for c in old.get('cards',[]) if c.get('type')!='saved_object']+setup_cards({'receipt':actual})}
        if actual:job['result']['business_status']='saved'
        job.pop('user_id',None);job.pop('request_hash',None);job.pop('input_snapshot',None)
        return job


def cancel_job(actor,identifier):
    require_runtime(actor)
    with actor_session(actor) as session:
        job=own_job(session,identifier)
        if job['state'] not in TERMINAL:
            session.execute(text("UPDATE integration.assistant_job SET cancel_requested=true,state=CASE WHEN state='QUEUED' THEN 'CANCELLED' ELSE state END,finished_at=CASE WHEN state='QUEUED' THEN now() ELSE finished_at END WHERE id=:id"),{'id':identifier})
    task=runtime.running.get(str(identifier))
    if task:task.get_loop().call_soon_threadsafe(task.cancel)
    return {'id':identifier,'cancel_requested':True,'receipt':job_status(actor,identifier).get('receipt')}
