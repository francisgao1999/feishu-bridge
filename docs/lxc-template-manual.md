# 模板 LXC 创建教程（PVE 控制台 + 容器内安装）

本教程创建模板 LXC（VMID `900`，主机名 `oc-template`）。创建一次，之后所有用户 LXC 都从它克隆。

流程分两段：

1. **网页控制台**里用「创建 CT」向导建容器；
2. **容器控制台**里用 shell 装代理、软件和 opencode，最后回网页控制台转成模板。

> 本方案**不提供**模板创建脚本，也不做自动部署。所有操作由管理员在 PVE 控制台手工完成，便于审计和排错。

## 0. 约定与参数

### 0.1 执行位置

本文命令分三类，每条命令前都标了位置：

| 标记 | 在哪执行 | 怎么打开 |
| --- | --- | --- |
| **网页控制台** | PVE 网页界面 | 浏览器打开 PVE，左侧选中节点 |
| **容器控制台** | CT 900 内部 shell | 左侧选中 `900 (oc-template)` → 右上角 `Console` |
| **宿主 Shell** | PVE 节点 shell | 左侧选中节点 → 右上角 `>_ Shell` |

- 容器控制台打开的是容器自己的 tty，会出现 `oc-template login:` 提示，用创建向导里设的 root 密码登录。
- 如果不想记密码，也可以从**宿主 Shell** 执行 `pct enter 900` 直接进入容器。
- 容器内命令都是 root 身份，不需要 `sudo`。

### 0.2 参数表

开工前把这张表填好，后面对应位置替换：

| 参数 | 示例 | 说明 |
| --- | --- | --- |
| `VMID` | `900` | 模板固定用 900 |
| `HOSTNAME` | `oc-template` | 模板主机名 |
| `TEMPLATE_IP` | `192.168.110.20` | 模板创建期临时 IP，转模板后不再使用 |
| `PREFIX` | `24` | 网段掩码，按实际填写 |
| `GATEWAY` | `192.168.110.1` | 网关 |
| `BRIDGE` | `vmbr0` | 网桥 |
| `DOCKER_IP` | `192.168.110.11` | 运行 bridge 的 docker LXC 的 IP |
| `PVE_IP` | `192.168.110.10` | PVE 宿主 IP，`add-user.sh` 和冒烟测试从这里发起 |
| `PROXY_HOST` | `192.168.110.11` | 代理地址，生产环境就是 docker LXC |
| `PROXY_PORT` | `10808` | HTTP 与 SOCKS5 同端口 |

固定规格：内存 `2048M`、swap `1024M`、磁盘 `8G`、CPU `2` 核。

> 模板盘用 8G（克隆快），用户 LXC 克隆后由 `add-user.sh` 扩到 24G。
> 模板 LXC 的 IP 只用于创建期验证，转成模板后不再启动，用户 LXC 克隆时覆盖 `net0`。

### 0.3 确认系统模板已下载

**宿主 Shell**：

```bash
pveam update
pveam available --section system | grep debian
```

没下载就先 `pveam download local debian-13-standard_13.1-1_amd64.tar.zst`（版本号以实际列表为准）。下载完成后在 **网页控制台** → 节点 → `local (存储)` → `CT 模板` 里能看到它。

---

## 1. 创建容器（网页控制台）

### 1.1 用向导创建

**网页控制台**：右上角 `创建 CT`，按下表填写。向导底部勾选 `高级`，能看到全部选项。

