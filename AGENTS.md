# AGENTS.md

本文件是本项目的 Agent 工作约定。所有在本仓库工作的 Agent 都必须遵守，尤其是「测试通过后自动推送并部署生产」和「升级 opencode 必须跑冒烟测试」两条强制流程。

## 1. 项目概览

飞书 ↔ OpenCode 桥接器（多用户 LXC 版）：通过飞书长连接接收消息和卡片回调，按 `open_id` 把任务路由到对应用户的 PVE LXC，调用该 LXC 内的 OpenCode V2 HTTP API 执行编码任务，并把进度、审批、提问、结果和文件回传飞书。

- 语言：Python 3.13
- 主要依赖：`httpx`、`httpx-sse`、`lark-oapi`
- 入口：`app/main.py`
- 测试：`tests/`
- 容器：`Dockerfile` + `docker-compose.yml`
- 用户 LXC 模板与脚本：`deploy/lxc/`
- 冒烟测试：`scripts/smoke-test.sh`
- 运行数据（**禁止提交**）：`bridge.env`、`config/users.json`、`data/`、`backups/`、`*.db`、`*.log`

### 架构

```
飞书 → bridge 容器（docker LXC） → users.json 路由 → 用户 LXC 的 opencode serve
```

- 每用户一个 PVE LXC，非特权容器，宿主映射 100000。
- 用户 LXC 内以 root 运行 `opencode serve --hostname 0.0.0.0 --port 4096`。
- 用户 LXC 不开放 SSH / code-server / ttyd。
- bridge 不持有 PVE API Token，不调用 `pct`；**创建 LXC 永远由管理员手工执行**。

### 代码结构

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 进程入口、事件循环、按用户启动 SSE |
| `app/bridge.py` | 飞书消息与 OpenCode 任务桥接、文件/图片收发、outbox |
| `app/feishu.py` | 飞书 API / 长连接 / 文件上传下载 |
| `app/forms.py` | 提问表单卡构建与答案转换 |
| `app/opencode.py` | OpenCode V2 客户端、多用户路由、文件投递 |
| `app/users.py` | `users.json` 解析与 mtime 热加载 |
| `app/cards.py` | 飞书卡片构建与更新 |
| `app/permissions.py` | 审批与只读 Agent 规则、沙箱模式 |
| `app/slash_commands.py` | `/help`、`/status`、`/smoke` 等命令 |
| `app/db.py` | SQLite 初始化、迁移与读写 |
| `app/config.py` | 环境变量与 `bridge.env` 加载 |
| `app/ws_patch.py` | 飞书 WebSocket 补丁 |
| `backup.sh` | 数据库、配置、`users.json` 和源码备份 |
| `Dockerfile` / `docker-compose.yml` | bridge 容器 |
| `deploy/lxc/` | 用户 LXC 的 systemd unit、配置、增删脚本 |
| `scripts/smoke-test.sh` | 冒烟测试 |
| `docs/` | 设计计划、模板教程、脚本手册 |
| `debug/` | 独立调试脚本，不参与生产运行 |

## 2. 开发环境

当前开发目录通常位于 WSL 的 `/mnt/d/workspace/code/feishu-bridge`。

```bash
# 首次准备
python3 -m venv .venv
export https_proxy=http://127.0.0.1:10808 http_proxy=http://127.0.0.1:10808
.venv/bin/pip install -r requirements.txt
```

说明：

- 下载依赖走本机代理 `127.0.0.1:10808`。
- `lark_oapi` 在 `/mnt/d`（9p 文件系统）上首次导入可能较慢，测试请给足超时，不要误判为卡死。
- `.venv/` 已在 `.gitignore` 中，不要提交。
- 本机不装 Docker 也可以跑单测；构建镜像和部署在 docker LXC 上执行。

## 3. 测试与检查（发布前必须全部通过）

