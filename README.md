# 飞书 → OpenCode 轻量桥接器

## for human

> 嘿，人类！这是一个由 deepseek 摸鱼时随手搓出来的项目，内部到底怎么实现的？本宝宝也说不清，真的。想安装、想折腾、或者想研究实现方式？很简单：`git clone` 下来，打开你的 agent，把项目地址像飞盘一样丢给它，然后坐等它一边挠头一边干活。Peace & Love，玩得开心！🌴✌️✨

## 架构

- 飞书自建应用 `oc-bot`，通过 WebSocket 长连接接收消息和卡片回调，不需要公网 IP。
- 桥接器：Python 3.13 + `lark-oapi 1.7.5`，由 systemd 常驻。
- OpenCode V2 HTTP API：`127.0.0.1:4096`，HTTP Basic 认证。
- SQLite：保存 chat / session / task / permission / schedule / 事件去重。
- SSE `/api/event` 接收进度和权限事件；每 30 秒轮询权限接口兜底。

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
│   └── test_backup.py
├── backup.sh            # 备份脚本
├── requirements.txt     # Python 依赖
├── bridge.env.example   # 环境变量示例
└── README.md
```

## 部署

以下以 Debian/Ubuntu + systemd、专用用户 `oc`、安装目录 `$HOME/feishu-opencode-bridge` 为例。桥接器使用 systemd 模板 unit，实例名就是运行用户；换用户时只需换实例名，不需要改 unit 文件。数据目录默认与安装目录相同，也可以用 `BRIDGE_DATA_DIR` 单独指定。

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
6. 发布应用。

不需要公网 IP、回调地址或内网穿透。`FEISHU_ALLOWED_OPEN_IDS` 留空表示不限制；建议部署后先发送 `/whoami` 获取自己的 `open_id`，再填入白名单并重启。

### 3. OpenCode 服务

如果你已经有独立的 OpenCode 安装脚本，可以跳过本节；桥接器只要求 `OPENCODE_BASE_URL` 可访问，不依赖 `opencode-serve.service` 这个 unit 名。

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

```bash
BRIDGE_USER=oc
BRIDGE_HOME=/home/$BRIDGE_USER/feishu-opencode-bridge

sudo useradd --create-home --user-group --shell /bin/bash "$BRIDGE_USER"   # 已存在则跳过
sudo -u "$BRIDGE_USER" git clone https://github.com/francisgao1999/feishu-bridge.git "$BRIDGE_HOME"
sudo -u "$BRIDGE_USER" python3.13 -m venv "$BRIDGE_HOME/.venv"
sudo -u "$BRIDGE_USER" "$BRIDGE_HOME/.venv/bin/pip" install -r "$BRIDGE_HOME/requirements.txt"
sudo -u "$BRIDGE_USER" cp "$BRIDGE_HOME/bridge.env.example" "$BRIDGE_HOME/bridge.env"
sudo chmod 600 "$BRIDGE_HOME/bridge.env"
sudo chmod +x "$BRIDGE_HOME/backup.sh"
```

编辑 `bridge.env`，至少填写 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ALLOWED_OPEN_IDS`、`OPENCODE_PASSWORD`。路径默认值：

- `OPENCODE_DIRECTORY=$BRIDGE_HOME/workspace`
- `BRIDGE_DATA_DIR` 默认等于 `BRIDGE_HOME`
- `BRIDGE_DB=$BRIDGE_DATA_DIR/bridge.db`
- `BRIDGE_LOG=$BRIDGE_DATA_DIR/bridge.log`
- `BRIDGE_TEMP_WORKSPACE_ROOT=$BRIDGE_DATA_DIR/temp`

创建默认工作目录和临时目录：

```bash
sudo -u "$BRIDGE_USER" mkdir -p "$BRIDGE_HOME/workspace" "$BRIDGE_HOME/temp"
```

如果 OpenCode 以其他用户运行，确保该用户也能读写 `$BRIDGE_HOME/workspace` 和 `$BRIDGE_HOME/temp`。

GitHub 仓库中的源码位于 `app/`；systemd 以 `$BRIDGE_HOME` 为工作目录，执行 `app/main.py`。

### 5. 桥接器 systemd

`/etc/systemd/system/feishu-opencode-bridge@.service` 见 [`deploy/feishu-opencode-bridge@.service`](deploy/feishu-opencode-bridge@.service)，关键配置：

