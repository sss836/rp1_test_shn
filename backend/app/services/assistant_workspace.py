"""Owner-bound chat UI metadata, durable progress, and diagnostic-only steering.

No network/model wait is performed while a database transaction is open.
Session history remains data; extra input never adds business-write authority.
"""
import json
import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from app.core.errors import ApiError
from app.services.assistant_privacy import redact_text, public_evidence
from app.services.setup import canonical


def runtime_services():
    # Late import avoids the runtime/workspace cycle.
    from app.services.assistant_chat import actor_session, own_job, require_runtime
    return actor_session, own_job, require_runtime


def thread_title(message):
    return re.sub(r'\s+', ' ', redact_text(message)).strip()[:64] or '附件诊断'


def context_payload(context):
    value=context.model_dump(mode='json',exclude_none=True)
    if not value.get('references'):value.pop('references',None)
    return value


def request_digest_payload(payload):
    value=payload.model_dump(mode='json',exclude={'request_key'})
    if value.get('after_job_id') is None:value.pop('after_job_id',None)
    if not value.get('context',{}).get('references'):value.get('context',{}).pop('references',None)
    return value


def append_event(session, job_id, kind, data):
    if kind not in {'text', 'tool', 'state', 'input'} or not isinstance(data, dict):
        raise ApiError(422, 'assistant_event_invalid', '任务进展格式无效。')
    safe = public_evidence(data)
    if kind=='text' and isinstance(safe.get('text'),str) and len(safe['text'].encode())>16000:
        raw=safe['text'].encode()[:16000]
        safe={**safe,'text':raw.decode('utf-8',errors='ignore'),'display_truncated':True}
    encoded = canonical(safe)
    if len(encoded.encode()) > 18000:
        raise ApiError(422, 'assistant_event_large', '任务进展超过显示范围。')
    return session.execute(text("""INSERT INTO integration.assistant_event(job_id,user_id,kind,data)
        VALUES(:id,iam.current_user_id(),:kind,CAST(:data AS jsonb)) RETURNING seq"""),
        {'id': job_id, 'kind': kind, 'data': encoded}).scalar_one()


def record_progress(actor, job_id, event):
    actor_session, own_job, _ = runtime_services()
    # Only allowlisted display fields, never arbitrary model blocks or thoughts.
    kind = event.get('kind')
    data = event.get('data') or {}
    if kind == 'text':
        body = data.get('text')
        if not isinstance(body, str) or len(body) > 16000:
            raise ApiError(422, 'assistant_event_invalid', '任务文本超出范围。')
        safe = {'text': redact_text(body), 'round': max(0, min(6, int(data.get('round', 0))))}
    elif kind == 'tool':
        tool = data.get('tool', '')
        if not isinstance(tool, str) or not re.fullmatch(r'[a-z_]{1,50}', tool):
            return
        safe = {'tool': tool, 'status': 'completed' if data.get('status') == 'completed' else 'running'}
    elif kind == 'state':
        safe = {'state': 'RUNNING'}
    else:
        return
    with actor_session(actor) as session:
        job = own_job(session, job_id)
        if job['state'] != 'RUNNING' or job.get('cancel_requested'):
            return
        append_event(session, job_id, kind, safe)


def events(actor, job_id, after):
    actor_session, own_job, require_runtime = runtime_services()
    require_runtime(actor)
    with actor_session(actor) as session:
        job = own_job(session, job_id)
        rows = [dict(r) for r in session.execute(text("""SELECT seq,kind,data,created_at
          FROM integration.assistant_event WHERE job_id=:id AND seq>:after
          AND created_at>=now()-interval '30 days' ORDER BY seq LIMIT 201"""),
          {'id': job_id, 'after': after}).mappings()]
        page=rows[:200]
        return {'items': page, 'next_seq': page[-1]['seq'] if page else after, 'state': job['state'],'has_more':len(rows)>200}


