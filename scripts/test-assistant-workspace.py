"""Disposable P0/P1 stack: existing local images, synthetic model, fresh tmpfs DB.

Never reads the site .env, connects to running site containers, or invokes a
live model. Every Docker operation is scoped to this runner's random project.
"""
from pathlib import Path
import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--report-dir',type=Path)
    parser.add_argument('--cleanup-report',type=Path,help='Stop only the disposable stack recorded by this runner.')
    parser.add_argument('--keep-running',action='store_true',help='Keep this new synthetic stack for further review; cleanup command is recorded.')
    parser.add_argument('--suite',choices=['workspace','runtime'],default='workspace')
    parser.add_argument('--restart-check',action='store_true',help='Restart only this disposable API while a synthetic original-session fixture waits.')
    parser.add_argument('--test-filter',help='Pytest -k expression within this runner fixed synthetic suites.')
    args=parser.parse_args()
    if args.cleanup_report:
        record=args.cleanup_report.resolve()
        data=json.loads((record/'result.json').read_text())
        project=data['project'];temp=Path(data['temporary_directory'])
        if not re.fullmatch(r'rp1-workspace-qa-[0-9a-f]{10}',project) or temp.parent!=Path('/tmp') or not temp.name.startswith(project+'-') or temp.is_symlink():
            raise RuntimeError('Refuse cleanup outside this runner dedicated project')
        command=['docker','compose','--env-file',str(temp/'compose.env'),'-p',project,
            '-f',str(ROOT/'deployment/compose.assistant-isolation.yml'),'-f',str(temp/'workspace.yml'),
            'down','--volumes','--remove-orphans']
        result=subprocess.run(command,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (record/'cleanup.log').write_text(result.stdout)
        if result.returncode:raise RuntimeError('Disposable cleanup failed')
        shutil.rmtree(temp)
        data['kept_running']=False;data['cleaned_up']=True
        (record/'result.json').write_text(json.dumps(data,ensure_ascii=False,indent=2));print('dedicated stack cleaned up')
        return
    if args.report_dir is None:parser.error('--report-dir or --cleanup-report is required')
    report=args.report_dir.resolve();report.mkdir(parents=True,exist_ok=True)
    project='rp1-workspace-qa-'+uuid4().hex[:10]
    temp=Path(tempfile.mkdtemp(prefix=project+'-'))
    archive=temp/'archives';archive.mkdir(mode=0o777);archive.chmod(0o777)
    bridge=temp/'bridge';bridge.write_text(secrets.token_urlsafe(48));bridge.chmod(0o444)
    env={
        'QA_ADMIN_PASSWORD':secrets.token_hex(20),'QA_APP_PASSWORD':secrets.token_hex(20),
        'QA_READ_PASSWORD':secrets.token_hex(20),'QA_HMAC_SECRET':secrets.token_hex(32),
        'QA_PILOT_IDS':','.join(str(uuid4()) for _ in range(4)),
        'QA_UID':str(os.getuid()),'QA_GID':str(os.getgid()),
        'QA_BRIDGE_SECRET_FILE':str(bridge),'QA_ARCHIVE_ROOT':str(archive),'QA_REPORT_DIR':str(report),
        'QA_API_IMAGE':'rp1-engineering-qa:20261022','QA_MIGRATE_IMAGE':'rp1-engineering-qa:20261022',
        'QA_PI_IMAGE':'rp1-diagnostics-pi:candidate-20261030-cdc8e3327626',
    }
    envfile=temp/'compose.env';envfile.write_text('\n'.join(k+'='+v for k,v in env.items()));envfile.chmod(0o600)
    override=temp/'workspace.yml'
    files=(['tests/test_assistant_runtime.py'] if args.suite=='runtime' else
           ['tests/test_assistant_workspace.py','tests/test_assistant_workspace_integration.py',
            'tests/test_assistant_support.py','tests/test_assistant_general_attachments.py',
            'tests/test_assistant_local_diagnostics.py','tests/test_assistant_permissions.py',
            'tests/test_assistant_diagnosis.py','tests/test_assistant_diagnosis_boundaries.py'])
    files=[f for f in files if (ROOT/'backend'/f).exists()]
    test_command=['python','-m','pytest','-p','no:cacheprovider',*files,'-q','--junitxml=/reports/workspace-junit.xml']
    if args.test_filter:test_command+=['-k',args.test_filter]
    override.write_text(f'''services:
  api:
    environment:
      ASSISTANT_RUNTIME_ENABLED: 'true'
      ASSISTANT_LEGACY_PROBES: 'false'
      ASSISTANT_PRICE_VALID_UNTIL: '2026-11-02T00:00:00+08:00'
  pi:
    volumes: ['{ROOT / 'assistant/src'}:/app/src:ro']
  node-checks:
    volumes: ['{ROOT / 'assistant/src'}:/app/src:ro']
  checks:
    volumes: ['{ROOT / 'hmi'}:/hmi:ro']
    environment:
      PYTHONPATH: '/app:/hmi'
      ASSISTANT_RUNTIME_ENABLED: 'true'
      ASSISTANT_LEGACY_PROBES: 'false'
      ASSISTANT_PRICE_VALID_UNTIL: '2026-11-02T00:00:00+08:00'
    command: {json.dumps(test_command)}
''')
    compose=['docker','compose','--env-file',str(envfile),'-p',project,'-f',str(ROOT/'deployment/compose.assistant-isolation.yml'),'-f',str(override)]
    def run(args,name,check=True):
        proc=subprocess.run(args,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
        (report/(name+'.log')).write_text(proc.stdout)
        print(name+': exit='+str(proc.returncode),flush=True)
        if check and proc.returncode:raise RuntimeError(name+' failed; see '+str(report/(name+'.log')))
        return proc
    result={'project':project,'temporary_directory':str(temp),'status':'not_run','images':{k:v for k,v in env.items() if k.endswith('_IMAGE')},'files':files}
    try:
        run(compose+['up','-d','--pull','never','--no-build','--wait','postgres','migrate','model-mock','pi','api','web'],'startup')
        run(compose+['run','--rm','--no-deps','--pull','never','node-checks'],'pi-tests')
        run(compose+['run','--rm','--no-deps','--pull','never','checks'],'backend-tests')
        if args.restart_check:
            with (report/'restart-check.log').open('w') as log:
                process=subprocess.Popen(compose+['run','--rm','--no-deps','--pull','never','checks','python','tests/verify_assistant_restart.py'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
                try:
                    deadline=time.monotonic()+25
                    while not(report/'restart-ready.json').exists():
                        if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Restart fixture did not reach a durable wait; see restart-check.log')
                        time.sleep(.1)
                    run(compose+['restart','--timeout','5','api'],'api-restart')
                    (report/'restart-completed').write_text('dedicated API restart command completed\n')
                    if process.wait(timeout=50):raise RuntimeError('Restart recovery check failed; see restart-check.log')
                    result['restart_check']='passed'
                finally:
                    if process.poll() is None:process.terminate();process.wait(timeout=10)
        result['status']='passed'
    except Exception as exc:
        result['status']='failed';result['error']=str(exc)
        raise
    finally:
        run(compose+['logs','--no-color','--tail','150','api','pi','migrate'],'service-logs',check=False)
        if args.keep_running:
            result['cleanup_command']=compose+['down','--volumes','--remove-orphans'];result['kept_running']=True
            # This file contains paths and cleanup args, never secret values.
        else:
            cleanup=run(compose+['down','--volumes','--remove-orphans'],'cleanup',check=False)
            result['cleanup_exit_code']=cleanup.returncode
            result['cleaned_up']=cleanup.returncode==0
            if cleanup.returncode==0:
                shutil.rmtree(temp)
            else:
                result['kept_running']=True
                result['cleanup_command']=compose+['down','--volumes','--remove-orphans']
                if result['status']=='passed':result['status']='cleanup_failed'
        (report/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        if result.get('cleanup_exit_code',0):
            raise RuntimeError('Disposable cleanup failed; preserved recovery files and command in result.json')


if __name__=='__main__':main()
