"""Run the real deployment script with isolated command stubs, never Docker."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.parametrize('scenario,success', [
    ('healthy', True), ('missing-caddy', False), ('public-down', False),
    ('reload-fails', False), ('compose-fails', False),
])
def test_deploy_requires_running_proxy_and_public_readiness(tmp_path, scenario, success):
    root = tmp_path / 'project'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy(Path(__file__).resolve().parents[2] / 'scripts/deploy.sh', root / 'scripts/deploy.sh')
    (root / '.env').write_text(f'APP_UID={os.getuid()}\nAPP_GID={os.getgid()}\nBASE_URL=https://test.invalid\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    docker = bin_dir / 'docker'
    docker.write_text(f'#!{sys.executable}\n' + '''import os,sys
args=sys.argv[1:]
scenario=os.environ['DEPLOY_SCENARIO']
if args[:2] == ['compose','exec'] and args[3] not in ('judgehelper', 'caddy'): sys.exit(1)
with open(os.environ['DEPLOY_CALLS'],'a') as f: f.write(repr(args)+'\\n')
if args[:2] == ['compose','ps'] and '--quiet' in args:
    if args[-1] == 'judgehelper': print('backend')
    elif scenario != 'missing-caddy': print('proxy')
elif args[:1] == ['inspect']: print('old-image')
elif args[:2] == ['compose','up'] and scenario == 'compose-fails': sys.exit(1)
elif args[:2] == ['compose','exec']:
    if 'caddy' in args and scenario == 'reload-fails': sys.exit(1)
    if 'hashlib' in ' '.join(args): print('digest')
    if args[-1] == 'https://test.invalid/ready' and scenario == 'public-down': sys.exit(1)
''')
    docker.chmod(0o755)
    # Preflight/backup/digest are unrelated to these proxy regressions.
    for command, body in [('python3', 'echo digest'), ('sleep', ':'), ('git', 'echo test-revision')]:
        stub = bin_dir / command
        stub.write_text('#!/bin/sh\n' + body + '\n')
        stub.chmod(0o755)
    calls = tmp_path / 'calls'
    result = subprocess.run(['bash', str(root / 'scripts/deploy.sh')], text=True, capture_output=True,
                            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
                                 'DEPLOY_SCENARIO': scenario, 'DEPLOY_CALLS': str(calls)}, timeout=15)
    assert (result.returncode == 0) is success, result.stdout + result.stderr
    assert ('Deployment completed successfully.' in result.stdout) is success
    if success or scenario == 'public-down':
        assert 'https://test.invalid/ready' in calls.read_text()
