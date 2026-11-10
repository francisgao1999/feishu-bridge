# 飞书 → OpenCode 轻量桥接器

## for human

> 嘿，人类！这是一个由 deepseek 摸鱼时随手搓出来的项目，内部到底怎么实现的？本宝宝也说不清，真的。想安装、想折腾、或者想研究实现方式？很简单：`git clone` 下来，打开你的 agent，把项目地址像飞盘一样丢给它，然后坐等它一边挠头一边干活。Peace & Love，玩得开心！🌴✌️✨

## 架构

- 飞书自建应用 `oc-bot`，通过 WebSocket 长连接接收消息和卡片回调，不需要公网 IP。
- 桥接器：Python 3.13 + `lark-oapi 1.7.5`，由 systemd 常驻。
- OpenCode V2 HTTP API：`127.0.0.1:4096`，HTTP Basic 认证。
- SQLite：保存 chat / session / task / permission / form / schedule / 事件去重。
- SSE `/api/event` 接收进度、权限和提问事件；每 30 秒轮询权限和提问接口兜底。

## 目录结构

```
.
├── app/                 # 桥接器源码，systemd 运行 app/main.py
│   ├── main.py
│   ├── bridge.py
│   ├── cards.py
│   ├── config.py
│   ├── db.py
│   ├── feishu.py
│   ├── forms.py
│   ├── opencode.py
│   ├── permissions.py
│   └── ws_patch.py
├── deploy/              # systemd unit 模板
│   ├── opencode-serve.service          # 参考；也可由独立脚本管理
│   ├── feishu-opencode-bridge@.service
│   ├── feishu-opencode-bridge-backup@.service
│   └── feishu-opencode-bridge-backup@.timer
├── debug/               # 独立调试脚本（不属于运行代码）
│   ├── probe.py
│   ├── card_probe.py
│   └── README.md
├── tests/               # 单元测试（unittest）
│   ├── test_config.py
│   ├── test_db.py
│   ├── test_forms.py
│   ├── test_opencode.py
│   ├── test_permissions.py
│   └── test_backup.py
├── backup.sh            # 备份脚本
├── requirements.txt     # Python 依赖
├── bridge.env.example   # 环境变量示例
└── README.md
```

## 部署

**推荐方式：模板 unit + 普通用户。** 桥接器直接以当前登录用户（通常也是运行 OpenCode 的用户）运行，安装目录固定为 `$HOME/feishu-opencode-bridge`，systemd 实例名就是该用户名。不需要创建专用用户，也不需要改 unit 文件。