- `User=%i`：实例名就是运行用户
- `WorkingDirectory=~`：解析为该用户的家目录
- `ExecStart` 通过 `$HOME` 定位安装目录，兼容任意家目录
- 不依赖 `EnvironmentFile`；`app/config.py` 会自己加载 `$HOME/feishu-opencode-bridge/bridge.env`
- 不依赖 `opencode-serve.service`；只要 `OPENCODE_BASE_URL` 可访问即可
- `MemoryMax=384M`、`NoNewPrivileges=true`、`PrivateTmp=true`

安装（以 `oc` 用户为例）：

```bash
sudo cp deploy/feishu-opencode-bridge@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-opencode-bridge@oc
```

换用户时只改实例名，例如 `feishu-opencode-bridge@bridge`。

### 6. 备份定时器

备份 unit 见 [`deploy/feishu-opencode-bridge-backup@.service`](deploy/feishu-opencode-bridge-backup@.service) 和 [`deploy/feishu-opencode-bridge-backup@.timer`](deploy/feishu-opencode-bridge-backup@.timer)。

安装（实例名与桥接器一致）：

```bash
sudo cp deploy/feishu-opencode-bridge-backup@.service deploy/feishu-opencode-bridge-backup@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feishu-opencode-bridge-backup@oc.timer
```

`backup.sh` 会自己定位安装目录并读取 `bridge.env`，因此 `BRIDGE_DB`、`BRIDGE_ENV_FILE` 改了也能正确备份。备份目录默认是 `$BRIDGE_HOME/backups`，可以用 `BRIDGE_BACKUP_DIR` 改到其他磁盘；需要异地备份时，把该目录同步到远端即可。

### 7. 验证

```bash
sudo systemctl status opencode-serve
sudo systemctl status feishu-opencode-bridge@oc
journalctl -u feishu-opencode-bridge@oc -f
```

在飞书私聊机器人发送 `/help`，再发送 `/status`；能收到回复即部署成功。日志开头会打印 `bridge home=... data=...`，可用来确认实际路径。

### 8. 升级

服务器目录已经是 Git 仓库，以运行用户拉取即可：

```bash
cd "$HOME/feishu-opencode-bridge"
./backup.sh
git pull --ff-only
.venv/bin/pip install -r requirements.txt
sudo systemctl restart feishu-opencode-bridge@oc
```

如果服务器直连 GitHub 较慢，先加载 OpenCode 的代理环境再拉取：

```bash
set -a
source "$HOME/.config/opencode/service.env"
set +a
export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY"
git pull --ff-only
```

数据库结构由 `db.init()` 在启动时自动创建/迁移；升级前仍建议先备份。

## 测试

```bash
python3 -m unittest discover -s tests -t . -v
python3 -m compileall -q app debug tests
bash -n backup.sh
```

测试覆盖 `app/config.py` 的默认路径、变量覆盖和 `bridge.env` 加载，以及 `backup.sh` 的默认/自定义路径、缺失数据库和备份权限。

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

常用命令（以 `oc` 用户为例）：

```bash
sudo systemctl status opencode-serve
sudo systemctl status feishu-opencode-bridge@oc
sudo systemctl restart feishu-opencode-bridge@oc
journalctl -u feishu-opencode-bridge@oc -f
tail -f "$HOME/feishu-opencode-bridge/bridge.log"
```

## 飞书命令

