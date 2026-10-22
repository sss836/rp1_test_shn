"""Read a curated, versioned source snapshot. No arbitrary host file access."""
import hashlib
import json
from pathlib import Path, PurePosixPath
from app.core.config import get_settings
from app.core.errors import ApiError
from app.services.assistant_privacy import redact_text

def manifest():
    root = Path(get_settings().assistant_source_root).resolve()
    try:
        data = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError):
        raise ApiError(503, "assistant_source_unavailable", "此部署尚未提供源码快照，不能声称已查阅源码。")
    return root, data

def source_query(args):
    root, index = manifest()
    path = args.get("path")
    if path:
        entry = next((x for x in index["files"] if x["path"] == path), None)
        if not entry or ".." in PurePosixPath(path).parts:
            raise ApiError(404, "source_not_found", "路径不在授权源码目录。")
        file = (root / path).resolve()
        if not file.is_relative_to(root) or file.is_symlink():
            raise ApiError(404, "source_not_found", "源码路径无效。")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ApiError(409, "source_changed", "源码快照校验失败。")
        lines = raw.decode("utf-8").splitlines()
        start = max(1, int(args.get("start", 1)))
        end = min(len(lines), start + 159)
        return {"version": index["version"], "snapshot_sha256": index["snapshot_sha256"],
                "path": path, "sha256": entry["sha256"], "start": start, "end": end,
                "text": redact_text("\n".join(f"{n+1}: {lines[n]}" for n in range(start-1, end))),
                "version_note": "快照版本不代表现场上位机安装版本；诊断时核对用户版本。"}
    query = str(args.get("query", "")).strip()
    if not query:
        return {"version": index["version"], "files": [{"path": x["path"]} for x in index["files"]],
                "instruction": "提供错误文字、函数名或关键词进行源码/技术文档检索。"}
    if len(query) > 200:
        raise ApiError(422, "source_query_long", "检索关键词最多200字。")
    prefix = args.get("directory", "")
    if prefix and prefix.split("/")[0] not in {"hmi", "backend", "frontend", "database", "docs", "assistant"}:
        raise ApiError(422, "source_directory_invalid", "请选择项目源码或技术文档目录。")
    matches = []
    for entry in index["files"]:
        if prefix and not entry["path"].startswith(prefix):
            continue
        raw = (root / entry["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ApiError(409, "source_changed", "源码快照校验失败。")
        for n, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            if query.casefold() in line.casefold():
                matches.append({"path": entry["path"], "line": n, "text": redact_text(line[:450])})
                if len(matches) >= 40:
                    return {"version": index["version"], "matches": matches, "truncated": True}
    return {"version": index["version"], "matches": matches, "truncated": False}
