#!/usr/bin/env python3
"""Fail a release when tracked files contain machine secrets, build products or broken key links."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
errors = []
secret_patterns = [r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", r"gh[pousr]_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{40,}", r"AKIA[0-9A-Z]{16}"]
for name in filter(None, names):
    path = ROOT / name
    if name.startswith(("deployment/secrets/", ".qa/", "backups/")) or Path(name).name == ".env" or any(part.startswith(".venv") or part in {"node_modules", "__pycache__"} for part in Path(name).parts):
        errors.append("Private/runtime file tracked: " + name)
    if path.stat().st_size > 20 * 1024 * 1024:
        errors.append("Large generated file tracked: " + name)
    try:
        body = path.read_text()
    except UnicodeError:
        continue
    if any(re.search(pattern, body) for pattern in secret_patterns):
        errors.append("Secret signature found: " + name)
    if ("/home/" + "shn/") in body:
        errors.append("Personal absolute path: " + name)
for name in ("README.md", "docs/OPERATIONS.md", "docs/ACCEPTANCE.md", "docs/HMI-PRESENCE.md", "hmi/README.md"):
    path = ROOT / name
    if not path.exists():
        errors.append("Missing document: " + name)
        continue
    for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
        target = target.split("#", 1)[0]
        if target and not target.startswith(("https://", "http://", "mailto:")) and not (path.parent / target).exists():
            errors.append(f"Broken link in {name}: {target}")
if errors:
    raise SystemExit("\n".join(errors))
print("PASS release file hygiene and documentation links")
