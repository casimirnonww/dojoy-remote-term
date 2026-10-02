#!/usr/bin/env bash
# DOJOY 服务器终端：在入口机（VPS）上一行安装。
#
# 在 VPS 的网页控制台里以 root 运行：
#   curl -fsSL https://raw.githubusercontent.com/casimirnonww/dojoy-remote-term/main/deploy/bootstrap.sh | bash
#
# 它下载本仓库 main 分支的代码到 /opt/remote-term-src，然后运行
# deploy/gateway/install_gateway.sh。安装过程中会问三样东西：GitHub OAuth App 的
# Client ID、Client secret，以及 ops 密码。重复运行是安全的（会更新代码，保留已有设置）。
set -euo pipefail

REPO="casimirnonww/dojoy-remote-term"
BRANCH="${DOJOY_BRANCH:-main}"
DOMAIN="${DOJOY_DOMAIN:-djai.djscz.com}"
GITHUB_USER="${DOJOY_GITHUB_USER:-casimirnonww}"
TARBALL="${DOJOY_TARBALL_URL:-https://codeload.github.com/$REPO/tar.gz/refs/heads/$BRANCH}"
SOURCE=/opt/remote-term-src

if [ "$(id -u)" -ne 0 ]; then
    echo "请用 root 运行（DigitalOcean 网页控制台默认就是 root；否则在命令前加 sudo）。" >&2
    exit 1
fi

echo "==> 下载代码（$REPO，分支 $BRANCH）"
download=$(mktemp -d)
trap 'rm -rf "$download"' EXIT
if ! curl -fsSL --proto '=https' "$TARBALL" | tar -xz -C "$download"; then
    echo "下载失败。请确认仓库已经改成公开（Public），然后重试。" >&2
    exit 1
fi
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
