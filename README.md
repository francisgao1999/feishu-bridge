# 飞书 → OpenCode 桥接器（多用户 LXC 版）

## for human

> 嘿，人类！这是 deepseek 摸鱼时随手搓出来的项目。这个版本把「一个桥接器服务所有人」改成了「每个用户一个 PVE LXC」，用户之间互相隔离。想装、想折腾、想研究实现？`git clone` 下来，把项目丢给你的 agent，让它帮你部署。玩得开心！🌴✌️✨

## 架构

```
飞书（长连接）
   │  im.message.receive_v1 / card.action.trigger
   ▼
docker LXC（bridge 容器）
   │  users.json 按 open_id 路由
   ├──► 用户 LXC 901  http://192.168.110.201:4096  /workspace
   ├──► 用户 LXC 902  http://192.168.110.202:4096  /workspace
   └──► 用户 LXC 903  http://192.168.110.203:4096  /workspace
```

- **每个用户一个 PVE LXC**（非特权容器，宿主映射 100000），内含 `/workspace` 和 `opencode serve --hostname 0.0.0.0 --port 4096`。
- **bridge 跑在 docker LXC 的容器里**，通过 LAN IP 访问各用户 LXC 的 4096 端口，按 `open_id` 路由。
- 用户 LXC 内不套 Docker；用户以 root 运行，可自行 `apt install` 数据库等软件。
- 不开放 SSH / code-server / ttyd；用户只通过飞书 + OpenCode 使用。
- 飞书长连接不需要公网 IP；bridge 不需要对外暴露端口。

### 信任模型

用户是**可信的内部人员**，方案主要防误操作，不追求恶意租户级隔离。同一个 PVE 上的 LXC 共享内核，隔离强度弱于 VM；需要更强隔离时，把某个用户单独放到 VM 上即可，bridge 不用改。

### 关键事实

- opencode 版本不固定，装官方安装脚本给的最新 v2；`fs/write` 是 experimental 接口，升级前必须先跑冒烟测试。
- 文件投递用 `POST /api/experimental/fs/write`（二进制安全，sha256 实测一致）。
- 出站用 `/workspace/outbox/` 目录约定 + `fs/list` + `fs/read`。
- 飞书入站资源上限 **100MB**（错误码 `234037`）；出站图片 ≤10MB、文件 ≤30MB。
- bridge 容器 `mem_limit: 768m`；上传并发 `Semaphore(2)`；大文件落盘，不整体读进内存。

## 目录结构

```
.
├── app/                       # 桥接器源码
│   ├── main.py                # 进程入口、事件循环
│   ├── bridge.py              # 飞书消息 ↔ OpenCode 任务
│   ├── feishu.py              # 飞书 API / 长连接 / 文件上传下载
│   ├── forms.py               # 提问表单卡
│   ├── opencode.py            # OpenCode V2 API 客户端 + 多用户路由
│   ├── users.py               # users.json 解析与热加载
│   ├── cards.py               # 飞书卡片
│   ├── permissions.py         # 审批规则
│   ├── slash_commands.py      # /help /status /new 等命令注册
│   ├── db.py                  # SQLite
│   ├── config.py              # 环境变量
│   └── ws_patch.py            # 飞书 WebSocket 补丁
├── config/
│   └── users.json.example     # 用户映射模板
├── deploy/
│   ├── lxc/                   # 用户 LXC 内的 unit / 配置 / 脚本
│   │   ├── opencode-serve.service
│   │   ├── opencode.jsonc
│   │   ├── workspace-AGENTS.md
│   │   ├── add-user.sh
│   │   └── remove-user.sh
│   └── feishu-opencode-bridge@.service   # legacy，已由 compose 取代
├── docs/
│   ├── plan-lxc-sandbox.md    # 设计与实施计划
│   ├── lxc-template-manual.md # 模板 LXC 手工创建教程
│   └── lxc-scripts.md         # add-user / remove-user 用法
├── scripts/
│   └── smoke-test.sh          # 冒烟测试
├── tests/
├── Dockerfile
├── docker-compose.yml
├── bridge.env.example
├── backup.sh
└── README.md
```

