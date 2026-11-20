# 模板 LXC 手工创建教程

本教程在 **PVE 宿主机**上手工创建模板 LXC（VMID `900`，主机名 `oc-template`）。创建一次，之后所有用户 LXC 都从它克隆。

> 本方案**不提供**模板创建脚本，也不做自动部署。所有命令由管理员手工执行，便于审计和排错。

---

## 0. 前置条件

| 项 | 要求 |
| --- | --- |
| PVE 版本 | 8.x 或 9.x，`pct` / `pvesh` 可用 |
| LXC 模板 | Debian 13（trixie）standard，已下载到 `local:vztmpl/` |
| 磁盘 | 模板盘 8G（克隆快），用户克隆后扩到 24G |
| 网络 | 有固定网段与网关；docker LXC 的 IP 已知 |
| 权限 | 在 PVE 宿主机上以 root 执行 |

先确认模板文件名：

```bash
pveam list local | grep debian-13
# 例如 local:vztmpl/debian-13-standard_13.x-1_amd64.tar.zst
```

下文用 `$TMPL` 表示这个值，先导出：

```bash
export TMPL="local:vztmpl/debian-13-standard_13.x-1_amd64.tar.zst"
export DOCKER_IP="10.0.0.10"      # 运行 bridge 的 docker LXC 的 IP
export TEMPLATE_IP="10.0.0.9"     # 模板 LXC 自己的临时 IP（创建期用）
export GATEWAY="10.0.0.1"
export BRIDGE="vmbr0"
export PREFIX="24"                # 按实际网段填写，下面所有 /24 都要一起改
export PROXY_HOST="192.168.110.11" # 代理所在主机（生产环境就是 docker LXC 的 IP）
export PROXY_PORT="10808"          # HTTP 与 SOCKS5 同端口
```

> 教程里的 `/24` 是示例。如果你的网段是 `/16` 或 `/25`，把 `$PREFIX` 改成实际值，并把下面命令中的 `/24` 替换为 `/$PREFIX`。

> 模板 LXC 的 IP 只用于创建期验证；转成模板后不再启动，用户 LXC 克隆时会覆盖 `net0`。

---

## 1. 创建 LXC（8G 盘）

```bash
pct create 900 "$TMPL" \
  --hostname oc-template \
  --unprivileged 1 \
  --features nesting=1 \
  --cores 2 \
  --memory 2048 \
  --swap 1024 \
  --rootfs local-lvm:8 \
  --net0 "name=eth0,bridge=$BRIDGE,ip=$TEMPLATE_IP/24,gw=$GATEWAY" \
  --onboot 0 \
  --start 1
```

说明：

- `--unprivileged 1`：容器内 root 映射到宿主 `100000`，隔离更稳。
- `--features nesting=1`：允许容器内运行嵌套工具（playwright 等需要）。
- `--rootfs local-lvm:8`：8G 足够装模板；用户克隆后用 `pct resize` 扩到 24G。
- `--start 1`：创建后立即启动，方便安装。

如果 `local-lvm` 不存在，用 `pvesm status` 查实际存储名替换。

### 1.1 配置出站代理（安装软件前）

生产环境用户 LXC 不能直连外网，所有出站流量走 docker LXC 上的代理（HTTP 与 SOCKS5 同为 `10808`）。**先配代理，再 `apt install` 和下载 opencode**，否则后半段会卡住。

```bash
pct push 900 deploy/lxc/proxy.env.example /etc/opencode/proxy.env
pct exec 900 -- env PROXY_HOST="$PROXY_HOST" PROXY_PORT="$PROXY_PORT" bash -c '
set -e
# 统一改成实际代理地址（示例文件默认 192.168.110.11）
sed -i "s/192\.168\.110\.11/$PROXY_HOST/g" /etc/opencode/proxy.env
chmod 600 /etc/opencode/proxy.env

# 登录 shell 自动加载代理变量（/etc/opencode/proxy.env 是唯一来源）
cat > /etc/profile.d/99-proxy.sh <<"EOF"
# 出站代理，由模板预置；不要手工 export，改 /etc/opencode/proxy.env 即可
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
'
```

验证代理可用（`pct exec` 默认没有环境变量，用 `bash -l` 才会加载 `/etc/profile.d/99-proxy.sh`）：

