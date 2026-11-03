"""Durable diagnostic feedback, published FAQ, reports and paired repair proposals.

This module never opens a local file, runs a command or operates a device. The
paired HMI must independently authorize and compare bytes before any write.
"""
from __future__ import annotations

from contextlib import contextmanager
import csv
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import hmac
import io
import json
from pathlib import PurePosixPath
import re
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.errors import ApiError
from app.services.assistant_privacy import public_evidence, redact_text

REPAIR_DIGEST_FIELDS = ('id', 'job_id', 'actor_id', 'role', 'installation_id', 'window_id',
                        'kind', 'path', 'expected_sha256', 'original', 'replacement', 'reason')
TERMINAL_REPAIRS = {'applied', 'conflict', 'failed', 'rejected'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False, default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _human(actor, *, admin=False, session=False):
    from app.services.assistant_chat import require_runtime
    require_runtime(actor)
    if (actor.principal_kind != 'HUMAN' or actor.role not in {'SYSTEM_ADMIN', 'TEST_EXECUTOR'}
            or (admin and actor.role != 'SYSTEM_ADMIN')):
        raise ApiError(403, 'assistant_support_forbidden', '此操作需要相应的执行员或管理员权限。')
    if session and not actor.session_public_id:
        raise ApiError(403, 'repair_session_required', '本机修复需要真实登录会话。')


@contextmanager
def support_session(actor):
    from app.services.assistant_chat import actor_session
    with actor_session(actor) as session:
        session.execute(text("SELECT set_config('app.session_public_id',:sid,true)"),
                        {'sid': str(actor.session_public_id or '')})
        yield session


def _lock(session, namespace, request_key):
    session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
                    {'key': namespace + ':' + str(request_key)})


def _job(session, identifier):
    from app.services.assistant_chat import own_job
    job = own_job(session, identifier)
    if job['created_at'] < datetime.now(timezone.utc) - timedelta(days=30):
        raise ApiError(410, 'assistant_evidence_expired', '个人聊天证据已超过30天保留期。')
    return job


def create_feedback(actor, payload):
    _human(actor)
    data = payload.model_dump(mode='json') if hasattr(payload, 'model_dump') else dict(payload)
    request_hash = digest({k: v for k, v in data.items() if k != 'request_key'})
    with support_session(actor) as session:
        job = _job(session, data['job_id'])
        _lock(session, 'assistant-feedback', data['request_key'])
        old = session.execute(text('SELECT * FROM integration.assistant_feedback WHERE id=:id'),
                              {'id': data['request_key']}).mappings().first()
        if old:
            if old['request_hash'] != request_hash:
                raise ApiError(409, 'idempotency_conflict', '反馈请求标识对应的内容已改变。')
            return {**dict(old), 'replayed': True}
        # Store an explicitly entered, redacted note and bounded provenance,
        # never automatically copy the personal question or model answer.
        note = redact_text(data.get('note', ''))
        if len(note) > 4000:
            raise ApiError(422, 'feedback_note_size', '脱敏后的反馈超过4000字，请缩短后重试。')
        hmi = (job.get('context') or {}).get('hmi') or {}
        evidence = public_evidence({'job_id': str(job['id']), 'input_sha256': job.get('input_hash'),
                                   'running_version': hmi.get('running_version'),
                                   'state': job['state'], 'source': 'assistant_user_feedback'})
        row = session.execute(text('''INSERT INTO integration.assistant_feedback
            (id,user_id,job_id,request_hash,rating,note,share_for_faq,evidence)
            VALUES(:id,iam.current_user_id(),:job,:hash,:rating,:note,:share,CAST(:evidence AS jsonb)) RETURNING *'''),
            {'id': data['request_key'], 'job': data['job_id'], 'hash': request_hash,
             'rating': data['rating'], 'note': note, 'share': data.get('share_for_faq', False),
             'evidence': canonical(evidence)}).mappings().one()
        return {**dict(row), 'replayed': False}


def list_feedback(actor, *, shared=False):
    _human(actor, admin=shared)
    with support_session(actor) as session:
        where = 'share_for_faq' if shared else 'user_id=iam.current_user_id()'
        rows = session.execute(text('SELECT id,job_id,rating,note,share_for_faq,evidence,created_at '
                                   'FROM integration.assistant_feedback WHERE ' + where +
                                   ' ORDER BY created_at DESC,id LIMIT 100')).mappings()
        return {'items': [dict(row) for row in rows]}


