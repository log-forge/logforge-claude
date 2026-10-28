"""Build and check this disposable Compose example; retain structured output for the run report."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parent
run_id = os.environ.get('LOGFORGE_RUN_ID', '')
if not re.fullmatch(r'[A-Za-z0-9-]{1,80}', run_id):
    raise SystemExit('Run this example through logforge_run.py so it has a valid run ID.')
compose = ['docker', 'compose', '-f', str(root / 'compose.yaml'), '-p', 'lf-checkout-' + run_id.lower()]

def command(args, timeout=300):
    return subprocess.run(args, cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=timeout)

code = 1
try:
    started = command(compose + ['up', '-d', '--build', '--wait', '--wait-timeout', '90'])
    if started.returncode:
        raise RuntimeError('Dockerized checkout did not become healthy. Check Docker/Compose and port 38500 availability.')
    checked = subprocess.run([shutil.which('node') or 'node', 'test-browser.mjs'], cwd=root, timeout=120)
    code = checked.returncode
finally:
    command(compose + ['stop', '--timeout', '10'], timeout=60)
    logs = command(compose + ['logs', '--no-color', '--no-log-prefix', 'app'], timeout=30)
    # The parent runner consumes only structured LogForge lines; ordinary app lines are ignored.
    sys.stdout.write(logs.stdout); sys.stdout.flush()
    removed = command(compose + ['down', '--timeout', '10'], timeout=60)
    if removed.returncode:
        code = 1
        print('Could not remove the disposable checkout Compose stack; its project is ' + compose[-1], file=sys.stderr)
raise SystemExit(code)