## 部署

### 1. 前置条件

- PVE 宿主机（能创建 LXC，有 `pct`）
- 一个运行 Docker 的 LXC（下文称 docker LXC，VMID 101，IP `192.168.110.11`）
- 每个用户一个固定 IP（由管理员显式分配）
- 飞书自建应用（一个机器人服务所有人）
- 一台能访问用户 LXC 4096 端口的机器用来跑冒烟测试

### 2. 飞书应用

1. 在开放平台创建企业自建应用，开启「机器人」能力。
2. 事件订阅选择「使用长连接接收事件」，订阅 `im.message.receive_v1`。
3. 卡片回调选择「使用长连接接收回调」，订阅 `card.action.trigger`。
4. 权限：
   - 接收单聊消息、以应用身份发消息、更新卡片
   - **获取与发送单聊、群组消息**（下载用户发来的文件/图片）
   - **上传图片或文件资源**（把 outbox 产物发回用户）
   - `application:app_slash_command:read/write`（命令面板，可选）
5. 发布应用，并设置可用范围（谁能搜到机器人）。

> 缺「获取与发送单聊、群组消息」时，下载用户文件会 403；缺「上传图片或文件资源」时，出站文件会失败。

### 3. 模板 LXC

按 [`docs/lxc-template-manual.md`](docs/lxc-template-manual.md) **手工**创建模板 LXC（VMID 900）：在 PVE 网页控制台用「创建 CT」向导建容器，再在容器控制台里装软件，最后转成模板。模板只创建一次，之后所有用户从它克隆。

模板包含：Debian 最小系统 + `git`/`python3`/`curl`/`jq` 等约 40MB 工具、Node.js 22（MCP 要求）、Playwright MCP + Chromium（供 agent 截图）、ast-grep MCP（结构化代码搜索）、opencode v2（不固定小版本）、`LANG=C.UTF-8`（避免控制台中文乱码）、`/workspace`、`/workspace/uploads`、`/workspace/outbox`、`opencode-serve.service`，以及**出站代理**配置：`/etc/opencode/proxy.env`（默认 `192.168.110.11:10808`，HTTP 与 SOCKS5 同端口）、`/etc/opencode/service.env`（模型与 MCP 密钥）、apt/git/pip/npm 的系统级代理、`/etc/profile.d/99-proxy.sh`。代理和密钥随模板克隆，每个用户 LXC 自动继承。

模板的 opencode 配置内置 5 个 MCP 服务器：`playwright`、`ast_grep`、`context7`、`exa`、`grep_app`。其中 `context7`/`exa` 的密钥从 `/etc/opencode/service.env` 读取，不要写进 `opencode.jsonc`。

nftables 规则（仅放行 docker LXC 和 PVE 宿主访问 4096）已写好，`add-user.sh` 的健康检查和冒烟测试从 PVE 宿主发起，所以**转模板时保持启用**；确实不想保留端口限制时，按教程 §5.1 处理。

工作区 `/workspace/AGENTS.md` 也包含「网络与代理」和「MCP 工具」两节，告诉 opencode 出站走代理、各工具如何显式指定、哪些配置不要改，以及模板内置了哪些 MCP。

模板默认**不安装**数据库、build-essential；用户需要时自行 `apt install`。Node.js 22/Playwright/Chromium/ast-grep MCP 已预装，agent 截图和代码搜索不用再临时安装。

### 4. bridge 容器

在 docker LXC 内：

```bash
cd /root/container/feishu-bridge
git clone https://github.com/francisgao1999/feishu-bridge.git .
cp bridge.env.example bridge.env
chmod 600 bridge.env
vi bridge.env          # 填 FEISHU_APP_ID / SECRET / BRIDGE_ADMIN_OPEN_IDS
mkdir -p config data
cp config/users.json.example config/users.json
chmod 600 config/users.json
# 容器内 bridge 以 UID 1000 运行；宿主机 oc 一般也是 1000。
# 如果 oc 不是 1000，执行：sudo chown -R 1000:1000 config data
docker compose build
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` 的关键点：