```bash
pct exec 900 -- bash -lc 'env | grep -i proxy'
pct exec 900 -- bash -lc 'curl -fsS -o /dev/null -w "%{http_code}\n" https://api.github.com'
# 期望输出 200
```

说明：

- `opencode-serve.service` 里有 `EnvironmentFile=-/etc/opencode/proxy.env`，opencode 及其启动的所有命令都会继承代理变量。
- 代理配置随模板克隆，**每个用户 LXC 自动带上**，`add-user.sh` 不需要再写。
- `NO_PROXY` 已包含 `localhost`、`127.0.0.1` 和私有网段，内网访问不绕代理。
- 有额外需要直连的域名时，往 `NO_PROXY` 里追加具体域名即可；不要删除 `127.0.0.1`（opencode 依赖回环通信）。
- `git` / `pip` 的系统级代理在 §2 装完软件后再写（此时 `git` 才存在）。

---

## 2. 安装基础软件包

```bash
pct exec 900 -- bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  python3 \
  python3-venv \
  python3-pip \
  jq \
  unzip \
  xz-utils \
  less \
  file \
  procps \
  nftables \
  tzdata
apt-get clean
rm -rf /var/lib/apt/lists/*
'
```

清单说明（约 40 MB）：

| 包 | 用途 |
| --- | --- |
| `ca-certificates` | HTTPS 必需 |
| `curl` | 安装 opencode、agent 下载 |
| `git` | opencode 快照/撤销依赖 |
| `python3` + `venv` + `pip` | 常用；venv 隔离 PEP 668 |
| `jq` | JSON 处理 |
| `unzip` / `xz-utils` | 解压常见格式 |
| `less` / `file` / `procps` | 排查工具 |
| `nftables` | 限制 4096 端口来源 |
| `tzdata` | 时区 |

**不安装**：`nodejs`/`npm`、`build-essential`、`postgresql-client`、`default-mysql-client`、`ripgrep`、`fonts-noto-cjk`。需要时由用户任务自己 `apt install`。

装完 `git` 后，再补系统级代理（`pct exec` 不加载 `/etc/profile.d`，所以写进配置文件而不是依赖环境变量）：

```bash
pct exec 900 -- bash -lc '
set -e
git config --system http.proxy "$HTTP_PROXY"
git config --system https.proxy "$HTTPS_PROXY"
mkdir -p /etc
cat > /etc/pip.conf <<EOF
[global]
proxy = $HTTP_PROXY
EOF
'
```

> `bash -lc` 会加载 `/etc/profile.d/99-proxy.sh`，因此这里能直接引用 `$HTTP_PROXY` / `$HTTPS_PROXY`。

---

## 3. 安装 opencode 2.3.2

固定版本，避免升级破坏 `fs/write` 等 experimental 接口。

```bash
pct exec 900 -- bash -lc '
set -e
curl -fsSL https://opencode.ai/install | bash -s -- --version 2.3.2
install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode
/usr/local/bin/opencode --version
'
```

> 这里用 `bash -lc`（登录 shell）是为了加载 `/etc/profile.d/99-proxy.sh`，让下载走代理；`bash -c` 不会读该文件。

预期输出：`2.3.2`。

> 如果容器内没有外网，先把二进制拷进去：
> `pct push 900 ./opencode /usr/local/bin/opencode --perms 0755`。

---

## 4. 创建目录与配置

```bash
pct exec 900 -- bash -c '
set -e
mkdir -p /workspace/uploads /workspace/outbox
mkdir -p /etc/opencode/opencode
mkdir -p /var/lib/opencode /var/cache/opencode
chmod 700 /etc/opencode
'
```

### 4.1 写入 systemd 单元

从仓库拷入：

```bash
pct push 900 deploy/lxc/opencode-serve.service /etc/systemd/system/opencode-serve.service
```

内容应包含：

