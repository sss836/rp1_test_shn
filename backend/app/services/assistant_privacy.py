"""Privacy boundary for model-visible evidence; never expose account records."""
import re

PRIVATE_KEYS = {
    "owner_name", "owner_public_id", "operator_name", "actor_name", "created_by",
    "updated_by", "submitted_by", "reviewer", "reviewer_name", "user_id", "user_name",
    "email", "phone", "password", "password_hash", "token", "cookie", "secret",
    "authorization", "api_key", "session_id", "session_public_id", "source_ip",
}

CHECKSUM_KEYS = {
    "sha256", "source_sha256", "index_sha256", "content_sha256", "file_sha256",
    "input_sha256", "result_sha256", "original_sha256", "proposal_sha256",
    "receipt_sha256", "expected_sha256", "source_result_sha256", "provided_result_sha256",
    "before_sha256", "after_sha256", "replacement_sha256",
    "request_sha256", "expected_request_sha256",
}
PUBLIC_UUID_KEYS = {"id", "attachment_id", "request_id", "job_id", "after_job_id", "installation_id", "execution_uuid", "thread_id", "archive_id"}

_URI_SCHEME_TOKEN = re.compile(r"[a-z][a-z0-9+.-]*", re.IGNORECASE)
_URI_USERINFO = re.compile(r"[^/\s:@]+:[^/\s@]+@")
_EMAIL_TOKEN = re.compile(r"[\w.+-]+|@")
_EMAIL_DOMAIN = re.compile(r"[\w.-]+")
_EMAIL_SUFFIX = re.compile(r"\.[a-zA-Z]{2,}")


def _redact_uri_credentials(value):
    if '://' not in value or '@' not in value:
        return value
    parts, cursor = [], 0
    # Tokenize each scheme run once. An unanchored scheme* followed by ://
    # retries every suffix of an ordinary long word and takes quadratic time.
    for scheme in _URI_SCHEME_TOKEN.finditer(value):
        if scheme.start() < cursor or not value.startswith('://', scheme.end()):
            continue
        userinfo = _URI_USERINFO.match(value, scheme.end() + 3)
        if userinfo:
            parts.extend((value[cursor:userinfo.start()], '[凭据]@'))
            cursor = userinfo.end()
    return ''.join(parts) + value[cursor:] if parts else value


def _redact_email_addresses(value):
    if '@' not in value or '.' not in value:
        return value
    parts, cursor, previous = [], 0, None
    # Every local/domain run is scanned a bounded number of times, including
    # malformed or overlong addresses. No repeated unanchored local-part+.
    for token in _EMAIL_TOKEN.finditer(value):
        if token.group() != '@':
            previous = token
            continue
        if previous is not None and previous.end() == token.start():
            start = max(cursor, previous.start())
            domain = _EMAIL_DOMAIN.match(value, token.end())
            end = None
            if start < token.start() and domain:
                for suffix in _EMAIL_SUFFIX.finditer(value, domain.start(), domain.end()):
                    if suffix.start() > domain.start():
                        end = suffix.end()
            if end is not None:
                parts.extend((value[cursor:start], '[邮箱已隐藏]'))
                cursor = end
        previous = None
    return ''.join(parts) + value[cursor:] if parts else value

def stable_reference(key, value):
    if not isinstance(value,str):return False
    if key in CHECKSUM_KEYS:return re.fullmatch(r"[a-fA-F0-9]{64}",value) is not None
    return key in PUBLIC_UUID_KEYS and re.fullmatch(r"[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}",value) is not None

def redact_text(value):
    value = _redact_uri_credentials(value)
    value = re.sub(r"(?i)\bsk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9._~-]+", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)((?:password|passwd|api[_-]?key|secret|access[_-]?token|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", value)
    value = _redact_email_addresses(value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", value)
    value = re.sub(r"(?i)(/home/|/Users/|[A-Z]:\\Users\\)[^/\\\s]+", r"\1[用户]", value)
    return value

def public_evidence(value):
    if isinstance(value, dict):
        return {str(k): (v if stable_reference(str(k).lower(),v) else public_evidence(v)) for k, v in value.items()
                if str(k).lower() not in PRIVATE_KEYS and not any(
                    word in str(k).lower() for word in ("password", "secret", "token", "cookie"))}
    if isinstance(value, (list, tuple)):
        return [public_evidence(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