- `./config:/app/config:ro` —— **必须挂目录**，不能单文件挂载；`pct push` 会换 inode，单文件挂载会让热加载失效。
- `./data:/app/data` —— SQLite、日志、上传临时文件。
- `mem_limit: 768m`。

### 5. 开通用户

在 PVE 宿主（不是 docker LXC）上：

```bash
cd /path/to/feishu-bridge
./deploy/lxc/add-user.sh alice ou_xxxxxxxx 192.168.110.201
```

脚本会克隆模板、设置 IP、生成密码、启动 opencode、写入 `users.json`、跑冒烟。用户 LXC 默认从 VMID `901` 开始编号，避免和基础设施 LXC（`101`/`102`）混在一起；可用 `--start-id` 调整起始号。`users.json` 通过目录挂载热加载，**不需要重启 bridge**。

完整参数与排错见 [`docs/lxc-scripts.md`](docs/lxc-scripts.md)。

### 6. 验证

在飞书私聊机器人：

1. `/help` —— 能看到帮助
2. `/status` —— 显示当前会话与工作区
3. `/smoke` —— 检查 bridge → 用户 LXC 的连接与文件投递
4. 发送一张图片 —— 应回复「已收到图片：/workspace/uploads/...」
5. 发送一条任务 —— 例如「在 /workspace/outbox 写一个 hello.txt，内容为 hi」

服务器侧也可以跑：

```bash
scripts/smoke-test.sh --users config/users.json
```

## 文件与图片

### 入站（用户 → agent）

- 支持飞书私聊里的**文件**和**图片**消息。
- bridge 下载到临时目录，再用 `fs/write` 写进用户 LXC 的 `/workspace/uploads/<时间戳>-<文件名>`。
- 下一条文本消息会自动带上文件路径，例如：

```
用户上传了以下文件：
- /workspace/uploads/20261120-183000-data.sql（12.3 MB，sha256 abcd1234…）

把这份 SQL 导入 PostgreSQL
```

- 上限 **100MB**（飞书硬限制）；超过会提示改用链接或分片。
- 图片按文件头判断扩展名（png/jpg/gif/webp/bmp）。

### 出站（agent → 用户）

- agent 把要回传的文件写到 `/workspace/outbox/`。
- 任务结束时 bridge 扫描 outbox，逐个下载并发回飞书：
  - 图片（≤10MB）→ `im/v1/images`
  - 其他文件（≤30MB）→ `im/v1/files`
- 用 `(open_id, sha256, size)` 去重，同一个文件不会重复发送。
- 超过 30MB 的文件不会发送，bridge 会在聊天里提示文件名和大小。

工作区 `/workspace/AGENTS.md` 里写明了这个约定，agent 会自己遵守。

## 新用户开通与生命周期

| 场景 | 操作 |
| --- | --- |
| 新增 | `./deploy/lxc/add-user.sh <slug> <open_id> <ip>` |
| 暂停 | `pct stop <vmid>`，保留 `users.json` 或临时移除 |
| 离职/删除 | `./deploy/lxc/remove-user.sh <slug>`（先 vzdump 归档） |
| 换 IP | 改 LXC `net0`，同步改 `users.json`，自动热加载 |
| 回滚 | `pct rollback <vmid> <snapshot>` |
| 需要更强隔离 | 把该用户迁到独立 VM，`users.json` 改地址即可 |

**未开通用户**：bridge 会回复「你还没有开通使用权限」，并通知 `BRIDGE_ADMIN_OPEN_IDS` 里的管理员；同一用户 1 小时内只通知一次。**不会自动创建 LXC。**

## 冒烟测试（强制）

`scripts/smoke-test.sh` 验证：

1. `/global/health` 可访问
2. `/api/model` 无认证 401、带认证 200（鉴权生效）
3. `fs/write` 写入 2MB 二进制
4. `fs/read` 读回，sha256 一致
5. `/api/event` SSE 可连接

