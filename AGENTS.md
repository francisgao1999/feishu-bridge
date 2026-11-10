# AGENTS.md

本文件是本项目的 Agent 工作约定。所有在本仓库工作的 Agent 都必须遵守，尤其是「测试通过后自动推送并部署生产」的流程。

## 1. 项目概览

飞书 ↔ OpenCode 桥接器：通过飞书长连接接收消息和卡片回调，调用本机 OpenCode V2 HTTP API 执行编码任务，并把进度、审批、提问和结果发回飞书。

- 语言：Python 3.13
- 主要依赖：`httpx`、`httpx-sse`、`lark-oapi`
- 入口：`app/main.py`
- 测试：`tests/`
- systemd 模板：`deploy/`
- 运行数据（**禁止提交**）：`bridge.env`、`bridge.db`、`bridge.log`、`backups/`、`temp/`

### 代码结构

| 路径 | 作用 |
| --- | --- |
| `app/main.py` | 进程入口、事件循环、任务调度 |
| `app/bridge.py` | 飞书消息与 OpenCode 任务桥接 |
| `app/feishu.py` | 飞书 API / 长连接封装 |
| `app/forms.py` | 提问表单卡构建与答案转换 |
| `app/opencode.py` | OpenCode V2 API 客户端 |
| `app/cards.py` | 飞书卡片构建与更新 |
| `app/permissions.py` | 审批与只读 Agent 规则 |
| `app/slash_commands.py` | `/help`、`/status`、`/new` 等命令 |
| `app/db.py` | SQLite 初始化、迁移与读写 |
| `app/config.py` | 环境变量与 `bridge.env` 加载 |
| `app/ws_patch.py` | 飞书 WebSocket 补丁 |
| `backup.sh` | 数据库、配置和源码备份 |
| `deploy/` | systemd unit / timer 模板 |
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

## 3. 测试与检查（发布前必须全部通过）

在仓库根目录执行：

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m compileall -q app debug tests
bash -n backup.sh
git diff --check
```

要求：

- 任何一项失败都**不得**提交、推送或部署；先修复并重新跑完整检查。
- 新增功能或修复 bug 时，应补充对应测试。
- 不要为了通过测试而删除、跳过或弱化已有测试。

## 4. 提交规范

- 使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、`refactor:`、`chore:`。
- 提交信息用中文描述具体变更，例如：`fix: 定时任务列表用 #id 显示`。
- 一个提交只做一件事，避免把无关改动混在一起。
- 提交前检查 `git status` 和 `git diff`，确认没有误提交密钥、数据库、日志或备份。
- **禁止**提交 `bridge.env`、`service.env`、`*.db`、`*.log`、`backups/`、`temp/`、`.venv/`。

## 5. 自动推送与部署（强制流程）

> 用户要求：每次更新代码，测试通过后，自动 push 到仓库，并更新生产服务器。Agent 不需要再逐次询问，按以下流程直接执行。

### 5.1 本地提交并推送

```bash
# 1. 确认变更范围
git status
git diff

# 2. 跑完整检查（见第 3 节），必须全部通过
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m compileall -q app debug tests
bash -n backup.sh
git diff --check

# 3. 提交并推送
git add -A
git commit -m "feat: ..."
git push origin main
```

### 5.2 更新生产服务器

生产服务器通过 SSH 别名访问：

```bash
ssh oc.vm.pve_hzdd     # 公网 / 外网环境
ssh oc.vm.pve          # 局域网环境（192.168.110.x 网段）
```

> 真实 HostName、端口、用户名和私钥路径保存在本机 `~/.ssh/config`，**不要**写入仓库或提交到 GitHub。

登录后执行：

```bash
cd "$HOME/feishu-opencode-bridge"

# 1. 先备份数据库和 bridge.env
./backup.sh

# 2. 确认工作区干净；有输出说明服务器有未提交修改，先停下确认来源
git status --short

# 3. 只允许快进拉取
git pull --ff-only

# 4. 更新依赖（每次都执行也安全；服务器网络慢时先加载代理）
.venv/bin/pip install -r requirements.txt

# 5. 重启桥接器
sudo systemctl restart feishu-opencode-bridge@oc

# 6. 验证
systemctl is-active feishu-opencode-bridge@oc
sudo journalctl -u feishu-opencode-bridge@oc -n 50 --no-pager
```

