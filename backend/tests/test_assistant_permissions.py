"""Consent identities and boundaries, without a database or device."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from uuid import uuid4
import hashlib
import json
import pytest
from app.services import assistant_permissions as permissions
from app.services.assistant_local_diagnostics import LocalReadArguments

def sample(state='pending'):
    return dict(id=uuid4(),job_id=uuid4(),user_id=1,origin_session_id=uuid4(),installation_id=uuid4(),window_id='window',
        arguments={'operation':'read','path':'$STATE/logs/control.log','offset':0,'limit':16384},
        request_payload={'actor_id':str(uuid4())},request_sha256='a'*64,state=state,result=None,
        created_at=datetime.now(timezone.utc),expires_at=datetime.now(timezone.utc)+timedelta(minutes=30),
        approval_origin=None,approved_scope=None,scope_root=None)

def test_directory_scope_is_exact_parent_for_a_file_and_target_for_a_directory():
    assert permissions.scope_targets({'operation':'read','path':'$STATE/logs/control.log'})['directory']=='$STATE/logs'
    assert permissions.scope_targets({'operation':'search','path':'$STATE/logs'})['directory']=='$STATE/logs'
    assert permissions.scope_targets({'operation':'csv_diagnosis','path':'$HOME/input.csv'})['directory']=='$HOME'

@pytest.mark.parametrize('state',['pending','approved'])
def test_approval_is_unread_and_not_synthetic_success(state):
    card=permissions._card(sample(state))
    assert card['data']['status']=='permission_required' and card['data']['unread'] is True
    assert card['data']['phase']=='awaiting_permission'
    assert 'text' not in card['data'] and 'request_payload' not in card['data']['permission_request']

def test_public_request_uses_plain_language_and_exact_scope_targets():
    public=permissions._public(sample())
    assert '读取文件' in public['purpose'] and '模型' in public['share_notice']
    assert public['scope_targets']['directory']=='$STATE/logs'
    assert 'user_id' not in public and 'result' not in public

def test_request_hash_contract_preserves_unicode_and_binding_fields():
    payload={'id':str(uuid4()),'arguments':{'operation':'read','path':'$HOME/诊断.log'},'executor_session_id':str(uuid4())}
    expected=hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    assert permissions.sha(payload)==expected
    assert permissions.sha({**payload,'executor_session_id':str(uuid4())})!=expected

@pytest.mark.parametrize('operation',['discover','csv_diagnosis','log_diagnosis'])
def test_diagnostic_operations_remain_strict_read_only(operation):
    assert LocalReadArguments(operation=operation,path='$STATE',query='{"time":"seconds"}').operation==operation
    with pytest.raises(ValueError):LocalReadArguments(operation=operation,path='$STATE',command='shell')

def test_completed_os_failure_is_explicit_evidence_not_permission_success():
    row=sample('completed');row['result']={'status':'os_denied','message':'Permission denied','unread':True}
    card=permissions._card(row)
    assert card['data']['status']=='os_denied' and card['data']['request_arguments']==row['arguments']

@pytest.mark.parametrize('elapsed',[True,-1,120001,'12',1.5,None])
def test_execution_elapsed_requires_a_bounded_real_integer(elapsed):
    from app.core.errors import ApiError
    with pytest.raises(ApiError) as error:
        permissions.complete(None,uuid4(),uuid4(),uuid4(),'window',{'status':'read','execution_elapsed_ms':elapsed})
    assert error.value.code=='local_execution_elapsed'
