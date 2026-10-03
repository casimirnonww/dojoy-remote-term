# DOJOY 服务器终端（八机）

一个网页，随时查看 8 台机器的运行状态，并能直接在网页里打开终端。

> 仓库是公开的。里面没有密码或密钥，但别人能看到服务器 IP 和整套配置结构。
> 以后想改回私有：VPS 安装或更新时改用 `deploy/bootstrap.sh` 顶部注释里带令牌的那一行。

---

## 上线步骤（照着做即可）

**第 1 步：GitHub（在浏览器里点）**
1. 开二次验证：打开 https://github.com/settings/security ，按页面提示开启 Two-factor authentication，并**保存好恢复码**。
2. 建 OAuth App：打开 https://github.com/settings/applications/new ，填写：
   - Application name：`DOJOY 服务器终端`
   - Homepage URL：`https://djai.djscz.com`
   - Authorization callback URL：`https://djai.djscz.com/oauth2/callback`

   点 **Register application**，记下页面上的 **Client ID**；再点 **Generate a new client secret**，记下 **Client secret**（只显示一次）。
3. 把仓库改成公开：打开 https://github.com/casimirnonww/dojoy-remote-term/settings ，拉到最下面 **Danger Zone** → **Change visibility** → **Make public**，按提示确认。

   > Client secret 和 ops 密码都**只在 VPS 安装时输入**，不要发到聊天、邮件或任何别的地方。

**第 2 步：VPS（在 DigitalOcean 网页上操作）**

先放行 80 和 443：打开 DigitalOcean → 左侧 **Networking** → **Firewalls**。如果有防火墙应用在这台 VPS 上，点进去，在 **Inbound Rules** 里点 **New rule**，加 **HTTP** 和 **HTTPS** 两条（来源保持 All IPv4、All IPv6），保存。证书申请和以后每 60 天的自动续期都要用 80。没有防火墙就跳过。本机的 ufw 如果开着，安装脚本会自动放行。

然后打开 DigitalOcean → Droplets → 点这台 VPS → 右上角 **Console**（网页终端）。粘贴下面一整行，回车：
```
curl -fsSL https://raw.githubusercontent.com/casimirnonww/dojoy-remote-term/main/deploy/bootstrap.sh | bash
```
过程中会问三样东西：Client ID、Client secret、**ops 密码**。ops 密码只设这一次，所有 Linux 机器用 sudo 时都输它，至少 10 位，请记住。

装完后屏幕上会列出其他 7 台机器各自的「接入命令」。

**第 3 步：其他 7 台机器（每台粘贴一行）**
- 用自己的电脑打开 https://djai.djscz.com/ ，用 GitHub 登录，点左上角「接入其他机器」，就能看到每台机器要粘贴的那一行。
- **财务服务器、腾讯云 · 新服务器、腾讯云 · 大总管**：在阿里云或腾讯云控制台打开这台服务器的网页终端（「远程连接」或「登录」），粘贴它那一行，回车。
- **三台 Mac**：在那台 Mac 上用对应账户（wanghui 的 Mac 用 wanghui，dojoy 的 MacBook Pro 用 dojoy，Chris 的 MacBook Air 用 jokerbu）打开「终端」App，粘贴它那一行，回车。Mac 需要开着「系统设置 → 通用 → 共享 → 远程登录」。
- **Windows 笔记本**：用 wangh 账户登录，在开始菜单上点右键，选「终端(管理员)」，粘贴它那一行，回车。脚本会开启 Windows 自带的 OpenSSH 服务（只监听本机、只认密钥），并装好开机自动运行的隧道和上报程序。

每台粘贴完大约 1 分钟，网页上就会显示这台机器「在线」，终端也能直接打开，不用再回 VPS 做任何事。每条接入命令只能用一次，用完自动作废；用之前一直有效，更新入口机也不会让它失效。

**之后**：任何电脑打开 https://djai.djscz.com/ ，用 GitHub 登录即可。终端里登录的是 `ops`，要管理员权限时输入 `sudo` 加 ops 密码。

