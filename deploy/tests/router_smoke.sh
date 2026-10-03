#!/usr/bin/env bash
# Start a real nginx with an existing stream SNI router on 443 (the test fixture in
# test_deploy.py) plus our site rendered in router mode, and check requests go where they
# should. `nginx -t` alone cannot catch two listeners on 443: only binding does.
# Needs root, a free port 80 and 443, and nginx's stream module (libnginx-mod-stream).
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
trap '[ -s "$work/nginx.pid" ] && kill "$(cat "$work/nginx.pid")" 2>/dev/null; rm -rf "$work"' EXIT
mkdir -p "$work/logs" "$work/live" "$work/www"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj /CN=djai.djscz.com \
    -keyout "$work/live/privkey.pem" -out "$work/live/fullchain.pem" 2>/dev/null

python3 - "$repo" "$work" <<'PY'
import sys
from pathlib import Path
repo, work = Path(sys.argv[1]), Path(sys.argv[2])
sys.path[:0] = [str(repo / "deploy"), str(repo / "status"), str(repo / "deploy" / "tests")]
import render_config
from hosts_config import load_hosts
from test_deploy import ROUTER

site = render_config.render_all(load_hosts(repo / "hosts.json"), "djai.djscz.com",
                                "127.0.0.1:18443")["nginx/remote-term.conf"]
site = site.replace("/etc/letsencrypt/live/djai.djscz.com", str(work / "live"))
site = site.replace("/var/www/remote-term", str(work / "www"))
# Not every test machine has IPv6; the IPv6 syntax is covered by the plain `nginx -t` check.
site = "\n".join(line for line in site.splitlines() if "[::]:" not in line)
router = "\n".join(line for line in ROUTER.splitlines() if "[::]:" not in line)
router += "\nupstream work_https { server 127.0.0.1:24444; }\nupstream plain_dashboard { server 127.0.0.1:41596; }\n"
(work / "site.conf").write_text(site, encoding="utf-8")
(work / "router.conf").write_text(router, encoding="utf-8")
(work / "nginx.conf").write_text(f"""load_module /usr/lib/nginx/modules/ngx_stream_module.so;
pid {work}/nginx.pid;
error_log {work}/logs/error.log;
events {{}}
http {{
    access_log off;
    include {work}/site.conf;
}}
stream {{
    include {work}/router.conf;
}}
""", encoding="utf-8")
PY

nginx -c "$work/nginx.conf" -p "$work"
sleep 1

fail() { echo "FAIL: $*" >&2; exit 1; }
request() { curl -s --noproxy '*' -k -o /dev/null -w '%{http_code}' --resolve "$1:$2:127.0.0.1" "$3"; }

# GET on the token-checked report endpoint is refused by our site itself (no login needed).
[ "$(request djai.djscz.com 443 https://djai.djscz.com/api/report)" = 403 ] \
    || fail "djai.djscz.com did not reach the site through the router"
# The router's default backend is ours too; unknown names get no certificate at all.
if curl -s --noproxy '*' -k -o /dev/null --resolve other.example.com:443:127.0.0.1 https://other.example.com/; then
    fail "an unknown name completed a TLS handshake"
fi
[ "$(request djai.djscz.com 80 http://djai.djscz.com/)" = 301 ] || fail "port 80 does not redirect to https"
echo "router smoke test passed"