| 页签 | 字段 | 值 |
| --- | --- | --- |
| 常规 | 节点 | 你的 PVE 节点 |
| 常规 | CT ID | `900` |
| 常规 | 主机名 | `oc-template` |
| 常规 | 密码 | 设一个临时 root 密码（转模板前可锁定，见 §8） |
| 模板 | 存储 | `local` |
| 模板 | 模板 | `debian-13-standard_*.tar.zst` |
| 磁盘 | 存储 | `local-lvm`（没有就选实际存储） |
| 磁盘 | 磁盘大小 | `8` GiB |
| CPU | 核心 | `2` |
| 内存 | 内存 | `2048` MiB |
| 内存 | 交换分区 | `1024` MiB |
| 网络 | 网桥 | `vmbr0` |
| 网络 | IPv4 | `静态` |
| 网络 | IPv4/CIDR | `<TEMPLATE_IP>/<PREFIX>`，例如 `192.168.110.20/24` |
| 网络 | 网关 | `<GATEWAY>` |
| 网络 | IPv6 | `无` |
| 确认 | 无特权容器 | ✅ 勾选 |
| 确认 | 创建后启动 | ✅ 勾选 |

> `local-lvm` 不存在时，先在 **宿主 Shell** 用 `pvesm status` 查实际存储名。

### 1.2 创建后的两项设置

**网页控制台**：左侧选中 `900 (oc-template)` → `选项`：

1. `功能` → 双击 → 勾选 `嵌套`（nesting），确定。playwright 等需要嵌套。
2. `开机自启动` → 双击 → 设为 `否`。模板不应该开机自启。

> 也可以用**宿主 Shell** 代替：`pct set 900 --features nesting=1 --onboot 0`。

### 1.3 登录容器控制台

**网页控制台**：选中 `900 (oc-template)` → 右上角 `Console`，用向导里设的 root 密码登录。

登录后先确认基本环境：

```bash
hostname                 # 期望 oc-template
ip -4 addr show eth0     # 期望 TEMPLATE_IP/PREFIX
ip route | head -3       # 期望默认路由指向 GATEWAY
```

### 1.4 配置出站代理

生产环境用户 LXC 不能直连外网，所有出站流量走 docker LXC 上的代理（HTTP 与 SOCKS5 同为 `10808`）。**先配代理，再装软件**，否则 `apt` 和 opencode 下载都会超时。

**容器控制台**：

```bash
export DOCKER_IP="192.168.110.11"
export PVE_IP="192.168.110.10"
export PROXY_HOST="192.168.110.11"
export PROXY_PORT="10808"

mkdir -p /etc/opencode
umask 077
cat > /etc/opencode/proxy.env <<EOF
# 出站代理配置（随模板克隆到每个用户 LXC，不需要逐台修改）。
# 权限 600，不要提交到任何仓库。
HTTP_PROXY=http://$PROXY_HOST:$PROXY_PORT
HTTPS_PROXY=http://$PROXY_HOST:$PROXY_PORT
http_proxy=http://$PROXY_HOST:$PROXY_PORT
https_proxy=http://$PROXY_HOST:$PROXY_PORT
ALL_PROXY=socks5h://$PROXY_HOST:$PROXY_PORT
all_proxy=socks5h://$PROXY_HOST:$PROXY_PORT
# 本机与内网直连，避免把内网流量绕到代理上。
NO_PROXY=localhost,127.0.0.1,::1,$PROXY_HOST,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12
no_proxy=localhost,127.0.0.1,::1,$PROXY_HOST,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12
EOF
chmod 600 /etc/opencode/proxy.env

# 登录 shell 自动加载
cat > /etc/profile.d/99-proxy.sh <<'EOF'
if [ -r /etc/opencode/proxy.env ]; then
    set -a
    . /etc/opencode/proxy.env
    set +a
fi
EOF
chmod 644 /etc/profile.d/99-proxy.sh

# apt 不读环境变量，单独配置
cat > /etc/apt/apt.conf.d/99proxy <<EOF
Acquire::http::Proxy "http://$PROXY_HOST:$PROXY_PORT";
Acquire::https::Proxy "http://$PROXY_HOST:$PROXY_PORT";
EOF

# 当前会话立即生效（profile.d 只在登录时读）
set -a
. /etc/opencode/proxy.env
set +a
```

验证代理：

```bash
env | grep -i proxy
curl -fsS -o /dev/null -w '%{http_code}\n' https://api.github.com   # 期望 200
```