> ⚠️ 风险提醒（你已知晓）：这台 VPS 在 2026-09-13 被入侵过，这次没有重装。如果入侵者还在里面，他可能看到这台机器上的一切。安装时会停用旧采集器和旧终端，并把**与本站冲突**的旧 nginx 网站配置（提供 djai.djscz.com 的，或占用 80、443、旧终端端口的）移到 `/var/backups/remote-term-legacy-*`；其他网站和已有的 443 分流保持不变。以后想彻底处理，按 [docs/恢复手册.md](docs/恢复手册.md) 重装即可。

旧版材料已移到 [archive/2026-09-06/](archive/2026-09-06/)，仅作留档，不要复用。

---

## 新架构

```
浏览器 ──HTTPS──▶ nginx（只认 djai.djscz.com；用 IP 访问时直接拒绝 TLS 握手）
                   │  443 上已有 nginx stream 按域名分流时，本站不占 443，改为监听分流给 djai.djscz.com 的本机端口
                   │  每个请求先过 oauth2-proxy：GitHub 登录 + 该账号的二次验证，只放行指定用户
                   ├─ /、app.js、hosts.js ……   静态页面（严格 CSP）
                   ├─ /status.json            ─▶ receiver（127.0.0.1:8790）
                   ├─ /<机器>/                ─▶ ttyd（本机 UNIX socket，只有 nginx 能连；-O 校验来源，每台最多 6 个）
                   │                               └─ ssh：每台机器单独一把钥匙，登录 ops（非 root）
                   └─ /api/report（不走登录）  ─▶ receiver：每台机器各一个 token
各台机器：agent 以低权限每 30 秒把指标“推”给入口（只出不进）
三台 Mac 和 Windows 笔记本：反向隧道登录入口机的 tunnel 账户（只能转发、只能监听自己的端口）
```

| | 旧版（已废弃） | 新版 |
|---|---|---|
| 网站登录 | 一个 Basic Auth 密码 | GitHub 登录 + 二次验证，只放行指定账号 |
| 终端登录身份 | 各机 **root** | 各机 `ops`，sudo **必须输密码** |
| 入口到各机的钥匙 | 一把共用 `id_ed25519_mesh` | 每台一把，`restrict,pty`（不能做任何转发） |
| WebSocket 来源校验 | 无 | ttyd `-O`，nginx 再校验一次 |
| 本机其他进程能否直连终端 | 能（ttyd 监听回环 TCP、没有自己的登录） | 不能：ttyd 只监听 UNIX socket，只有 nginx 和 remote-term 能连 |
| 状态采集 | 入口 root 登录各机执行代码 | 各机主动上报，入口不持有登录各机的凭据 |
| Mac 隧道 | 以 root 登录 VPS | 专用 `tunnel` 账户：没有 shell，只能监听自己的端口 |
| 公网暴露 | 443 + 18443（IP 直连）+ 免登录路由 | 只有 443/80，必须用域名访问 |
| 能否靠仓库重建 | 不能 | 能：`install_gateway.sh` 一次装好 |

**入口机再次失陷时**：攻击者最多拿到各机 `ops` 和三台 Mac 用户账户的 shell。sudo 还要密码，入口机上也没有各机的 root 凭据。**例外是 Windows 笔记本**：网页终端登录的是 wangh 这个管理员账户，Windows 通过 SSH 登录没有「再输一次密码」这道关，所以入口机失陷就等于这台笔记本的最高权限（按你的选择）。

## 目录结构

```
hosts.json                机器清单的唯一真源（id、名称、类型、SSH 地址与用户）
web/                      页面：index.html + app.js + app.css；hosts.js 由 hosts.json 生成
status/                   probe.py（采集）、agent.py（上报）、receiver.py（接收并提供 status.json）
                          hosts_config.py、status_schema.py（共用校验）、tests/
deploy/bootstrap.sh       VPS 上的一行安装：下载代码并运行 install_gateway.sh
deploy/render_config.py   由 hosts.json 生成 nginx、ttyd、ssh_config、hosts.js
deploy/gateway/           入口机：install_gateway.sh、remote-term-admin（含 join / sync 自动接入）、nginx 模板、systemd、sshd、oauth2-proxy
deploy/target/            目标机：setup_ops_user.sh（Linux）、setup_mac.sh（Mac）
deploy/agent/             上报程序安装：install_agent_linux.sh、install_agent_macos.sh、systemd unit
deploy/windows/           Windows：join.ps1.tmpl（接入脚本模板）、agent.ps1（上报）、tunnel.ps1（隧道）
docs/恢复手册.md           从事件处置到逐台接入、验收的完整步骤
archive/2026-09-06/       旧版报告、回执与配置（已失效）
```

