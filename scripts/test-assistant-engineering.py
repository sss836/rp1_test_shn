#!/usr/bin/env python3
"""Create and remove this task's own Docker stack, never read the site's .env."""
import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def stop_requested(*_):
    # Keep cleanup scoped to this runner's own disposable compose project.
    raise KeyboardInterrupt('isolated runner stopped')


signal.signal(signal.SIGTERM,stop_requested)
parser = argparse.ArgumentParser()
parser.add_argument("--report-dir", type=Path, required=True)
parser.add_argument("--deepseek-key-file", type=Path, help="optional server key file; makes one live probe with synthetic data")
parser.add_argument("--setup-checks", action="store_true", help="run milestone 3 unified setup checks")
parser.add_argument("--chat-checks", action="store_true", help="run milestone 4 runtime checks")
parser.add_argument("--production-checks", action="store_true", help="production API/Web settings on a disposable DB; requires server key file")
parser.add_argument("--observe-seconds",type=int,help="continuous synthetic observation, 60..86400 seconds; no live API key")
parser.add_argument("--engineering-checks",action="store_true")
parser.add_argument("--query-control-checks",action="store_true")
parser.add_argument("--source-root",type=Path)
args = parser.parse_args()
if args.engineering_checks:
    args.chat_checks=True
    if not args.source_root:parser.error("--engineering-checks requires --source-root")
if args.production_checks and not args.deepseek_key_file:
    parser.error('--production-checks requires --deepseek-key-file')
if args.observe_seconds:
    if args.deepseek_key_file or not 60<=args.observe_seconds<=86400:
        parser.error('observation requires a synthetic model and 60..86400 seconds')
    args.chat_checks=True
report = args.report_dir.resolve()
report.mkdir(parents=True, exist_ok=True)
project = "rp1-assistant-qa-" + uuid4().hex[:12]


def run(command, name, *, capture=True):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if capture:
        (report / (name + ".txt")).write_text(result.stdout)
    print(f"{name}: exit={result.returncode}", flush=True)
    if result.returncode:
        print(result.stdout[-5000:], flush=True)
        raise RuntimeError(name + " failed")
    return result.stdout


