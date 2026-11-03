"""Privacy semantics and bounded long-input work; synthetic bytes only."""
import json
import subprocess
import sys

import pytest

from app.services.assistant_privacy import public_evidence, redact_text, stable_reference


@pytest.mark.parametrize('source,expected', [
    ('postgresql+psycopg://alice:pa:ss@db:5432/name', 'postgresql+psycopg://[凭据]@db:5432/name'),
    ('HTTPS://user%40mail:pa%3Ass@host/x', 'HTTPS://[凭据]@host/x'),
    ('custom.v2-x://u:p@host/x', 'custom.v2-x://[凭据]@host/x'),
    ('report+tag.first-last@example.co.uk,', '[邮箱已隐藏],'),
    ('用户@例子.com', '[邮箱已隐藏]'),
    ('first@one.com second@two.org.', '[邮箱已隐藏] [邮箱已隐藏].'),
    ('x@y.com+z@q.com', '[邮箱已隐藏][邮箱已隐藏]'),
    ('password=synthetic-secret, phone=13800138000', 'password=[已隐藏], phone=[手机号已隐藏]'),
    ('Bearer synthetic-token-123456', '[凭据已隐藏]'),
    ('/home/synthetic/source.py /Users/synthetic/config.json', '/home/[用户]/source.py /Users/[用户]/config.json'),
    ('u@localhost u:p@host file:///tmp/a', 'u@localhost u:p@host file:///tmp/a'),
])
def test_normal_privacy_redaction_semantics_are_preserved(source, expected):
    assert redact_text(source) == expected


def test_strict_uuid_and_checksum_evidence_survive_but_private_values_do_not():
    identifier = '00000000-0000-0000-0000-13800138000a'
    checksum = 'a' * 20 + '13800138000' + 'b' * 33
    assert len(checksum) == 64
    assert stable_reference('attachment_id', identifier)
    assert stable_reference('sha256', checksum)
    assert not stable_reference('attachment_id', 'prefix-' + identifier)
    assert not stable_reference('sha256', checksum + 'z')
    result = public_evidence({'attachment_id': identifier, 'job_id': identifier,
                              'source_sha256': checksum, 'sha256': 'prefix-13800138000',
                              'note': 'phone=13800138000', 'id': 'invalid-13800138000',
                              'owner_public_id': identifier, 'password': 'synthetic-private'})
    assert result['attachment_id'] == result['job_id'] == identifier
    assert result['source_sha256'] == checksum
    assert '13800138000' not in result['sha256'] + result['note'] + result['id']
    assert 'owner_public_id' not in result and 'password' not in result


@pytest.mark.parametrize('length', [100000, 200000])
@pytest.mark.parametrize('kind', ['letters', 'incomplete_email', 'missing_tld', 'long_valid_email'])
def test_large_text_has_a_bounded_redaction_budget(length, kind):
    # A subprocess enforces the budget even if a future regex regresses. The
    # expected work is milliseconds, while old unanchored patterns exceed it.
    worker = """import json,sys,time
from app.services.assistant_privacy import redact_text
length=int(sys.argv[1]);kind=sys.argv[2]
value=('a'*length if kind=='letters' else 'a'*(length//2)+'@'+'b'*(length-length//2-1)
       if kind=='incomplete_email' else 'a'*(length//2)+'@'+'b'*(length-length//2-2)+'.'
       if kind=='missing_tld' else 'a'*length+'@example.com')
start=time.monotonic();result=redact_text(value);elapsed=time.monotonic()-start
assert result=='[邮箱已隐藏]' if kind=='long_valid_email' else result==value
print(json.dumps({'elapsed_s':elapsed}))
"""
    result = subprocess.run([sys.executable, '-c', worker, str(length), kind],
                            text=True, capture_output=True, timeout=2, check=True)
    assert json.loads(result.stdout)['elapsed_s'] < 1