## 怎么用

- 打开 https://djai.djscz.com/ ，用 GitHub 登录。默认显示运行概览，不会自动打开终端。卡片和顶部标签用图标、颜色和标签区分类型：青色机架是服务器，紫色苹果是 Mac，蓝色四格是 Windows。
- 选一台机器即可打开终端：左边是终端，右边是运行概览。「+ 新开终端」最多 6 个，「×」关闭，「重连当前」重建连接。
- 有终端开着时，刷新或关闭页面会先让你确认，因为断开会挂断远端正在运行的前台程序。长任务请放进 `tmux`。
- 状态里的「在线」表示该机最近 90 秒内上报成功，**不代表某个终端还连着**。
- 登录有效期 12 小时；过期后刷新页面重新登录。

## 日常维护（入口机上）

| 要做什么 | 命令 |
|---|---|
| 查看每台机器的接入进度 | `sudo remote-term-admin list` |
| 增删改机器 | 改 `hosts.json` → 运行 `deploy/render_config.py --write-web web/hosts.js` → 提交 → 入口机更新代码后执行 `sudo remote-term-admin apply` |
| 重新接入某台机器（重装后等） | `sudo remote-term-admin join <id> --force`，把打印的一行粘贴到那台机器上 |
| 轮换某台机器的终端钥匙 | `sudo remote-term-admin keygen <id> --force`，再 `join <id> --force` 重新接入 |
| 轮换或作废上报 token | `sudo remote-term-admin token <id>` / `revoke-token <id>` |
| 停用某台机器的终端 | `sudo remote-term-admin disable <id>` |
| 更新代码 | 在 VPS 上重新粘贴「上线步骤」第 2 步那一行（可重复执行，保留密钥、token 和证书；已接入的机器不受影响，不用重新接入） |
| 日志 | `journalctl -u remote-ttyd@<id> -u remote-term-receiver -u oauth2-proxy -u nginx` |
| 证书申请报 `Timeout during connect` | 外网连不上本机 80 端口：在 DigitalOcean 云防火墙放行 HTTP 和 HTTPS，再重新粘贴第 2 步那一行（已输入的不会再问） |

## 开发与测试

```bash
python3 -m unittest discover -s status      # 采集、上报、接收（需 Python 3.9+；agent/probe 兼容 3.7+）
python3 -m unittest discover -s deploy      # 配置生成与 remote-term-admin 辅助函数
python3 deploy/render_config.py --check-web web/hosts.js
node --check web/app.js
shellcheck deploy/*/*.sh deploy/gateway/remote-ttyd-ssh
```

CI（`.github/workflows/ci.yml`）会运行以上全部检查，并用自签证书对生成的 nginx 配置执行 `nginx -t`。

## 已知限制与剩余风险

- **GitHub 账号就是登录入口**：必须开启二次验证。账号被盗就能打开终端，但拿到 root 仍需要 `ops` 的 sudo 密码。
- 入口机失陷能拿到 `ops` 和三台 Mac 用户账户的 shell（不是 root），Mac 账户里有个人数据；Windows 笔记本则是管理员权限。
- 登录过期后，已打开的终端 iframe 会被重定向到 GitHub 并被拦截，需要刷新页面。
- 三台 Mac 只在用户登录、未休眠、能连到入口机时可用。Windows 笔记本开机即可用（不需要登录），睡眠或关机时显示离线。
- Windows 的网页终端是 PowerShell，图形界面程序打不开。
- 页面里原来的「知识库」（`/knowledge/`），以及 Hermes 等旧 VPS 上的其他服务，不在本仓库范围内，重建时需另行处理。
- CPU 与网速是短窗口采样（Linux 约 0.3 秒，macOS 约 1 秒），不是 30 秒平均值；Mac 的内存是 `vm_stat` 估计值；磁盘只统计根文件系统。
