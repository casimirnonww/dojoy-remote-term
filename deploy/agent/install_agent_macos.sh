#!/bin/bash
# Install the metrics agent on a Mac (LaunchAgent, runs as you, every 30 seconds).
#
# Run ON THE MAC as your normal user (no sudo), from a checkout of this repository:
#   ./deploy/agent/install_agent_macos.sh --url https://djai.djscz.com/api/report
# You are asked for the token printed by `remote-term-admin token <id>` on the gateway.
#
# The agent only makes outbound HTTPS requests; it reports while you are logged in.
set -euo pipefail

usage() {
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
}

url=""
token_file=""
while [ $# -gt 0 ]; do
    case "$1" in
        --url) url=${2:-}; shift 2 ;;
        --token-file) token_file=${2:-}; shift 2 ;;
        -h | --help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

repo=$(cd "$(dirname "$0")/../.." && pwd)
[ "$(uname -s)" = "Darwin" ] || { echo "这个脚本只用于 macOS。" >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "请用普通用户运行，不要加 sudo。" >&2; exit 1; }
[[ "$url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/ ]] || { echo "--url 必须是 https://..." >&2; exit 64; }
case "$HOME" in
    *'&'* | *'<'* | *'>'* | *'"'*) echo "HOME 路径含特殊字符，无法写入 LaunchAgent。" >&2; exit 1 ;;
esac
python=/usr/bin/python3
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null \
    || { echo "需要 /usr/bin/python3（3.7+）。如提示安装命令行工具，请先安装后重试。" >&2; exit 1; }

if [ -n "$token_file" ]; then
    token=$(tr -d '[:space:]' < "$token_file")
else
    read -r -s -p "粘贴上报 token（输入不显示）：" token
    echo
fi
[[ "$token" =~ ^[A-Za-z0-9_-]{20,512}$ ]] || { echo "token 格式不对。" >&2; exit 64; }

dir="$HOME/Library/Application Support/remote-term-agent"
mkdir -p "$dir"
chmod 700 "$dir"
cp "$repo/status/agent.py" "$repo/status/probe.py" "$dir/"
(umask 077 && printf '%s\n' "$token" > "$dir/token")

# The system python's OpenSSL may not see the keychain; point it at macOS's CA bundle.
cafile=()
cafile_xml=""
if [ -r /etc/ssl/cert.pem ]; then
    cafile=(--cafile /etc/ssl/cert.pem)
    cafile_xml="<string>--cafile</string><string>/etc/ssl/cert.pem</string>"
fi
label="com.dojoy.remote-term-agent"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$HOME/Library/Logs/remote-term-agent.log"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>$python</string>
    <string>$dir/agent.py</string>
    <string>--url</string>
    <string>$url</string>
    <string>--token-file</string>
    <string>$dir/token</string>
    $cafile_xml
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONDONTWRITEBYTECODE</key>
    <string>1</string>
  </dict>
  <key>StartInterval</key>
  <integer>30</integer>
  <key>RunAtLoad</key>
  <true/>
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

# ${a[@]+...}: bash 3.2 (macOS) treats an empty array as unset under set -u.
if PYTHONDONTWRITEBYTECODE=1 "$python" "$dir/agent.py" --url "$url" --token-file "$dir/token" \
    ${cafile[@]+"${cafile[@]}"}; then
    echo "上报成功；登录期间每 30 秒自动上报一次（日志：${log}）。"
else
    echo "首次上报失败，见上面的提示。" >&2
    exit 1
fi
