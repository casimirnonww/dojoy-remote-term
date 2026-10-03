#!/usr/bin/env bash
# Install the metrics agent on a Linux host (systemd timer, every 30 seconds).
#
# Run as root ON THE HOST, from a checkout of this repository:
#   sudo ./deploy/agent/install_agent_linux.sh --url https://djai.djscz.com/api/report
# You are asked for the token printed by `remote-term-admin token <id>` on the gateway
# (or pass it on stdin with --token-stdin). The token never goes on the command line.
#
# The agent runs as a throwaway unprivileged user (systemd DynamicUser), only makes
# outbound HTTPS requests, and gives the gateway no way to log in to this host.
set -euo pipefail

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
}

url=""
token_stdin=0
while [ $# -gt 0 ]; do
    case "$1" in
        --url) url=${2:-}; shift 2 ;;
        --token-stdin) token_stdin=1; shift ;;
        -h | --help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

repo=$(cd "$(dirname "$0")/../.." && pwd)
[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行。" >&2; exit 1; }
[[ "$url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/ || "$url" =~ ^http://127\.0\.0\.1(:[0-9]+)?/ ]] \
    || { echo "--url 必须是 https://...（入口机本机可用 http://127.0.0.1:8790/api/report）。" >&2; exit 64; }
command -v systemctl >/dev/null || { echo "需要 systemd。" >&2; exit 1; }
systemd_version=$(systemctl --version | awk 'NR == 1 {print $2}')
[ "${systemd_version%%.*}" -ge 235 ] 2>/dev/null || { echo "systemd 版本过旧（需要 235+）。" >&2; exit 1; }
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' 2>/dev/null \
    || { echo "需要 /usr/bin/python3（3.7 或更新）。" >&2; exit 1; }

if [ "$token_stdin" -eq 1 ]; then
    read -r token
elif [ -t 0 ]; then
    read -r -s -p "粘贴上报 token（输入不显示）：" token
    echo
else
    echo "没有终端可输入 token；请用 --token-stdin。" >&2
    exit 64
fi
# Length is checked apart: macOS regex allows repeat counts up to 255 only ({20,512} never matches there).
if ! [[ "$token" =~ ^[A-Za-z0-9_-]+$ ]] || [ "${#token}" -lt 20 ] || [ "${#token}" -gt 512 ]; then
    echo "token 格式不对。" >&2
    exit 64
fi

install -d -m 755 /opt/remote-term-agent
install -m 644 "$repo/status/agent.py" "$repo/status/probe.py" /opt/remote-term-agent/

umask 077
env_file=/etc/remote-term-agent.env
printf 'REMOTE_TERM_URL=%s\nREMOTE_TERM_TOKEN=%s\n' "$url" "$token" > "$env_file.tmp"
chmod 600 "$env_file.tmp"
mv -f "$env_file.tmp" "$env_file"
umask 022

install -m 644 "$repo/deploy/agent/remote-term-agent.service" "$repo/deploy/agent/remote-term-agent.timer" \
    /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now remote-term-agent.timer >/dev/null

if systemctl start remote-term-agent.service; then
    echo "上报成功；之后每 30 秒自动上报一次。"
else
    echo "首次上报失败，最近的日志：" >&2
    journalctl -u remote-term-agent.service -n 5 --no-pager >&2 || true
    exit 1
fi
