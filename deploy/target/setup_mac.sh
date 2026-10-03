#!/bin/bash
# Connect a Mac to the gateway: reverse SSH tunnel + web terminal key.
#
# Run ON THE MAC as the user the web terminal should log in as (not with sudo):
#   ./setup_mac.sh --id mac-local --gateway djai.djscz.com --tunnel-port 22223 \
#       --gateway-hostkey 'ssh-ed25519 AAAA...' \
#       --pubkey 'restrict,pty ssh-ed25519 AAAA... remote-term-mac-local'
#   --gateway-hostkey: printed at the end of install_gateway.sh
#   --pubkey:          printed by `remote-term-admin keygen <id>` on the gateway
#   --tunnel-port:     this Mac's ssh.port in hosts.json (22223 mac-local, 22224 mbp-dojoy, 22225 mba-chris)
#
# What it does:
#   * stops and moves away the old tunnels that logged in to the old VPS as root;
#   * adds the gateway's terminal key to ~/.ssh/authorized_keys with restrict,pty;
#   * creates a tunnel-only key and a LaunchAgent that keeps `ssh -N -R` running as the
#     gateway's restricted "tunnel" account (pinned gateway host key);
#   * prints what to run on the gateway next.
set -euo pipefail

usage() {
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
}

host_id=""
gateway=""
tunnel_port=""
gateway_hostkey=""
pubkey=""
next_steps=1
while [ $# -gt 0 ]; do
    case "$1" in
        --id) host_id=${2:-}; shift 2 ;;
        --gateway) gateway=${2:-}; shift 2 ;;
        --tunnel-port) tunnel_port=${2:-}; shift 2 ;;
        --gateway-hostkey) gateway_hostkey=${2:-}; shift 2 ;;
        --pubkey) pubkey=${2:-}; shift 2 ;;
        --no-next-steps) next_steps=0; shift ;;
        -h | --help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "这个脚本只用于 macOS。" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "请用要登录的普通用户运行，不要加 sudo。" >&2; exit 1; }
[[ "$host_id" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || { echo "--id 无效。" >&2; exit 64; }
[[ "$gateway" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] || { echo "--gateway 无效。" >&2; exit 64; }
if ! [[ "$tunnel_port" =~ ^[0-9]{4,5}$ ]] || [ "$tunnel_port" -gt 65535 ]; then
    echo "--tunnel-port 无效。" >&2
    exit 64
fi
case "$HOME" in
    *'&'* | *'<'* | *'>'* | *'"'*) echo "HOME 路径含特殊字符，无法写入 LaunchAgent。" >&2; exit 1 ;;
esac

# Return "type blob" for the first SSH public key found in the given text.
extract_key() {
    local word previous=""
    for word in $1; do
        case "$previous" in
            ssh-ed25519 | ecdsa-sha2-nistp256 | ecdsa-sha2-nistp384 | ecdsa-sha2-nistp521 | ssh-rsa)
                if [[ "$word" =~ ^[A-Za-z0-9+/]+=*$ ]]; then
                    echo "$previous $word"
                    return 0
                fi
                ;;
        esac
        previous=$word
    done
    return 1
}

terminal_key=$(extract_key "$pubkey") || { echo "--pubkey 里没有找到有效的 SSH 公钥。" >&2; exit 64; }
gateway_key=$(extract_key "$gateway_hostkey") || { echo "--gateway-hostkey 无效。" >&2; exit 64; }

# 1. Retire the old tunnels (they logged in to the old VPS as root).
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents"
retired="$agents/retired-$(date +%Y%m%d%H%M%S)"
for label in com.dojoy.reverse-ssh-hermes local.codex.controller-reverse-ssh-hermes; do
    if [ -f "$agents/$label.plist" ]; then
        launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
        mkdir -p "$retired"
        mv "$agents/$label.plist" "$retired/"
        echo "已停用旧隧道 ${label}（plist 移到 ${retired}）。"
    fi
done

# 2. Authorize the gateway's web terminal key for this user.
ssh_dir="$HOME/.ssh"
mkdir -p "$ssh_dir"
chmod 700 "$ssh_dir"
keys="$ssh_dir/authorized_keys"
touch "$keys"
chmod 600 "$keys"
tag="remote-term-$host_id"
tmp=$(mktemp "$ssh_dir/.authorized_keys.XXXXXX")
awk -v tag="$tag" 'NF == 0 || $NF != tag' "$keys" > "$tmp"
echo "restrict,pty $terminal_key $tag" >> "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$keys"
echo "已安装网页终端公钥（${tag}，restrict,pty）。"

# 3. Tunnel-only key and pinned gateway host key.
tunnel_key="$ssh_dir/id_ed25519_remote_term_tunnel"
if [ ! -f "$tunnel_key" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "remote-term-tunnel-$host_id" -f "$tunnel_key"
fi
known_hosts="$ssh_dir/remote_term_known_hosts"
echo "remote-term-gateway $gateway_key" > "$known_hosts"
chmod 600 "$known_hosts"

# 4. LaunchAgent that keeps the reverse tunnel up while this user is logged in.
label="com.dojoy.remote-term-tunnel"
plist="$agents/$label.plist"
log="$HOME/Library/Logs/remote-term-tunnel.log"
mkdir -p "$HOME/Library/Logs"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/ssh</string>
    <string>-N</string>
    <string>-R</string>
    <string>127.0.0.1:$tunnel_port:127.0.0.1:22</string>
    <string>-i</string>
    <string>$tunnel_key</string>
    <string>-o</string>
    <string>IdentitiesOnly=yes</string>
    <string>-o</string>
    <string>BatchMode=yes</string>
    <string>-o</string>
    <string>HostKeyAlias=remote-term-gateway</string>
    <string>-o</string>
    <string>UserKnownHostsFile=$known_hosts</string>
    <string>-o</string>
    <string>StrictHostKeyChecking=yes</string>
    <string>-o</string>
    <string>ExitOnForwardFailure=yes</string>
    <string>-o</string>
    <string>ServerAliveInterval=30</string>
    <string>-o</string>
    <string>ServerAliveCountMax=3</string>
    <string>tunnel@$gateway</string>
  </array>
  <key>KeepAlive</key>
  <true/>
  <key>RunAtLoad</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$log</string>
  <key>StandardErrorPath</key>
  <string>$log</string>
</dict>
</plist>
PLIST
plutil -lint "$plist" >/dev/null
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "隧道 LaunchAgent 已启动（日志：${log}）。"

if ! nc -z -G 2 127.0.0.1 22 2>/dev/null; then
    echo
    echo "注意：本机没有开启「远程登录」。请到 系统设置 → 通用 → 共享 → 远程登录 打开，"
    echo "并只允许当前用户（$(id -un)）。"
fi

# Join scripts enroll the keys automatically, so there is nothing to copy by hand.
[ "$next_steps" -eq 1 ] || exit 0
echo
echo "下一步：在入口机上运行（整行复制）："
echo "  sudo remote-term-admin tunnel-key $host_id $(cut -d' ' -f1,2 "$tunnel_key.pub")"
for hostkey in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub; do
    if [ -r "$hostkey" ]; then
        echo "  sudo remote-term-admin hostkey $host_id $(cut -d' ' -f1,2 "$hostkey")"
        echo "  （本机主机公钥指纹：$(ssh-keygen -lf "$hostkey" | cut -d' ' -f2)）"
        break
    fi
done
echo "  sudo remote-term-admin enable $host_id"
