#!/usr/bin/env python3
"""Bench registry + presence checks in a disposable database; never reads site .env."""
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    name = "rp1-stations-qa-" + uuid4().hex[:12]
    env = os.environ.copy()
    env.update(POSTGRES_USER="postgres", POSTGRES_DB="rp1_presence_stations",
               POSTGRES_PASSWORD=secrets.token_hex(24), RP1_APP_USER="rp1_app",
               RP1_APP_PASSWORD=secrets.token_hex(24), RP1_READONLY_USER="rp1_readonly",
               RP1_READONLY_PASSWORD=secrets.token_hex(24))

    def flags(keys):
        return [item for key in keys for item in ("-e", key)]

    def run(command, **kwargs):
        return subprocess.run(command, env=env, check=True, **kwargs)

    created = False
    try:
        run(["docker", "run", "-d", "--name", name, "--label", "rp1.disposable=stations-test",
             *flags(("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD", "RP1_APP_USER", "RP1_APP_PASSWORD", "RP1_READONLY_USER", "RP1_READONLY_PASSWORD")),
             "-v", str(ROOT / "database/docker-entrypoint-initdb.d") + ":/docker-entrypoint-initdb.d:ro",
             "postgres:16-alpine"], stdout=subprocess.DEVNULL)
        created = True
        for _ in range(90):
            ready = subprocess.run(["docker", "exec", name, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(.5)
        else:
            raise RuntimeError("Disposable PostgreSQL did not become ready")
        env.update(DATABASE_URL=f"postgresql+psycopg://postgres:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}", IMPORT_LEGACY_HISTORY="false")
        run(["docker", "run", "--rm", "--network", "container:" + name, "--workdir", "/workspace",
             "-v", str(ROOT / "database") + ":/workspace:ro",
             *flags(("DATABASE_URL", "RP1_APP_USER", "RP1_READONLY_USER", "IMPORT_LEGACY_HISTORY")),
             "rp1-archive-qa:local", "alembic", "upgrade", "head"])
        env["HMI_TEST_ADMIN_URL"] = env["DATABASE_URL"]
        env.update(DATABASE_URL=f"postgresql+psycopg://{env['RP1_APP_USER']}:{env['RP1_APP_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}",
                   APP_ENV="test", AUTH_MODE="session", AUTH_COOKIE_SECURE="false", PYTHONPATH="/app", SEED_ADMIN_USERNAME="")
        run(["docker", "run", "--rm", "--network", "container:" + name, "--workdir", "/app",
             "-v", str(ROOT / "backend") + ":/app:ro", "-v", str(ROOT / "database") + ":/database:ro",
             *flags(("DATABASE_URL", "HMI_TEST_ADMIN_URL", "APP_ENV", "AUTH_MODE", "AUTH_COOKIE_SECURE", "PYTHONPATH", "SEED_ADMIN_USERNAME")),
             "rp1-archive-qa:local", "python", "-m", "pytest", "-p", "no:cacheprovider", "-c", "pytest.ini",
             "tests/test_setup_integration.py", "tests/test_hmi_presence_integration.py", "tests/test_station_registry_integration.py", "tests/test_waist_hip_integration.py", "tests/test_hmi_runtime_conflict_integration.py", "tests/test_hmi_sample_options_integration.py", "-q", "--disable-warnings"])
    finally:
        if created:
            subprocess.run(["docker", "rm", "-f", "-v", name], check=True, stdout=subprocess.DEVNULL)
        print("Disposable station-test database removed; site services were not changed", flush=True)


if __name__ == "__main__":
    main()