说明：

- `opencode-serve.service` 里有 `EnvironmentFile=-/etc/opencode/proxy.env`，opencode 及其启动的所有命令都会继承代理变量。
- 代理配置随模板克隆，**每个用户 LXC 自动带上**，`add-user.sh` 不需要再写。
- `NO_PROXY` 保留 `127.0.0.1`——opencode 依赖回环通信。
- `git` / `pip` 的系统级代理在 §2 装完软件后再写（此时 `git` 才存在）。
- 以上变量只在当前会话有效，重新登录或重开控制台后需要重新 `export`。

---

## 2. 安装基础软件包

**容器控制台**：

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates curl git bash coreutils \
  python3 python3-venv python3-pip \
  nodejs npm \
  jq unzip xz-utils less file procps \
  nftables tzdata
apt-get clean
rm -rf /var/lib/apt/lists/*
```

装完 `git` 后补系统级代理：

```bash
git config --system http.proxy "$HTTP_PROXY"
git config --system https.proxy "$HTTP_PROXY"
mkdir -p /etc
cat > /etc/pip.conf <<EOF
[global]
proxy = $HTTP_PROXY
EOF
npm config set proxy "$HTTP_PROXY" --location=global
npm config set https-proxy "$HTTP_PROXY" --location=global
```

**不安装**：`build-essential`、`postgresql-client`、`default-mysql-client`、`ripgrep`。需要时由用户任务自己 `apt install`。

`nodejs`/`npm` 预装，供 Playwright MCP 使用；`fonts-noto-cjk` 在 §2.3 安装。

### 2.1 取仓库文件

后面要用的 `opencode-serve.service`、`opencode.jsonc`、`workspace-AGENTS.md`、`scripts/smoke-test.sh` 都在仓库里。

**容器控制台**（走代理克隆；仓库是私有的就用带 token 的地址）：

```bash
git clone --depth 1 https://github.com/francisgao1999/feishu-bridge.git /root/feishu-bridge
cd /root/feishu-bridge
ls deploy/lxc scripts
```

克隆不通时，改用**宿主 Shell** 推送。先在开发机把需要的文件传到 PVE 宿主：

```bash
# 开发机（WSL）
scp -r deploy/lxc scripts pve_hzdd:/root/feishu-bridge-files/
```

再在 **PVE 宿主 Shell** 里推到容器：

```bash
pct push 900 /root/feishu-bridge-files/deploy/lxc/opencode-serve.service /tmp/opencode-serve.service
pct push 900 /root/feishu-bridge-files/deploy/lxc/opencode.jsonc /tmp/opencode.jsonc
pct push 900 /root/feishu-bridge-files/deploy/lxc/workspace-AGENTS.md /tmp/workspace-AGENTS.md
pct push 900 /root/feishu-bridge-files/scripts/smoke-test.sh /tmp/smoke-test.sh
```

然后回到**容器控制台**，把 `/tmp` 下的文件归位：

```bash
mkdir -p /root/feishu-bridge/deploy/lxc /root/feishu-bridge/scripts
mv /tmp/opencode-serve.service /tmp/opencode.jsonc /tmp/workspace-AGENTS.md /root/feishu-bridge/deploy/lxc/
mv /tmp/smoke-test.sh /root/feishu-bridge/scripts/
chmod +x /root/feishu-bridge/scripts/smoke-test.sh
```

> 仓库文件只在安装期用，转模板前会删掉（§8），不会留在用户 LXC 里。

### 2.2 设置 locale（避免中文乱码）

Debian 最小系统默认 `LANG=C`，在 PVE 控制台里粘贴或显示中文会乱码。装完软件包后设置：

```bash
printf 'LANG=C.UTF-8\n' > /etc/default/locale
chmod 644 /etc/default/locale
```

`C.UTF-8` 是 glibc 内置，不需要 `locale-gen`。重新登录控制台后生效，模板克隆后每个用户 LXC 自动继承。

> 已经在容器里时，也可以执行 `localectl set-locale LANG=C.UTF-8`（如果系统装了 `systemd` 的 `localectl`），效果相同。

### 2.3 安装 Playwright MCP 与 Chromium

模板预装 Playwright MCP，用户让 agent 截图时不必再临时 `apt install nodejs npm`。

**容器控制台**：

```bash
# 先取 MCP 依赖的 Playwright 版本，保证浏览器 revision 和 MCP 对得上
PW_VERSION=$(npm view @playwright/mcp@latest dependencies.playwright)
npm install -g @playwright/mcp@latest "playwright@$PW_VERSION"
playwright install --with-deps chromium
```

- `npm install -g` 把 `playwright-mcp` 和 `playwright` 两个命令装到 `/usr/local/bin`，`opencode.jsonc` 里的 MCP 直接调用全局命令，不走 `npx` 下载。
- 必须装同版本的 `playwright`：`@playwright/mcp` 依赖的版本可能不是稳定版，版本不一致会导致 MCP 找不到已下载的 Chromium。
- `playwright install --with-deps chromium` 下载 Chromium 到 `/root/.cache/ms-playwright`，并安装运行所需的系统库。下载走代理，命令可能跑几分钟。
- 容器内以 root 运行，`opencode.jsonc` 已给 MCP 加 `--no-sandbox`；无显示器，已加 `--headless`。

验证：

```bash
command -v playwright-mcp playwright
playwright --version
ls /root/.cache/ms-playwright
```

需要截中文页面时再装字体（约 200 MB，非必需）：

```bash
apt-get install -y --no-install-recommends fonts-noto-cjk
```

> 浏览器缓存目录 `/root/.cache/ms-playwright` 会随模板克隆到每个用户 LXC，转模板前不要删。

---

## 3. 安装 opencode

不固定版本，装官方安装脚本给的最新 v2 版本。

**容器控制台**：

```bash
curl -fsSL https://opencode.ai/v2/install | bash -s -- --no-modify-path
install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode
rm -rf /root/.opencode          # 安装器目录只留一份，模板里不必带
opencode --version              # 记下实际版本号
```

> `--no-modify-path` 让安装器不改 `.bashrc`；`systemd` 单元固定用 `/usr/local/bin/opencode`，所以这里把二进制拷过去，模板里不再保留 `/root/.opencode` 副本。

> 下载走代理，当前会话的 `$HTTPS_PROXY` 已在 §1.4 加载。

`fs/write` 是 experimental 接口，不同版本可能调整路径或行为。**装完必须跑 §7 的冒烟测试**，不通过就不要转模板。把 `opencode --version` 的输出记到部署记录里，方便出问题时对照。

---

## 4. 创建目录与配置

**容器控制台**：

```bash
mkdir -p /workspace/uploads /workspace/outbox
mkdir -p /etc/opencode/opencode
mkdir -p /var/lib/opencode /var/cache/opencode
chmod 700 /etc/opencode
```

### 4.1 systemd 单元

```bash
cp /root/feishu-bridge/deploy/lxc/opencode-serve.service /etc/systemd/system/
chmod 644 /etc/systemd/system/opencode-serve.service
```

单元内容要点：

- `EnvironmentFile=-/etc/opencode/proxy.env` 在最前，保证代理变量先加载；
- 后面依次加载 `service.env`、`serve.env`（`-` 表示文件不存在不报错）；
- `ExecStart=/usr/local/bin/opencode serve --hostname 0.0.0.0 --port 4096`；
- `WorkingDirectory=/workspace`，`XDG_CONFIG_HOME=/etc/opencode`。

### 4.2 opencode 配置

```bash
cp /root/feishu-bridge/deploy/lxc/opencode.jsonc /etc/opencode/opencode/opencode.jsonc
```

该文件已内置 `playwright` MCP：调用全局 `playwright-mcp`，固定 `--headless --browser chromium --no-sandbox --isolated`，并把浏览器代理指向 `192.168.110.11:10808`。模板地址变了要同步改这里。

然后填模型服务商的 API Key（也可以放到 §4.4 的 `service.env`）：

```bash
nano /etc/opencode/opencode/opencode.jsonc
```

确认配置路径（`XDG_CONFIG_HOME` 要和 systemd 单元一致，否则会读 `/root/.config/opencode`）：

```bash
XDG_CONFIG_HOME=/etc/opencode opencode debug config | head -20
```

v2 的输出是目录列表，能看到 `/etc/opencode` 即可；实际读取的是 `/etc/opencode/opencode/opencode.jsonc`。

### 4.3 工作区约定

```bash
cp /root/feishu-bridge/deploy/lxc/workspace-AGENTS.md /workspace/AGENTS.md
```

该文件含「网络与代理」一节，告诉 opencode 出站走代理、各工具如何显式指定、哪些配置不要改。**不要**删掉这一段。

### 4.4 模型密钥（可选）

```bash
umask 077
cat > /etc/opencode/service.env <<EOF
SYSTALYZE_API_KEY=<你的 key>
EOF
```

### 4.5 克隆行为说明

`add-user.sh` 用 `pct clone --full` 克隆，模板里的 `opencode.jsonc`、`/workspace/AGENTS.md`、`/etc/opencode/proxy.env` 会**完整复制**到每个用户 LXC，不是每次重新生成。

反过来，模板改完后**不会**同步到已开通的用户，需要逐台下发或重建 LXC。

---

## 5. 配置 nftables

先确认 `$DOCKER_IP` 和 `$PVE_IP` 在当前会话里（`export` 只在当前会话有效，换控制台后要重新导出）：

```bash
echo "$DOCKER_IP $PVE_IP"
```

**容器控制台**：

```bash
cat > /etc/nftables.conf <<EOF
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    ip saddr { $DOCKER_IP, $PVE_IP } tcp dport 4096 accept
    ip saddr { $DOCKER_IP, $PVE_IP } icmp type echo-request accept
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF

nft -c -f /etc/nftables.conf    # 先检查语法
systemctl enable nftables
systemctl restart nftables
nft list ruleset
```

> 非特权 LXC 里 nftables 加载失败时，可退化为 `iptables`，或暂时不启用（bridge 仍用 basic auth）。不要为此关闭 basic auth。

### 5.1 关于转模板时是否保留 nftables

上面的规则同时放行 `$DOCKER_IP`（bridge 容器）和 `$PVE_IP`（`add-user.sh` 与冒烟测试的发起方），所以**转模板时保留 nftables 启用**，克隆出来的用户 LXC 会自动带上端口限制。

> 早期版本默认在转模板前禁用 nftables，原因是健康检查从 PVE 宿主发起、当时只放行了 docker LXC。现在把 PVE 宿主也加进放行列表，就不需要禁用了。
>
> 如果你确实不想在用户 LXC 里保留端口限制，可以在 §8 额外执行 `systemctl disable nftables`，但这样 4096 会对整个局域网开放（仍有 basic auth 兜底）。

---

## 6. 启动并验证

**容器控制台**：

```bash
systemctl daemon-reload
systemctl enable --now opencode-serve
systemctl status opencode-serve --no-pager
```

健康检查（容器内回环，nftables 已放行 `lo`）：

```bash
curl -fsS http://127.0.0.1:4096/global/health
```

确认服务进程真的拿到了代理变量（`EnvironmentFile` 加载的变量不会出现在 `systemctl show -p Environment` 里，要读进程环境）：

```bash
PID=$(systemctl show -p MainPID --value opencode-serve)
tr '\0' '\n' < "/proc/$PID/environ" | grep -i proxy
curl -fsS -o /dev/null -w '%{http_code}\n' https://api.github.com   # 期望 200
```

设置模板期临时密码并验证鉴权：

```bash
umask 077
printf 'OPENCODE_PASSWORD=template-temp\nOPENCODE_SERVER_PASSWORD=template-temp\n' > /etc/opencode/serve.env
systemctl restart opencode-serve
sleep 2

curl -fsS -u opencode:template-temp http://127.0.0.1:4096/global/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:4096/api/model   # 期望 401
```

> 同时写 `OPENCODE_PASSWORD` 和 `OPENCODE_SERVER_PASSWORD`：早期 v2 版本读前者，新版本读后者，两个都写最稳。
> 这个临时密码会被克隆到用户 LXC，`add-user.sh` 会立即覆盖成随机密码，所以模板里用什么值都行。

---

## 7. 跑冒烟测试

**容器控制台**（`smoke-test.sh` 需要 `curl`、`python3`、`sha256sum`，§2 已装）：

```bash
cd /root/feishu-bridge
./scripts/smoke-test.sh --url http://127.0.0.1:4096 --password template-temp
```

全部 ✅ 后再继续。冒烟覆盖：

1. `/global/health` 200
2. `/api/model` 无认证 401、带认证 200
3. `fs/write` 写 2 MB
4. `fs/read` 读回 sha256 一致
5. `/api/event` SSE 200

失败就排查，**不要把失败的模板转成模板**。

---

## 8. 写入就绪标记并转模板

### 8.1 清理模板

**容器控制台**：

```bash
systemctl disable --now opencode-serve   # 模板不启动服务
rm -f /etc/opencode/serve.env            # 删掉模板期临时密码
rm -rf /root/feishu-bridge               # 仓库只在安装期用
rm -rf /root/.opencode                   # 安装器目录（已拷到 /usr/local/bin）
rm -rf /root/.config/opencode /root/.cache/opencode \
       /root/.local/share/opencode /root/.local/state/opencode
rm -f /root/.bash_history /tmp/opencode-smoke.bin
touch /etc/opencode/ready                # add-user.sh 等待的就绪标记
```

> `nftables` 保持启用（见 §5.1），克隆出来的用户 LXC 会继承端口限制。

> **不要删** `/root/.cache/ms-playwright`（§2.3 下载的 Chromium），否则克隆出来的用户 LXC 还要重新下载浏览器。

> 可选：`passwd -l root` 锁定 root 密码。用户 LXC 不装 SSH，登录入口只有 PVE 控制台 / `pct enter`，锁定不影响 opencode 以 root 运行。

### 8.2 转成模板

**网页控制台**：

1. 选中 `900 (oc-template)` → `停止`（或右键 `关机`）。
2. 确认状态是 `已停止`。
3. 右键 `900` → `转换成模板`（`More` → `Convert to template`）。

转完后 CT 900 显示模板图标，不能再启动，只能克隆。

> 也可以用**宿主 Shell**：`pct stop 900 && pct template 900`。

---

## 9. 验收清单

**宿主 Shell**：

```bash
pct config 900 | grep -E 'template|rootfs|memory|swap|cores|features'
```

逐项确认：

- [ ] `template: 1`，`rootfs` 为 8G
- [ ] `memory: 2048`、`swap: 1024`、`cores: 2`
- [ ] `features: nesting=1`
- [ ] `pct start 900` 报错（模板不能启动）
- [ ] 克隆测试：`pct clone 900 999 --full && pct start 999`
- [ ] 克隆内 `/etc/opencode/ready` 存在
- [ ] 克隆内 `/etc/default/locale` 为 `LANG=C.UTF-8`
- [ ] 克隆内 `systemctl is-enabled nftables` 为 `enabled`
- [ ] 克隆内 `nft list ruleset` 能看到放行 `$DOCKER_IP` 和 `$PVE_IP` 的 4096 规则
- [ ] 克隆内 `PID=$(systemctl show -p MainPID --value opencode-serve); tr '\0' '\n' < /proc/$PID/environ | grep -i proxy` 有输出
- [ ] 克隆内 `bash -lc 'curl -s -o /dev/null -w "%{http_code}" https://api.github.com'` 返回 200
- [ ] 克隆内 `/workspace/AGENTS.md` 含「网络与代理」一节
- [ ] 克隆内 `playwright --version` 有输出，`/root/.cache/ms-playwright` 里有 Chromium
- [ ] 克隆内 `XDG_CONFIG_HOME=/etc/opencode opencode debug config | grep -i playwright` 能看到 MCP 配置
- [ ] 克隆内 `/root/feishu-bridge` 已不存在
- [ ] 冒烟测试已通过（§7）
- [ ] 清理测试容器：`pct stop 999 && pct destroy 999`

---

## 10. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `apt update` 卡住/超时 | 代理没配或地址错 | 容器内 `cat /etc/apt/apt.conf.d/99proxy`；`curl -v https://deb.debian.org` |
| 控制台里中文乱码 | `LANG=C`，`en_US.UTF-8` 没生成 | 按 §2.2 设 `LANG=C.UTF-8`；文件本身是否损坏用 `python3 -c "open('/workspace/AGENTS.md','rb').read().decode()"` 验证 |
| `curl` 外网超时 | 当前会话没加载代理 | `set -a; . /etc/opencode/proxy.env; set +a` |
| opencode 下载失败 | 代理不通 | 先 `curl -I https://opencode.ai` 确认 200 |
| `opencode serve` 启动即退出 | 缺依赖 | `journalctl -u opencode-serve -n 50 --no-pager` |
| `fs/write` 返回 404 | 版本不对或接口路径变了 | 确认 `opencode --version` 是 v2 系列；对照 `app/opencode.py` 的接口路径 |
| `fs/read` 返回 404 | 缺少 `location[directory]` 参数 | 读文件必须带 `?location%5Bdirectory%5D=/tmp`（见 `scripts/smoke-test.sh`） |
| 4096 从别的机器连不上 | nftables 只放行 `$DOCKER_IP` 和 `$PVE_IP` | 正常；需要时按 §5.1 追加来源 |
| 从 PVE 宿主 curl 4096 超时 | 同上 | 正常；健康检查请在容器内打 `127.0.0.1` |
| nftables 启动失败 | 非特权 LXC 限制 | 退化为 iptables，或暂时不启用 |
| 克隆后 opencode 起不来 | 模板没 `disable` 或没写 `ready` | 检查 §8.1 |
| 克隆后外网不通 | 模板缺 `/etc/opencode/proxy.env` | 按 §1.4 重建模板 |
| agent 截图时仍 `apt install nodejs` | 模板没装 Node.js，或用户 LXC 是旧模板克隆的 | 按 §2.3 装；旧用户 LXC 需逐台补装或重建 |
| Playwright MCP 启动失败 | 缺系统库或 Chromium 没下载 | `playwright install --with-deps chromium`；`journalctl -u opencode-serve -n 50 --no-pager` |
| Chromium 报 sandbox 错误 | 容器内以 root 运行 | `opencode.jsonc` 里已有 `--no-sandbox`，确认没被覆盖 |
| 截图空白/超时 | 浏览器没走代理 | 检查 MCP 的 `--proxy-server` 是否是 `192.168.110.11:10808` |

---

## 11. 升级 opencode 的流程

模板不能启动，升级要克隆一个临时容器来做：

1. **宿主 Shell**：`pct clone 900 999 --full && pct start 999`
2. **容器控制台**（进入 999）：按 §3 装新版本，`opencode --version` 确认
3. 按 §7 跑冒烟测试，全部通过
4. **宿主 Shell**：`pct stop 999 && pct destroy 999`
5. 重建模板（重新走 §1–§8），或按需逐台升级已有用户 LXC
6. 已有用户 LXC 升级后必须跑 `/smoke` 或服务器侧冒烟

**每次升级 opencode 都必须跑冒烟测试**，见 `AGENTS.md`。
