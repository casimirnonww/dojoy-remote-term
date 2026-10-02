"""Local browser regression fixture. Never connects to SSH or remote hosts.

Uses the workstation's existing websockets package for testing only.
The browser page has no build dependency; production collection uses only Python's standard library.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response

PAGE = Path(__file__).resolve().parents[2] / '01_五机终端/01_选机页/index.html'
HOST_PATHS = {'/vps/', '/aliyun-new/', '/tencent-new/', '/tencent-main/', '/mbp-dojoy/', '/mac-local/'}
active = {}
events = []
sequence = 0
status_mode = 'online'
status_requests = 0
LIVE_STATUS = Path(__file__).with_name('status-live.json')


def sample_status(mode):
    """Synthetic, labelled fixtures for UI regressions; never business evidence."""
    now = datetime.now(timezone.utc)
    stamp = (now - timedelta(minutes=10) if mode == 'stale' else now).isoformat()
    hosts = []
    for host_id, name in [('vps', 'VPS'), ('fa', '财务机'), ('tencent-new', '腾讯新机'),
                          ('tencent-main', '腾讯大总管'), ('mbp-dojoy', '另一台 Mac'), ('mac-local', '本机 Mac')]:
        offline = mode == 'unreachable' and host_id == 'mbp-dojoy'
        hosts.append({'id': host_id, 'name': name, 'checked_at': stamp,
                      'last_success_at': (now - timedelta(minutes=10)).isoformat() if offline else stamp,
                      'status': 'unreachable' if offline else 'online', 'duration_ms': 500,
                      'error': '本地回归：连接超时' if offline else None,
                      'metrics': {'hostname': 'fixture-' + host_id, 'os': '本地回归数据',
                                  'arch': 'test', 'cpu_model': '<b id="audit-injection">CPU</b>' if mode == 'invalid' else '测试 CPU',
                                  'cpu_cores': 4, 'cpu_percent': 'invalid' if mode == 'invalid' else 25,
                                  'memory_total_bytes': 8 * 1024**3, 'memory_used_bytes': 2 * 1024**3,
                                  'disk_total_bytes': 100 * 1024**3, 'disk_used_bytes': 40 * 1024**3,
                                  'uptime_seconds': 90061, 'load_1': 0.3,
                                  'network_rx_bytes_per_second': 1024, 'network_tx_bytes_per_second': 2048,
                                  'network_interface': 'test0'}})
    return {'schema_version': 1, 'generated_at': stamp, 'refresh_interval_seconds': 30, 'hosts': hosts}

TERMINAL = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<style>body{background:#101824;color:#e8eef7;font:16px system-ui;padding:20px}
input,button{font:inherit;margin:4px;padding:8px}</style>
<h1>本地回归终端</h1><p>仅测试页面连接生命周期，没有连接任何真实主机。</p>
<p id="status">正在建立本地测试连接</p>
<label>测试消息 <input id="message" value="roundtrip"></label>
<button id="send" disabled>发送测试消息</button><pre id="output"></pre>
<script>
const socket = new WebSocket('ws://' + location.host + '/ws?host='
  + encodeURIComponent(location.pathname) + '&session='
  + encodeURIComponent(new URLSearchParams(location.search).get('session') || ''));
const statusEl=document.querySelector('#status'), send=document.querySelector('#send');
socket.onopen=()=>{statusEl.textContent='本地 WebSocket 已连接';send.disabled=false;};
socket.onmessage=e=>document.querySelector('#output').textContent=e.data;
socket.onclose=()=>{statusEl.textContent='本地 WebSocket 已断开';send.disabled=true;};
send.onclick=()=>socket.send(document.querySelector('#message').value);
</script></html>'''


def reply(status, body, content_type='text/html; charset=utf-8'):
    body = body.encode() if isinstance(body, str) else body
    return Response(status, {200: 'OK', 401: 'Unauthorized', 404: 'Not Found', 502: 'Bad Gateway'}[status],
                    Headers({'Content-Type': content_type, 'Content-Length': str(len(body)),
                             'Cache-Control': 'no-store', 'Connection': 'close'}), body)


async def route(connection, request):
    global status_mode, status_requests
    path = urlsplit(request.path).path
    if path == '/ws':
        return None
    if path == '/__review/state':
        state = {'active': list(active.values()), 'events': events, 'status_requests': status_requests}
        return reply(200, json.dumps(state), 'application/json')
    if path == '/__review/scenario':
        candidate = parse_qs(urlsplit(request.path).query).get('mode', [''])[0]
        if candidate not in {'online', 'stale', 'unreachable', 'invalid', 'missing', 'unauthorized', 'live'}:
            return reply(404, 'unknown test scenario')
        status_mode = candidate
        return reply(200, json.dumps({'mode': status_mode}), 'application/json')
    if path == '/status.json':
        status_requests += 1
        if status_mode == 'missing':
            return reply(404, 'fixture status missing')
        if status_mode == 'unauthorized':
            return reply(401, 'fixture authentication required')
        if status_mode == 'live':
            return reply(200, LIVE_STATUS.read_bytes(), 'application/json') if LIVE_STATUS.exists() else reply(404, 'live sample missing')
        return reply(200, json.dumps(sample_status(status_mode)), 'application/json')
    if path in {'/', '/index.html'}:
        return reply(200, PAGE.read_bytes())
    if path == '/knowledge/':
        return reply(200, '<meta charset="utf-8"><h1>本地知识库入口回归</h1><a href="/">返回终端</a>')
    if path == '/tencent-new/':
        return reply(502, '<meta charset="utf-8"><h1>502 Bad Gateway</h1><p>本地错误页夹具，没有连接远端。</p>')
    if path in HOST_PATHS:
        return reply(200, TERMINAL)
    return reply(404, '<h1>404</h1>')


async def socket_handler(connection):
    global sequence
    sequence += 1
    connection_id = sequence
    query = parse_qs(urlsplit(connection.request.path).query)
    item = {'id': connection_id, 'host': query.get('host', [''])[0],
            'session': query.get('session', [''])[0]}
    active[connection_id] = item
    events.append({'event': 'open', **item})
    try:
        await connection.send('本地连接编号 ' + str(connection_id))
        async for message in connection:
            if message == 'disconnect-test':
                await connection.close(code=1000, reason='local regression disconnect')
            else:
                await connection.send('本地回显：' + message)
    finally:
        active.pop(connection_id, None)
        events.append({'event': 'close', **item})


async def main():
    async with serve(socket_handler, '127.0.0.1', 0, process_request=route,
                     close_timeout=1, max_size=4096) as server:
        print('LOCAL_REVIEW_URL=http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]) + '/', flush=True)
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