以下以 Debian/Ubuntu + systemd 为例，命令中的 `$USER` 会展开为当前登录用户。旧的专用 `oc` 用户装法仍然兼容，见「[兼容旧版 oc 专用用户](#兼容旧版-oc-专用用户)」。

### 1. 前置条件

- Linux + systemd
- Python 3.13（含 `venv`，Debian/Ubuntu 还需 `python3.13-venv`）
- OpenCode V2，能通过 `opencode serve` 启动
- 飞书企业自建应用，已开启机器人能力

### 2. 飞书应用

1. 在飞书开放平台创建企业自建应用，记录 App ID / App Secret。
2. 开启「机器人」能力。
3. 事件订阅选择「使用长连接接收事件」，订阅 `im.message.receive_v1`。
4. 卡片回调选择「使用长连接接收回调」，订阅 `card.action.trigger`。
5. 按需申请权限：接收单聊消息、以应用身份发消息、更新消息卡片等。
6. 如需多人使用，在「应用发布 → 版本管理与发布」创建版本时设置可用范围（全员或指定成员/部门）；可用范围决定谁能搜到并私聊应用。
7. 发布应用。

不需要公网 IP、回调地址或内网穿透。`FEISHU_ALLOWED_OPEN_IDS` 留空表示不限制；建议部署后先发送 `/whoami` 获取自己的 `open_id`，再填入白名单并重启。多人共用的配置和边界见「[多人共用同一个机器人](#多人共用同一个机器人)」。

### 3. OpenCode 服务

如果你已经有独立的 OpenCode 安装脚本，可以跳过本节；桥接器只要求 `OPENCODE_BASE_URL` 可访问，不依赖 `opencode-serve.service` 这个 unit 名。

下面的 unit 示例沿用旧版 `oc` 用户；如果你用普通用户运行 OpenCode，把 `User`、`Group`、`WorkingDirectory`、`EnvironmentFile`、`ExecStart` 中的 `/home/oc` 换成你的家目录即可。

安装 OpenCode V2（例如 `curl -fsSL https://opencode.ai/v2/install | bash`），准备：

- `~/.config/opencode/opencode.jsonc`：provider、model、agent、MCP 配置。
- `~/.config/opencode/service.env`：代理、API Key、服务密码。V2 用 `OPENCODE_SERVER_PASSWORD` 开启 HTTP Basic 认证，用户名默认 `opencode`。

`/etc/systemd/system/opencode-serve.service` 见 [`deploy/opencode-serve.service`](deploy/opencode-serve.service)，关键配置：

- `User=oc`、`Group=oc`
- `WorkingDirectory=/home/oc/workspace`
- `EnvironmentFile=/home/oc/.config/opencode/service.env`
- `ExecStart=/home/oc/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 4096 --print-logs --log-level info`
- `Restart=always`、`RestartSec=3`

安装：

```bash
sudo cp deploy/opencode-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opencode-serve
```

确认 `bridge.env` 的 `OPENCODE_USERNAME` / `OPENCODE_PASSWORD`、`service.env` 的 `OPENCODE_SERVER_PASSWORD`、`service.json` 的 `password` 三者一致。

桥接器按 `/api/...` 路径调用 OpenCode；升级 OpenCode 后如果接口路径有变化，需要同步调整 `app/opencode.py`。

### 4. 安装桥接器

以普通用户身份安装，不需要 `sudo`：

```bash
cd "$HOME"
git clone https://github.com/francisgao1999/feishu-bridge.git feishu-opencode-bridge
cd feishu-opencode-bridge
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp bridge.env.example bridge.env
chmod 600 bridge.env
chmod +x backup.sh
mkdir -p workspace/temp
```

编辑 `bridge.env`，至少填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ALLOWED_OPEN_IDS`、`OPENCODE_PASSWORD`。路径默认值：

- `BRIDGE_HOME` 默认是仓库根目录（`$HOME/feishu-opencode-bridge`）
- `OPENCODE_DIRECTORY=$BRIDGE_HOME/workspace`
- `BRIDGE_DATA_DIR` 默认等于 `BRIDGE_HOME`
- `BRIDGE_DB=$BRIDGE_DATA_DIR/bridge.db`
- `BRIDGE_LOG=$BRIDGE_DATA_DIR/bridge.log`
- `BRIDGE_TEMP_WORKSPACE_ROOT=$OPENCODE_DIRECTORY/temp`

上面的 `mkdir -p workspace/temp` 已经创建了默认目录。如果 OpenCode 以其他用户运行，确保该用户也能读写 `$HOME/feishu-opencode-bridge/workspace` 和 `$HOME/feishu-opencode-bridge/workspace/temp`。

GitHub 仓库中的源码位于 `app/`；systemd 以 `$HOME/feishu-opencode-bridge` 为工作目录，执行 `app/main.py`。

### 5. 桥接器 systemd

`/etc/systemd/system/feishu-opencode-bridge@.service` 见 [`deploy/feishu-opencode-bridge@.service`](deploy/feishu-opencode-bridge@.service)，关键配置：

- `User=%i`：实例名就是运行用户
- `WorkingDirectory=~`：解析为该用户的家目录
- `ExecStart` 通过 `$HOME` 定位安装目录，兼容任意家目录
- 不依赖 `EnvironmentFile`；`app/config.py` 会自己加载 `$HOME/feishu-opencode-bridge/bridge.env`
- 不依赖 `opencode-serve.service`；只要 `OPENCODE_BASE_URL` 可访问即可
- `MemoryMax=384M`、`NoNewPrivileges=true`、`PrivateTmp=true`

安装（实例名 = 运行用户，推荐直接用当前登录用户）：

```bash
sudo cp deploy/feishu-opencode-bridge@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-opencode-bridge@$USER
```

`$USER` 会由当前 shell 展开。如果 OpenCode 以其他用户运行，把实例名换成那个用户名，并确保该用户对 `$HOME/feishu-opencode-bridge` 有读写权限。

### 6. 备份定时器

备份 unit 见 [`deploy/feishu-opencode-bridge-backup@.service`](deploy/feishu-opencode-bridge-backup@.service) 和 [`deploy/feishu-opencode-bridge-backup@.timer`](deploy/feishu-opencode-bridge-backup@.timer)。

安装（实例名与桥接器一致）：

```bash
sudo cp deploy/feishu-opencode-bridge-backup@.service deploy/feishu-opencode-bridge-backup@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-opencode-bridge-backup@$USER.timer
```

`backup.sh` 会自己定位安装目录并读取 `bridge.env`，因此 `BRIDGE_DB`、`BRIDGE_ENV_FILE` 改了也能正确备份。备份目录默认是 `$BRIDGE_HOME/backups`，可以用 `BRIDGE_BACKUP_DIR` 改到其他磁盘；需要异地备份时，把该目录同步到远端即可。

### 7. 验证

```bash
sudo systemctl status opencode-serve
sudo systemctl status feishu-opencode-bridge@$USER
journalctl -u feishu-opencode-bridge@$USER -f
```

在飞书私聊机器人发送 `/help`，再发送 `/status`；能收到回复即部署成功。日志开头会打印 `bridge home=... data=...`，可用来确认实际路径。

### 8. 更新与同步

推荐流程：**本地开发 → 推送到 GitHub → 生产服务器拉取**。生产服务器只做部署，不直接改代码；确需热修复时，在服务器提交并推回 GitHub，再让本地拉取。

#### 8.1 本地 → GitHub

在开发机的仓库根目录：

```bash
git status
git diff
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app debug tests
bash -n backup.sh

git add -A
git commit -m "feat: ..."
git push origin main
```

如果本地还没有仓库：

```bash
git clone https://github.com/francisgao1999/feishu-bridge.git
cd feishu-bridge
```

#### 8.2 GitHub → 生产服务器

以运行用户登录服务器（默认安装目录 `$HOME/feishu-opencode-bridge`）：

```bash
cd "$HOME/feishu-opencode-bridge"

# 1. 先备份数据库和 bridge.env
./backup.sh

# 2. 确认工作区干净，避免覆盖服务器上的临时修改
git status --short
git log -1 --oneline

# 3. 拉取最新代码（只允许快进，避免产生合并提交）
git pull --ff-only

# 4. 依赖有变化时更新（每次都执行也安全）
.venv/bin/pip install -r requirements.txt

# 5. 重启桥接器
sudo systemctl restart feishu-opencode-bridge@$USER

# 6. 验证
sudo systemctl status feishu-opencode-bridge@$USER --no-pager
sudo journalctl -u feishu-opencode-bridge@$USER -n 50 --no-pager
```

然后在飞书私聊发送 `/status`，确认机器人正常回复。

如果服务器直连 GitHub 较慢，先加载 OpenCode 的代理环境再拉取：

```bash
set -a
source "$HOME/.config/opencode/service.env"
set +a
export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"
git pull --ff-only
```

如果 `git pull --ff-only` 提示本地有修改或分支已分叉，不要直接 `git reset --hard`。先看 `git status` 和 `git log --oneline --graph --decorate -10`，确认服务器上的修改是否需要保留；需要保留就先提交并推送，不需要再处理。

#### 8.3 生产服务器 → GitHub（热修复）

不建议直接在服务器改代码。如果确实需要紧急修复：

```bash
cd "$HOME/feishu-opencode-bridge"
git status
git diff

# 修改并验证后
git add -A
git commit -m "hotfix: ..."
git push origin main
```

如果服务器没有配置 Git 身份，先设置：

```bash
git config user.name "oc-bot"
git config user.email "oc-bot@example.com"
```

如果 `git push` 提示认证，用户名填 GitHub 用户名，密码填 Personal Access Token（GitHub 已不支持账号密码推送）。推送后回到开发机执行 `git pull --ff-only`，保持两边一致。

#### 8.4 回滚

推荐用 `git revert` 生成反向提交，保留历史。在开发机执行：

```bash
git log --oneline -10
git revert <bad-commit>
git push origin main
```

然后在生产服务器按 8.2 的步骤拉取、安装依赖并重启。

紧急情况下也可以在服务器临时切到上一个可用提交：

```bash
cd "$HOME/feishu-opencode-bridge"
git log --oneline -10
git checkout <good-commit>
sudo systemctl restart feishu-opencode-bridge@$USER
```

确认修复后切回 `main` 并拉取最新代码：

```bash
git checkout main
git pull --ff-only
sudo systemctl restart feishu-opencode-bridge@$USER
```

数据库回滚：先停服务，按「[备份与恢复](#备份与恢复)」恢复 `bridge.db`，再启动。数据库结构由 `db.init()` 在启动时自动创建/迁移；升级前仍建议先备份。

#### 8.5 同步检查

两边版本应一致：

```bash
# 开发机
git log -1 --oneline

# 生产服务器
cd "$HOME/feishu-opencode-bridge"
git log -1 --oneline
git status --short
```

`git status --short` 没有输出表示服务器工作区干净；如果有输出，说明服务器上有未提交的修改，先确认来源再拉取。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app debug tests
bash -n backup.sh
```

测试覆盖 `app/config.py` 的默认路径、变量覆盖、只读 Agent 配置和 `bridge.env` 加载，`app/opencode.py` 的 Agent 列表解析，`app/permissions.py` 的只读 Agent 检测和会话权限规则，`app/forms.py` 的表单卡构建与答案转换，`app/db.py` 的 Agent 字段迁移、forms CRUD 与读写，以及 `backup.sh` 的默认/自定义路径、缺失数据库和备份权限。

## 调试工具

`debug/` 下是独立调试脚本，不参与桥接器运行：

- `debug/probe.py`：验证飞书长连接和 `im.message.receive_v1` 事件订阅，打印原始消息事件。
- `debug/card_probe.py`：向 `FEISHU_TEST_CHAT_ID` 发送测试审批卡，打印原始 `CARD` 帧和按钮回调；仅用于调试，生产环境使用 `app/ws_patch.py`。

凭据从环境变量读取，不要硬编码：

```bash
cd "$HOME/feishu-opencode-bridge"
set -a
source bridge.env
set +a
export FEISHU_TEST_CHAT_ID=oc_xxx
.venv/bin/python debug/probe.py
.venv/bin/python debug/card_probe.py
```

详细说明见 [`debug/README.md`](debug/README.md)。

## 服务

| 服务 | 作用 |
| --- | --- |
| `opencode-serve.service` | OpenCode API，监听 `127.0.0.1:4096`（也可由独立脚本管理） |
| `feishu-opencode-bridge@<用户>.service` | 飞书桥接器，实例名是运行用户 |
| `feishu-opencode-bridge-backup@<用户>.timer` | 每日 03:30 备份，保留 7 天 |

常用命令（实例名 = 运行用户，下面用 `$USER`）：

```bash
sudo systemctl status opencode-serve
sudo systemctl status feishu-opencode-bridge@$USER
sudo systemctl restart feishu-opencode-bridge@$USER
journalctl -u feishu-opencode-bridge@$USER -f
tail -f "$HOME/feishu-opencode-bridge/bridge.log"
```

## 飞书命令

- `/help` 或 `/start`：帮助
- `/new [标题]`：创建并切换到新会话；参数是会话标题，不会发给模型。创建后发送的内容才会发给模型
- `/temp [标题]`（或 `/t`、`/临时`）：创建并切换到临时会话；参数是会话标题，不会发给模型。临时会话使用独立空目录，空闲 30 分钟后自动清理
- `/temp end`（或 `/temp 结束`、`/temp stop`）：结束当前临时会话，删除 OpenCode 会话和临时工作目录，并清空当前会话
- `/temp list`（或 `/temp 列表`）：查看临时会话、剩余时间和临时目录
- `/sessions`（或 `/list`）：以卡片列出全部会话、运行状态、待审批/待回答数量；顶部固定显示当前会话，列表中用 `[当前]` 标记
- `/switch <序号|session_id|标题>`（或 `/use`）：切换当前会话；同时显示目标会话的模型、Agent、上次任务的状态、完成时间和结果摘要
- `/last [序号|session_id]`（或 `/result`）：重发该会话最后一次任务结果，默认当前会话
- `/delete <序号|session_id|标题>`（或 `/remove`、`/删除`）：删除一个空闲会话（当前会话不能删除）
- `/clean`（或 `/清理`）：列出全部空闲会话；`/clean confirm` 确认清理（保留当前会话；confirm 也可写 `确认`/`yes`/`y`）
- `/schedule`（或 `/定时`）：定时任务 / 单次提醒，详见下方「定时任务」
- `/status`：查看当前会话、全部会话、当前任务、待审批、当前模型和 Agent
- `/model`：回复模型选择卡片，点击按钮切换
- `/model <序号>`：按 `/model` 列表中的序号切换
- `/model <供应商>/<模型>`：直接指定切换，例如 `/model systalyze/deepseek-ai/DeepSeek-V4-Pro-0813`
- `/agent`：回复 Agent 选择卡片，点击按钮切换
- `/agent <序号>`：按 `/agent` 列表中的序号切换
- `/agent <agent_id|名称>`：直接指定切换，例如 `/agent plan`
- `/stop [序号|session_id]`：中止任务，默认当前会话；并清理该会话的待审批和待回答提问
- `/whoami`：显示 open_id / chat_id / session_id / 会话标题 / 当前模型 / 当前 Agent
- 直接发送文本：在当前会话派任务
- 当前没有会话时（例如刚执行 `/temp end`），直接发送文本会自动创建普通会话，标题取消息前 40 个字符

### 飞书输入框命令面板

桥接器启动时会通过飞书 OpenAPI 幂等同步 14 个顶层命令到输入框的 `/` 面板：

`/new`、`/temp`、`/sessions`、`/switch`、`/status`、`/stop`、`/model`、`/agent`、`/schedule`、`/help`、`/last`、`/delete`、`/clean`、`/whoami`

- 需要应用开通 `application:app_slash_command:read` 和 `application:app_slash_command:write`，并发布新版本。
- 飞书客户端要求：桌面端 7.70+，移动端 7.71+。
- 注册/修改后约 5 分钟生效（客户端缓存），重启飞书客户端可加快。
- 只注册顶层命令；别名（`/t`、`/list`、`/use` 等）和子命令（`/temp end`、`/schedule list` 等）仍可手打，不单独占用面板。
- 同步只新增和更新描述，不删除面板里已有的其他命令；同步失败只记日志，不影响机器人收发消息。
- 如果启动日志出现 `sync slash commands failed`，优先检查权限是否已开通并发布版本。

## 多会话并行

- 一个飞书聊天可以绑定多个 OpenCode 会话，`chats.session_id` 只表示“当前会话”。
- 会话 A 正在执行任务时，发送 `/new B` 会创建会话 B 并切换过去；再发送任务即可在 B 中执行，A 的任务继续运行。
- 发送 `/sessions` 查看全部会话，发送 `/switch 1` 或 `/switch <session_id>` 切回 A。
- `/switch` 会显示目标会话上次任务的状态、完成时间和结果摘要；需要完整结果时发送 `/last`（或 `/last <序号|session_id>`）重发结果卡。
- 每个会话同一时间只允许一个任务；不同会话可以并行执行。
- 进度卡、审批卡、结果卡都按 `session_id` 路由，不会串到别的会话。
- 模型按会话保存；`/model` 只修改当前会话，`/new` 创建的新会话继承当前会话的模型。
- Agent 也按会话保存；`/agent` 只修改当前会话，`/new` 和 `/temp` 创建的新会话继承当前会话的 Agent。
- 历史会话会自动迁移到 `sessions` 表，升级后可以直接 `/sessions` 看到并切换。

## 多人共用同一个机器人

一个飞书自建应用（一个 `oc-bot`）可以让企业内多个人同时私聊使用，桥接器没有“单用户”限制。是否放行由两层控制：

1. **飞书应用可用范围**：开放平台 → 应用发布 → 版本管理与发布 → 创建版本时设置可用范围（全员或指定成员/部门），然后发布（企业管理员审核后生效）。不在可用范围内的人搜不到应用，也无法私聊。
2. **`FEISHU_ALLOWED_OPEN_IDS` 白名单**：留空表示不限制（租户内所有能用该应用的人都可以用）；非空时只有列出的 `open_id` 能收到回复，其他人发消息会被拒绝并记录日志。

放行新成员：

```bash
# 1. 让对方先给机器人发一条消息
# 2. 在服务器上查日志拿 open_id
grep "rejected message" ~/feishu-opencode-bridge/bridge.log | tail

# 3. 编辑 bridge.env，逗号分隔追加
FEISHU_ALLOWED_OPEN_IDS=ou_xxx,ou_yyy

# 4. 重启桥接器（实例名 = 运行用户）
sudo systemctl restart feishu-opencode-bridge@$USER
```

也可以让对方发送 `/whoami` 查看自己的 `open_id`，但需要先把对方加入白名单，或临时留空白名单。

### 隔离范围

- **按私聊隔离**：每个私聊有独立的 `chat_id`；会话、任务、审批、定时任务、模型和 Agent 选择都按 `chat_id` 保存。`/sessions`、`/switch`、`/last`、`/delete`、`/clean`、`/stop`、`/schedule`、`/status`、`/model`、`/agent`、`/temp` 都只能操作自己聊天里的数据，不能通过 session_id 访问别人的会话。
- **只支持私聊**：群聊里的消息（包括 @机器人）会被忽略，机器人不会在群里回复。
- **共享资源**：所有用户共用同一个桥接器进程、同一个 OpenCode 服务、同一个 `OPENCODE_DIRECTORY` 工作目录、同一个系统用户、同一套 agent/MCP 配置和同一套 API Key/额度。
- **并发**：不同私聊的任务可以并行；同一会话同一时间只允许一个任务。多人同时跑重任务时，CPU、内存、模型额度会互相影响。

### 边界情况

- **不是多租户隔离**：所有用户的 agent 都能读写同一个工作目录，也能执行 shell 命令。A 用户可以让 agent 查看/修改 B 用户会话产生的文件，甚至执行 `git reset --hard` 影响所有人的工作区；`/temp` 只是空工作目录，不是沙箱，仍可能访问目录外文件。只应把机器人开放给信任的人。
- **审批权限按白名单校验，不按任务归属校验**：审批卡只会发到发起任务的私聊，但回调只检查点击者是否在白名单内，不检查点击者是否属于该审批所在聊天。不要把审批卡转发给其他白名单用户，否则对方可以替你点「允许」。
- **模型卡和 Agent 卡按会话校验**：模型/Agent 切换会校验卡片所属会话，不能通过转发修改别人会话的模型或 Agent。
- **白名单变更需要重启**：`FEISHU_ALLOWED_OPEN_IDS` 只在启动时读取，改完必须重启桥接器；飞书可用范围变更需要重新发布版本。
- **移除白名单不会停止已有任务和定时任务**：白名单只拦截新的入站消息。已经在跑的任务会继续执行并把结果发到原聊天；已创建的定时任务到点仍会执行。移除前先 `/stop` 并清理该用户的定时任务。
- **移除白名单后无法再审批或回答提问**：该用户点击审批卡/提问卡会被拒绝，正在等待审批的任务会在 30 分钟后自动拒绝并中止；正在等待回答的提问会在 30 分钟后自动取消并中断任务。
- **重新加入白名单会恢复原有数据**：历史会话、任务和定时任务没有删除，重新加入后仍可访问。
- **成员离开企业**：其 `open_id` 留在白名单里不会产生新的访问（对方已无法使用应用），但历史会话、定时任务和数据库记录会保留；定时任务发送失败会记录日志。建议离开前清理其定时任务和会话。
- **未在白名单的人收不到任何回复**：对方可能以为机器人坏了；建议同时收紧飞书可用范围，或在应用说明里写明使用范围。
- **日志和数据库包含 open_id/chat_id**：多人使用时这些标识会出现在 `bridge.log` 和 `bridge.db` 中，按内部数据管理。
- **额度与密钥共享**：所有用户消耗同一个 OpenCode 服务配置的 API Key 和额度；一个用户的长任务或大输出会影响其他人。
- **全局配置对所有人生效**：`BRIDGE_MAX_FINAL_CHARS`、`BRIDGE_TASK_TIMEOUT_SECONDS`、`OPENCODE_AGENT`、`OPENCODE_READONLY_AGENTS`、默认模型等不能按用户单独设置。
- **审批超时**：10 分钟未处理发提醒，30 分钟自动拒绝并中止任务；提醒只发到任务发起人的私聊。
- **提问超时**：5 分钟未处理发提醒，30 分钟自动取消并中断任务；提醒只发到任务发起人的私聊。
- **桥接器或 OpenCode 重启影响所有人**：所有用户的未完成任务都会中断并收到通知；飞书接口限流也按应用计算，多人高频使用可能互相影响。
- **只处理文本消息**：图片、文件、富文本等会收到「目前只支持文本消息」提示。

需要真正的隔离时，给每个用户独立部署一套桥接器 + OpenCode，或使用独立系统用户/容器/VM，不要只依赖白名单。

## 临时会话

- `/temp [标题]` 创建临时会话并切换过去；参数是标题，不会发给模型。之后发送的内容才会发给模型，可以继续多轮对话。
- 临时会话使用独立空目录，默认位于 `$OPENCODE_DIRECTORY/temp`（默认 `$BRIDGE_HOME/workspace/temp`），创建 OpenCode 会话时作为工作目录传入。
- 临时会话默认空闲 30 分钟后自动清理；任务执行中、有待审批或待回答提问时不会清理。
- 每次发送消息、提交任务、任务完成、`/switch` 进入临时会话都会重新计时。
- 到期时如果临时会话是当前会话，会先清空当前会话，再删除 OpenCode 会话、临时目录和 SQLite 记录；之后发送任意消息会自动创建普通会话。
- `/temp end` 立即结束当前临时会话；`/temp list` 查看临时会话、剩余时间和目录。
- `/sessions` 中临时会话带 `[临时]` 标记；`/switch` 进入临时会话会重置计时。
- `/new` 创建普通会话并切换后，原临时会话继续倒计时，到期后静默删除。
- 临时会话继承创建时当前会话的模型和 Agent；之后可以用 `/model`、`/agent` 单独切换。
- `/delete` 非当前临时会话会同时删除目录；当前临时会话请用 `/temp end`。
- `/clean` 会清理空闲临时会话及其目录。
- 临时目录是工作目录，不是沙箱；agent 仍可能读写目录外文件。空目录意味着 agent 默认看不到项目文件，需要时请使用绝对路径。
- 删除临时目录不会杀掉 agent 启动的后台进程；需要更强隔离时请使用容器/VM。
- 如果 OpenCode 服务启用了 `ProtectHome`、`ReadWritePaths` 等 systemd 限制，需要确保它能读写 `BRIDGE_TEMP_WORKSPACE_ROOT`。

## 会话清理

- `/sessions` 太长时，可以用 `/delete <序号|session_id|标题>` 删除单个空闲会话。
- `/clean` 会列出所有可删除的空闲会话（不含当前会话、不含正在执行任务的会话），再发送 `/clean confirm` 才会真正删除。
- 删除会话会同时调用 OpenCode `DELETE /api/session/{sessionID}`，删除该会话及其子会话；SQLite 中的会话记录、任务记录、审批记录和提问记录也会移除，删除后不可恢复。
- 当前会话不能删除；如需删除，先 `/switch` 到其他会话。
- 有任务正在执行的会话不能删除；先等待完成或 `/stop`。
- 删除会话前会自动拒绝该会话的待审批请求，并取消待回答的提问。
- 如果定时任务绑定的会话被删除，定时任务会在下次执行时自动创建新会话。

## 任务可靠性

- 上游模型报错时，如果 OpenCode 上报 `session.execution.failed`，机器人会更新进度卡并发送「任务失败: <错误信息>」。
- 如果 OpenCode 上报 `session.retry.scheduled`（自动重试），进度卡会显示「上游报错，重试中（第 N 次）」，并发送一次重试提醒。
- 如果上报 `session.execution.interrupted`，任务会标记为已中止并通知。
- 如果只上报 `session.error`，桥接器也会把当前任务标记失败并通知。
- 后台每 60 秒对账一次：如果任务仍在执行但 OpenCode 已空闲，会读取会话的 `idle` 消息补发结果；SSE 断线期间错过的完成/失败事件可以自动恢复。
- 任务正在等待用户回答提问时，对账会跳过该任务，由提问超时循环负责中断，避免把正常等待误判为卡死。
- 任务超过 60 分钟仍未完成会自动中止并通知，避免一直卡在「执行中」。超时时间可通过 `BRIDGE_TASK_TIMEOUT_SECONDS` 调整。

## 定时任务

- 支持单次提醒、单次任务和周期任务，命令和自然语言都可以创建。

### 单次提醒 / 单次任务

```
/schedule 13:38 提醒我【去接水】
/schedule 13:38分提醒我【去接水】
/schedule 明天 09:00 帮我查一下今天的天气
/schedule 2026-11-10 13:38 整理当天会议纪要
/schedule once 13:38 帮我查一下今天的天气
```

- 也可以直接发送自然语言，例如 `13:38分提醒我【去接水】`、`明天下午3点提醒我开会`。
- 时间格式支持 `13:38`、`13点38分`、`下午3点`、`明天/后天`、`2026-11-10`、`11-10`、`11月10日`。
- 只写时间且时间已过时，会自动顺延到明天；明确写了日期但时间已过会提示无法创建。
- 单次提醒到点直接发送提醒卡片，不创建 OpenCode 会话，也不会切换当前会话。
- 单次任务到点后使用独立会话执行，结果作为新卡片发到本聊天。
- 单次任务执行后自动标记为「已完成」，不能重复执行；如需再次执行请重新创建。
- 错过执行时间 30 分钟内会补执行；超过 30 分钟会取消并发送提示。
- 可以 `/schedule pause <id>` 暂停、`/schedule resume <id>` 恢复（仅限未执行且时间未过）、`/schedule run <id>` 立即执行。

### 周期任务

- 支持命令和自然语言两种创建方式：

```
/schedule daily 10:00 整理昨日 linux.do 福利羊毛板块的帖子摘要
/schedule weekdays 09:30 汇总今天的告警
/schedule weekly mon 10:00 整理上周周报
```

- 也可以直接发送自然语言，例如：

```
每天早上10点给我发送昨日到现在 linux.do 中福利羊毛板块的帖子内容摘要
每周一 09:30 整理上周周报
工作日下午3点 汇总今天的告警
```

- 管理命令：

```
/schedule add daily 10:00 <任务>   添加周期任务（也可写 添加）
/schedule list              查看定时任务
/schedule pause <id>        暂停
/schedule resume <id>       恢复
/schedule remove <id>       删除（也可写 delete）
/schedule run <id>          立即执行一次（不影响下次执行时间）
```

- `<id>` 是 `/schedule list` 里显示的 `#N` 编号，来自数据库自增 ID；删除后不会复用，所以列表里的编号可能不连续。

- 子命令也支持中文：列表/暂停/恢复/删除/执行/单次/一次性/添加；周期可写每天/每日、工作日、每周一。

- 每个周期任务使用独立会话，标题为 `定时: <任务摘要>`；创建和执行都不会切换当前会话。
- 定时任务创建会话时继承该聊天当前会话的模型和 Agent；之后修改当前会话不会影响已创建的定时任务会话。
- 到点后复用普通任务链路：发进度卡 → 执行 → 在聊天底部发结果卡，因此会触发飞书消息通知。
- 时区固定为 `Asia/Shanghai`；后台每 30 秒检查一次到期任务。
- 服务重启后，错过执行时间 30 分钟内会补跑一次；超过 30 分钟则跳过本次并发送提示。
- 如果该定时任务上一次还没执行完，本次会跳过，避免任务堆积。
- 定时任务如果触发审批，仍需要手动点击审批卡片；30 分钟未处理会自动拒绝并中止本次任务。
- 定时任务的会话和聊天记录在删除任务后仍会保留，可在 `/sessions` 中查看历史。

## 模型切换

- 模型列表来自 `/api/model`，包含 OpenCode 内置的免费模型（`opencode/*-free`、`opencode/big-pickle` 等）以及 `opencode.jsonc` 中 `providers` 下配置的供应商和模型；旧版本 OpenCode 没有 `/api/model` 时自动回退到 `/api/config`。
- 发送 `/model` 后回复卡片，按供应商分组列出模型，当前模型带 ✓。
- 点击按钮后立即切换卡片所属会话的模型，并把选择持久化到 SQLite `sessions.model_provider` / `sessions.model_id`（同时同步 `chats` 表）。
- 后续 `/new` 创建的新会话继承当前会话的模型；`/status` 显示当前会话的模型；未选择时回退 `bridge.env` 中的默认模型。
- 卡片所属会话有任务执行时不允许切换，需先 `/stop` 或等待完成；其他会话在跑不影响切换。
- 切换只影响后续请求，不会重跑已完成的任务。

## Agent 切换

- Agent 列表来自 OpenCode V2 的 `GET /api/agent`，自动包含 `build`、`plan` 以及 `opencode.jsonc` 中自定义的 `primary` / `all` agent；`subagent` 和隐藏的维护 agent（`compaction`、`title`、`summary` 等）不会出现在列表中。
- 发送 `/agent` 后回复选择卡片，当前 Agent 带 ✓；也可以 `/agent <序号>`、`/agent <agent_id>` 或 `/agent <名称>` 直接切换，例如 `/agent plan`。
- 点击按钮或直接切换会调用 `POST /api/session/{sessionID}/agent`，并把选择持久化到 SQLite `sessions.agent`（同时同步 `chats.agent`）。
- 后续 `/new`、`/temp` 创建的新会话继承当前会话的 Agent；`/status`、`/whoami` 和 `/switch` 都会显示当前 Agent；未选择时回退 `bridge.env` 中的 `OPENCODE_AGENT`（默认 `build`）。
- 卡片所属会话有任务执行时不允许切换，需先 `/stop` 或等待完成；其他会话在跑不影响切换。
- 切换只影响后续请求，不会重跑已完成的任务。
- 只读 Agent（指不注入会话级 `edit * ask`，让 Agent 自己的 `edit * deny` 生效）：`plan` 默认按只读处理（`OPENCODE_READONLY_AGENTS`，逗号分隔）。如果不省略这条规则，它会覆盖 plan 自己的 `edit * deny`，导致 plan 仍能通过 edit 工具改文件。桥接器也会读取 Agent 自身的 `permissions`，如果最后一条匹配 `edit *` 的规则是 `deny`，同样按只读处理。
- Agent 列表按会话工作目录查询：普通会话用 `OPENCODE_DIRECTORY`，临时会话用其临时目录；目录查询失败时回退到全局列表。
- 如果保存的 Agent 已不存在（例如自定义 agent 被删除），创建新会话时会回退到 `OPENCODE_AGENT`，再回退到列表中的第一个可用 Agent。
- 需要 OpenCode V2 提供 `/api/agent`、`/api/session/{sessionID}/agent` 和 `PATCH /api/session/{sessionID}`；旧版本没有这些接口时 `/agent` 会提示获取列表失败，普通任务不受影响。

## 审批策略

- 会话级规则：普通 `shell` 命令默认 `allow`；危险命令（`rm`、`sudo`、`dd`、`mkfs`、`systemctl` 启停、包管理器安装/卸载、`git push`/`reset --hard`、`docker rm` 等）`ask`。
- 普通 Agent 的 `edit` 仍然全部 `ask`；只读 Agent（默认 `plan`，可用 `OPENCODE_READONLY_AGENTS` 调整）不注入 `edit * ask`，让 Agent 自己的 `edit * deny` 生效。`read` 匹配 `*.env`、`*.env.*` 也 `ask`，`*.env.example` 例外允许。
- 危险命令列表在 `app/permissions.py` 的 `DANGEROUS_SHELL_PATTERNS`，可自行增删。
- 这是尽力而为的黑名单，不是安全边界；通过解释器或脚本（如 `python -c`、`bash script.sh`）仍可能绕过。需要更强隔离时，建议使用容器/VM 或改成白名单。
- 审批卡片只有「允许一次」和「拒绝」，没有“总是批准”。
- 10 分钟未处理发文字提醒，30 分钟自动拒绝并中止任务。
- 点击后卡片立即更新；重复回调幂等。
- 任务完成后，原进度卡只收尾为「完成」，结果统一在聊天底部另发一张新结果卡。飞书更新卡片不会触发消息通知，只更新原卡容易让用户错过结果；审批卡也可能把结果压在更上方。
- 桥接器重启时，未完成任务会标记失败、更新卡片并发送通知。

## 提问（form）交互

OpenCode 的 `question` 工具会创建 form，等待用户输入。桥接器把它转换成飞书卡片 2.0 表单，直接在聊天里填写和提交。

- 收到 `form.created` 后发送「OpenCode 需要你的输入」表单卡；卡片包含任务摘要、问题标题/描述和对应输入组件。
- 有选项的字段使用下拉框；`multiple: true` 使用多选；`custom: true` 额外提供一个「其他答案」输入框，填写后优先使用自定义值。
- 布尔字段用「是/否」下拉框；数字/整数字段用输入框，由桥接器转换和校验，整数不接受小数。
- 所有表单组件都不启用飞书前端必填校验，必填由桥接器在提交时校验，避免自定义答案被前端拦住；校验失败会返回 toast，不会关闭卡片。
- 提交后调用 `POST /api/session/{sessionID}/form/{formID}/reply`，卡片更新为「提问已提交」并显示答案，任务继续执行。
- 点击「取消提问」会调用 `DELETE /api/session/{sessionID}/form/{formID}`，取消 form、中断任务，并更新任务卡和表单卡。
- 默认 5 分钟未处理发一次文字提醒，30 分钟未处理自动取消 form 并中断任务；表单卡会明确显示「提问已过期 / 任务已中断」。
- 超时时间可通过 `BRIDGE_FORM_REMIND_SECONDS` / `BRIDGE_FORM_EXPIRE_SECONDS` 调整，设为 `0` 可关闭提醒或自动过期。
- 后台每 30 秒轮询一次 form 接口兜底，SSE 断线时也能发现新提问和已回答/已取消状态。
- 桥接器重启后，未完成任务会被标记失败；相关 form 会自动取消，卡片更新为失效状态并标明任务已中断。
- `/stop`、`/delete` 和新任务提交都会取消该会话的待回答 form，避免 OpenCode 里留下悬挂请求；临时会话有待回答提问时不会被清理，等提问过期后再清理。
- 当前任务正在等待回答时，新消息会提示先处理提问卡片或 `/stop`；`/sessions` 会显示「待回答 N」。

限制：

- 飞书卡片 2.0 的 `form` 容器只能放在根节点；桥接器已按此约束生成卡片。
- 飞书 `input` 只支持文本/多行文本/密码，数字和布尔值由桥接器转换。
- 自定义答案只对带选项的字符串/多选字段显示；纯文本字段本身就可以直接输入任意内容。
- 表单卡只发到任务发起人的私聊，回调仍按白名单校验点击者。

## 配置

- `$BRIDGE_HOME/bridge.env`（600，`BRIDGE_HOME` 默认是仓库根目录）
  - 飞书 App ID / Secret
  - 白名单 open_id（逗号分隔；留空表示不限制，多人共用见「[多人共用同一个机器人](#多人共用同一个机器人)」）
  - OpenCode 地址、用户名、密码
  - 工作目录、默认 Agent `OPENCODE_AGENT`、只读 Agent `OPENCODE_READONLY_AGENTS`、模型
  - 进度间隔、审批提醒/超时、提问提醒/超时、任务对账/超时、结果截断长度
  - 临时会话空闲时长 `BRIDGE_TEMP_SESSION_TTL_SECONDS`（默认 1800 秒）
  - 临时工作目录根路径 `BRIDGE_TEMP_WORKSPACE_ROOT`（默认 `$OPENCODE_DIRECTORY/temp`）
  - 仓库中的 `bridge.env.example` 是变量模板，复制为 `bridge.env` 后填写
- `$HOME/.config/opencode/service.env`（600）
  - OpenCode 服务环境变量：代理、API Key、服务密码 `OPENCODE_SERVER_PASSWORD`
- `$HOME/.config/opencode/service.json`（600）
  - OpenCode 服务密码 `password`，需与 `service.env` 的 `OPENCODE_SERVER_PASSWORD`、`bridge.env` 的 `OPENCODE_PASSWORD` 保持一致
- `$HOME/.config/opencode/opencode.jsonc`
  - 模型、agent、MCP 配置

路径变量说明：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRIDGE_HOME` | 仓库根目录 | 安装目录；默认 `bridge.env` 也在这里 |
| `BRIDGE_DATA_DIR` | `$BRIDGE_HOME` | 数据目录；相对路径按 `BRIDGE_HOME` 解析 |
| `BRIDGE_DB` | `$BRIDGE_DATA_DIR/bridge.db` | SQLite 数据库 |
| `BRIDGE_LOG` | `$BRIDGE_DATA_DIR/bridge.log` | 日志文件 |
| `BRIDGE_TEMP_WORKSPACE_ROOT` | `$OPENCODE_DIRECTORY/temp` | 临时会话目录 |
| `OPENCODE_DIRECTORY` | `$BRIDGE_HOME/workspace` | 普通会话工作目录 |
| `BRIDGE_BACKUP_DIR` | `$BRIDGE_HOME/backups` | 备份输出目录（仅 `backup.sh`） |

优先级从高到低：**进程环境变量 > `bridge.env` > 默认值**。`BRIDGE_HOME` / `BRIDGE_DATA_DIR` 的完整优先级和相对路径规则见「[用户与路径](#用户与路径)」。

`BRIDGE_ENV_FILE` 只在启动前通过环境变量指定，不能写在 `bridge.env` 里；默认是 `$BRIDGE_HOME/bridge.env`。

修改后重启：

```bash
sudo systemctl restart feishu-opencode-bridge@$USER
```

## 备份与恢复

备份目录默认是 `$BRIDGE_HOME/backups/`，保留 7 天；可以用 `BRIDGE_BACKUP_DIR` 改到其他磁盘。备份内容包含 SQLite 一致性快照、`bridge.env` 和 `app/` 源码。

手动备份：

```bash
"$HOME/feishu-opencode-bridge/backup.sh"
```

恢复（先停服务，并删除 WAL/SHM 残留；如果自定义了 `BRIDGE_DATA_DIR` / `BRIDGE_DB`，把下面的数据库路径换成实际路径）：

```bash
sudo systemctl stop feishu-opencode-bridge@$USER
rm -rf /tmp/bridge-restore
mkdir -p /tmp/bridge-restore
tar -xzf "$HOME/feishu-opencode-bridge/backups/bridge-YYYYmmdd-HHMMSS.tar.gz" \
  -C /tmp/bridge-restore
DB="$HOME/feishu-opencode-bridge/bridge.db"
rm -f "$DB-wal" "$DB-shm"
cp /tmp/bridge-restore/bridge.db "$DB"
cp /tmp/bridge-restore/bridge.env "$HOME/feishu-opencode-bridge/bridge.env"
chmod 600 "$HOME/feishu-opencode-bridge/bridge.env"
sudo systemctl start feishu-opencode-bridge@$USER
```

注意：恢复数据库前先停服务并删除 `bridge.db-wal` / `bridge.db-shm`，否则 SQLite 可能继续使用旧的 WAL；`bridge.env` 含密钥，权限保持 600。需要异地备份时，把 `$BRIDGE_BACKUP_DIR` 同步到远端存储即可。

## 故障排查

- **机器人不回消息**
  - 检查 `systemctl status feishu-opencode-bridge@$USER`
  - 飞书应用是否已发布
  - 是否启用“使用长连接接收事件”
  - 是否订阅 `im.message.receive_v1`
  - 是否启用“使用长连接接收回调”并订阅 `card.action.trigger`
- **卡片按钮无反应**
  - `lark-oapi 1.7.5` 会丢弃 `CARD` 帧，必须保留 `app/ws_patch.py` 的 monkey-patch。
- **审批卡不更新**
  - 必须在卡片回调响应里返回 `card: {"type": "raw", "data": <card>}`。
  - 回调处理期间单独调用 `PATCH /im/v1/messages/{id}` 会被忽略。
- **提问卡没有出现 / 任务卡在等待输入**
  - 确认 OpenCode 版本支持 form API：`GET /api/session/{sessionID}/form` 可访问。
  - 查看日志中的 `form card sent`、`send form card failed`、`form poll failed`。
  - 确认飞书已订阅 `card.action.trigger`，且 SSE `/api/event` 连接正常。
  - 表单卡使用卡片 2.0，飞书客户端需要 7.20 及以上版本。
  - 默认 30 分钟未处理会自动取消并中断任务；可调整 `BRIDGE_FORM_EXPIRE_SECONDS`。
- **OpenCode 401**
  - 检查 `bridge.env` 的 `OPENCODE_PASSWORD`、`service.env` 的 `OPENCODE_SERVER_PASSWORD`、`service.json` 的 `password` 三者是否一致。
- **`/agent` 获取列表失败或看不到自定义 Agent**
  - 确认 OpenCode 是 V2，且 `GET /api/agent` 可访问；旧版本没有该接口。
  - 自定义 Agent 的 `mode` 需要是 `primary` 或 `all`；`subagent` 和 `hidden: true` 的 Agent 不会列出。
  - 普通会话按 `OPENCODE_DIRECTORY` 查询 Agent，临时会话按临时目录查询；目录不存在时会回退到全局列表。
  - 保存的 Agent 被删除后，新会话会自动回退到 `OPENCODE_AGENT` 或列表中的第一个 Agent。
- **`plan` 仍然能通过 edit 工具修改文件**
  - 确认 `OPENCODE_READONLY_AGENTS` 包含 `plan`（默认已包含），或该 Agent 自身最后一条 `edit *` 规则是 `deny`。
  - 切换 Agent 只影响后续任务；切换前已提交的任务仍按原规则执行。
- **任务卡在“执行中”**
  - 后台每 60 秒会对账一次，OpenCode 已空闲时会自动补发结果或标记失败。
  - 超过 60 分钟仍未完成会自动中止并通知。
  - 桥接器重启会自动标记失败并通知；也可以发送 `/stop`。
- **临时会话创建失败或目录不可写**
  - 检查 `BRIDGE_TEMP_WORKSPACE_ROOT` 是否存在、桥接器是否有写权限。
  - 确认 OpenCode 服务能访问同一路径；如有 `ProtectHome` / `ReadWritePaths`，把该路径加入白名单。
  - 桥接器和 OpenCode 最好以同一用户运行；不同用户时需保证临时目录对 OpenCode 可读写。
  - 查看日志中的 `remove temp dir` / `create temp session` 错误。
- **内存不足**
  - `free -h` 查看。
  - 桥接器约 180MB，OpenCode 约 270MB，Playwright MCP 约 200MB。
  - 不需要浏览器自动化时，可在 `opencode.jsonc` 中把 `playwright` MCP 设为 `"enabled": false`，可省约 200MB。
- **路径不对 / 找不到 bridge.env**
  - 日志开头的 `bridge home=... data=...` 是实际使用的路径。
  - 检查 systemd 实例名是否与运行用户一致（`feishu-opencode-bridge@<用户>`）。
  - `BRIDGE_ENV_FILE` 只能通过环境变量或 unit 指定，不能写在 `bridge.env` 里。

## 用户与路径

### 推荐：普通用户 + 模板 unit

桥接器推荐以当前登录用户运行，实例名就是用户名：

```bash
sudo systemctl enable --now feishu-opencode-bridge@$USER
sudo systemctl enable --now feishu-opencode-bridge-backup@$USER.timer
```

unit 使用 `WorkingDirectory=~` 和 `$HOME` 定位安装目录，因此安装目录固定为 `$HOME/feishu-opencode-bridge`，不依赖 `/home/<用户>` 这种固定路径。普通用户只需要在安装 systemd unit 时使用 `sudo`，日常运行、升级和备份都不需要 root。

### 兼容旧版 oc 专用用户

以前的专用 `oc` 用户装法仍然可用，不需要改 unit 文件，只要把实例名换成 `oc`：

```bash
sudo systemctl enable --now feishu-opencode-bridge@oc
sudo systemctl enable --now feishu-opencode-bridge-backup@oc.timer
```

如果 `oc` 用户的家目录是 `/home/oc`，安装目录就是 `/home/oc/feishu-opencode-bridge`，与旧版一致。旧版 `bridge.env` 里显式写死的 `/home/oc/...` 路径也能继续使用；新变量都有默认值，不迁移也能跑。

### BRIDGE_HOME / BRIDGE_DATA_DIR 优先级

`BRIDGE_HOME` 和 `BRIDGE_DATA_DIR` 都按以下优先级取值，从高到低：

1. **进程环境变量**：systemd unit 的 `Environment=` / `EnvironmentFile=`、shell `export`、`systemctl set-environment` 等
2. **`bridge.env` 中的值**
3. **默认值**

具体默认值和相对路径解析基准：

| 变量 | 默认值 | 相对路径解析基准 |
| --- | --- | --- |
| `BRIDGE_HOME` | 仓库根目录 | 绝对路径或 `~`；相对路径按进程工作目录解析 |
| `BRIDGE_DATA_DIR` | `$BRIDGE_HOME` | `$BRIDGE_HOME` |
| `BRIDGE_DB` | `$BRIDGE_DATA_DIR/bridge.db` | `$BRIDGE_DATA_DIR` |
| `BRIDGE_LOG` | `$BRIDGE_DATA_DIR/bridge.log` | `$BRIDGE_DATA_DIR` |
| `BRIDGE_TEMP_WORKSPACE_ROOT` | `$OPENCODE_DIRECTORY/temp` | `$BRIDGE_DATA_DIR` |
| `OPENCODE_DIRECTORY` | `$BRIDGE_HOME/workspace` | `$BRIDGE_HOME` |
| `BRIDGE_BACKUP_DIR` | `$BRIDGE_HOME/backups` | `$BRIDGE_HOME`（仅 `backup.sh`） |

注意：

- `BRIDGE_ENV_FILE` 是启动引导变量，只能来自进程环境，不能写在 `bridge.env` 里；默认 `$BRIDGE_HOME/bridge.env`，相对路径按 `BRIDGE_HOME` 解析。
- 如果 `BRIDGE_HOME` 只写在 `bridge.env` 里，`bridge.env` 本身仍从默认位置（仓库根目录）加载；`BRIDGE_HOME` 只影响后续路径解析。要改变 `bridge.env` 的加载位置，必须通过进程环境设置 `BRIDGE_HOME` 或 `BRIDGE_ENV_FILE`。
- 进程环境优先于 `bridge.env`，所以 systemd drop-in 里的 `Environment=BRIDGE_DATA_DIR=...` 会覆盖 `bridge.env` 中的同名变量。
- 把数据放到其他位置时，在 `bridge.env` 中设置 `BRIDGE_DATA_DIR` 即可；`BRIDGE_DB` / `BRIDGE_LOG` 会自动跟随。`BRIDGE_TEMP_WORKSPACE_ROOT` 默认跟随 `OPENCODE_DIRECTORY`（`$OPENCODE_DIRECTORY/temp`），需要单独指定时再覆盖。

示例：

```bash
# bridge.env
BRIDGE_DATA_DIR=/var/lib/feishu-bridge
# 或者分别指定
BRIDGE_DB=/var/lib/feishu-bridge/bridge.db
BRIDGE_LOG=/var/lib/feishu-bridge/bridge.log
BRIDGE_TEMP_WORKSPACE_ROOT=/var/lib/feishu-bridge/temp
```

确保运行用户和 OpenCode 都能读写这些目录。

## 从旧版本升级

旧版本使用静态 unit（`feishu-opencode-bridge.service`、`feishu-opencode-bridge-backup.service`、`feishu-opencode-bridge-backup.timer`），新版本改为模板 unit。迁移步骤：

```bash
# 1. 备份
"$HOME/feishu-opencode-bridge/backup.sh"

# 2. 拉取新代码
cd "$HOME/feishu-opencode-bridge"
git pull --ff-only
.venv/bin/pip install -r requirements.txt

# 3. 停用并删除旧 unit
sudo systemctl disable --now feishu-opencode-bridge.service
sudo systemctl disable --now feishu-opencode-bridge-backup.timer
sudo rm -f /etc/systemd/system/feishu-opencode-bridge.service \
           /etc/systemd/system/feishu-opencode-bridge-backup.service \
           /etc/systemd/system/feishu-opencode-bridge-backup.timer

# 4. 安装模板 unit（实例名 = 运行用户）
sudo cp deploy/feishu-opencode-bridge@.service \
        deploy/feishu-opencode-bridge-backup@.service \
        deploy/feishu-opencode-bridge-backup@.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-opencode-bridge@oc
sudo systemctl enable --now feishu-opencode-bridge-backup@oc.timer

# 5. 验证
sudo systemctl status feishu-opencode-bridge@oc
journalctl -u feishu-opencode-bridge@oc -n 50
```

旧版 `bridge.env` 可以继续使用；新变量都有默认值，不需要改。若旧版显式写了 `/home/oc/...` 路径也能正常工作。

升级到支持 `/agent` 的版本后，`db.init()` 会自动给 `chats` / `sessions` 增加 `agent` 列，历史会话无需手动迁移；未设置过 Agent 的会话使用 `OPENCODE_AGENT`（默认 `build`）。`/agent` 依赖 OpenCode V2 的 `/api/agent` 和 `/api/session/{sessionID}/agent` 接口。

如果旧版使用专用 `oc` 用户，迁移后继续用 `@oc` 实例即可，不需要改 unit 文件；如果改用普通用户，把实例名换成该用户名，并确保该用户对安装目录有读写权限。

## 安全

- 只允许白名单 `open_id`，只处理私聊（p2p）；白名单留空表示不限制，建议多人共用时显式配置。
- 多人共用同一个机器人时不是多租户隔离：所有用户共享工作目录、OpenCode 服务和系统用户，详见「[多人共用同一个机器人](#多人共用同一个机器人)」。
- 日志不记录 App Secret / API Key。
- `debug/` 下的脚本从环境变量读取凭据，不硬编码密钥；不要把 `bridge.env`、`service.env` 或真实密钥提交到 Git。
- `bridge.env`、`service.env` 权限 600。
- OpenCode 只监听 `127.0.0.1`，使用 Basic 认证。
- 审批只允许一次，不写入“总是允许”规则。
