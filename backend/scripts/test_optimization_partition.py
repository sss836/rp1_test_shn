#!/usr/bin/env python3
"""Create a new disposable PostgreSQL/database/files volume; never reuse site DB settings."""
import argparse
import os
from pathlib import Path
import secrets
import subprocess
import time
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tests',nargs='*',default=['tests/test_factory_partition_integration.py'])
    parser.add_argument('--tools-image',default='rp1-archive-qa:local')
    args=parser.parse_args()
    name='rp1-factory-partition-qa-'+uuid4().hex[:10]
    volume=name+'-files'
    env=os.environ.copy()
    env.update(POSTGRES_USER='postgres',POSTGRES_DB='rp1_presence_factory_partition',POSTGRES_PASSWORD=secrets.token_hex(24),RP1_APP_USER='rp1_app',RP1_APP_PASSWORD=secrets.token_hex(24),RP1_READONLY_USER='rp1_readonly',RP1_READONLY_PASSWORD=secrets.token_hex(24),IMPORT_LEGACY_HISTORY='false')
    def run(cmd,**kw):return subprocess.run(cmd,env=env,check=True,**kw)
    def flags(keys):return [item for k in keys for item in ('-e',k)]
    made=False;volume_made=False
    baseline=tempfile.TemporaryDirectory(prefix='rp1-factory-baseline-')
    baseline_paths=['app/main.py','app/api/routes/archives.py','app/repositories/archives.py','app/schemas/archives.py','tests/test_archives.py']
    for path in baseline_paths:
        target=Path(baseline.name)/path;target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(subprocess.check_output(['git','show','d04ef766524e25cab8206f6dd9df0f726464b16b:backend/'+path],cwd=ROOT))
    try:
        run(['docker','volume','create','--label','rp1.disposable=factory-partition-test',volume],stdout=subprocess.DEVNULL);volume_made=True
        run(['docker','run','-d','--name',name,'--label','rp1.disposable=factory-partition-test',*flags(('POSTGRES_USER','POSTGRES_DB','POSTGRES_PASSWORD','RP1_APP_USER','RP1_APP_PASSWORD','RP1_READONLY_USER','RP1_READONLY_PASSWORD')),'-v',str(ROOT/'database/docker-entrypoint-initdb.d')+':/docker-entrypoint-initdb.d:ro','postgres:16-alpine'],stdout=subprocess.DEVNULL);made=True
        for _ in range(90):
            if subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
            time.sleep(.3)
        else:raise RuntimeError('Disposable PostgreSQL did not become ready')
        env['DATABASE_URL']=f"postgresql+psycopg://postgres:{env['POSTGRES_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}"
        print('Migrate NEW disposable database '+name,flush=True)
        run(['docker','run','--rm','--network','container:'+name,'--workdir','/database','-v',str(ROOT/'database')+':/database:ro',*flags(('DATABASE_URL','RP1_APP_USER','RP1_READONLY_USER','IMPORT_LEGACY_HISTORY')),args.tools_image,'alembic','upgrade','20261022_0051'])
        env['HMI_TEST_ADMIN_URL']=env['DATABASE_URL']
        env.update(DATABASE_URL=f"postgresql+psycopg://rp1_app:{env['RP1_APP_PASSWORD']}@127.0.0.1:5432/{env['POSTGRES_DB']}",APP_ENV='test',AUTH_MODE='session',AUTH_COOKIE_SECURE='false',PYTHONPATH='/app:/hmi',SEED_ADMIN_USERNAME='',ARCHIVE_ROOT='/archive',ASSISTANT_ENABLED='false')
        base_command=['docker','run','--rm','--user','0','--network','container:'+name,'--workdir','/app','-v',str(ROOT/'backend')+':/app:ro','-v',str(ROOT/'database')+':/database:ro','-v',str(ROOT/'hmi')+':/hmi:ro','-v',volume+':/archive',*flags(('DATABASE_URL','HMI_TEST_ADMIN_URL','APP_ENV','AUTH_MODE','AUTH_COOKIE_SECURE','PYTHONPATH','SEED_ADMIN_USERNAME','ARCHIVE_ROOT','ASSISTANT_ENABLED'))]
        overlays=[item for path in baseline_paths for item in ('-v',str(Path(baseline.name)/path)+':/app/'+path+':ro')]
        print('Seed actual pre-0052 approval evidence through predecessor API modules and real sessions',flush=True)
        run([*base_command,*overlays,args.tools_image,'python','-m','pytest','-p','no:cacheprovider','-c','pytest.ini','tests/factory_partition_legacy_seed.py','-q','--disable-warnings'])
        app_url=env['DATABASE_URL'];env['DATABASE_URL']=env['HMI_TEST_ADMIN_URL']
        run(['docker','run','--rm','--network','container:'+name,'--workdir','/database','-v',str(ROOT/'database')+':/database:ro',*flags(('DATABASE_URL','RP1_APP_USER','RP1_READONLY_USER','IMPORT_LEGACY_HISTORY')),args.tools_image,'alembic','upgrade','head'])
        print('Verify repeated Alembic upgrade head is a no-op on the same disposable database',flush=True)
        run(['docker','run','--rm','--network','container:'+name,'--workdir','/database','-v',str(ROOT/'database')+':/database:ro',*flags(('DATABASE_URL','RP1_APP_USER','RP1_READONLY_USER','IMPORT_LEGACY_HISTORY')),args.tools_image,'alembic','upgrade','head'])
        env['DATABASE_URL']=app_url
        run(['docker','run','--rm','--user','0','--network','container:'+name,'--workdir','/app','-v',str(ROOT/'backend')+':/app:ro','-v',str(ROOT/'database')+':/database:ro','-v',str(ROOT/'hmi')+':/hmi:ro','-v',volume+':/archive',*flags(('DATABASE_URL','HMI_TEST_ADMIN_URL','APP_ENV','AUTH_MODE','AUTH_COOKIE_SECURE','PYTHONPATH','SEED_ADMIN_USERNAME','ARCHIVE_ROOT','ASSISTANT_ENABLED')),args.tools_image,'python','-m','pytest','-p','no:cacheprovider','-c','pytest.ini',*args.tests,'-q','--disable-warnings'])
    finally:
        if made:subprocess.run(['docker','rm','-f','-v',name],check=True,stdout=subprocess.DEVNULL)
        if volume_made:subprocess.run(['docker','volume','rm',volume],check=True,stdout=subprocess.DEVNULL)
        baseline.cleanup()
        print('Disposable database/files removed; no site DATABASE_URL or production storage used',flush=True)
if __name__=='__main__':main()