在仓库根目录执行：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m compileall -q app debug tests
bash -n backup.sh
bash -n deploy/lxc/add-user.sh
bash -n deploy/lxc/remove-user.sh
bash -n scripts/smoke-test.sh
git diff --check
```

要求：

- 任何一项失败都**不得**提交、推送或部署；先修复并重新跑完整检查。
- 新增功能或修复 bug 时，应补充对应测试。
- 不要为了通过测试而删除、跳过或弱化已有测试。

### 冒烟测试（强制）

以下场景**必须**执行冒烟测试，失败即停止发布：

1. 变更 opencode 版本（模板或用户 LXC）
2. 重建模板 LXC 或克隆新用户
3. 升级 bridge 镜像且涉及 `app/opencode.py`、`app/feishu.py`、`app/bridge.py` 的文件投递逻辑
4. 部署后首次验证

执行方式：

```bash
# 服务器侧（docker LXC 内）
scripts/smoke-test.sh --users config/users.json

# 或者飞书里
/smoke
```

`scripts/smoke-test.sh` 逐用户验证：`/global/health`、Basic 认证、`fs/write` 2MB 二进制、`fs/read` sha256、`/api/event` SSE。

**不得跳过、不得只测一个用户、不得只看 bridge 日志就认为通过。**

## 4. 提交规范

- 使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。
- 提交信息用中文描述具体变更，例如：`fix: 定时任务列表用 #id 显示`。
- 一个提交只做一件事，避免把无关改动混在一起。
- 提交前检查 `git status` 和 `git diff`，确认没有误提交密钥、数据库、日志、备份或 `users.json`。
- **禁止**提交 `bridge.env`、`service.env`、`config/users.json`、`data/`、`*.db`、`*.log`、`backups/`、`temp/`、`.venv/`。

## 5. 自动推送与本地构建（强制流程）

> 用户要求：每次更新代码，测试通过后，自动 push 到仓库，并在**本地**构建镜像和产物。**不要远程部署**，生产服务器由用户手动更新。

### 5.1 本地提交并推送

```bash
# 1. 确认变更范围
git status
git diff

# 2. 跑完整检查（见第 3 节），必须全部通过
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m compileall -q app debug tests
bash -n backup.sh
bash -n deploy/lxc/add-user.sh
bash -n deploy/lxc/remove-user.sh
bash -n scripts/smoke-test.sh
git diff --check

# 3. 提交并推送
git add -A
git commit -m "feat: ..."
git push origin main
```

### 5.2 本地构建产物

推送后在开发机（WSL）构建并验证镜像，**不登录任何服务器**：

```bash
cd /mnt/d/workspace/code/feishu-bridge

# 本机代理只监听 127.0.0.1，构建时用 host 网络并显式传代理
docker build --network host \
  --build-arg http_proxy=http://127.0.0.1:10808 \
  --build-arg https_proxy=http://127.0.0.1:10808 \
  --build-arg no_proxy=localhost,127.0.0.1 \
  -t feishu-opencode-bridge:latest .
```

- 构建成功后检查 `docker images feishu-opencode-bridge`。
- 需要时用 `docker run --rm -it --entrypoint sh feishu-opencode-bridge:latest` 做冒烟检查；不要在容器里连生产。
- **不要**执行 `ssh` 到生产服务器，不执行 `docker compose up`，不改服务器上的任何文件。

### 5.3 部署范围（用户手动执行）

- 修改了 `app/`、`requirements.txt`、`Dockerfile`、`docker-compose.yml`：用户需要重建镜像并重启 bridge。
- 修改了 `scripts/`、`deploy/lxc/`、`docs/`：用户需要把脚本/配置同步到服务器或用户 LXC。
- 纯文档变更：只需 push。
- **不要**在服务器上执行 `pct` 创建/删除 LXC；创建 LXC 永远由管理员手工执行。

### 5.4 异常处理

- 测试失败：停止，不提交、不推送、不构建。
- 构建失败：报告错误（网络/代理/依赖），不要试着远程构建。
- `git push` 失败（认证/网络）：报告错误，不要强推，不要改写历史。
- **禁止** `git push --force` 到 `main`。

### 5.5 完成后汇报

向用户简要汇报：

1. 本次提交的 commit hash 和提交信息；
2. 测试结果（含冒烟测试是否执行）；
3. 本地镜像是否构建成功（镜像名与大小）；
4. 如需手动部署，说明需要执行的命令和注意事项。

## 6. 部署目标（用户手动维护）

> 以下信息供用户手动部署时参考，Agent **不要**自动化执行。真实主机、端口、用户名以用户本机 `~/.ssh/config` 为准；如与实际不符，让用户确认后再更新本节。