如果服务器直连 GitHub 较慢：

```bash
set -a
source "$HOME/.config/opencode/service.env"
set +a
export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"
git pull --ff-only
```

### 5.3 部署范围

- 修改了 `app/`、`deploy/`、`requirements.txt`、`backup.sh`：必须拉取、安装依赖并重启服务。
- 纯文档变更（如 `README.md`、`AGENTS.md`、`todo.md`）：仍需 push；生产服务器执行 `git pull --ff-only` 保持同步即可，可不重启。
- 数据库结构由 `app/db.py` 在启动时自动迁移；升级前 `./backup.sh` 必须成功。

### 5.4 异常处理

- 测试失败：停止，不提交、不推送、不部署。
- `git push` 失败（认证/网络）：报告错误，不要强推，不要改写历史。
- 服务器 `git status --short` 有输出：停止部署，先确认服务器上的修改是否需要保留；**禁止**直接 `git reset --hard`。
- `git pull --ff-only` 分叉：先看 `git log --oneline --graph --decorate -10`，按 README「更新与同步」处理。
- 部署后服务未启动：查看 `journalctl`，必要时回滚并报告。
- **禁止** `git push --force` 到 `main`。

### 5.5 完成后汇报

向用户简要汇报：

1. 本次提交的 commit hash 和提交信息；
2. 测试结果；
3. 生产服务器是否已拉取、服务是否 `active`；
4. 如有异常，说明卡在哪一步以及当前状态。

## 6. 生产服务器信息

| 项目 | 值 |
| --- | --- |
| SSH 别名（公网） | `oc.vm.pve_hzdd` |
| SSH 别名（局域网） | `oc.vm.pve` |
| 运行用户 | `oc` |
| 安装目录 | `/home/oc/feishu-opencode-bridge` |
| 桥接器服务 | `feishu-opencode-bridge@oc` |
| OpenCode 服务 | `opencode-serve`（监听 `127.0.0.1:4096`） |
| Python 虚拟环境 | `/home/oc/feishu-opencode-bridge/.venv` |
| 配置/密钥 | `/home/oc/feishu-opencode-bridge/bridge.env`（仅服务器本地） |
| 数据库 | `/home/oc/feishu-opencode-bridge/bridge.db` |
| 日志 | `journalctl -u feishu-opencode-bridge@oc` |
| 备份 | `cd ~/feishu-opencode-bridge && ./backup.sh`，输出到 `backups/` |
| sudo | 用户 `oc` 已配置 `NOPASSWD: ALL` |

常用运维命令：

```bash
sudo systemctl status feishu-opencode-bridge@oc --no-pager
sudo systemctl restart feishu-opencode-bridge@oc
sudo journalctl -u feishu-opencode-bridge@oc -f
sudo systemctl status opencode-serve --no-pager
```

## 7. 回滚

优先用 `git revert` 保留历史：

```bash
# 开发机
git log --oneline -10
git revert <bad-commit>
git push origin main
```

然后按 5.2 部署到生产。

紧急情况下可在服务器临时切到上一个可用提交：

```bash
cd "$HOME/feishu-opencode-bridge"
git log --oneline -10
git checkout <good-commit>
sudo systemctl restart feishu-opencode-bridge@oc
```

确认修复后切回 `main` 并重新拉取：

```bash
git checkout main
git pull --ff-only
sudo systemctl restart feishu-opencode-bridge@oc
```

数据库回滚：先停服务，按 README「备份与恢复」恢复 `bridge.db`，再启动。

## 8. 安全约定

- 不在代码、提交、日志、卡片或聊天中输出明文密钥。
- 不把 `bridge.env`、`service.env`、数据库、日志、备份复制进仓库。
- 生产环境只做部署，不直接改代码；确需热修复时，在服务器提交并推回 GitHub，再让本地拉取。
- 执行删除、覆盖、数据库写操作等高风险命令前先确认影响范围。
- 通过 SSH 执行需要 `sudo` 的命令前，先确认目标用户已配置免密 sudo；若未配置，停止并让用户先配置 `visudo`，不要尝试交互式输入密码或其它提权方式。

## 9. 参考文档

- `README.md`：完整部署、更新、回滚、备份与故障排查说明。
- `todo.md`：后续优化方向与优先级。
- `bridge.env.example`：全部环境变量及默认值。
