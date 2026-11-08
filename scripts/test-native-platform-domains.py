#!/usr/bin/env python3
"""Run native platform domains against disposable source/target PG and real HTTP.

This runner deliberately ignores the site's Compose settings, databases and volumes.
"""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--monitor-repo', type=Path, default=ROOT.parent / 'rp1-monitor-app')
    parser.add_argument('--tools-image', default='rp1-archive-qa:local')
    parser.add_argument('--test-installed-app', action='store_true',
                        help='Use app code inside the supplied QA image; mount only tests and pytest config')
    parser.add_argument('--skip-backend-tests', action='store_true', help='Only rerun the real native-service HTTP acceptance')
    args = parser.parse_args()
    monitor = args.monitor_repo.resolve()
    if not (monitor / 'src/rp1_monitor/platform_workspace.py').is_file():
        parser.error('The adapted rp1-monitor-app checkout is required')
    name = 'rp1-native-domains-qa-' + uuid4().hex[:12]
    volume = name + '-files'
    env = os.environ.copy()
    env.update(POSTGRES_USER='postgres', POSTGRES_DB='rp1_presence_native_domains',
               POSTGRES_PASSWORD=secrets.token_hex(24), RP1_APP_USER='rp1_app',
               RP1_APP_PASSWORD=secrets.token_hex(24), RP1_READONLY_USER='rp1_readonly',
               RP1_READONLY_PASSWORD=secrets.token_hex(24), IMPORT_LEGACY_HISTORY='false')

    def flags(keys):
        return [item for key in keys for item in ('-e', key)]

    def run(command, **kw):
        return subprocess.run(command, env=env, check=True, **kw)

    env.pop('SEED_ADMIN_PASSWORD', None)
    made = volume_made = False
    try:
        run(['docker', 'volume', 'create', '--label', 'rp1.disposable=native-domains', volume], stdout=subprocess.DEVNULL)
        volume_made = True
        run(['docker', 'run', '--pull', 'never', '-d', '--name', name, '--label', 'rp1.disposable=native-domains',
             *flags(('POSTGRES_USER', 'POSTGRES_DB', 'POSTGRES_PASSWORD', 'RP1_APP_USER',
                     'RP1_APP_PASSWORD', 'RP1_READONLY_USER', 'RP1_READONLY_PASSWORD')),
             '-v', str(ROOT / 'database/docker-entrypoint-initdb.d') + ':/docker-entrypoint-initdb.d:ro',
             'postgres:16-alpine'], stdout=subprocess.DEVNULL)
        made = True
        for _ in range(90):
            if subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                break
            time.sleep(.3)
        else:
            raise RuntimeError('Disposable PostgreSQL did not become ready')
        admin_url = f"postgresql+psycopg://postgres:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}"
        env['DATABASE_URL'] = admin_url
        migration_command = ['docker', 'run', '--pull', 'never', '--rm', '--network', 'container:' + name, '-w', '/database',
             '-v', str(ROOT / 'database') + ':/database:ro',
             *flags(('DATABASE_URL', 'RP1_APP_USER', 'RP1_READONLY_USER', 'IMPORT_LEGACY_HISTORY')),
             args.tools_image, 'alembic', 'upgrade']
        run([*migration_command, '20261103_0059'])
        env['RP1_DISPOSABLE_UPGRADE_QA'] = 'yes'
        upgrade_probe = ['docker', 'run', '--pull', 'never', '--rm', '--network', 'container:' + name,
             '-v', str(ROOT / 'scripts/qa_factory_upgrade_probe.py') + ':/upgrade_probe.py:ro',
             '-v', volume + ':/archive', *flags(('DATABASE_URL', 'RP1_DISPOSABLE_UPGRADE_QA')),
             args.tools_image, 'python', '/upgrade_probe.py']
        run([*upgrade_probe, 'snapshot', '/archive/upgrade-0059.json'])
        run([*migration_command, 'head'])
        run([*upgrade_probe, 'verify', '/archive/upgrade-0059.json'])
        env.update(HMI_TEST_ADMIN_URL=admin_url,
                   DATABASE_URL=f"postgresql+psycopg://rp1_app:{env['RP1_APP_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}",
                   APP_ENV='test', AUTH_MODE='session', AUTH_COOKIE_SECURE='false',
                   SEED_ADMIN_USERNAME='', ASSISTANT_ENABLED='false', ARCHIVE_ROOT='/archive',
                   PYTHONPATH='/app:/app/tests:/monitor/src', QA_NATIVE_PASSWORD=secrets.token_urlsafe(24))
        backend_mounts = (['-v', str(ROOT / 'backend/tests') + ':/app/tests:ro',
                           '-v', str(ROOT / 'backend/pytest.ini') + ':/app/pytest.ini:ro']
                          if args.test_installed_app else ['-v', str(ROOT / 'backend') + ':/app:ro'])
        qa_command = ['docker', 'run', '--pull', 'never', '--rm', '--network', 'container:' + name, '-w', '/app',
             *backend_mounts,
             '-v', str(ROOT / 'database') + ':/database:ro',
             '-v', str(ROOT / 'scripts/qa_native_platform_domains.py') + ':/qa.py:ro',
             '-v', str(monitor) + ':/monitor:ro', '-v', volume + ':/archive',
             *flags(('DATABASE_URL', 'HMI_TEST_ADMIN_URL', 'APP_ENV', 'AUTH_MODE',
                     'AUTH_COOKIE_SECURE', 'SEED_ADMIN_USERNAME', 'ASSISTANT_ENABLED',
                     'ARCHIVE_ROOT', 'PYTHONPATH', 'QA_NATIVE_PASSWORD', 'RP1_READONLY_USER', 'RP1_READONLY_PASSWORD')),
             args.tools_image]
        if not args.skip_backend_tests:
            run([*qa_command, 'python', '-m', 'pytest', '-p', 'no:cacheprovider',
                 'tests/test_monitor_recording_csv.py', 'tests/test_monitor_recording_archive_integration.py',
                 'tests/test_archives.py', 'tests/test_curve_analysis.py', 'tests/test_curve_gaps.py',
                 'tests/test_factory_robot_results.py', 'tests/test_archive_imports_integration.py',
                 'tests/test_archive_curve_inspection_integration.py',
                 'tests/test_factory_robot_results_integration.py', 'tests/test_factory_reviewer_routes.py',
                 'tests/test_security_http.py', 'tests/test_archives_integration.py',
                 'tests/test_submissions_integration.py', '-q', '--disable-warnings'])
        run([*qa_command, 'python', '/qa.py'])
    finally:
        if made:
            subprocess.run(['docker', 'rm', '-f', '-v', name], check=True, stdout=subprocess.DEVNULL)
        if volume_made:
            subprocess.run(['docker', 'volume', 'rm', volume], check=True, stdout=subprocess.DEVNULL)
        print('Removed disposable native-domains cluster/source/target/files; no site data or real devices used', flush=True)


if __name__ == '__main__':
    main()