def rename_thread(actor, identifier, title):
    actor_session, _, require_runtime = runtime_services()
    require_runtime(actor)
    with actor_session(actor) as session:
        row = session.execute(text("""UPDATE integration.assistant_thread SET title=:title,title_updated_at=now()
          WHERE id=:id AND deleted_at IS NULL AND updated_at>=now()-interval '30 days'
          RETURNING id,title,updated_at"""), {'id': identifier, 'title': re.sub(r'\s+',' ',redact_text(title)).strip()[:120]}).mappings().first()
        if row is None:
            raise ApiError(404, 'assistant_thread_not_found', '会话不存在或不可访问。')
        return dict(row)


def delete_thread(actor, identifier):
    actor_session, _, require_runtime = runtime_services()
    require_runtime(actor)
    with actor_session(actor) as session:
        row = session.execute(text('SELECT id FROM integration.assistant_thread WHERE id=:id AND deleted_at IS NULL FOR UPDATE'), {'id': identifier}).first()
        if row is None:
            raise ApiError(404, 'assistant_thread_not_found', '会话不存在或不可访问。')
        if session.execute(text("SELECT 1 FROM integration.assistant_job WHERE thread_id=:id AND state IN ('QUEUED','RUNNING','WAITING_PERMISSION') LIMIT 1"), {'id': identifier}).first():
            raise ApiError(409, 'assistant_thread_busy', '请先停止当前会话的任务并核对回执，再删除会话。')
        session.execute(text('UPDATE integration.assistant_thread SET deleted_at=now() WHERE id=:id'), {'id': identifier})


def submit_input(actor, job_id, payload):
    actor_session, own_job, require_runtime = runtime_services()
    require_runtime(actor)
    if payload.expected_job_id != job_id or not actor.session_public_id:
        raise ApiError(409, 'assistant_input_generation', '当前任务已改变，请重新核对后补充。')
    if re.search(r'sk-[a-zA-Z0-9]{20,}|Authorization\s*:\s*Bearer', payload.message):
        raise ApiError(422, 'assistant_secret_rejected', '请勿在补充中发送密钥或登录凭据。')
    with actor_session(actor) as session:
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'assistant-input:' + str(job_id)})
        job = own_job(session, job_id)
        if str(job.get('origin_session_id')) != str(actor.session_public_id):
            raise ApiError(403, 'assistant_input_session', '只能从发起当前任务的原登录会话补充。')
        old = session.execute(text('SELECT job_id,message,session_id FROM integration.assistant_input WHERE request_key=:key'), {'key': payload.request_key}).mappings().first()
        if old:
            if str(old['job_id']) != str(job_id) or old['message'] != payload.message or str(old['session_id']) != str(actor.session_public_id):
                raise ApiError(409, 'assistant_input_conflict', '相同补充标识的内容不能改变。')
            return {'accepted': True, 'replayed': True, 'mode': 'steer'}
        if job['state'] != 'RUNNING' or job.get('cancel_requested') or job.get('steering_closed') or job.get('rounds',0)>=6 or not job.get('deadline') or job['deadline'] <= datetime.now(timezone.utc):
            raise ApiError(409, 'assistant_task_ended', '任务已结束；请把补充作为下一条问题发送。')
        if session.execute(text('SELECT count(*) FROM integration.assistant_input WHERE job_id=:id'), {'id': job_id}).scalar_one() >= 8:
            raise ApiError(409, 'assistant_input_limit', '本次补充已达上限，请等待答复后再发送。')
        session.execute(text("""INSERT INTO integration.assistant_input(request_key,job_id,user_id,session_id,message)
          VALUES(:key,:id,iam.current_user_id(),:session,:message)"""),
          {'key': payload.request_key, 'id': job_id, 'session': actor.session_public_id, 'message': payload.message})
        append_event(session, job_id, 'input', {'message': redact_text(payload.message), 'mode': 'steer', 'write_authority': 'unchanged_read_only'})
        return {'accepted': True, 'replayed': False, 'mode': 'steer'}