**必须跑冒烟的场景**：

- 变更 opencode 版本
- 重建模板或克隆新用户
- 升级 bridge 镜像且涉及 `app/opencode.py` 或文件传输
- 部署后首次上线

任何一项失败都不得发布。详见 `AGENTS.md`。

## 飞书命令

- `/help` 或 `/start`：帮助
- `/new [标题]`：创建会话
- `/sessions` 或 `/list`：会话列表
- `/switch <编号|session_id>` 或 `/use`：切换当前会话
- `/last [编号]` 或 `/result`：查看最后一次结果
- `/delete <编号>` 或 `/remove`、`/删除`：删除空闲会话
- `/clean [confirm]` 或 `/清理`：清理空闲会话
- `/status`：状态
- `/stop`：中止任务
- `/model`：模型选择卡片
- `/agent`：Agent 选择卡片
- `/schedule ...` 或 `/定时 ...`：定时任务
- `/whoami`：显示 open_id / chat_id
- `/smoke`：冒烟测试（检查 bridge → 用户 LXC 全链路）

> 旧的 `/temp` 临时会话命令已移除。

### 命令面板

bridge 启动时会把顶层命令同步到飞书输入框 `/` 面板：缺失的创建、描述变化的更新、已从命令目录移除的删除。需要应用开通 `application:app_slash_command:read/write`；同步失败只记日志，不影响机器人收发消息。

## 多会话并行

- 一个飞书聊天可以绑定多个 OpenCode 会话，`chats.session_id` 是「当前会话」。
- 会话按 `chat_id` 隔离：`/sessions`、`/switch`、`/delete` 只能操作自己聊天里的会话。
- 每个会话同一时间只允许一个任务；不同会话可以并行。
- 模型和 Agent 按会话保存，`/new` 继承当前会话的选择。

## 用户隔离

每个用户有独立的 LXC、独立的 `/workspace`、独立的 OpenCode 进程和独立的 API Key（在模板 `opencode.jsonc` 里配置，或由用户自己改）。

| 隔离项 | 说明 |
| --- | --- |
| 文件系统 | 每用户独立 LXC，互不可见 |
| 进程 | 每用户独立 `opencode serve` |
| 网络 | 每用户独立 IP，nftables 只放行 docker LXC |
| 资源 | 2048M 内存 / 1024M swap / 24G 磁盘（可调） |
| 数据库 | 模板不预装，用户按需 `apt install` |

**共享**：同一个飞书应用、同一个 bridge 进程、同一个 SQLite、同一个 PVE 宿主内核。

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

- 单次提醒到点直接发提醒卡片，不创建 OpenCode 会话。
- 单次任务到点使用独立会话执行，结果作为新卡片发到本聊天。

### 周期任务

```
/schedule daily 10:00 整理昨日 linux.do 福利羊毛板块的帖子摘要
/schedule weekdays 09:30 汇总今天的告警
/schedule weekly mon 10:00 整理上周周报
```

- 周期任务每次执行使用独立会话，标题为 `定时: <任务摘要>`。
- 任务执行完成后自动标记为「已完成」，下一次到点自动重新执行。

### 管理

```
/schedule list             查看定时任务
/schedule pause <id>       暂停
/schedule resume <id>      恢复
/schedule remove <id>      删除
/schedule run <id>         立即执行一次
```

## 模型切换

- 模型列表来自 `/api/model`，包含 OpenCode 内置免费模型和 `opencode.jsonc` 中配置的供应商。
- 发送 `/model` 回复卡片，按供应商分组列出模型，当前模型带 ✓。
- 点击按钮立即切换卡片所属会话的模型，并把选择持久化到 SQLite `sessions.model_provider` / `sessions.model_id`。
- 后续 `/new` 创建的新会话继承当前会话的模型；未选择时回退 `bridge.env` 中的默认模型。
- 切换只影响后续请求，不会重跑已完成的任务。

## Agent 切换

