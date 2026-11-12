"""Signed data-only catalogs for offline motor parameter maintenance."""

import base64
import binascii
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import tempfile

from .motor_parameter_contract import canonical, sha, validate
from .updates import _public_key, _run


KIND = "rp1.local_motor_parameter_templates.v1"
MAX_CATALOG_BYTES = 1024 * 1024
_RECORD_FIELDS = {"template", "sha256", "published_by", "published_at"}


def _unique_object(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("参数模板目录含有重复字段")
    return value


def read_catalog_json(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("参数模板目录必须是无符号链接的本地绝对路径")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
            raise ValueError("参数模板目录必须是普通文件且不超过 1 MiB")
        data = stream.read(MAX_CATALOG_BYTES + 1)
    if len(data) > MAX_CATALOG_BYTES:
        raise ValueError("参数模板目录不超过 1 MiB")
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError("参数模板目录不是有效的 JSON") from exc


def catalog_body(records):
    """Normalize and validate the exact content administrators sign."""
    if not isinstance(records, list) or not 1 <= len(records) <= 128:
        raise ValueError("本地目录须包含 1 到 128 个已发布模板")
    seen_ids = set()
    seen_versions = set()
    verified = []
    for row in records:
        if not isinstance(row, dict) or set(row) not in (_RECORD_FIELDS, _RECORD_FIELDS | {"request_id"}):
            raise ValueError("参数模板缺少发布回执或字段无效")
        publisher = row["published_by"]
        published_at = row["published_at"]
        request_id = row.get("request_id")
        if (not isinstance(publisher, str) or not publisher.strip() or len(publisher) > 256
                or not isinstance(published_at, str) or len(published_at) > 64
                or request_id is not None and (not isinstance(request_id, str) or len(request_id) > 256)):
            raise ValueError("参数模板发布回执无效")
        try:
            published = datetime.fromisoformat(published_at)
            if published.utcoffset() is None:
                raise ValueError("参数模板发布时间须带时区")
            template = validate(row["template"], published=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"本地参数模板无效：{exc}") from exc
        if not isinstance(template["template_id"], str) or not isinstance(template["version"], str):
            raise ValueError("模板编号和版本必须为字符串")
        if sha(template) != row["sha256"]:
            raise ValueError("本地参数模板 SHA256 不符")
        template_id = template["template_id"]
        version_key = (template["model"], template["version"])
        if template_id in seen_ids or version_key in seen_versions:
            raise ValueError("本地参数目录含有重复模板编号或型号版本")
        seen_ids.add(template_id)
        seen_versions.add(version_key)
        verified.append({**row, "template": template})
    return {"kind": KIND, "templates": verified}


def verify_catalog(envelope, public_key):
    if (not isinstance(envelope, dict) or set(envelope) != {"kind", "templates", "signature"}
            or envelope["kind"] != KIND or not isinstance(envelope["signature"], str)):
        raise ValueError("本地参数目录签名封装或类型无效")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("本地参数目录签名编码无效") from exc
    if len(signature) != 64:
        raise ValueError("本地参数目录必须使用 Ed25519 签名")
    body = catalog_body(envelope["templates"])
    key = _public_key(public_key)
    with tempfile.TemporaryDirectory(prefix="rp1-parameter-catalog-") as directory:
        message = Path(directory) / "catalog.json"
        signature_file = Path(directory) / "catalog.sig"
        message.write_bytes(canonical(body))
        signature_file.write_bytes(signature)
        result = _run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(key),
                       "-rawin", "-in", str(message), "-sigfile", str(signature_file)])
    if result.returncode:
        raise ValueError("本地参数目录未通过可信签名校验")
    return body["templates"]