def pending_inputs(item, closing=False):
    actor_session, own_job, _ = runtime_services()
    with actor_session(item['actor']) as session:
        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'), {'key': 'assistant-input:' + str(item['id'])})
        own_job(session, item['id'])
        rows = [dict(r) for r in session.execute(text("""UPDATE integration.assistant_input SET delivered_at=now()
          WHERE job_id=:id AND delivered_at IS NULL RETURNING request_key,message,created_at"""), {'id': item['id']}).mappings()]
        rows.sort(key=lambda r:(r['created_at'],str(r['request_key'])))
        # Closing and accepting an input share one lock: either this poll sees
        # it and continues, or the UI receives a conflict and retains its draft.
        if closing and not rows:
            session.execute(text('UPDATE integration.assistant_job SET steering_closed=true WHERE id=:id'), {'id':item['id']})
        return {'messages': [{'id': str(r['request_key']), 'text': redact_text(r['message'])} for r in rows]}


REFERENCE_QUERIES = {
    'assets': ('test.asset', 'asset_code', 'asset_kind'),
    'stations': ('test.station', 'station_code', 'name'),
    'executions': ('test.research_execution', 'execution_code', 'status'),
}


def reference_rows(session, kind, q='', identifier=None):
    if kind not in REFERENCE_QUERIES:
        raise ApiError(422, 'assistant_reference_kind', '请选择样品、工位或执行记录。')
    table, label, detail = REFERENCE_QUERIES[kind]  # Only fixed SQL identifiers.
    rows = session.execute(text(f"""SELECT public_id AS id,{label} AS label,{detail} AS detail
      FROM {table} WHERE (CAST(:id AS uuid) IS NULL OR public_id=CAST(:id AS uuid))
      AND ({label} ILIKE :q OR CAST({detail} AS text) ILIKE :q)
      ORDER BY {label},public_id LIMIT 20"""),
      {'id': str(identifier) if identifier else None, 'q': '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'}).mappings()
    return [{'kind': kind, **dict(r)} for r in rows]


def references(actor, kind, q):
    actor_session, _, require_runtime = runtime_services()
    require_runtime(actor)
    with actor_session(actor) as session:
        return {'items': reference_rows(session, kind, q)}


def validate_references(session, selected):
    for ref in selected:
        actual=reference_rows(session, ref.kind, identifier=ref.id)
        if not actual:
            raise ApiError(404, 'assistant_reference_not_found', '所选对象已不可访问，请重新选择。')
        ref.label=str(actual[0]['label'])


def has_steering(session, job_id):
    return bool(session.execute(text('SELECT 1 FROM integration.assistant_input WHERE job_id=:id LIMIT 1'), {'id': job_id}).first())


async def consume_bridge(response,on_progress):
    """Consume bounded NDJSON records; legacy JSON remains compatible.

    The result record is authoritative; EOF without one is a failure. A stream
    never executes tools, changes the user's snapshot, or implies a DB commit.
    """
    if response.status_code!=200 or 'application/x-ndjson' not in response.headers.get('content-type',''):
        await response.aread()
        try:data=response.json()
        except ValueError:data={}
        return response.status_code,data if isinstance(data,dict) else {}
    buffer=b'';result=None;records=0
    async for chunk in response.aiter_bytes():
        buffer+=chunk
        if len(buffer)>256*1024:raise ValueError('oversized progress record')
        while b'\n' in buffer:
            line,buffer=buffer.split(b'\n',1)
            if not line:continue
            records+=1
            if records>2048 or result is not None:raise ValueError('invalid progress sequence')
            event=json.loads(line)
            if not isinstance(event,dict):raise ValueError('invalid progress record')
            if event.get('kind')=='result':
                result=event.get('data')
                if not isinstance(result,dict):raise ValueError('invalid result')
            elif event.get('kind')=='error':
                error=event.get('data') or {}
                return 502,{'error':error.get('error','tool_failed')}
            else:
                await on_progress(event)
    if buffer.strip() or result is None:raise ValueError('incomplete progress stream')
    return 200,result
