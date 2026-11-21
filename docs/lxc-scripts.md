# LXC 脚本用法

两个脚本都在 **PVE 宿主机**上以 root 执行：

| 脚本 | 作用 |
| --- | --- |
| `deploy/lxc/add-user.sh` | 从模板克隆用户 LXC、配置网络/资源、生成密码、更新 `users.json`、跑冒烟 |
| `deploy/lxc/remove-user.sh` | 归档并删除用户 LXC，并从 `users.json` 移除 |

> **IP 必须由管理员显式传入**。脚本不做网段扫描、不做 DHCP 分配、不做自动扩容。

---

## 1. add-user.sh

### 1.1 基本用法

```bash
cd /root/feishu-opencode-bridge          # 仓库目录（在 PVE 宿主上）
./deploy/lxc/add-user.sh <slug> <open_id> <ip>
```

示例：

```bash
./deploy/lxc/add-user.sh alice ou_2f1c... 192.168.110.201
./deploy/lxc/add-user.sh bob   ou_8a3d... 192.168.110.202 --memory 3072 --cores 4
```

### 1.2 参数

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `slug` | 是 | — | 用户名，`^[a-z][a-z0-9-]{0,20}$`，主机名变成 `oc-<slug>` |
| `open_id` | 是 | — | 飞书 `open_id`，`ou_` 开头 |
| `ip` | 是 | — | 固定 IP，脚本只校验格式与是否被占用 |
| `--template <id>` | 否 | `900` | 模板 LXC ID |
| `--start-id <id>` | 否 | `901` | 用户 LXC 起始 VMID，从该号向上找第一个空闲号 |
| `--docker-lxc <id>` | 否 | `101` | 运行 bridge 的 docker LXC ID |
| `--gateway <ip>` | 否 | `192.168.110.254` | 网关 |
| `--bridge <name>` | 否 | `vmbr0` | PVE 网桥 |
| `--prefix <bits>` | 否 | `24` | IP 前缀长度，按实际网段填写 |
| `--memory <MB>` | 否 | `2048` | 内存 |
| `--swap <MB>` | 否 | `1024` | swap |
| `--cores <n>` | 否 | `4` | CPU 核心 |
| `--disk <size>` | 否 | `24G` | 磁盘（先克隆 8G 模板盘再扩容） |
| `--config-path <path>` | 否 | `/root/container/feishu-bridge/config/users.json` | docker LXC 内 `users.json` 路径 |
| `--skip-smoke` | 否 | 关 | 跳过冒烟测试（不推荐） |
| `--dry-run` | 否 | 关 | 只打印命令，不实际执行 |
| `-h, --help` | 否 | — | 显示帮助 |

> 用户 LXC 默认从 `901` 开始编号，避免和基础设施 LXC（101/102）混在一起；`--start-id` 可改起始号。起始号被占用时会自动向上找第一个空闲号。

### 1.3 执行步骤

1. 校验参数、模板存在、docker LXC 存在
2. `ping` 检查 IP 未被占用
3. 检查 `users.json` 中是否已有同 `slug` 或同 `open_id`
4. 从 `--start-id`（默认 `901`）开始找第一个空闲 VMID；若 PVE 的 `nextid` 已经大于等于起始号，则直接用它
5. `pct clone 900 <vmid> --hostname oc-<slug> --full`
6. `pct set` 网络、内存、swap、cores
7. `pct start`，等待 `/etc/opencode/ready`（最长约 60 秒）
8. `pct resize <vmid> rootfs 24G`（运行中扩容，PVE 会自动 `resize2fs`）
9. 生成 24 字节随机密码，写入 `/etc/opencode/serve.env`
10. `systemctl enable --now opencode-serve`
11. 等待 `/global/health` 通过（最长约 60 秒）
12. 验证出站代理（`/etc/opencode/proxy.env` + `curl` 探测；失败只告警，不中断）
13. 更新 docker LXC 内的 `users.json`（备份为 `.bak`）
14. 跑 `scripts/smoke-test.sh`

### 1.4 失败处理

脚本会打印 VMID。任一步失败时按提示手工清理：

```bash
pct stop <vmid> && pct destroy <vmid>
```

常见失败：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `IP 已被占用` | 网段里有设备占用 | 换一个 IP，或确认后手工 `pct set` |
| `slug 已存在` | `users.json` 里已有 | 先 `remove-user.sh`，或换 slug |
| `等待就绪标记超时` | 模板没写 `/etc/opencode/ready` | 按 `docs/lxc-template-manual.md` 重建模板 |
| `健康检查超时` | opencode 启动失败 | `pct exec <vmid> -- journalctl -u opencode-serve -n 50 --no-pager` |
| `出站代理不可用`（警告） | 模板没配代理，或代理地址错 | 按 `docs/lxc-template-manual.md` §1.4 检查 `/etc/opencode/proxy.env` |
| `冒烟测试失败` | 网络/鉴权/接口问题 | 按冒烟输出逐项排查 |

### 1.5 幂等与安全

- 脚本**不会**覆盖已存在的 `slug`/`open_id`，发现冲突直接退出。
- 密码只写入 LXC，不打印到 stdout（脚本末尾打印的密码除外，用于交接）。
- `users.json` 更新前自动备份为 `users.json.bak`。
- `--dry-run` 可以先看完整命令序列。

### 1.6 代理

用户 LXC 的出站代理由**模板**提供，克隆时原样复制，脚本不会再写：