- `/help` 或 `/start`：帮助
- `/new [任务]`：创建并切换到新会话；带任务时立即在新会话执行
- `/temp [任务]`（或 `/t`、`/临时`）：创建并切换到临时会话；带任务时立即执行。临时会话使用独立空目录，空闲 30 分钟后自动清理
- `/temp end`（或 `/temp 结束`、`/temp stop`）：结束当前临时会话，删除 OpenCode 会话和临时工作目录，并清空当前会话
- `/temp list`（或 `/temp 列表`）：查看临时会话、剩余时间和临时目录
- `/sessions`（或 `/list`）：列出全部会话、运行状态、待审批数量；顶部固定显示当前会话，列表中用 `[当前]` 标记
- `/switch <序号|session_id|标题>`（或 `/use`）：切换当前会话；同时显示目标会话的模型、上次任务的状态、完成时间和结果摘要
- `/last [序号|session_id]`（或 `/result`）：重发该会话最后一次任务结果，默认当前会话
- `/delete <序号|session_id|标题>`（或 `/remove`、`/删除`）：删除一个空闲会话（当前会话不能删除）
- `/clean`（或 `/清理`）：列出全部空闲会话；`/clean confirm` 确认清理（保留当前会话；confirm 也可写 `确认`/`yes`/`y`）
- `/schedule`（或 `/定时`）：定时任务 / 单次提醒，详见下方「定时任务」
- `/status`：查看当前会话、全部会话、当前任务、待审批、当前模型
- `/model`：回复模型选择卡片，点击按钮切换
- `/model <序号>`：按 `/model` 列表中的序号切换
- `/model <供应商>/<模型>`：直接指定切换，例如 `/model systalyze/deepseek-ai/DeepSeek-V4-Pro-0813`
- `/stop [序号|session_id]`：中止任务，默认当前会话；并清理该会话的待审批
- `/whoami`：显示 open_id / chat_id / session_id / 会话标题 / 当前模型
- 直接发送文本：在当前会话派任务
- 当前没有会话时（例如刚执行 `/temp end`），直接发送文本会自动创建普通会话，标题取消息前 40 个字符

## 多会话并行

- 一个飞书聊天可以绑定多个 OpenCode 会话，`chats.session_id` 只表示“当前会话”。
- 会话 A 正在执行任务时，发送 `/new B任务` 会创建会话 B、切换到 B，并立即在 B 中执行 `B任务`；A 的任务继续运行。
- 发送 `/sessions` 查看全部会话，发送 `/switch 1` 或 `/switch <session_id>` 切回 A。
- `/switch` 会显示目标会话上次任务的状态、完成时间和结果摘要；需要完整结果时发送 `/last`（或 `/last <序号|session_id>`）重发结果卡。
- 每个会话同一时间只允许一个任务；不同会话可以并行执行。
- 进度卡、审批卡、结果卡都按 `session_id` 路由，不会串到别的会话。
- 模型按会话保存；`/model` 只修改当前会话，`/new` 创建的新会话继承当前会话的模型。
- 历史会话会自动迁移到 `sessions` 表，升级后可以直接 `/sessions` 看到并切换。

## 临时会话

- `/temp [任务]` 创建临时会话并切换过去；带任务时立即执行，之后可以继续多轮对话。
- 临时会话使用独立空目录，默认位于 `$BRIDGE_DATA_DIR/temp`（默认 `$BRIDGE_HOME/temp`），创建 OpenCode 会话时作为工作目录传入。
- 临时会话默认空闲 30 分钟后自动清理；任务执行中、有待审批时不会清理。
- 每次发送消息、提交任务、任务完成、`/switch` 进入临时会话都会重新计时。
- 到期时如果临时会话是当前会话，会先清空当前会话，再删除 OpenCode 会话、临时目录和 SQLite 记录；之后发送任意消息会自动创建普通会话。
- `/temp end` 立即结束当前临时会话；`/temp list` 查看临时会话、剩余时间和目录。
- `/sessions` 中临时会话带 `[临时]` 标记；`/switch` 进入临时会话会重置计时。
- `/new` 创建普通会话并切换后，原临时会话继续倒计时，到期后静默删除。
- `/delete` 非当前临时会话会同时删除目录；当前临时会话请用 `/temp end`。
- `/clean` 会清理空闲临时会话及其目录。
- 临时目录是工作目录，不是沙箱；agent 仍可能读写目录外文件。空目录意味着 agent 默认看不到项目文件，需要时请使用绝对路径。
- 删除临时目录不会杀掉 agent 启动的后台进程；需要更强隔离时请使用容器/VM。
- 如果 OpenCode 服务启用了 `ProtectHome`、`ReadWritePaths` 等 systemd 限制，需要确保它能读写 `BRIDGE_TEMP_WORKSPACE_ROOT`。

## 会话清理

- `/sessions` 太长时，可以用 `/delete <序号|session_id|标题>` 删除单个空闲会话。
- `/clean` 会列出所有可删除的空闲会话（不含当前会话、不含正在执行任务的会话），再发送 `/clean confirm` 才会真正删除。
- 删除会话会同时调用 OpenCode `DELETE /api/session/{sessionID}`，删除该会话及其子会话；SQLite 中的会话记录、任务记录和审批记录也会移除，删除后不可恢复。
- 当前会话不能删除；如需删除，先 `/switch` 到其他会话。
- 有任务正在执行的会话不能删除；先等待完成或 `/stop`。
- 删除会话前会自动拒绝该会话的待审批请求。
- 如果定时任务绑定的会话被删除，定时任务会在下次执行时自动创建新会话。

