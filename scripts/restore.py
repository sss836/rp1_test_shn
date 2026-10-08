#!/usr/bin/env python3
"""Restore into a NEW clone/project only. Existing Docker volumes are never overwritten."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile

from backup import ROOT, IMAGE, compose

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("backup", type=Path)
parser.add_argument("--project", required=True)
parser.add_argument("--port", type=int, default=8443)
args = parser.parse_args()
if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project) or not 1 <= args.port <= 65535:
    parser.error("Invalid project or port")
if (ROOT / ".env").exists() or (ROOT / "deployment/secrets").exists():
    parser.error("Use a fresh clone without .env or deployment/secrets")
existing = subprocess.check_output(["docker", "volume", "ls", "--format", "{{.Name}}"], text=True).splitlines()
if any(name.startswith(args.project + "_") for name in existing):
    parser.error("Target project already owns volumes; choose a new project name")
for line in (args.backup / "SHA256SUMS").read_text().splitlines():
    digest, name = line.split("  ", 1)
    if Path(name).name != name:
        parser.error("Invalid backup manifest")
    with (args.backup / name).open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
            parser.error("Checksum failed: " + name)
os.umask(0o077)
with tarfile.open(args.backup / "config.tar.gz") as archive:
    archive.extractall(ROOT, filter="data")
settings = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))
settings["COMPOSE_PROJECT_NAME"] = args.project
settings["WEB_PORT"] = str(args.port)
from urllib.parse import urlsplit
host = urlsplit(settings["PLATFORM_URL"]).hostname
settings["PLATFORM_URL"] = f"https://{host}:{args.port}"
(ROOT / ".env").write_text("".join(f"{key}={value}\n" for key, value in settings.items()))
compose("up", "-d", "--wait", "postgres")
with (args.backup / "postgres.dump").open("rb") as handle:
    compose("exec", "-T", "postgres", "pg_restore", "--exit-on-error", "--no-owner", "-U", settings["POSTGRES_USER"], "-d", settings["POSTGRES_DB"], stdin=handle)
subprocess.run([str(ROOT / "scripts/up.sh")], cwd=ROOT, check=True)
print("Restore completed. Run python3 scripts/verify.py; update DNS/certificates if the server address changes.")