with tempfile.TemporaryDirectory(prefix="rp1-assistant-qa-") as directory:
    temp = Path(directory)
    archive_dir=temp/'archives'
    archive_dir.mkdir(mode=0o777)
    archive_dir.chmod(0o777)
    secret_file = temp / "bridge"
    secret_file.write_text(secrets.token_urlsafe(48))
    secret_file.chmod(0o444)
    values = {
        "QA_ADMIN_PASSWORD": secrets.token_hex(24), "QA_APP_PASSWORD": secrets.token_hex(24),
        "QA_READ_PASSWORD": secrets.token_hex(24), "QA_BRIDGE_SECRET_FILE": str(secret_file),
        "QA_HMAC_SECRET":secrets.token_hex(32),
        "QA_PILOT_IDS": ",".join(str(uuid4()) for _ in range(4)),
        "QA_UID": str(os.getuid()), "QA_GID": str(os.getgid()), "QA_REPORT_DIR": str(report),
        "QA_ARCHIVE_ROOT":str(archive_dir),
    }
    if args.deepseek_key_file:
        live_secret = temp / "deepseek"
        shutil.copyfile(args.deepseek_key_file.resolve(strict=True), live_secret)
        live_secret.chmod(0o444)
        values["QA_DEEPSEEK_SECRET_FILE"] = str(live_secret)
    for key in ("QA_API_IMAGE", "QA_WEB_IMAGE", "QA_MIGRATE_IMAGE"):
        if os.getenv(key): values[key] = os.environ[key]
    envfile = temp / "compose.env"
    envfile.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    envfile.chmod(0o600)
    compose = ["docker", "compose", "--env-file", str(envfile), "-p", project, "-f", str(ROOT / "deployment/compose.assistant-isolation.yml")]
    if args.deepseek_key_file:
        compose += ["-f", str(ROOT / "deployment/compose.assistant-live-isolation.yml")]
    if args.setup_checks or args.chat_checks:
        override = temp / "setup-checks.yml"
        override.write_text("services:\n  checks:\n    command: [python, -m, pytest, -p, 'no:cacheprovider', tests/test_unified_setup.py, tests/test_unified_setup_integration.py, tests/test_security_http.py, -q, '--junitxml=/reports/backend-junit.xml']\n")
        compose += ["-f", str(override)]
    if args.chat_checks:
        override = temp / 'chat-checks.yml'
        files='tests/test_assistant_engineering.py tests/test_assistant_engineering_integration.py' if args.engineering_checks else 'tests/test_assistant_runtime.py tests/test_assistant_runtime_live.py'
        if args.query_control_checks:files='tests/test_assistant_query_control.py tests/test_assistant_query_control_live.py tests/test_assistant_runtime.py tests/test_assistant_engineering.py'
        override.write_text("services:\n  api:\n    environment:\n      ASSISTANT_RUNTIME_ENABLED: 'true'\n      ASSISTANT_LEGACY_PROBES: 'false'\n  checks:\n    environment:\n      ASSISTANT_RUNTIME_ENABLED: 'true'\n      ASSISTANT_LEGACY_PROBES: 'false'\n    command: [python, -m, pytest, -p, 'no:cacheprovider', "+', '.join(files.split())+", -q, '--junitxml=/reports/backend-junit.xml']\n")
        compose += ['-f',str(override)]
    if args.production_checks:
        override=temp/'production-checks.yml'
        override.write_text("""services:
  api:
    environment:
      APP_ENV: production
      SERVICE_API_KEY_HMAC_SECRET: ${QA_HMAC_SECRET:?}
      AUTH_COOKIE_SECURE: 'true'
      ASSISTANT_RUNTIME_ENABLED: 'true'
      ASSISTANT_PRODUCTION_ENABLED: 'true'
      ASSISTANT_LEGACY_PROBES: 'false'
  pi:
    environment:
      APP_ENV: production
  web:
    image: rp1-shn-web:2.0.7-assistant-20261022-r1
    entrypoint: []
    secrets: [tls-cert, tls-key]
    healthcheck:
      test: [CMD, wget, -qO-, 'http://127.0.0.1:8080/healthz']
  checks:
    environment:
      ASSISTANT_QA_PRODUCTION: 'true'
    command: [python, -m, pytest, -p, 'no:cacheprovider', tests/test_assistant_production.py, -v, '--junitxml=/reports/backend-junit.xml']
secrets:
  tls-cert: {file: ./secrets/server.crt}
  tls-key: {file: ./secrets/server.key}
""")
        compose+=['-f',str(override)]
    if args.observe_seconds:
        override=temp/'observation.yml'
        override.write_text("services:\n  checks:\n    environment:\n      ASSISTANT_OBSERVE_SECONDS: '"+str(args.observe_seconds)+"'\n    command: [python, scripts/observe_assistant.py]\n")
        compose+=['-f',str(override)]
    if args.engineering_checks:
        override=temp/'engineering-source.yml'
        source=str(args.source_root.resolve(strict=True))
        override.write_text("services:\n  api:\n    environment:\n      ASSISTANT_SOURCE_ROOT: /qa-source\n    volumes: ['"+source+":/qa-source:ro']\n  checks:\n    environment:\n      ASSISTANT_SOURCE_ROOT: /qa-source\n    volumes: ['"+source+":/qa-source:ro']\n")
        compose+=['-f',str(override)]
    try:
        if not os.getenv("QA_API_IMAGE"):
            run(["docker", "build", "-t", "rp1-assistant-qa:milestone2", "-f", str(ROOT / "deployment/ArchiveQa.Dockerfile"), str(ROOT)], "qa-image-build")
        run(compose + ["build", "pi"], "pi-build")
        run(compose + ["up", "-d", "--wait", "--wait-timeout", "120", "api", "pi", "web", "model-mock"], "stack-start")
        containers = run(compose + ["ps", "-q", "pi", "postgres"], "qa-container-ids").split()
        inspected = json.loads(run(["docker", "inspect", *containers], "inspect-private", capture=False))
        # Save only non-secret security and network properties; never raw inspect.
        topology = [{"name": item["Name"], "image_id": item["Image"], "user": item["Config"]["User"],
                     "networks": list(item["NetworkSettings"]["Networks"]), "ports": item["HostConfig"]["PortBindings"],
                     "read_only": item["HostConfig"]["ReadonlyRootfs"], "cap_drop": item["HostConfig"]["CapDrop"],
                     "mount_targets": [mount["Destination"] for mount in item["Mounts"]],
                     "env_names": [value.split("=", 1)[0] for value in item["Config"]["Env"]]}
                    for item in inspected]
        pi = next(item for item in topology if item["name"].endswith("-pi-1"))
        postgres = next(item for item in topology if item["name"].endswith("-postgres-1"))
        assert set(pi["networks"]).isdisjoint(postgres["networks"])
        assert not pi["ports"] and pi["read_only"] and "ALL" in pi["cap_drop"] and pi["user"] == "node"
        assert not {"DATABASE_URL", "PGHOST", "PLC_HOST", "CAN_CHANNEL"}.intersection(pi["env_names"])
        expected_mounts = {"/run/secrets/bridge"} | ({"/run/secrets/deepseek"} if args.deepseek_key_file else set())
        assert set(pi["mount_targets"]) == expected_mounts
        (report / "topology.json").write_text(json.dumps(topology, indent=2))
        run(compose + ["exec", "-T", "pi", "node", "--input-type=module", "-e", "import dns from 'node:dns/promises'; import fs from 'node:fs'; try {await dns.lookup('postgres'); process.exit(1)} catch(e) {if(e.code!=='ENOTFOUND'&&e.code!=='EAI_AGAIN')throw e;} if(fs.existsSync('/var/run/docker.sock')||fs.existsSync('/var/lib/rp1/archives'))process.exit(1); console.log('postgres not resolvable; no Docker socket or archive mount');"], "network-boundary")
        run(compose + ["run", "--rm", "node-checks"], "node-tests")
        run(compose + ["run", "--rm", "checks"], "backend-tests")
        run(compose + ["exec", "-T", "api", "python", "-c", "import fastapi, httpx, sqlalchemy; print('fastapi',fastapi.__version__,'httpx',httpx.__version__,'sqlalchemy',sqlalchemy.__version__)"], "python-versions")
        (report / "result.json").write_text(json.dumps({"status": "checks_passed", "project": project, "model": "DeepSeek official API, synthetic input" if args.deepseek_key_file else "SYNTHETIC SSE FIXTURE; no DeepSeek request", "database": "disposable tmpfs rp1_presence_assistant", "site_enabled": False}, indent=2))
    finally:
        # Metadata logs omit request payloads and credentials by design.
        subprocess.run(compose + ["logs", "--no-color", "api", "pi", "migrate"], stdout=(report / "service-logs.txt").open("w"), stderr=subprocess.STDOUT)
        run(compose + ["down", "--volumes", "--remove-orphans"], "stack-cleanup")
