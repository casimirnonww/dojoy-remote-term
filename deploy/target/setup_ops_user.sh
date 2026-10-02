#!/usr/bin/env bash
# Prepare a freshly reinstalled Linux host for the web terminal.
#
# Run as root ON THE TARGET HOST (fa, tencent-new, tencent-main, or the gateway itself):
#   sudo ./setup_ops_user.sh --id fa --pubkey 'restrict,pty ssh-ed25519 AAAA... remote-term-fa'
# The --pubkey line is what `remote-term-admin keygen <id>` printed on the gateway.
# --password-hash '$6$...' sets ops' password from a hash (the join scripts pass the one
# chosen at install time) instead of asking for it.
#
# What it does:
#   * creates the "ops" account (the web terminal logs in as ops, never as root);
#   * puts ops in the sudo/wheel group and makes you set a password, so sudo always asks;
#   * installs the gateway key with restrict,pty (no forwarding of any kind);
#   * prints this host's SSH host key for `remote-term-admin hostkey <id> ...`.
set -euo pipefail

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 64
}

host_id=""
pubkey=""
user="ops"
password_hash=""
while [ $# -gt 0 ]; do
    case "$1" in
        --id) host_id=${2:-}; shift 2 ;;
        --password-hash) password_hash=${2:-}; shift 2 ;;
        --pubkey) pubkey=${2:-}; shift 2 ;;
        --user) user=${2:-}; shift 2 ;;
        -h | --help) usage ;;
        *) echo "未知参数：$1" >&2; usage ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { echo "请用 root 运行。" >&2; exit 1; }
[[ "$host_id" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] || { echo "--id 无效。" >&2; exit 64; }
[[ "$user" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$user" != "root" ]] || { echo "--user 无效。" >&2; exit 64; }
if [ -n "$password_hash" ] && ! [[ "$password_hash" =~ ^\$(6|5|y)\$[./A-Za-z0-9$]+$ ]]; then
    echo "--password-hash 无效（应为 \$6\$ 开头的哈希）。" >&2
    exit 64
fi

# Accept the full line from remote-term-admin or a bare "type base64 [comment]" key.
key_type=""
key_blob=""
read -r -a words <<< "$pubkey"
for ((i = 0; i < ${#words[@]} - 1; i++)); do
    case "${words[$i]}" in
        ssh-ed25519 | ecdsa-sha2-nistp256 | ecdsa-sha2-nistp384 | ecdsa-sha2-nistp521 | ssh-rsa)
            key_type=${words[$i]}
            key_blob=${words[$((i + 1))]}
            break
            ;;
    esac
done
if [ -z "$key_type" ] || ! [[ "$key_blob" =~ ^[A-Za-z0-9+/]+=*$ ]]; then
    echo "--pubkey 里没有找到有效的 SSH 公钥。" >&2
    exit 64
fi
tag="remote-term-$host_id"
line="restrict,pty $key_type $key_blob $tag"

if ! id "$user" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$user"
    echo "已创建用户 $user。"
fi
admin_group=""
for group in sudo wheel; do
    if getent group "$group" >/dev/null; then
        admin_group=$group
        break
    fi
done
if [ -n "$admin_group" ]; then
    usermod -aG "$admin_group" "$user"
else
    echo "警告：没有 sudo/wheel 组，$user 无法 sudo。" >&2
fi

# sudo must always ask for ops' password: a NOPASSWD rule would hand root to whoever
# controls the gateway's key.
if grep -rEqs "^[^#]*\b$user\b.*NOPASSWD" /etc/sudoers /etc/sudoers.d; then
    echo "警告：sudoers 里有 $user 的 NOPASSWD 规则，请删除；remote-term-admin enable 会拒绝启用。" >&2
fi
password_state=$(passwd -S "$user" 2>/dev/null | awk '{print $2}')
if [ -n "$password_hash" ]; then
    usermod -p "$password_hash" "$user"
    echo "已设置 $user 的密码（sudo 时输入安装入口机时设的 ops 密码）。"
elif [ "$password_state" != "P" ]; then
    if [ -t 0 ]; then
        echo "请为 $user 设置密码（sudo 时要输入，不要和其他机器相同）："
        passwd "$user"
    else
        echo "警告：$user 还没有密码；请交互运行 passwd $user，否则 sudo 不可用。" >&2
    fi
fi

home=$(getent passwd "$user" | cut -d: -f6)
install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
keys="$home/.ssh/authorized_keys"
touch "$keys"
chown "$user:$user" "$keys"
chmod 600 "$keys"
tmp=$(mktemp "$home/.ssh/.authorized_keys.XXXXXX")
awk -v tag="$tag" 'NF == 0 || $NF != tag' "$keys" > "$tmp"
echo "$line" >> "$tmp"
chown "$user:$user" "$tmp"
chmod 600 "$tmp"
mv -f "$tmp" "$keys"
echo "已为 $user 安装网页终端公钥（$tag，restrict,pty）。"

echo
echo "下一步：在入口机上运行（整行复制）："
for hostkey in /etc/ssh/ssh_host_ed25519_key.pub /etc/ssh/ssh_host_ecdsa_key.pub; do
    if [ -r "$hostkey" ]; then
        echo "  sudo remote-term-admin hostkey $host_id $(cut -d' ' -f1,2 "$hostkey")"
        echo "  （指纹：$(ssh-keygen -lf "$hostkey" | cut -d' ' -f2)，与入口机输出比对）"
        break
    fi
done
