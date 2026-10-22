#!/usr/bin/env python3
"""Export only tracked, reviewed source types; no runtime/config/secret mounts."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.services.assistant_privacy import redact_text

parser = argparse.ArgumentParser()
parser.add_argument("destination", type=Path)
args = parser.parse_args()
dest = args.destination.resolve()
if dest.exists() and (dest / "manifest.json").exists():
    raise SystemExit("Use a new immutable snapshot directory.")
dest.mkdir(parents=True, exist_ok=True)
files = []
for name in subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0"):
    path = Path(name)
    if not name or path.parts[0] not in {"hmi", "backend", "frontend", "database", "docs", "assistant"}:
        continue
    if any(x in path.parts for x in ("tests", "test", "dist", "imports", "node_modules", "secrets")):
        continue
    if path.suffix not in {".py", ".ts", ".tsx", ".mjs", ".cpp", ".hpp", ".h", ".sql", ".md", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".rst", ".txt", ".c", ".cc", ".cxx", ".sh", ".cmake"}:
        continue
    if any(word in name.upper() for word in ("CONVERSATION", "PROJECT_CONTEXT", "HANDOFF")):
        continue
    if path.is_absolute() or any(x.startswith(".") for x in path.parts) or (root/path).is_symlink():
        continue
    raw = (root/path).read_bytes()
    if len(raw) > 400000:
        continue
    content = redact_text(raw.decode("utf-8")).encode()
    target = dest / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
version = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
snapshot_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
if subprocess.run(["git","diff","--quiet","HEAD"],cwd=root).returncode:
    version += "+working-"+snapshot_hash[:12]
(dest/"manifest.json").write_text(json.dumps({"version": version, "snapshot_sha256": snapshot_hash, "files": files}, ensure_ascii=False))
print(json.dumps({"version": version, "snapshot_sha256": snapshot_hash, "files": len(files)}))