| 项目 | 值 |
| --- | --- |
| 安装目录 | `/root/container/feishu-bridge`（docker LXC 101 内） |
| bridge 容器 | `feishu-bridge`（`docker compose`） |
| compose 文件 | `/root/container/feishu-bridge/compose.yaml`（部署时使用；仓库内为 `docker-compose.yml`） |
| 用户映射 | `/root/container/feishu-bridge/config/users.json`（仅服务器本地） |
| 配置/密钥 | `/root/container/feishu-bridge/bridge.env`（仅服务器本地） |
| 数据库 | `/root/container/feishu-bridge/data/bridge.db` |
| 日志 | `docker compose logs -f bridge` |
| 备份 | 仓库根目录 `backup.sh`；复制到 `/root/container/feishu-bridge/` 后执行 `./backup.sh`，输出到 `backups/` |
| 模板 LXC | VMID 900，`oc-template` |
| 用户 LXC | 从 901 开始，按 `oc-<slug>` 命名 |

用户部署时的常用命令（由用户执行）：

```bash
cd /root/container/feishu-bridge
git pull --ff-only
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f bridge
```

用户 LXC 运维（在 PVE 宿主执行）：

```bash
pct list
pct status 901
pct enter 901
journalctl -u opencode-serve -n 50 --no-pager   # 在 LXC 内
```

## 7. 回滚

优先用 `git revert` 保留历史：

```bash
# 开发机
git log --oneline -10
git revert <bad-commit>
git push origin main
```

然后由用户按 5.2 在本地重新构建镜像，并在服务器上手动部署。

用户紧急回滚时可在服务器临时切到上一个可用提交：

```bash
cd /root/container/feishu-bridge
git log --oneline -10
git checkout <good-commit>
docker compose up -d --build
```

确认修复后切回 `main` 并重新拉取：

```bash
git checkout main
git pull --ff-only
docker compose up -d --build
```

数据库回滚：先停容器，按 README「备份与恢复」恢复 `data/bridge.db`，再启动。

用户 LXC 回滚：`pct rollback <vmid> <snapshot>`；或从 `vzdump` 备份 `pct restore`。

## 8. 安全约定

- 不在代码、提交、日志、卡片或聊天中输出明文密钥。
- 不把 `bridge.env`、`service.env`、`config/users.json`、数据库、日志、备份复制进仓库。
- 生产环境由用户手动部署，Agent 不远程操作；确需热修复时，在服务器提交并推回 GitHub，再让本地拉取。
- 执行删除、覆盖、数据库写操作等高风险命令前先确认影响范围。
- 通过 SSH 执行需要 `sudo` 的命令前，先确认目标用户已配置免密 sudo；若未配置，停止并让用户先配置 `visudo`，不要尝试交互式输入密码或其它提权方式。
- 不在生产服务器上执行 `pct create` / `pct destroy` / `pct resize`，除非用户明确要求并确认 VMID。
- 用户 LXC 不开放 SSH；不要给用户 LXC 配置 root 密码或 SSH 服务。

## 9. opencode 升级流程（强制）

opencode 的 `fs/write` 是 experimental 接口，升级可能破坏文件投递。升级步骤：

1. 在**模板 LXC** 内按 `docs/lxc-template-manual.md` 安装新版本（或从模板克隆一个测试 LXC）。
2. 跑 `scripts/smoke-test.sh --url http://<test-ip>:4096 --password <pw>`，全部通过。
3. 用新模板重建用户 LXC，或逐个在用户 LXC 内升级。
4. 每个用户升级后跑 `/smoke` 或服务器侧冒烟。
5. 任何一步失败：停止，回滚到上一个版本，不要继续推广。

## 10. 参考文档

- `README.md`：架构、部署、文件收发、新用户开通、故障排查。
- `docs/plan-lxc-sandbox.md`：完整设计与实施计划。
- `docs/lxc-template-manual.md`：模板 LXC 手工创建教程。
- `docs/lxc-scripts.md`：`add-user.sh` / `remove-user.sh` 用法与排错。
- `todo.md`：后续优化方向与优先级。
- `bridge.env.example`：全部环境变量及默认值。