def publish_faq(actor, payload):
    _human(actor, admin=True)
    data = payload.model_dump(mode='json', exclude_none=True) if hasattr(payload, 'model_dump') else dict(payload)
    request_hash = digest({k: v for k, v in data.items() if k != 'request_key'})
    question, answer = redact_text(data['question']).strip(), redact_text(data['answer']).strip()
    if not question or not answer:
        raise ApiError(422, 'faq_empty', 'FAQ问题和答案不能为空。')
    if len(question) > 2000 or len(answer) > 12000:
        raise ApiError(422, 'faq_text_size', '脱敏后的FAQ超过问题2000字或答案12000字的限制。')
    with support_session(actor) as session:
        _lock(session, 'assistant-faq-request', data['request_key'])
        old = session.execute(text('SELECT * FROM integration.assistant_faq_version WHERE request_key=:id'),
                              {'id': data['request_key']}).mappings().first()
        if old:
            if old['request_hash'] != request_hash:
                raise ApiError(409, 'idempotency_conflict', 'FAQ请求标识对应的内容已改变。')
            return {**dict(old), 'replayed': True}
        source = session.execute(text('SELECT id FROM integration.assistant_feedback WHERE id=:id '
                                      'AND (user_id=iam.current_user_id() OR share_for_faq)'),
                                 {'id': data['feedback_id']}).first()
        if not source:
            raise ApiError(404, 'feedback_not_shared', '反馈不存在或尚未允许整理为共享FAQ。')
        identifier = data.get('faq_id') or data['request_key']
        _lock(session, 'assistant-faq-version', identifier)
        previous = session.execute(text('SELECT max(revision) FROM integration.assistant_faq_version WHERE id=:id'),
                                   {'id': identifier}).scalar()
        if data.get('faq_id') and previous is None:
            raise ApiError(404, 'faq_not_found', '要追加版本的FAQ不存在。')
        if data.get('faq_id') and data.get('expected_revision') != previous:
            raise ApiError(409, 'faq_revision_conflict', 'FAQ已被其他发布更新，或未提供修订基线；请重新读取最新版本。')
        if not data.get('faq_id') and data.get('expected_revision') is not None:
            raise ApiError(422, 'faq_revision_without_id', '新建FAQ不应携带旧版本基线。')
        row = session.execute(text('''INSERT INTO integration.assistant_faq_version
            (id,revision,request_key,request_hash,source_feedback_id,published_by,question,answer)
            VALUES(:id,:revision,:key,:hash,:source,iam.current_user_id(),:question,:answer) RETURNING *'''),
            {'id': identifier, 'revision': (previous or 0) + 1, 'key': data['request_key'],
             'hash': request_hash, 'source': data['feedback_id'], 'question': question, 'answer': answer}).mappings().one()
        return {**dict(row), 'replayed': False}


def query_faq(actor, q=''):
    _human(actor)
    q = str(q).strip()
    if len(q) > 500:
        raise ApiError(422, 'faq_query_size', 'FAQ查询最长500字。')
    with support_session(actor) as session:
        rows = session.execute(text('''SELECT id,revision,question,answer,source_feedback_id,published_at
            FROM (SELECT DISTINCT ON(id) * FROM integration.assistant_faq_version ORDER BY id,revision DESC) f
            WHERE :q='' OR position(lower(:q) in lower(question||' '||answer))>0
            ORDER BY published_at DESC,id LIMIT 50'''), {'q': q}).mappings()
        return {'items': [dict(row) for row in rows]}


def search_shared_faq(actor, arguments):
    if not isinstance(arguments, dict) or set(arguments) - {'query','limit'}:
        raise ApiError(422, 'faq_arguments', 'FAQ检索仅接受query和limit。')
    limit=arguments.get('limit',20)
    if type(limit) is not int or not 1<=limit<=20:
        raise ApiError(422,'faq_arguments','FAQ返回数量须为1至20。')
    result=query_faq(actor,arguments.get('query',''))
    result={**result,'items':result['items'][:limit]}
    return {'type': 'shared_faq', 'title': '管理员已发布FAQ', 'source': '/api/v1/assistant/faq',
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'data': public_evidence(result)}