```ini
[Unit]
Description=OpenCode V2 API server (per-user sandbox)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/workspace
Environment=HOME=/root
Environment=XDG_CONFIG_HOME=/etc/opencode
Environment=XDG_DATA_HOME=/var/lib/opencode
Environment=XDG_STATE_HOME=/var/lib/opencode/state
Environment=XDG_CACHE_HOME=/var/cache/opencode
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile=-/etc/opencode/service.env
EnvironmentFile=-/etc/opencode/serve.env
ExecStart=/usr/local/bin/opencode serve --hostname 0.0.0.0 --port 4096 --print-logs --log-level info
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

### 4.2 写入 opencode 配置

```bash
pct push 900 deploy/lxc/opencode.jsonc /etc/opencode/opencode/opencode.jsonc
```

然后**手工编辑**，填入模型服务商的 API Key（或改用 `service.env` 里的环境变量）：

```bash
pct exec 900 -- nano /etc/opencode/opencode/opencode.jsonc
```

确认读取路径：

```bash
pct exec 900 -- /usr/local/bin/opencode debug config
```

输出里的 `config` 路径应为 `/etc/opencode/opencode/opencode.jsonc`。若不同，按实际路径调整。

> 克隆使用 `pct clone --full`，模板里的 `opencode.jsonc`、`/workspace/AGENTS.md`、`/etc/opencode/proxy.env` 会**完整复制**到每个用户 LXC，不是每次新建。反过来，模板改完后**不会**同步到已开通的用户，需要逐台 `pct exec` 下发或重建 LXC。

### 4.3 写入工作区约定

```bash
pct push 900 deploy/lxc/workspace-AGENTS.md /workspace/AGENTS.md
```

该文件已包含「网络与代理」一节，告诉 opencode 出站必须走代理、各工具如何显式指定代理、哪些配置不要改。**不要**为了省事删掉这一段，否则 agent 遇到外网工具时会自己改配置。

### 4.4 写入模型密钥（可选）

```bash
pct exec 900 -- bash -c '
umask 077
cat > /etc/opencode/service.env <<EOF
SYSTALYZE_API_KEY=replace-me
EOF
'
```

---

## 5. 配置 nftables（只放行 docker LXC 访问 4096）

```bash
pct exec 900 -- bash -c "
umask 022
cat > /etc/nftables.conf <<'EOF'
#!/usr/sbin/nft -f
flush ruleset

table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    iif lo accept
    ct state established,related accept
    icmp type echo-request accept
    ip saddr $DOCKER_IP tcp dport 4096 accept
  }
  chain forward {
    type filter hook forward priority 0; policy drop;
  }
  chain output {
    type filter hook output priority 0; policy accept;
  }
}
EOF
systemctl enable --now nftables
nft list ruleset
"
```

> 注意：`$DOCKER_IP` 是**宿主 shell** 变量，`pct exec ... bash -c "..."` 的双引号会展开它。如果不想展开，改成单引号并手工替换。

确认规则生效：

```bash
pct exec 900 -- nft list ruleset | grep -A2 'dport 4096'
```

> 如果 nftables 在非特权 LXC 里加载失败，可退化为 `iptables`，或暂时不启用（bridge 仍用 basic auth）。**不要**因此关闭 basic auth。

---

## 6. 启动并验证

```bash
pct exec 900 -- systemctl daemon-reload
pct exec 900 -- systemctl enable --now opencode-serve
pct exec 900 -- systemctl status opencode-serve --no-pager
```

健康检查（模板 LXC 内自测）：

```bash
pct exec 900 -- curl -fsS http://127.0.0.1:4096/global/health
```

确认服务进程真的拿到了代理变量（`EnvironmentFile=-/etc/opencode/proxy.env`）：

```bash
pct exec 900 -- systemctl show opencode-serve -p Environment \
  | tr ' ' '\n' | grep -i proxy