- `/etc/opencode/proxy.env`：`HTTP_PROXY` / `HTTPS_PROXY` = `http://192.168.110.11:10808`，`ALL_PROXY` = `socks5h://...`，600
- `opencode-serve.service` 通过 `EnvironmentFile` 注入 opencode 进程
- `/etc/profile.d/99-proxy.sh`、`/etc/apt/apt.conf.d/99proxy`、git 系统配置、`/etc/pip.conf`

脚本在第 12 步会验证：`/etc/opencode/proxy.env` 是否存在、`bash -lc 'curl https://api.github.com'` 是否通。失败只告警，不中断开通。

手工复核：

```bash
pct exec <vmid> -- cat /etc/opencode/proxy.env
pct exec <vmid> -- bash -lc 'env | grep -i proxy'
pct exec <vmid> -- bash -lc 'curl -fsS -o /dev/null -w "%{http_code}\n" https://api.github.com'
```

模板里没有代理配置时，按 `docs/lxc-template-manual.md` §1.4 重建模板；已有用户需要手工补配置或重建 LXC。

---

## 2. remove-user.sh

### 2.1 基本用法

```bash
./deploy/lxc/remove-user.sh <slug>
./deploy/lxc/remove-user.sh --vmid 105
```

### 2.2 参数

| 参数 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `slug` | 二选一 | — | 按主机名 `oc-<slug>` 查找 VMID |
| `--vmid <id>` | 二选一 | — | 直接指定 VMID |
| `--docker-lxc <id>` | 否 | `101` | docker LXC ID |
| `--config-path <path>` | 否 | `/root/container/feishu-bridge/config/users.json` | `users.json` 路径 |
| `--backup-dir <path>` | 否 | `/var/lib/vz/dump` | `vzdump` 输出目录 |
| `--keep-lxc` | 否 | 关 | 只移除映射，不停止/删除 LXC |
| `--dry-run` | 否 | 关 | 只打印操作 |

### 2.3 执行步骤

1. 按 `slug` 或 `--vmid` 定位 LXC
2. `pct stop`
3. `vzdump <vmid> --dumpdir <dir> --mode stop --compress zstd`
4. `pct destroy <vmid> --purge`
5. 从 `users.json` 移除对应条目（备份 `.bak`）

归档文件可用 `pct restore <newid> <backup-file>` 恢复。

### 2.4 只暂停不删除

```bash
pct stop 105
./deploy/lxc/remove-user.sh --vmid 105 --keep-lxc
```

之后需要恢复时，把条目重新加回 `users.json` 并 `pct start 105`。

---

## 3. users.json 的维护

### 3.1 位置

- 宿主机（docker LXC 内）：`/root/container/feishu-bridge/config/users.json`
- bridge 容器内：`/app/config/users.json`（只读挂载）

**必须挂目录**（`./config:/app/config:ro`），不能挂单文件。原因：`pct push` 会替换 inode，单文件 bind mount 会继续指向旧文件，热加载失效。

### 3.2 格式

```json
[
  {
    "open_id": "ou_xxx",
    "slug": "alice",
    "url": "http://192.168.110.201:4096",
    "password": "随机密码",
    "workspace": "/workspace"
  }
]
```

`workspace` 可省略，默认 `/workspace`。

### 3.3 手工修改

```bash
# 在 docker LXC 内
cd /root/container/feishu-bridge
cp config/users.json config/users.json.bak
vi config/users.json
# 校验 JSON
python3 -c 'import json;json.load(open("config/users.json"))'
```

bridge 每 5 秒检查 mtime，改动后自动生效，无需重启。

### 3.4 热加载失败的排查

1. `docker compose exec bridge ls -l /app/config/users.json` 看 mtime 是否变化
2. 确认 compose 挂的是目录不是文件
3. 确认 `BRIDGE_USERS_FILE` 指向 `/app/config/users.json`
4. 看日志：`docker compose logs -f bridge | grep users`

---

## 4. 换 IP

1. 停 bridge 对该用户的路由：从 `users.json` 移除该条目（或先改 `url`）
2. 在 PVE 宿主改 LXC 网络：

```bash
pct set 105 --net0 "name=eth0,bridge=vmbr0,ip=192.168.110.205/24,gw=192.168.110.254"
pct reboot 105
```

3. 更新 `users.json` 的 `url`，等 5 秒热加载
4. 跑 `scripts/smoke-test.sh --url http://192.168.110.205:4096 --password <pw>`

---

## 5. 回滚

| 场景 | 操作 |
| --- | --- |
| 用户环境损坏 | `pct rollback <vmid> <snapshot>` |
| LXC 误删 | `pct restore <newid> <vzdump 文件>` |
| `users.json` 改坏 | `cp users.json.bak users.json` |
| bridge 镜像升级失败 | `docker compose down && docker compose up -d`（回退到上一个 tag） |

---

## 6. 冒烟测试

```bash
# 单个用户
./scripts/smoke-test.sh --url http://192.168.110.201:4096 --password <pw>

# 全部用户
./scripts/smoke-test.sh --users /root/container/feishu-bridge/config/users.json
```

退出码 `0` 表示全部通过；非 `0` 表示有失败项，按输出逐项排查。

**强制场景**：

- opencode 版本变更
- 重建模板 / 克隆新用户
- bridge 镜像升级且涉及 `app/opencode.py` 或文件传输逻辑
- 部署后首次验证
