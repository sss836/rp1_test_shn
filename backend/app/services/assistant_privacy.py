"""Privacy boundary for model-visible evidence; never expose account records."""
import re

PRIVATE_KEYS = {
    "owner_name", "owner_public_id", "operator_name", "actor_name", "created_by",
    "updated_by", "submitted_by", "reviewer", "reviewer_name", "user_id", "user_name",
    "email", "phone", "password", "password_hash", "token", "cookie", "secret",
    "authorization", "api_key", "session_id", "session_public_id", "source_ip",
}

def redact_text(value):
    value = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@", r"\1[凭据]@", value)
    value = re.sub(r"(?i)\bsk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9._~-]+", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)((?:password|passwd|api[_-]?key|secret|access[_-]?token|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[邮箱已隐藏]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", value)
    value = re.sub(r"(?i)(/home/|/Users/|[A-Z]:\\Users\\)[^/\\\s]+", r"\1[用户]", value)
    return value

def public_evidence(value):
    if isinstance(value, dict):
        return {str(k): public_evidence(v) for k, v in value.items()
                if str(k).lower() not in PRIVATE_KEYS and not any(
                    word in str(k).lower() for word in ("password", "secret", "token", "cookie"))}
    if isinstance(value, (list, tuple)):
        return [public_evidence(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