- Agent 列表来自 `GET /api/agent`，自动包含 `build`、`plan` 以及 `opencode.jsonc` 中自定义的 `primary` / `all` agent；`subagent` 和 `hidden: true` 的 agent 不会列出。
- 发送 `/agent` 回复选择卡片，当前 Agent 带 ✓；也可以 `/agent <序号>`、`/agent <agent_id>` 或 `/agent <名称>` 直接切换。
- 切换会调用 `POST /api/session/{sessionID}/agent`，并把选择持久化到 SQLite `sessions.agent`。
- 只读 Agent（默认 `plan`）不注入会话级 `edit * ask`，让 Agent 自己的 `edit * deny` 生效。
- 保存的 Agent 被删除后，新会话会自动回退到 `OPENCODE_AGENT` 或列表中的第一个 Agent。

## 审批策略

- 会话级规则：普通 `shell` 命令默认 `allow`；危险命令（`rm`、`dd`、`mkfs`、`fdisk`、`shutdown`、`git push --force`、`docker rm` 等）`ask`。
- 普通 Agent 的 `edit` 仍然全部 `ask`；只读 Agent（`plan`）不注入 `edit * ask`。
- **沙箱模式**（`BRIDGE_SANDBOX_MODE=container`）下，`sudo`、`service`、`systemctl` 从危险列表剔除，因为用户 LXC 内本来就需要这些命令安装/启动数据库。
- 审批卡片只有「允许一次」和「拒绝」，没有“总是批准”。
- 10 分钟未处理发文字提醒，30 分钟自动拒绝并中止任务。
- 审批卡片只发到任务发起人的私聊；回调会校验点击者是否在白名单内。

## 提问（form）交互

OpenCode 的 `question` 工具会创建 form，等待用户输入。bridge 把它转换成飞书卡片 2.0 表单，直接在聊天里填写和提交。

- 收到 `form.created` 后发送「OpenCode 需要你的输入」表单卡；卡片包含任务摘要、问题标题/描述和对应输入组件。
- 有选项的字段使用下拉框；`multiple: true` 使用多选；`custom: true` 额外提供「其他答案」输入框。
- 提交后调用 `POST /api/session/{sessionID}/form/{formID}/reply`，卡片更新为「提问已提交」并显示答案。
- 点击「取消提问」会调用 `DELETE /api/session/{sessionID}/form/{formID}`，取消 form 并中断任务。
- 默认 5 分钟未处理发一次文字提醒，30 分钟未处理自动取消 form 并中断任务。
- 表单卡只发到任务发起人的私聊，回调按白名单校验点击者。

## 配置

### bridge.env

`$BRIDGE_HOME/bridge.env`（600，`BRIDGE_HOME` 默认是仓库根目录）。容器内 `BRIDGE_HOME=/app`。

必填：

- `FEISHU_APP_ID` / `FEISHU_APP_SECRET`
- `OPENCODE_PASSWORD`（单用户兼容模式；多用户模式由 `users.json` 提供）

多用户模式：

- `BRIDGE_USERS_FILE`：用户映射文件，默认 `config/users.json`
- `BRIDGE_ADMIN_OPEN_IDS`：未开通用户通知的管理员 open_id（逗号分隔）
- `BRIDGE_UNKNOWN_USER_COOLDOWN_SECONDS`：同一用户通知冷却，默认 3600
- `BRIDGE_SANDBOX_MODE=container`：放宽 `sudo`/`service`/`systemctl` 审批
- `BRIDGE_UPLOAD_TMP_DIR`：上传临时目录，默认 `data/uploads`
- `BRIDGE_MAX_UPLOAD_BYTES`：入站上限，默认 104857600（100MB）
- `BRIDGE_UPLOAD_SUBDIR`：入站子目录，默认 `uploads`
- `BRIDGE_OUTBOX_SUBDIR`：出站子目录，默认 `outbox`
- `BRIDGE_OUTBOX_SCAN_SECONDS`：outbox 扫描间隔，默认 60
- `BRIDGE_MAX_OUTBOX_IMAGE_BYTES`：出站图片上限，默认 10485760（10MB）
- `BRIDGE_MAX_OUTBOX_FILE_BYTES`：出站文件上限，默认 31457280（30MB）

