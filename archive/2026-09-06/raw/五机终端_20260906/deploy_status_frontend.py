"""Deploy the reviewed HTML to the already-authorized existing site."""
import base64
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--expected-sha', default='ce26f6b16c66f2ea3ca4ff2a689a8c32b5ae43a23ebcb1551e94f772bb9dc044')
parser.add_argument('--backup-name', default='index.before-status.html')
parser.add_argument('--receipt-name', default='status-frontend-deploy-receipt.json')
args = parser.parse_args()
assert len(args.expected_sha) == 64 and all(c in '0123456789abcdef' for c in args.expected_sha)
assert '/' not in args.backup_name and '/' not in args.receipt_name
page = (ROOT / '01_五机终端/01_选机页/index.html').read_bytes()
digest = hashlib.sha256(page).hexdigest()
payload = base64.b64encode(page).decode('ascii')
remote = r'''
import base64, hashlib, json, os, subprocess
from pathlib import Path
from datetime import datetime, timezone
page = Path('/var/www/remote-term/index.html')
backup = Path('/var/backups/remote-term/20260906T020251Z-status')
old_hash = EXPECTED_SHA
sha = lambda data: hashlib.sha256(data).hexdigest()
assert sha(page.read_bytes()) == old_hash, 'Production HTML changed; stop for review'
previous = backup / BACKUP_NAME
if previous.exists():
    assert sha(previous.read_bytes()) == old_hash, 'Existing backup differs'
else:
    previous.write_bytes(page.read_bytes())
assert sha(previous.read_bytes()) == old_hash
data = base64.b64decode(PAYLOAD)
assert sha(data) == DIGEST
staged = page.with_name('index.status.pending.html')
with staged.open('wb') as output:
    output.write(data)
    output.flush()
    os.fsync(output.fileno())
staged.chmod(0o644)
os.replace(staged, page)
assert sha(page.read_bytes()) == DIGEST
units = ['nginx.service', 'remote-term-status.timer', 'remote-ttyd@vps.service',
         'remote-ttyd@aliyun-new.service', 'remote-ttyd@tencent-new.service',
         'remote-ttyd@tencent-main.service', 'remote-ttyd@mbp-dojoy.service']
states = {unit: subprocess.check_output(['systemctl', 'is-active', unit], text=True).strip() for unit in units}
status = json.loads(Path('/var/www/remote-term/status.json').read_text())
receipt = {'phase': 'status_frontend_deployed', 'deployed_at': datetime.now(timezone.utc).isoformat(),
           'backup': str(backup), 'previous_page': str(previous), 'html_sha256': DIGEST, 'html_bytes': len(data),
           'units': states, 'nginx_pid': subprocess.check_output(['systemctl', 'show', 'nginx', '--property=MainPID', '--value'], text=True).strip(),
           'nginx_config_sha256': sha(Path('/etc/nginx/conf.d/hermes-skill-dashboard.conf').read_bytes()),
           'snapshot_generated_at': status['generated_at'],
           'hosts': [{'id': host['id'], 'status': host['status']} for host in status['hosts']]}
(backup / RECEIPT_NAME).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'receipt': receipt, 'snapshot': status}, ensure_ascii=False))
'''.replace('PAYLOAD', repr(payload)).replace('DIGEST', repr(digest)).replace('EXPECTED_SHA', repr(args.expected_sha)).replace('BACKUP_NAME', repr(args.backup_name)).replace('RECEIPT_NAME', repr(args.receipt_name))
result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
                         'hermes-vps', 'env CODEX_APPROVED_RISK=1 codex-safe-run -- python3 - --deploy-five-machine-status-page'],
                        input=remote, text=True, capture_output=True, timeout=45)
if result.returncode:
    print('DEPLOY_FAILED', result.returncode)
    print(result.stderr[-2500:])
    raise SystemExit(result.returncode)
parsed = json.loads(result.stdout)
(EVIDENCE / args.receipt_name).write_text(json.dumps(parsed['receipt'], ensure_ascii=False, indent=2) + '\n')
(EVIDENCE / 'status-live.json').write_text(json.dumps(parsed['snapshot'], ensure_ascii=False, indent=2) + '\n')
print(json.dumps(parsed['receipt'], ensure_ascii=False))
