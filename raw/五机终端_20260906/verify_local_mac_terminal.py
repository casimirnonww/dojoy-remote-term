"""Read-only real ttyd/SSH acceptance over an ephemeral loopback SSH forward."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
from urllib.request import urlopen
from websockets.asyncio.client import connect

with socket.socket() as candidate:
    candidate.bind(('127.0.0.1',0))
    port=candidate.getsockname()[1]
forward=subprocess.Popen(['ssh','-N','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8',
                          '-o','ExitOnForwardFailure=yes','-L',f'127.0.0.1:{port}:127.0.0.1:7686','hermes-vps'],
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

async def verify():
    origin=f'http://127.0.0.1:{port}'
    with urlopen(origin+'/mac-local/',timeout=5) as response:
        assert response.status==200
        body=response.read()
        assert b'ttyd' in body
    with urlopen(origin+'/mac-local/token',timeout=5) as response:
        token=json.load(response).get('token','')
    code='import json,platform,subprocess; print("LOCAL_MAC_ACCEPTANCE="+json.dumps({"hostname":platform.node(),"model":subprocess.check_output(["sysctl","-n","hw.model"],text=True).strip(),"memory_bytes":int(subprocess.check_output(["sysctl","-n","hw.memsize"],text=True))}))'
    command='python3 -c '+shlex.quote(code)+'\n'
    async with connect(f'ws://127.0.0.1:{port}/mac-local/ws',subprotocols=['tty'],origin=origin,open_timeout=8) as ws:
        await ws.send(json.dumps({'AuthToken':token,'columns':120,'rows':30}))
        await ws.send(b'0'+command.encode())
        output=''
        deadline=time.monotonic()+18
        result=None
        while time.monotonic()<deadline:
            message=await asyncio.wait_for(ws.recv(),timeout=max(0.1,deadline-time.monotonic()))
            if isinstance(message,bytes):
                if message[:1]!=b'0':continue
                text=message[1:].decode(errors='replace')
            else:
                if not message.startswith('0'):continue
                text=message[1:]
            output+=text
            match=re.search(r'LOCAL_MAC_ACCEPTANCE=(\{[^\r\n]+\})',output)
            if match:
                result=json.loads(match.group(1))
                break
        assert result and result['model']=='MacBookPro18,2' and result['memory_bytes']==68719476736
        await ws.send(b'0exit\n')
        return {'verified_at':datetime.now(timezone.utc).isoformat(),'ttyd_http':200,'websocket':'passed','identity':result,'test_session_exit_sent':True}

try:
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=0.5):break
        except OSError:
            assert forward.poll() is None, 'Temporary SSH forward failed'
            time.sleep(0.15)
    receipt=asyncio.run(verify())
finally:
    forward.terminate()
    try:forward.wait(timeout=5)
    except subprocess.TimeoutExpired:forward.kill();forward.wait(timeout=3)
receipt['temporary_forward_closed']=forward.poll() is not None
Path(__file__).with_name('local-mac-terminal-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False))
