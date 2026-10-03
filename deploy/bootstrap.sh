#!/usr/bin/env bash
# DOJOY 服务器终端：在入口机（VPS）上一行安装。
#
# 在 VPS 的网页控制台里以 root 粘贴这一行（仓库是公开的）：
#   curl -fsSL https://raw.githubusercontent.com/casimirnonww/dojoy-remote-term/main/deploy/bootstrap.sh | bash
#
# 如果仓库改回私有，改用只读令牌（fine-grained token，只勾选本仓库的 Contents: Read-only），
# 粘贴这一行，按提示输入令牌：
#   read -rsp 'GitHub 令牌：' T && echo && printf 'header = "Authorization: Bearer %s"\nheader = "Accept: application/vnd.github.raw"\n' "$T" | curl -fsSL -K - -o /root/dojoy-bootstrap.sh 'https://api.github.com/repos/casimirnonww/dojoy-remote-term/contents/deploy/bootstrap.sh?ref=main' && DOJOY_GITHUB_TOKEN="$T" bash /root/dojoy-bootstrap.sh; unset T
#
# 它下载本仓库 main 分支的代码到 /opt/remote-term-src，然后运行
# deploy/gateway/install_gateway.sh。安装过程中会问三样东西：GitHub OAuth App 的
# Client ID、Client secret，以及 ops 密码。重复运行是安全的（会更新代码，保留已有设置）。
# 有 DOJOY_GITHUB_TOKEN 时，令牌只用来下载代码：不写入磁盘，不出现在命令行参数里，
# 也不传给 install_gateway.sh。
set -euo pipefail

REPO="casimirnonww/dojoy-remote-term"
BRANCH="${DOJOY_BRANCH:-main}"
DOMAIN="${DOJOY_DOMAIN:-djai.djscz.com}"
GITHUB_USER="${DOJOY_GITHUB_USER:-casimirnonww}"
TOKEN="${DOJOY_GITHUB_TOKEN:-}"
unset DOJOY_GITHUB_TOKEN
if [ -n "$TOKEN" ]; then
    TARBALL="${DOJOY_TARBALL_URL:-https://api.github.com/repos/$REPO/tarball/$BRANCH}"
else
    TARBALL="${DOJOY_TARBALL_URL:-https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH}"
fi
SOURCE="${DOJOY_SOURCE:-/opt/remote-term-src}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行（DigitalOcean 网页控制台默认就是 root；否则在命令前加 sudo）。" >&2
    exit 1
fi

case "$TOKEN" in
    *[!A-Za-z0-9_]*) echo "令牌格式不对：应该是 github_pat_ 开头的一串字母、数字和下划线。" >&2; exit 1 ;;
esac

fetch() {
    if [ -n "$TOKEN" ]; then
        # printf is a shell builtin, so the token never shows up in any process's argv.
        # The API answers with a redirect to a pre-signed codeload URL; curl does not
        # forward the Authorization header to that other host.
        printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -fsSL --proto '=https' -K - -L "$TARBALL"
    else
        curl -fsSL --proto '=https' "$TARBALL"
    fi
}

echo "==> 下载代码（${REPO}，分支 ${BRANCH}）"
download=$(mktemp -d)
trap 'rm -rf "$download"' EXIT
if ! fetch | tar -xz -C "$download"; then
    if [ -n "$TOKEN" ]; then
        echo "下载失败：令牌不对、已过期，或者建令牌时没有选这个仓库、没有给 Contents 只读权限。" >&2
    else
        echo "下载失败。请确认仓库是公开的（Public）；如果仓库是私有的，用本脚本顶部注释里带令牌的那一行。" >&2
    fi
    exit 1
fi
TOKEN=
extracted=$(find "$download" -mindepth 1 -maxdepth 1 -type d | head -n 1)
[ -x "$extracted/deploy/gateway/install_gateway.sh" ] || { echo "下载的代码不完整。" >&2; exit 1; }
rm -rf "$SOURCE"
mv "$extracted" "$SOURCE"

# Questions are read from the keyboard, not from this pipe.
if { : </dev/tty; } 2>/dev/null; then
    exec bash "$SOURCE/deploy/gateway/install_gateway.sh" --domain "$DOMAIN" --github-user "$GITHUB_USER" \
        ${DOJOY_EMAIL:+--email "$DOJOY_EMAIL"} </dev/tty
fi
exec bash "$SOURCE/deploy/gateway/install_gateway.sh" --domain "$DOMAIN" --github-user "$GITHUB_USER" \
    ${DOJOY_EMAIL:+--email "$DOJOY_EMAIL"}