def report_document(job):
    snapshot = job.get('input_snapshot') or {}
    result = job.get('result') or {}
    cards = result.get('cards') or []
    return public_evidence({'report_type': 'derived_diagnostic_report', 'formal_archive': False,
        'report_generator_version': 'assistant-support-report-v1',
        'notice': '派生诊断报告；不代表正式测试归档、审批或真实设备验收。',
        'job_id': str(job['id']), 'thread_id': str(job['thread_id']), 'state': job['state'],
        'created_at': job['created_at'].isoformat(), 'finished_at': str(job.get('finished_at') or ''),
        'input_sha256': job.get('input_hash'), 'result_sha256': digest(result),
        'after_job_id': str(job['after_job_id']) if job.get('after_job_id') else None,
        'execution_sources': job.get('execution_sources') or {},
        'scope': job.get('context') or {}, 'attachments': snapshot.get('attachments') or [],
        'read_coverage': result.get('attachment_read_coverage') or {'status': 'not_recorded', 'complete': False},
        'sources': [{'source': c.get('source'), 'updated_at': c.get('updated_at'),
                     'version': (c.get('data') or {}).get('version') if isinstance(c.get('data'), dict) else None,
                     'algorithm_version': (c.get('data') or {}).get('algorithm_version', 'not_recorded')
                         if isinstance(c.get('data'), dict) else 'not_recorded'}
                    for c in cards], 'result': result})


