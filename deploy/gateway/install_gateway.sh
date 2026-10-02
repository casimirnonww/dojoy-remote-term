#!/usr/bin/env bash
# Install the remote-term gateway on a FRESHLY REINSTALLED Ubuntu 24.04 server.
#
#   sudo ./deploy/gateway/install_gateway.sh --domain djai.djscz.com \
#        --github-user <your GitHub username> --email <you@example.com>
#
# Before running (see docs/恢复手册.md):
#   * the domain's DNS points at this server and ports 80/443 are reachable;
#   * your GitHub account has two-factor authentication turned on;
#   * you created a GitHub OAuth App with callback https://<domain>/oauth2/callback and
#     have its client ID and secret at hand (asked once, on the first run).
#
# Safe to re-run: existing keys, tokens, certificate and oauth2-proxy config are kept.
set -euo pipefail

OAUTH2_PROXY_VERSION="v7.15.5"

usage() {
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
}

domain=""
github_user=""
email=""
while [ $# -gt 0 ]; do
    case "$1" in
        --domain) domain=${2:-}; shift 2 ;;
        --github-user) github_user=${2:-}; shift 2 ;;
        --email) email=${2:-}; shift 2 ;;
        -h | --help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

repo=$(cd "$(dirname "$0")/../.." && pwd)
step() { printf '\n==> %s\n' "$*"; }
die() { echo "错误：$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo）。"
[[ "$domain" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
    || die "--domain 必须是完整域名，例如 djai.djscz.com。"
[[ "$github_user" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,37}[A-Za-z0-9])?$ ]] || die "--github-user 无效。"
[[ "$email" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]] || die "--email 无效（用于 Let's Encrypt 证书通知）。"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "只支持 Ubuntu（当前：${ID:-unknown}）。"
[ "${VERSION_ID:-}" = "24.04" ] || echo "提示：脚本按 Ubuntu 24.04 编写，当前是 ${VERSION_ID:-?}。"
python3 - "$repo/hosts.json" <<'PY' || die "hosts.json 里必须有 id 为 vps、ssh.host 为 127.0.0.1 的入口机条目。"
import json, sys
hosts = json.load(open(sys.argv[1], encoding="utf-8"))["hosts"]
sys.exit(not any(h.get("id") == "vps" and h.get("ssh", {}).get("host") == "127.0.0.1" for h in hosts))
PY

step "安装软件包（nginx、ttyd、certbot）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q nginx ttyd python3 certbot openssh-server curl ca-certificates

step "创建系统账户（都没有登录 shell）"
ensure_user() {
    local name=$1 home=$2
    if ! id "$name" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "$home" --shell /usr/sbin/nologin "$name"
    fi
}
ensure_user remote-term /var/lib/remote-term-ttyd     # runs ttyd and the outgoing SSH
ensure_user remote-term-rx /nonexistent               # runs the status receiver
ensure_user oauth2-proxy /nonexistent                 # runs the login proxy
ensure_user tunnel /var/lib/remote-term-tunnel        # holds the Macs' reverse tunnels
install -d -m 755 -o remote-term -g remote-term /var/lib/remote-term-ttyd
install -d -m 755 -o root -g root /var/lib/remote-term-tunnel
# Public-key login must work for "tunnel" even though it has no password.
usermod -p '*' tunnel

step "安装程序到 /opt/remote-term"
staging=$(mktemp -d /opt/.remote-term.XXXXXX)
cp -r "$repo/status" "$repo/deploy" "$repo/web" "$repo/hosts.json" "$repo/README.md" "$staging/"
[ -d "$repo/docs" ] && cp -r "$repo/docs" "$staging/"
find "$staging" -name '__pycache__' -prune -exec rm -rf {} +
chown -R root:root "$staging"
chmod -R u=rwX,go=rX "$staging"
chmod 755 "$staging/deploy/gateway/remote-term-admin" "$staging/deploy/gateway/remote-ttyd-ssh"
rm -rf /opt/remote-term.previous
[ -d /opt/remote-term ] && mv /opt/remote-term /opt/remote-term.previous
mv "$staging" /opt/remote-term
rm -rf /opt/remote-term.previous
install -m 755 /opt/remote-term/deploy/gateway/remote-ttyd-ssh /usr/local/bin/remote-ttyd-ssh
ln -sfn /opt/remote-term/deploy/gateway/remote-term-admin /usr/local/sbin/remote-term-admin

step "配置目录 /etc/remote-term"
install -d -m 755 /etc/remote-term /etc/remote-term/ttyd
install -d -m 700 -o remote-term -g remote-term /etc/remote-term/keys
[ -f /etc/remote-term/known_hosts ] || install -m 644 /dev/null /etc/remote-term/known_hosts
[ -f /etc/remote-term/tunnel_authorized_keys ] || install -m 644 /dev/null /etc/remote-term/tunnel_authorized_keys
if [ ! -f /etc/remote-term/agent-tokens.json ]; then
    printf '{"tokens":{}}\n' > /etc/remote-term/agent-tokens.json
fi
chown root:remote-term-rx /etc/remote-term/agent-tokens.json
chmod 640 /etc/remote-term/agent-tokens.json
printf '%s\n' "$domain" > /etc/remote-term/domain
install -d -m 755 /var/www/remote-term /var/www/letsencrypt

step "systemd 服务"
install -m 644 "$repo"/deploy/gateway/systemd/*.service /etc/systemd/system/
systemctl daemon-reload

step "sshd：只能做反向隧道的 tunnel 账户"
install -m 644 "$repo/deploy/gateway/sshd/60-remote-term-tunnel.conf" /etc/ssh/sshd_config.d/
sshd -t
systemctl try-reload-or-restart ssh.service

step "oauth2-proxy ${OAUTH2_PROXY_VERSION}（GitHub 登录）"
if ! /usr/local/bin/oauth2-proxy --version 2>/dev/null | grep -qF "$OAUTH2_PROXY_VERSION"; then
    arch=$(dpkg --print-architecture)
    case "$arch" in amd64 | arm64) ;; *) die "不支持的架构：$arch" ;; esac
    base="oauth2-proxy-${OAUTH2_PROXY_VERSION}.linux-${arch}"
    release="https://github.com/oauth2-proxy/oauth2-proxy/releases/download/${OAUTH2_PROXY_VERSION}"
    download=$(mktemp -d)
    curl -fsSL --proto '=https' -o "$download/$base.tar.gz" "$release/$base.tar.gz"
    expected=""
    for sums in "$base.tar.gz-sha256sum.txt" "$base-sha256sum.txt"; do
        if curl -fsSL --proto '=https' -o "$download/sums" "$release/$sums" 2>/dev/null; then
            expected=$(awk 'NR == 1 {print $1}' "$download/sums")
            break
        fi
    done
    actual=$(sha256sum "$download/$base.tar.gz" | awk '{print $1}')
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        die "oauth2-proxy 下载校验失败。"
    fi
    tar -xzf "$download/$base.tar.gz" -C "$download"
    install -m 755 "$download/$base/oauth2-proxy" /usr/local/bin/oauth2-proxy
    rm -rf "$download"
fi
install -d -m 750 -o root -g oauth2-proxy /etc/oauth2-proxy
oauth_config=/etc/oauth2-proxy/oauth2-proxy.cfg
if [ ! -f "$oauth_config" ]; then
    client_id=${OAUTH2_CLIENT_ID:-}
    client_secret=${OAUTH2_CLIENT_SECRET:-}
    if [ -z "$client_id" ]; then
        read -r -p "GitHub OAuth App 的 Client ID：" client_id
    fi
    if [ -z "$client_secret" ]; then
        read -r -s -p "GitHub OAuth App 的 Client secret（输入不显示）：" client_secret
        echo
    fi
    [[ "$client_id" =~ ^[A-Za-z0-9._-]{8,}$ ]] || die "Client ID 格式不对。"
    [[ "$client_secret" =~ ^[A-Za-z0-9._-]{16,}$ ]] || die "Client secret 格式不对。"
    umask 077
    CLIENT_ID=$client_id CLIENT_SECRET=$client_secret DOMAIN=$domain GITHUB_USER=$github_user \
        python3 - "$repo/deploy/gateway/oauth2-proxy/oauth2-proxy.cfg.example" "$oauth_config.tmp" <<'PY'
import base64, os, secrets, sys
text = open(sys.argv[1], encoding="utf-8").read()
values = {
    "@@CLIENT_ID@@": os.environ["CLIENT_ID"], "@@CLIENT_SECRET@@": os.environ["CLIENT_SECRET"],
    "@@DOMAIN@@": os.environ["DOMAIN"], "@@GITHUB_USER@@": os.environ["GITHUB_USER"],
    "@@COOKIE_SECRET@@": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
}
for key, value in values.items():
    text = text.replace(key, value)
assert "@@" not in text
open(sys.argv[2], "w", encoding="utf-8").write(text)
PY
    umask 022
    chown root:oauth2-proxy "$oauth_config.tmp"
    chmod 640 "$oauth_config.tmp"
    mv -f "$oauth_config.tmp" "$oauth_config"
else
    echo "保留已有的 $oauth_config（如要更换 GitHub 账号或 OAuth App，请直接编辑后重启 oauth2-proxy）。"
fi

step "TLS 证书（Let's Encrypt）"
if [ ! -f "/etc/letsencrypt/live/$domain/fullchain.pem" ]; then
    # Temporary HTTP-only site so Let's Encrypt can reach the challenge directory.
    cat > /etc/nginx/sites-available/remote-term.conf <<NGINX
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name $domain;
    location ^~ /.well-known/acme-challenge/ { root /var/www/letsencrypt; }
    location / { return 404; }
}
NGINX
    ln -sfn /etc/nginx/sites-available/remote-term.conf /etc/nginx/sites-enabled/remote-term.conf
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl reload-or-restart nginx
    certbot certonly --webroot -w /var/www/letsencrypt -d "$domain" --email "$email" \
        --agree-tos --no-eff-email --non-interactive --deploy-hook "systemctl reload nginx"
fi

step "生成并安装 nginx / ttyd / SSH 配置"
remote-term-admin apply

step "启动服务"
systemctl enable --now remote-term-receiver.service oauth2-proxy.service nginx.service
systemctl reload nginx

step "入口机自己的终端（vps）：ops 账户 + 专用密钥"
remote-term-admin keygen vps >/dev/null
"$repo/deploy/target/setup_ops_user.sh" --id vps --pubkey "$(remote-term-admin pubkey vps)"
read -r -a own_key < /etc/ssh/ssh_host_ed25519_key.pub
remote-term-admin hostkey vps "${own_key[0]}" "${own_key[1]}" >/dev/null
remote-term-admin enable vps

step "入口机自己的状态上报"
if [ ! -f /etc/remote-term-agent.env ]; then
    remote-term-admin token vps 2>/dev/null \
        | "$repo/deploy/agent/install_agent_linux.sh" --url "http://127.0.0.1:8790/api/report" --token-stdin
else
    echo "已安装（/etc/remote-term-agent.env 存在）。"
fi

step "完成"
echo "网页入口：https://$domain/ （用 GitHub 账号 $github_user 登录）"
echo
echo "两台 Mac 运行 setup_mac.sh 时需要的入口机主机公钥（--gateway-hostkey，整段复制）："
echo "  ${own_key[0]} ${own_key[1]}"
echo "  指纹：$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | cut -d' ' -f2)"
echo
echo "接下来按 docs/恢复手册.md 逐台接入其他机器；随时用 sudo remote-term-admin list 查看进度。"
