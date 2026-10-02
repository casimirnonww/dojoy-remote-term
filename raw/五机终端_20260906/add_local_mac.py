"""Add this Mac to the explicitly authorized existing terminal site, without publishing source."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = Path(__file__).resolve().parent
SSH = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8', 'hermes-vps']
GUARD = 'env CODEX_APPROVED_RISK=1 codex-safe-run -- python3 - --add-authorized-local-mac'
sha = lambda b: hashlib.sha256(b).hexdigest()

def remote(code, timeout=80):
    result = subprocess.run(SSH + [GUARD], input=code, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        print('REMOTE_OPERATION_FAILED', result.returncode, result.stderr[-2200:])
        raise RuntimeError('Remote operation did not complete')
    return json.loads(result.stdout)

assert pwd.getpwuid(os.geteuid()).pw_name == 'wanghui'
assert subprocess.check_output(['sysctl', '-n', 'hw.model'], text=True).strip() == 'MacBookPro18,2'
trusted_host_key = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()[:2]
key_setup = '''
import json,subprocess,os
from pathlib import Path
expected = TRUSTED_HOST_KEY
r = subprocess.run(['ssh-keyscan','-T','5','-p','22223','-t','ed25519','127.0.0.1'],text=True,capture_output=True,timeout=8)
assert any(line.split()[1:3] == expected for line in r.stdout.splitlines() if not line.startswith('#')), 'Tunnel does not lead to the trusted current Mac'
p=Path('/root/.ssh/remote_term_mac_local_ed25519')
if not p.exists():
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C','remote-term-mac-local','-f',str(p)],check=True)
assert p.with_suffix('.pub').exists()
p.chmod(0o600)
print(json.dumps({'public_key':p.with_suffix('.pub').read_text().strip(),'tunnel_host_key_verified':True}))
'''.replace('TRUSTED_HOST_KEY', repr(trusted_host_key))
key = remote(key_setup, 25)
parts = key['public_key'].split()
assert len(parts) >= 2 and parts[0] == 'ssh-ed25519'
auth = Path('/Users/wanghui/.ssh/authorized_keys')
assert auth.exists() and not auth.is_symlink()
old = auth.read_bytes()
line = 'from="127.0.0.1,::1",no-agent-forwarding,no-X11-forwarding,no-port-forwarding ' + ' '.join(parts[:2]) + ' remote-term-mac-local\n'
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
auth_backup = auth.with_name('authorized_keys.before-remote-term-local-' + stamp)
if parts[1].encode() not in old:
    auth_backup.write_bytes(old)
    auth_backup.chmod(0o600)
    assert auth.read_bytes() == old, 'Local authorization changed concurrently'
    staged = auth.with_name('authorized_keys.remote-term-local.pending')
    staged.write_bytes(old + (b'\n' if old and not old.endswith(b'\n') else b'') + line.encode())
    staged.chmod(0o600)
    os.replace(staged, auth)
else:
    auth_backup = None
assert line.encode().strip() in auth.read_bytes(), 'Existing key has different restrictions; inspect before proceeding'
print('LOCAL_AUTHORIZATION_READY', flush=True)

source = ROOT / '01_五机终端'
payload = {'page':base64.b64encode((source/'01_选机页/index.html').read_bytes()).decode(),
           'collector':base64.b64encode((source/'03_运行状态/collect_status.py').read_bytes()).decode(),
           'local_auth_backup':str(auth_backup) if auth_backup else None}
code = 'PAYLOAD = ' + repr(payload) + '\n' + (EVIDENCE/'deploy_local_mac_remote.py').read_text()
result = remote(code)
receipt = result['receipt']
receipt['local_authorization_backup'] = str(auth_backup) if auth_backup else None
receipt['local_authorization_sha256'] = sha(auth.read_bytes())
receipt['host_key_matched_current_mac'] = key['tunnel_host_key_verified']
(EVIDENCE/'local-mac-deploy-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
(EVIDENCE/'status-live.json').write_text(json.dumps(result['snapshot'],ensure_ascii=False,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False))