def csv_safe(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    string = canonical(value) if isinstance(value, (dict, list, tuple)) else str(value if value is not None else '')
    # Spreadsheet applications may ignore whitespace before formula markers.
    return "'" + string if string.lstrip(' \t\r\n\ufeff').startswith(('=', '+', '-', '@')) else string


def render_report(document, format):
    if format == 'json':
        return json.dumps(document, ensure_ascii=False, indent=2, default=str).encode(), 'application/json'
    if format == 'csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['section', 'source', 'source_sha256', 'scope', 'column', 'unit', 'metric', 'value'])
        def row(*cells):
            writer.writerow([csv_safe(cell) for cell in cells])
        for key, value in document.items():
            if key != 'result':
                row('metadata', document.get('job_id'), '', document.get('scope'), '', '', key, value)
        result = document.get('result') or {}
        row('diagnosis', document.get('job_id'), '', document.get('scope'), '', '', 'reply', result.get('reply'))
        for card in result.get('cards') or []:
            data = card.get('data') if isinstance(card.get('data'), dict) else {}
            scope = card.get('read_scope') or data.get('range') or card.get('scope') or ''
            columns = data.get('columns')
            if card.get('operation') == 'csv_statistics' and isinstance(columns, list):
                row('csv_statistics', card.get('source'), card.get('sha256') or card.get('source_sha256'),
                    scope, '', '', 'algorithm_version', data.get('algorithm_version', 'not_recorded'))
                for column in columns:
                    if not isinstance(column, dict):
                        continue
                    for metric, value in column.items():
                        if metric not in {'name', 'unit', 'index'}:
                            row('csv_statistics', card.get('source'), card.get('sha256') or card.get('source_sha256'),
                                scope, column.get('name'), column.get('unit'), metric, value)
            else:
                row('evidence', card.get('source'), card.get('sha256') or card.get('source_sha256'),
                    scope, '', '', card.get('type'), card)
        return ('\ufeff' + output.getvalue()).encode('utf-8'), 'text/csv; charset=utf-8'
    if format != 'markdown':
        raise ApiError(422, 'report_format', '报告格式应为markdown、csv或json。')
    # Keep model-produced content as text, not active HTML in the downloaded report.
    body = str((document.get('result') or {}).get('reply') or '本任务没有答复正文。')
    lines = ['# RP1 派生诊断报告', '', document['notice'], '', '## 诊断结论', '', body, '', '## 来源、范围与校验', '']
    lines += ['```json', json.dumps({k: v for k, v in document.items() if k != 'result'},
                                   ensure_ascii=False, indent=2, default=str).replace('```', '\\u0060\\u0060\\u0060'), '```', '',
              '## 证据与回执', '', '```json', json.dumps(document.get('result'), ensure_ascii=False, indent=2,
                  default=str).replace('```', '\\u0060\\u0060\\u0060'), '```', '']
    return '\n'.join(lines).encode(), 'text/markdown; charset=utf-8'


def download_report(actor, identifier, format):
    _human(actor)
    with support_session(actor) as session:
        document = report_document(_job(session, identifier))
    raw, mime = render_report(document, format)
    return raw, mime, hashlib.sha256(raw).hexdigest()


def validate_repair_content(kind, path, original, replacement, role):
    if not isinstance(path, str) or '\\' in path or any(ord(c) < 32 for c in path):
        raise ApiError(422, 'repair_path', '修复路径无效。')
    normalized = path
    for prefix in ('$PROJECT/hmi/', '$INSTALL/hmi/', '$PROJECT/', '$INSTALL/'):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or '..' in parsed.parts or normalized != str(parsed):
        raise ApiError(422, 'repair_path', '仅允许已配对上位机根目录中的明确相对路径。')
    if len(original.encode()) > 65536 or len(replacement.encode()) > 65536 or '\x00' in original + replacement:
        raise ApiError(413, 'repair_size', '单文件文本修复最大64KiB，不能包含NUL。')
    if original == replacement:
        raise ApiError(422, 'repair_unchanged', '修复前后内容没有变化。')
    if re.search(r'(?i)\bsk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9._~-]+', original + '\n' + replacement):
        raise ApiError(422, 'repair_secret', '修复内容包含疑似凭据，请先移除凭据。')
    if kind == 'code':
        if role != 'SYSTEM_ADMIN' or len(parsed.parts) < 2 or parsed.parts[0] != 'factory_hmi' or parsed.suffix != '.py':
            raise ApiError(403, 'repair_code_forbidden', '仅管理员可提议配对上位机factory_hmi目录内的Python源码修复。')
        return
    if kind != 'config' or normalized != 'factory_hmi/config/reliability_platform.yaml':
        raise ApiError(403, 'repair_config_forbidden', '普通配置修复仅允许平台配置显示label或等价格式调整。')
    try:
        import yaml
    except ImportError as exc:
        raise ApiError(503, 'repair_yaml_unavailable', '服务器尚未安装安全YAML校验依赖。') from exc
    try:
        # Alias graphs can expand far beyond the bounded input file size.
        for source in (original, replacement):
            if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                   for token in yaml.scan(source)):
                raise ValueError('yaml_alias')
        old, new = yaml.safe_load(original), yaml.safe_load(replacement)
        def without_labels(value):
            if not isinstance(value, dict) or not isinstance(value.get('profiles'), list):
                raise ValueError('profiles')
            output = dict(value)
            profiles = []
            for profile in value['profiles']:
                if not isinstance(profile, dict):
                    raise ValueError('profile')
                item = dict(profile)
                label = item.pop('label', None)
                if label is not None and (not isinstance(label, str) or not label.strip() or len(label) > 200):
                    raise ValueError('label')
                profiles.append(item)
            output['profiles'] = profiles
            return output
        if canonical(without_labels(old)) != canonical(without_labels(new)):
            raise ValueError('protected_fields')
    except (ValueError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise ApiError(403, 'repair_protected_config', '修复不能改变通信目标、账号映射、运动、保护、判据等字段。') from exc


def _bound_job(actor, session, job_id):
    job = _job(session, job_id)
    hmi = (job.get('context') or {}).get('hmi') or {}
    if str(job.get('origin_session_id') or '') != str(actor.session_public_id):
        raise ApiError(403, 'repair_session_changed', '只能在原任务的原登录会话内确认和接收修复。')
    try:
        installation = str(UUID(str(hmi['installation_id'])))
        window = hmi['window_id']
        if not isinstance(window, str) or not window or len(window) > 128:
            raise ValueError('window')
    except (KeyError, ValueError, TypeError) as exc:
        raise ApiError(422, 'repair_hmi_unpaired', '此任务没有关联已登录的本机上位机窗口。') from exc
    return job, installation, window


def _repair_public(row, actor):
    result = dict(row)
    result.pop('user_id', None)
    result.pop('request_hash', None)
    result.pop('origin_session_id', None)
    result['actor_id'] = str(actor.public_id)
    return result


def _settlement_secret():
    from app.core.config import get_settings
    from app.services.assistant import service_secret
    return service_secret(get_settings()).encode('utf-8')


def _settlement_token(row, actor):
    """Receipt-only capability. Never add it to model/public proposal cards."""
    payload = {'purpose': 'rp1-hmi-repair-settlement-v1', 'repair_id': str(row['id']),
        'owner_id': str(actor.public_id), 'original_session_id': str(row['origin_session_id']),
        'proposal_sha256': row['proposal_sha256'], 'installation_id': str(row['installation_id']),
        'window_id': row['window_id'], 'role': row['role'], 'confirmed_at': str(row['confirmed_at'])}
    return 'v1.' + hmac.new(_settlement_secret(), canonical(payload).encode('utf-8'), hashlib.sha256).hexdigest()


def _valid_settlement_token(row, actor, token):
    return (isinstance(token, str) and re.fullmatch(r'v1\.[a-f0-9]{64}', token) is not None
            and hmac.compare_digest(token, _settlement_token(row, actor)))


def propose_local_repair(actor, job_id, arguments):
    _human(actor, session=True)
    from app.api.routes.assistant_support import RepairProposal
    from pydantic import ValidationError
    try:
        payload = RepairProposal.model_validate(arguments)
    except ValidationError as exc:
        raise ApiError(422, 'repair_arguments', '修复提议缺少有效路径、原字节、SHA256或替换内容。') from exc
    data = payload.model_dump(mode='json')
    validate_repair_content(data['kind'], data['path'], data['original'], data['replacement'], actor.role)
    if hashlib.sha256(data['original'].encode('utf-8')).hexdigest() != data['expected_sha256']:
        raise ApiError(422, 'repair_original_hash', '原始文本与提供的SHA256不匹配。')
    with support_session(actor) as session:
        job, installation, window = _bound_job(actor, session, job_id)
        request_key = data['request_key']
        request_hash = digest({'job_id': str(job_id), **{k: v for k, v in data.items() if k != 'request_key'}})
        _lock(session, 'assistant-repair', request_key)
        old = session.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id'), {'id': request_key}).mappings().first()
        if old:
            if old['request_hash'] != request_hash:
                raise ApiError(409, 'idempotency_conflict', '修复请求标识对应的内容已改变。')
            return {**_repair_public(old, actor), 'replayed': True}
        proposal = {'id': request_key, 'job_id': str(job_id), 'actor_id': str(actor.public_id),
                    'role': actor.role, 'installation_id': installation, 'window_id': window,
                    **{k: data[k] for k in ('kind', 'path', 'expected_sha256', 'original', 'replacement', 'reason')}}
        proposal_sha256 = digest(proposal)
        diff = ''.join(difflib.unified_diff(data['original'].splitlines(True), data['replacement'].splitlines(True),
                                          fromfile=data['path'], tofile=data['path']))
        row = session.execute(text('''INSERT INTO integration.assistant_repair
            (id,user_id,job_id,origin_session_id,role,installation_id,window_id,kind,path,expected_sha256,
             original,replacement,replacement_sha256,reason,diff,proposal_sha256,request_hash)
            VALUES(:id,iam.current_user_id(),:job_id,:session,:role,:installation_id,:window_id,:kind,:path,
                   :expected_sha256,:original,:replacement,:replacement_sha256,:reason,:diff,:proposal_sha256,:request_hash) RETURNING *'''),
            {**proposal, 'session': actor.session_public_id, 'diff': diff,
             'replacement_sha256': hashlib.sha256(data['replacement'].encode()).hexdigest(),
             'proposal_sha256': proposal_sha256, 'request_hash': request_hash}).mappings().one()
        return {**_repair_public(row, actor), 'replayed': False}


def list_repairs(actor, job_id):
    _human(actor)
    with support_session(actor) as session:
        _job(session, job_id)
        return {'items': [_repair_public(row, actor) for row in session.execute(text(
            'SELECT * FROM integration.assistant_repair WHERE job_id=:id ORDER BY created_at,id'), {'id': job_id}).mappings()]}


def confirm_repair(actor, identifier, proposal_sha256):
    _human(actor, session=True)
    with support_session(actor) as session:
        # Read with SELECT RLS before acquiring UPDATE locks. PostgreSQL also
        # applies UPDATE policies to SELECT FOR UPDATE; a replacement login
        # would otherwise look like a missing proposal rather than a clear
        # original-session refusal for its own account.
        row = session.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id'), {'id': identifier}).mappings().first()
        if row is None:
            raise ApiError(404, 'repair_not_found', '修复提议不存在或不属于当前账号。')
        _bound_job(actor, session, row['job_id'])
        row = session.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id FOR UPDATE'), {'id': identifier}).mappings().first()
        if row is None:
            raise ApiError(403, 'repair_binding_changed', '修复确认的原会话或当前权限已变化。')
        if row['role'] != actor.role or row['proposal_sha256'] != proposal_sha256:
            raise ApiError(409, 'repair_proposal_changed', '提议哈希或当前权限已变化，请重新审阅。')
        if row['expires_at'] <= datetime.now(timezone.utc):
            raise ApiError(410, 'repair_expired', '修复提议已过期，请重新读取本机基线。')
        if row['state'] == 'proposed':
            row = session.execute(text("UPDATE integration.assistant_repair SET state='queued',confirmed_at=now() WHERE id=:id RETURNING *"), {'id': identifier}).mappings().one()
        return _repair_public(row, actor)


def pending_repairs(actor, installation_id, window_id):
    _human(actor, session=True)
    with support_session(actor) as session:
        rows = session.execute(text('''SELECT r.* FROM integration.assistant_repair r
            JOIN integration.assistant_job j ON j.id=r.job_id JOIN integration.assistant_thread t ON t.id=j.thread_id
            WHERE r.user_id=iam.current_user_id() AND r.origin_session_id=:session
              AND r.installation_id=:installation AND r.window_id=:window AND r.role=:role
              AND r.state='queued' AND r.expires_at>now() AND t.deleted_at IS NULL
            ORDER BY r.created_at,r.id LIMIT 20'''),
            {'session': actor.session_public_id, 'installation': installation_id, 'window': window_id, 'role': actor.role}).mappings()
        return {'items': [{**_repair_public(row, actor), 'settlement_token': _settlement_token(row, actor)} for row in rows]}


def repair_receipt(actor, identifier, installation_id, window_id, result, *, settlement_token=None):
    _human(actor, session=True)
    if not isinstance(result, dict) or result.get('status') not in TERMINAL_REPAIRS:
        raise ApiError(422, 'repair_receipt_status', '修复回执必须明确applied、conflict、failed或rejected。')
    try:
        encoded = canonical(result).encode()
    except (ValueError, TypeError, RecursionError) as exc:
        raise ApiError(422, 'repair_receipt_json', '修复回执必须是有限JSON对象。') from exc
    if len(encoded) > 32768:
        raise ApiError(413, 'repair_receipt_size', '修复回执不能超过32KiB。')
    receipt_hash = hashlib.sha256(encoded).hexdigest()
    with support_session(actor) as session:
        row = session.execute(text('SELECT * FROM integration.assistant_repair WHERE id=:id'), {'id': identifier}).mappings().first()
        if row is None:
            raise ApiError(404, 'repair_not_found', '修复提议不存在或不属于当前账号。')
        if (str(row['installation_id']) != str(installation_id) or row['window_id'] != window_id
                or row['role'] != actor.role):
            raise ApiError(403, 'repair_binding', '修复回执的实例、窗口或角色不匹配。')
        original_session = str(row['origin_session_id']) == str(actor.session_public_id)
        if not original_session or settlement_token is not None:
            if not _valid_settlement_token(row, actor, settlement_token):
                raise ApiError(403, 'repair_settlement_token', '补报需要原已确认提议的有效回执同步凭证，不能执行新的修复。')
            session.execute(text("SELECT set_config('app.repair_settlement_verified',:proof,true)"),
                            {'proof': str(row['id']) + ':' + row['proposal_sha256']})
        if row['receipt_sha256']:
            if row['receipt_sha256'] != receipt_hash:
                raise ApiError(409, 'repair_receipt_conflict', '不能用不同内容覆盖原始修复回执。')
            return {'accepted': True, 'replayed': True, 'state': row['state']}
        if row['confirmed_at'] is None or row['state'] not in {'queued', 'rejected'}:
            raise ApiError(409, 'repair_not_queued', '修复尚未明确确认或已经结束。')
        if result['status'] == 'applied':
            if (result.get('before_sha256') != row['expected_sha256']
                    or result.get('after_sha256') != row['replacement_sha256']
                    or not result.get('backup_path')):
                raise ApiError(422, 'repair_receipt_evidence', '成功回执需要实际前后哈希及备份位置。')
        # Narrow SECURITY DEFINER function updates only settlement metadata;
        # ordinary UPDATE RLS/confirm/pending remain original-session bound.
        return session.execute(text('SELECT integration.assistant_repair_settle(:id,:proposal,CAST(:receipt AS jsonb),:hash)'),
            {'id': identifier, 'proposal': row['proposal_sha256'],
             'receipt': canonical(public_evidence(result)), 'hash': receipt_hash}).scalar_one()