# 应能看到 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY / NO_PROXY
pct exec 900 -- bash -lc 'curl -fsS -o /dev/null -w "%{http_code}\n" https://api.github.com'
# 期望 200；失败说明代理不通，先查 1.1
```

从宿主机验证鉴权（需要 `serve.env` 里的密码；模板期还没写密码，先手工写一个临时密码）：

```bash
# 同时写两个变量：2.3.x 读 OPENCODE_PASSWORD，更新的版本读 OPENCODE_SERVER_PASSWORD。
pct exec 900 -- sh -c "umask 077; printf 'OPENCODE_PASSWORD=template-temp\nOPENCODE_SERVER_PASSWORD=template-temp\n' > /etc/opencode/serve.env"
pct exec 900 -- systemctl restart opencode-serve
curl -fsS -u opencode:template-temp "http://$TEMPLATE_IP:4096/global/health"
curl -s -o /dev/null -w '%{http_code}\n' "http://$TEMPLATE_IP:4096/api/model"   # 期望 401
```

> `serve.env` 里的临时密码会被克隆到每个用户 LXC，`add-user.sh` 会立即覆盖成随机密码，所以模板里用什么值都行。

---

## 7. 跑冒烟测试

在仓库目录（或任意有 `scripts/smoke-test.sh` 的位置）执行：

```bash
./scripts/smoke-test.sh --url "http://$TEMPLATE_IP:4096" --password template-temp
```

全部 ✅ 后再继续。冒烟覆盖：

1. `/global/health` 200
2. `/api/model` 无认证 401
3. `fs/write` 写 2 MB
4. `fs/read` 读回 sha256 一致
5. `/api/event` SSE 200

失败时按提示排查，**不要**把失败的模板转成模板。

---

## 8. 写入就绪标记并转模板

```bash
pct exec 900 -- bash -c '
set -e
systemctl disable opencode-serve
systemctl disable nftables
rm -f /etc/opencode/serve.env
touch /etc/opencode/ready
'
pct stop 900
pct template 900
```

说明：

- `disable` 后 `add-user.sh` 会 `systemctl enable --now` 重新启用。
- 删除模板期的临时 `serve.env`，避免克隆后残留弱密码。
- `/etc/opencode/ready` 是 `add-user.sh` 等待的就绪标记。
- `pct template 900` 之后 LXC 不能再启动，只能克隆。

---

## 9. 验收清单

- [ ] `pct config 900` 显示 `template: 1`、`rootfs` 8G
- [ ] `pct exec 900` 已不可用（模板不能启动）
- [ ] 克隆测试：`pct clone 900 999 --full && pct start 999`，容器内 `/etc/opencode/ready` 存在
- [ ] 克隆测试后删除：`pct stop 999 && pct destroy 999`
- [ ] `/etc/opencode/proxy.env` 存在、权限 600，`systemctl show opencode-serve -p Environment` 能看到代理变量
- [ ] `bash -lc 'curl https://api.github.com'` 返回 200
- [ ] `/workspace/AGENTS.md` 含「网络与代理」一节
- [ ] 冒烟测试通过（模板期已跑）

---

## 10. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `opencode serve` 启动即退出 | 缺 `ca-certificates` 或 `bash` | `apt install -y ca-certificates bash` |
| `fs/write` 返回 404 | opencode 版本变了 | 固定回 2.3.2；升级前先跑冒烟 |
| `curl` 连不上 4096 | nftables 拦了 | 检查 `ip saddr` 是否为 docker LXC 的 IP |
| 配置不生效 | 读的不是 `/etc/opencode/...` | `opencode debug config` 看实际路径 |
| 磁盘满 | 用户装了数据库 | `pct resize <vmid> rootfs 32G` |
| 内存不够 | 同时跑 PG + MySQL | `pct set <vmid> --memory 3072` |
| `curl` / `apt` 超时 | 代理没配或地址错 | `pct exec 900 -- bash -lc 'env \| grep -i proxy'`；检查 `/etc/opencode/proxy.env`、`/etc/apt/apt.conf.d/99proxy` |
| opencode 调用模型超时 | 服务没拿到代理变量 | `systemctl show opencode-serve -p Environment \| tr ' ' '\n' \| grep -i proxy`；确认单元里有 `EnvironmentFile=-/etc/opencode/proxy.env` |
| agent 自己 `unset` 代理后外网失败 | 工作区约定被改 | 重新 `pct push` `deploy/lxc/workspace-AGENTS.md` |

---

## 11. 升级 opencode 的流程

模板一旦转成模板就不能启动。升级需要：

1. 从模板克隆一个临时 LXC（如 `999`）
2. 按 §3 安装新版本
3. 按 §7 跑冒烟测试
4. 通过后删除临时 LXC，并重新创建模板（或直接 `pct template` 新 LXC）
5. 已有的用户 LXC 需要单独升级（`pct exec <id> -- ...` 或重建）

**每次升级 opencode 都必须跑冒烟测试**，见 `AGENTS.md`。