运行参数：

- `BRIDGE_TASK_TIMEOUT_SECONDS`：任务超时，默认 7200（2 小时）
- `BRIDGE_TASK_RECONCILE_SECONDS` / `BRIDGE_TASK_RECONCILE_GRACE_SECONDS`：对账间隔与宽限
- `BRIDGE_PROGRESS_MIN_INTERVAL`：进度卡片最小更新间隔
- `BRIDGE_PERMISSION_REMIND_SECONDS` / `BRIDGE_PERMISSION_EXPIRE_SECONDS`：审批提醒/超时
- `BRIDGE_FORM_REMIND_SECONDS` / `BRIDGE_FORM_EXPIRE_SECONDS`：提问提醒/超时
- `BRIDGE_MAX_FINAL_CHARS` / `BRIDGE_MAX_TEXT_CHUNK`：结果截断与消息分片

路径：

- `BRIDGE_HOME`：安装目录，默认仓库根目录
- `BRIDGE_DATA_DIR`：数据目录，默认 `$BRIDGE_HOME`
- `BRIDGE_DB`：SQLite 路径，默认 `$BRIDGE_DATA_DIR/bridge.db`
- `BRIDGE_LOG`：日志路径，默认 `$BRIDGE_DATA_DIR/bridge.log`
- `OPENCODE_DIRECTORY`：单用户兼容模式的工作目录，默认 `$BRIDGE_HOME/../workspace`
- `BRIDGE_BACKUP_DIR`：备份目录，默认 `$BRIDGE_HOME/backups`

`bridge.env.example` 是完整模板。

### config/users.json

```json
[
  {
    "open_id": "ou_xxxxxxxxxxxxxxxx",
    "slug": "alice",
    "url": "http://192.168.110.201:4096",
    "password": "随机密码",
    "workspace": "/workspace"
  }
]
```

- `workspace` 可选，默认 `/workspace`。
- 文件按 mtime 热加载（约 5 秒轮询），加用户不需要重启 bridge。
- 修改后先 `python3 -m json.tool config/users.json` 校验，再保存。

### 用户 LXC 配置

- `deploy/lxc/opencode-serve.service`：systemd 单元，监听 `0.0.0.0:4096`，`EnvironmentFile=/etc/opencode/serve.env`
- `deploy/lxc/opencode.jsonc`：模型、agent、MCP 配置
- `/etc/opencode/serve.env`：`OPENCODE_PASSWORD=...`（600）
- `deploy/lxc/workspace-AGENTS.md`：工作区约定，放到 `/workspace/AGENTS.md`

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -t . -v
.venv/bin/python -m compileall -q app debug tests
bash -n backup.sh
git diff --check
```

## 备份与恢复

### 备份

`backup.sh` 打包 SQLite 一致性快照、`bridge.env`、`config/users.json` 和 `app/` 源码，输出到 `backups/bridge-<时间戳>.tar.gz`（600），保留 7 天。

```bash
cd /root/container/feishu-bridge
./backup.sh
```

生产环境建议用 systemd timer 或 cron 每日执行。

### 恢复

```bash
docker compose stop bridge
tar -xzf backups/bridge-YYYYmmdd-HHMMSS.tar.gz -C /tmp/bridge-restore
cp /tmp/bridge-restore/bridge.db data/bridge.db
cp /tmp/bridge-restore/bridge.env bridge.env
cp /tmp/bridge-restore/config/users.json config/users.json
docker compose start bridge
```

恢复前先删除 `data/bridge.db-wal` / `data/bridge.db-shm`，避免旧 WAL 干扰。

### 用户 LXC 备份

```bash
# 在 PVE 宿主上
vzdump 101 --mode snapshot --compress zstd --storage backup
```

## 故障排查

### 机器人不回消息

- `docker compose ps` 确认 bridge 容器 `Up`
- `docker compose logs --tail 100 bridge`
- 飞书应用是否已发布、是否开启长连接
- 是否订阅了 `im.message.receive_v1` 和 `card.action.trigger`

### 用户发消息被拒绝

- 日志出现 `rejected message from open_id=...`：不在 `users.json` 里
- 检查 `config/users.json` 的 `open_id` 是否与日志一致
- 修改后等 5 秒热加载，或 `docker compose restart bridge`

### 文件上传失败

- 日志 `附件下载失败`：应用缺「获取与发送单聊、群组消息」权限，或未发布新版本
- 日志 `附件写入工作区失败`：用户 LXC 的 `/workspace/uploads` 不存在，或 `opencode serve` 不可达
- 错误码 `234037`：文件超过飞书 100MB 上限

### 文件回传失败

- 日志 `deliver outbox file ... failed`：应用缺「上传图片或文件资源」权限
- 文件超过 30MB：bridge 会提示，不会发送
- 图片超过 10MB：按文件发送（如果 ≤30MB）

### OpenCode 401 / 连接失败

- `users.json` 里的 `password` 与用户 LXC 的 `/etc/opencode/serve.env` 不一致
- 用户 LXC 的 nftables 没放行 docker LXC 的 IP
- `curl -u opencode:<password> http://<ip>:4096/global/health` 手工验证