## 任务可靠性

- 上游模型报错时，如果 OpenCode 上报 `session.execution.failed`，机器人会更新进度卡并发送「任务失败: <错误信息>」。
- 如果 OpenCode 上报 `session.retry.scheduled`（自动重试），进度卡会显示「上游报错，重试中（第 N 次）」，并发送一次重试提醒。
- 如果上报 `session.execution.interrupted`，任务会标记为已中止并通知。
- 如果只上报 `session.error`，桥接器也会把当前任务标记失败并通知。
- 后台每 60 秒对账一次：如果任务仍在执行但 OpenCode 已空闲，会读取会话的 `idle` 消息补发结果；SSE 断线期间错过的完成/失败事件可以自动恢复。
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

- 子命令也支持中文：列表/暂停/恢复/删除/执行/单次/一次性/添加；周期可写每天/每日、工作日、每周一。

- 每个周期任务使用独立会话，标题为 `定时: <任务摘要>`；创建和执行都不会切换当前会话。
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

## 审批策略

- 会话级规则：普通 `shell` 命令默认 `allow`；危险命令（`rm`、`sudo`、`dd`、`mkfs`、`systemctl` 启停、包管理器安装/卸载、`git push`/`reset --hard`、`docker rm` 等）`ask`。
- `edit` 仍然全部 `ask`；`read` 匹配 `*.env`、`*.env.*` 也 `ask`，`*.env.example` 例外允许。
- 危险命令列表在 `app/permissions.py` 的 `DANGEROUS_SHELL_PATTERNS`，可自行增删。
- 这是尽力而为的黑名单，不是安全边界；通过解释器或脚本（如 `python -c`、`bash script.sh`）仍可能绕过。需要更强隔离时，建议使用容器/VM 或改成白名单。
- 审批卡片只有「允许一次」和「拒绝」，没有“总是批准”。
- 10 分钟未处理发文字提醒，30 分钟自动拒绝并中止任务。
- 点击后卡片立即更新；重复回调幂等。
- 任务完成后，原进度卡只收尾为「完成」，结果统一在聊天底部另发一张新结果卡。飞书更新卡片不会触发消息通知，只更新原卡容易让用户错过结果；审批卡也可能把结果压在更上方。
- 桥接器重启时，未完成任务会标记失败、更新卡片并发送通知。

## 配置

- `$BRIDGE_HOME/bridge.env`（600，`BRIDGE_HOME` 默认是仓库根目录）
  - 飞书 App ID / Secret
  - 白名单 open_id
  - OpenCode 地址、用户名、密码
  - 工作目录、agent、模型
  - 进度间隔、审批提醒/超时、任务对账/超时、结果截断长度
  - 临时会话空闲时长 `BRIDGE_TEMP_SESSION_TTL_SECONDS`（默认 1800 秒）
  - 临时工作目录根路径 `BRIDGE_TEMP_WORKSPACE_ROOT`（默认 `$BRIDGE_DATA_DIR/temp`）
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
| `BRIDGE_HOME` | 仓库根目录 | 安装目录；`bridge.env` 默认在这里 |
| `BRIDGE_DATA_DIR` | `$BRIDGE_HOME` | 数据目录；相对路径按 `BRIDGE_HOME` 解析 |
| `BRIDGE_DB` | `$BRIDGE_DATA_DIR/bridge.db` | SQLite 数据库 |
| `BRIDGE_LOG` | `$BRIDGE_DATA_DIR/bridge.log` | 日志文件 |
| `BRIDGE_TEMP_WORKSPACE_ROOT` | `$BRIDGE_DATA_DIR/temp` | 临时会话目录 |
| `OPENCODE_DIRECTORY` | `$BRIDGE_HOME/workspace` | 普通会话工作目录 |
| `BRIDGE_BACKUP_DIR` | `$BRIDGE_HOME/backups` | 备份输出目录（仅 `backup.sh`） |

`BRIDGE_ENV_FILE` 只在启动前通过环境变量指定，不能写在 `bridge.env` 里；默认是 `$BRIDGE_HOME/bridge.env`。

