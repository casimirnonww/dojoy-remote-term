"""Remote guarded deployment body; PAYLOAD is injected by add_local_mac.py."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

sha = lambda b: hashlib.sha256(b).hexdigest()
page = Path('/var/www/remote-term/index.html')
collector = Path('/usr/local/lib/remote-term-status/collect_status.py')
nginx = Path('/etc/nginx/conf.d/hermes-skill-dashboard.conf')
ssh_config = Path('/root/.ssh/config')
env_file = Path('/etc/default/remote-ttyd-mac-local')
assert sha(page.read_bytes()) == '3ab4b06a8ca24e68371404c4f0a5e7be60f2552b3bf9294eddb06a5d6d761521', 'HTML changed concurrently'
assert sha(collector.read_bytes()) == 'e8e825437b5f1524e9de2b5ebe383fef64484585ab7bc7246db8c6632b55f278', 'Collector changed concurrently'
assert sha(nginx.read_bytes()) == 'dc15bc579aebd6bc380ad814986f391a21c71130750cb54c9af4fd8cddc508e0', 'nginx changed concurrently'
assert not env_file.exists(), 'ttyd configuration already exists'
ssh_old = ssh_config.read_bytes()
assert not re.search(rb'(?im)^Host\s+.*\bmac-local\b', ssh_old)
new_page = base64.b64decode(PAYLOAD['page'])
new_collector = base64.b64decode(PAYLOAD['collector'])
assert b'"mac-local"' in new_page and b'"mac-local"' in new_collector

identity_command = ['ssh','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes','-o','ConnectTimeout=5',
                    '-i','/root/.ssh/remote_term_mac_local_ed25519','-p','22223','wanghui@127.0.0.1','python3','-']
identity_source = 'import platform,pwd,os,json,subprocess\nprint(json.dumps({"hostname":platform.node(),"user":pwd.getpwuid(os.getuid()).pw_name,"model":subprocess.check_output(["sysctl","-n","hw.model"],text=True).strip(),"arch":platform.machine()}))\n'
r = subprocess.run(identity_command,input=identity_source,text=True,capture_output=True,timeout=12)
assert r.returncode == 0, 'Dedicated Mac SSH authentication failed'
identity = json.loads(r.stdout)
assert identity['model']=='MacBookPro18,2' and identity['user']=='wanghui', 'Unexpected target identity'

backup = Path('/var/backups/remote-term')/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-local-mac')
backup.mkdir(mode=0o700)
originals = {page:page.read_bytes(),collector:collector.read_bytes(),nginx:nginx.read_bytes(),ssh_config:ssh_old}
for path,body in originals.items():
    dest=backup/({'index.html':'index.before-local-mac.html','collect_status.py':'collect_status.before-local-mac.py','hermes-skill-dashboard.conf':'nginx.before-local-mac.conf','config':'ssh-config.before-local-mac'}[path.name])
    dest.write_bytes(body)
    dest.chmod(0o600)
nginx_text=originals[nginx].decode()
match=re.search(r'(?ms)^    location \^~ /mbp-dojoy/ \{\n.*?^    \}',nginx_text)
assert match and '/mac-local/' not in nginx_text
new_location=match.group().replace('/mbp-dojoy/','/mac-local/').replace('127.0.0.1:7685','127.0.0.1:7686')
new_nginx=nginx_text[:match.end()]+'\n\n'+new_location+nginx_text[match.end():]
alias='''Host mac-local
  HostName 127.0.0.1
  Port 22223
  User wanghui
  IdentityFile /root/.ssh/remote_term_mac_local_ed25519
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking yes
  ServerAliveInterval 30
  ServerAliveCountMax 3

'''
options='TTYD_OPTIONS=-i lo -p 7686 -b /mac-local -W -s 15 -t titleFixed=mac-local -t fontSize=14 /usr/local/bin/remote-ttyd-ssh mac-local\n'

def atomic(path,body,mode):
    temporary=path.with_name(path.name+'.local-mac.pending')
    with temporary.open('wb') as handle:
        handle.write(body);handle.flush();os.fsync(handle.fileno())
    temporary.chmod(mode)
    os.replace(temporary,path)

try:
    atomic(ssh_config,alias.encode()+ssh_old,0o600)
    atomic(env_file,options.encode(),0o644)
    atomic(nginx,new_nginx.encode(),0o644)
    subprocess.run(['nginx','-t'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    subprocess.run(['systemctl','enable','--now','remote-ttyd@mac-local.service'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    subprocess.run(['systemctl','reload','nginx'],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    atomic(collector,new_collector,0o644)
    subprocess.run(['systemctl','start','remote-term-status.service'],check=True,timeout=27)
    snapshot=json.loads(Path('/var/www/remote-term/status.json').read_text())
    if not any(h['id']=='mac-local' for h in snapshot['hosts']):
        subprocess.run(['systemctl','start','remote-term-status.service'],check=True,timeout=27)
        snapshot=json.loads(Path('/var/www/remote-term/status.json').read_text())
    local=next(h for h in snapshot['hosts'] if h['id']=='mac-local')
    assert len(snapshot['hosts'])==6 and local['status']=='online', 'New Mac metrics not online'
    assert local['metrics']['arch']=='arm64' and local['metrics']['memory_total_bytes']==68719476736, 'Wrong Mac metrics'
    atomic(page,new_page,0o644)
except Exception:
    for path,body in originals.items():
        atomic(path,body,0o600 if path==ssh_config else 0o644)
    subprocess.run(['systemctl','disable','--now','remote-ttyd@mac-local.service'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if env_file.exists():os.replace(env_file,backup/'failed-remote-ttyd-mac-local')
    subprocess.run(['nginx','-t'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    subprocess.run(['systemctl','reload','nginx'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    raise

units=['nginx.service','remote-term-status.timer']+['remote-ttyd@'+h+'.service' for h in ['vps','aliyun-new','tencent-new','tencent-main','mbp-dojoy','mac-local']]
receipt={'deployed_at':datetime.now(timezone.utc).isoformat(),'backup':str(backup),'identity':identity,
         'html_sha256':sha(page.read_bytes()),'html_bytes':page.stat().st_size,'collector_sha256':sha(collector.read_bytes()),
         'nginx_sha256':sha(nginx.read_bytes()),'units':{unit:subprocess.check_output(['systemctl','is-active',unit],text=True).strip() for unit in units},
         'nginx_pid':subprocess.check_output(['systemctl','show','nginx','--property=MainPID','--value'],text=True).strip(),
         'generated_at':snapshot['generated_at'],'hosts':[{'id':h['id'],'status':h['status']} for h in snapshot['hosts']]}
(backup/'local-mac-deploy-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'receipt':receipt,'snapshot':snapshot},ensure_ascii=False))