### 卡片按钮无反应

- `lark-oapi 1.7.5` 会丢弃 `CARD` 帧，必须保留 `app/ws_patch.py` 的 monkey-patch
- 检查飞书是否订阅了「使用长连接接收回调」

### 审批卡不更新

- 必须在卡片回调响应里返回 `card: {"type": "raw", "data": <card>}`
- 回调处理期间单独调用 `PATCH /im/v1/messages/{id}` 会被忽略

### 提问卡没有出现

- 确认 OpenCode 版本支持 form API：`GET /api/session/{sessionID}/form`
- 查看日志 `form card sent` / `send form card failed`
- 确认飞书已订阅 `card.action.trigger`

### 任务卡在“执行中”

- 后台每 60 秒对账一次；OpenCode 已空闲时会补发结果
- 超过 `BRIDGE_TASK_TIMEOUT_SECONDS`（默认 7200 秒）会自动中止
- 可以用 `/stop` 手动中止

### 内存不足

- `docker stats` 看 bridge 容器；`mem_limit: 768m`
- 大文件上传是落盘的，但飞书 SDK 下载会有一次约等于文件大小的内存峰值
- 用户 LXC 内存不足时调 `pct set <vmid> --memory 4096`

## 安全

- 不开放 SSH / code-server / ttyd；用户只通过飞书 + OpenCode 使用。
- 用户 LXC 非特权（宿主映射 100000），容器内 root 不等于宿主 root。
- nftables 只放行 docker LXC 访问 4096；OpenCode 仍需 Basic 认证。
- `bridge.env`、`config/users.json`、`/etc/opencode/serve.env` 权限 600。
- 日志不记录 App Secret / API Key / 密码。
- 审批只允许一次，不写入“总是允许”规则。
- 不要提交 `bridge.env`、`users.json`、`*.db`、`*.log`、`backups/`、`temp/`。

## 从旧版单用户升级

旧版是「一个 bridge 进程 + 一个 OpenCode 服务 + 一个工作目录」。新版改成「每用户一个 LXC」。

迁移步骤：

1. 在 PVE 上按 `docs/lxc-template-manual.md` 创建模板 LXC。
2. 在 docker LXC 里部署新 bridge 容器（`docker compose up -d`）。
3. 用 `add-user.sh` 为每个现有用户创建 LXC，并把他们的 `open_id` 写进 `users.json`。
4. 旧版 `bridge.db` 可以直接复用（表结构向后兼容，`sessions.temporary` 等列保留但不再使用）。
5. 确认新链路正常后，停用旧的 `feishu-opencode-bridge@*.service` 和 `opencode-serve.service`。

旧版的 `bridge.env` 大部分变量仍然有效；新增变量都有默认值。