修改后重启：

```bash
sudo systemctl restart feishu-opencode-bridge@oc
```

## 备份与恢复

备份目录默认是 `$BRIDGE_HOME/backups/`，保留 7 天；可以用 `BRIDGE_BACKUP_DIR` 改到其他磁盘。备份内容包含 SQLite 一致性快照、`bridge.env` 和 `app/` 源码。

手动备份：

```bash
"$HOME/feishu-opencode-bridge/backup.sh"
```

恢复（先停服务，并删除 WAL/SHM 残留；如果自定义了 `BRIDGE_DATA_DIR` / `BRIDGE_DB`，把下面的数据库路径换成实际路径）：

```bash
sudo systemctl stop feishu-opencode-bridge@oc
rm -rf /tmp/bridge-restore
mkdir -p /tmp/bridge-restore
tar -xzf "$HOME/feishu-opencode-bridge/backups/bridge-YYYYmmdd-HHMMSS.tar.gz" \
  -C /tmp/bridge-restore
DB="$HOME/feishu-opencode-bridge/bridge.db"
rm -f "$DB-wal" "$DB-shm"
cp /tmp/bridge-restore/bridge.db "$DB"
cp /tmp/bridge-restore/bridge.env "$HOME/feishu-opencode-bridge/bridge.env"
chmod 600 "$HOME/feishu-opencode-bridge/bridge.env"
sudo systemctl start feishu-opencode-bridge@oc
```

注意：恢复数据库前先停服务并删除 `bridge.db-wal` / `bridge.db-shm`，否则 SQLite 可能继续使用旧的 WAL；`bridge.env` 含密钥，权限保持 600。需要异地备份时，把 `$BRIDGE_BACKUP_DIR` 同步到远端存储即可。

## 故障排查

- **机器人不回消息**
  - 检查 `systemctl status feishu-opencode-bridge@oc`
  - 飞书应用是否已发布
  - 是否启用“使用长连接接收事件”
  - 是否订阅 `im.message.receive_v1`
  - 是否启用“使用长连接接收回调”并订阅 `card.action.trigger`
- **卡片按钮无反应**
  - `lark-oapi 1.7.5` 会丢弃 `CARD` 帧，必须保留 `app/ws_patch.py` 的 monkey-patch。
- **审批卡不更新**
  - 必须在卡片回调响应里返回 `card: {"type": "raw", "data": <card>}`。
  - 回调处理期间单独调用 `PATCH /im/v1/messages/{id}` 会被忽略。
- **OpenCode 401**
  - 检查 `bridge.env` 的 `OPENCODE_PASSWORD`、`service.env` 的 `OPENCODE_SERVER_PASSWORD`、`service.json` 的 `password` 三者是否一致。
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

## 自定义用户 / 路径

桥接器使用 systemd 模板 unit，实例名就是运行用户：

```bash
sudo systemctl enable --now feishu-opencode-bridge@bridge
sudo systemctl enable --now feishu-opencode-bridge-backup@bridge.timer
```

unit 使用 `WorkingDirectory=~` 和 `$HOME` 定位安装目录，因此安装目录固定为 `$HOME/feishu-opencode-bridge`，不依赖 `/home/<用户>` 这种固定路径。如果需要把数据放到其他位置，在 `bridge.env` 中设置：

```bash
BRIDGE_DATA_DIR=/var/lib/feishu-bridge
# 或者分别指定
BRIDGE_DB=/var/lib/feishu-bridge/bridge.db
BRIDGE_LOG=/var/lib/feishu-bridge/bridge.log
BRIDGE_TEMP_WORKSPACE_ROOT=/var/lib/feishu-bridge/temp
```

相对路径按 `BRIDGE_DATA_DIR` 解析；`OPENCODE_DIRECTORY` 的相对路径按 `BRIDGE_HOME` 解析。确保运行用户和 OpenCode 都能读写这些目录。

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

## 安全

- 只允许白名单 `open_id`，只处理私聊（p2p）。
- 日志不记录 App Secret / API Key。
- `debug/` 下的脚本从环境变量读取凭据，不硬编码密钥；不要把 `bridge.env`、`service.env` 或真实密钥提交到 Git。
- `bridge.env`、`service.env` 权限 600。
- OpenCode 只监听 `127.0.0.1`，使用 Basic 认证。
- 审批只允许一次，不写入“总是允许”规则。
