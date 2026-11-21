# Plan：feishu-bridge 多用户隔离（每用户一个 OpenCode LXC + bridge 容器化）

- 日期：2026-11-20
- 状态：设计定稿，待实施（本次仅完成文档，未改代码）
- 适用仓库：`feishu-bridge`
- 前置决策：方案 C（每用户独立 LXC，不在 LXC 内套 Docker），bridge 打包为镜像放到已有 docker LXC

### 本版修订要点

1. 文件与图片**双向**：入站 `file`/`image`，出站 `/workspace/outbox` 约定（§6.4、§6.9）
2. 上传上限维持 **100 MB**（飞书硬限制，非 OpenCode 限制），超限翻译为可执行建议
3. 用户 LXC 资源默认 **2048M / swap 1024M / 24G**（§7.3、§9.2）
4. 模板默认软件包清单定稿：预装 `nodejs`/`npm` 与 Playwright MCP + Chromium，不装 `build-essential`、`postgresql-client`、`default-mysql-client`（§9.1）
5. 创建模板与用户 LXC **永远手动**，删除所有自动化设想（§7.6）
6. IP 必须由管理员显式传入；新增 `docs/lxc-scripts.md` 脚本手册（§7.8）
7. 模板不设 root 密码，管理员用 `pct enter` 进入（§7.9）
8. 模板 LXC 不提供创建脚本，改为 `docs/lxc-template-manual.md` 手工教程（§9.1）

---

## 1. 目标

1. 每个飞书用户拥有独立的 OpenCode 运行环境与工作区，互不可见。
2. 用户可在自己的环境里 `apt install` 数据库（PostgreSQL / MySQL）、导入 SQL、建视图。
3. bridge 继续提供现有能力：审批卡、提问表单、模型/Agent 切换、定时任务。
4. bridge 以镜像 + compose 形式交付，跑在现有的 docker LXC 中。
5. 删除临时会话（`/temp`）功能。
6. 新增：飞书文件消息 → 落盘 → 通过 OpenCode 文件 API 投递到用户环境。
7. 新增：冒烟测试，并写入项目记忆，任何 opencode 版本变更后必须执行。
8. 新用户开通由管理员执行脚本完成，**首次消息不自动克隆 LXC**。
9. 文件与图片**双向**：用户发给 agent；agent 的产物（如 playwright 截图）回传给用户。
10. 模板 LXC 与用户 LXC **永远手动创建**，不接入任何自动化。

### 信任模型

可信用户，主要防误操作，不追求恶意租户级隔离。LXC 为非特权容器（容器内 root 映射宿主 100000），共享宿主内核。

---

## 2. 最终架构

```
PVE 宿主（不在宿主上手工维护任何数据目录）
├─ LXC 100  docker-host            ← 现有 docker LXC
│   └─ container: feishu-bridge    ← 本项目镜像，/opt/feishu-bridge 下放置配置
│       ├─ config/
│       │   └─ users.json          ← 目录挂载，mtime 热加载
│       ├─ bridge.env
│       └─ data/                   ← bridge.db、日志、上传临时目录
│
├─ LXC 900  oc-template            ← 模板（Debian 13 + opencode + 依赖），不启动
├─ LXC 101  oc-alice  10.0.0.11    ← opencode serve + 用户自行安装的 DB
├─ LXC 102  oc-bob    10.0.0.12
└─ LXC 103  ...
```

### 数据流

```
飞书消息（文本）
  → bridge → 该用户 opencode serve（HTTP，LAN IP，basic auth）
  → SSE 事件回流 → 审批卡 / 表单卡 / 结果

飞书消息（文件 / 图片，≤100MB）
  → bridge 下载 → 落盘 data/uploads/<open_id>/<message_id>/<name>
  → POST http://<lxc>:4096/api/experimental/fs/write?path=/workspace/uploads/<date>/<name>
  → 删除本地临时文件
  → 给 agent 的 prompt 只包含路径

agent 产物（outbox）
  → agent 把文件写进 /workspace/outbox/
  → 任务结束时 bridge 调 GET /api/fs/list?path=/workspace/outbox
  → 逐个 GET /api/fs/read/<path> 读回
  → 图片走 im/v1/images（≤10MB）/ 其他走 im/v1/files（≤30MB）
  → 发送成功按 sha256 记入已发送清单，不重复发送
```

### 关键事实（2026-11-20 实测，opencode v2.3.2）

| 项 | 结论 |
| --- | --- |
| 写文件 API | `POST /api/experimental/fs/write?path=<abs>&location[directory]=/workspace`，body 为原始字节，`Content-Type: application/octet-stream` |
| 读文件 API | `GET /api/fs/read/<urlencoded absolute path>`，返回二进制 |
| 列目录 API | `GET /api/fs/list?path=<dir>&location[directory]=/workspace`，返回文件列表 |
| 校验结果 | 1 MB 随机二进制写入后 sha256 完全一致 |
| 前提 | `location[directory]` 必须是已存在的目录（`/workspace` 需提前创建） |
| 鉴权 | basic auth（用户名 `opencode` + `OPENCODE_PASSWORD`），`/api/model` 无认证返回 401 |
| SSE | `GET /api/event`，需要认证 |
| 官方镜像 | `ghcr.io/anomalyco/opencode:<version>`，Alpine 基础，仅含 opencode + rg，**不用于本方案**（LXC 直接用官方安装脚本） |
| 空闲内存 | 单个 `opencode serve` 约 182 MB |

> `fs/write` 是 `experimental` 前缀；`fs/read`、`fs/list` 是正式路径。三者都在 OpenAPI 文档内，前两个已实测。

风险：`fs/write` 属于 `experimental` 接口，升级 opencode 后可能变化。缓解措施见 §8 冒烟测试与 §11。

---

## 3. 组件职责

| 组件 | 位置 | 职责 |
| --- | --- | --- |
| bridge | docker LXC 容器内 | 飞书长连接、会话/任务编排、审批与表单、文件下载与投递、定时任务、未开通用户通知 |
| opencode serve | 每用户 LXC，systemd 管理 | 执行 agent 任务，监听 `0.0.0.0:4096` |
| 用户 DB | 每用户 LXC 内，systemd 管理 | 用户任务产物，随 LXC 备份 |
| `add-user.sh` | PVE 宿主执行 | `pct clone` 新 LXC、配 IP、写 `users.json`、跑冒烟 |
| `docs/lxc-template-manual.md` | 人工教程 | 模板 LXC 由管理员照教程手工创建，不提供自动化脚本 |

bridge 不访问 Docker，也不需要任何宿主目录。

---

## 4. 仓库改动清单

### 新增

| 文件 | 说明 |
| --- | --- |
| `Dockerfile` | `python:3.13-slim` + requirements + `app/`，`CMD python /app/app/main.py` |
| `.dockerignore` | 排除 `.venv`、`tests`、`backups`、`data`、`*.db`、`*.log`、`debug` |
| `docker-compose.yml` | 只含 bridge 一个 service；`env_file: ./bridge.env`；挂载 `./config:/app/config:ro`、`./data:/app/data` |
| `config/users.json.example` | 用户映射样例（`users.json` 本身不提交） |
| `deploy/lxc/opencode-serve.service` | 用户 LXC 内的 systemd 单元（`EnvironmentFile` 依次加载 `proxy.env` / `service.env` / `serve.env`，监听 `0.0.0.0:4096`） |
| `deploy/lxc/proxy.env.example` | 出站代理（生产 `192.168.110.11:10808`，HTTP 与 SOCKS5 同端口），安装为 `/etc/opencode/proxy.env` |
| `deploy/lxc/opencode.jsonc` | 用户 LXC 内的 opencode 配置模板（providers/agents/MCP，**不含 `dbx`**） |
| `deploy/lxc/workspace-AGENTS.md` | 投放到 `/workspace/AGENTS.md` 的工作区约定（`apt install -y`、超时分片、outbox 用法、网络与代理） |
| `deploy/lxc/add-user.sh` | 克隆 LXC、写入 IP 与 serve.env、更新 `users.json`、`pct push` 到 docker LXC、跑冒烟 |
| `deploy/lxc/remove-user.sh` | 停用/归档用户 LXC 并从 `users.json` 移除 |
| `docs/lxc-template-manual.md` | **模板 LXC 手工创建教程**：逐步命令、软件包清单、systemd、nftables、就绪标记、冒烟 |
| `docs/lxc-scripts.md` | **脚本用法文档**：`add-user.sh` / `remove-user.sh` 参数、示例、排错、回滚 |
| `scripts/smoke-test.sh` | 用户环境冒烟测试（见 §8） |
| `app/users.py` | `UserEntry` + `UserRegistry`（加载、mtime 热加载、open_id 索引） |

### 修改

| 文件 | 改动 |
| --- | --- |
| `app/config.py` | 新增 `BRIDGE_USERS_FILE`、`BRIDGE_ADMIN_OPEN_IDS`、`BRIDGE_UNKNOWN_USER_COOLDOWN`、`BRIDGE_SANDBOX_MODE`、`BRIDGE_UPLOAD_TMP_DIR`、`BRIDGE_MAX_UPLOAD_BYTES`；保留单用户 `OPENCODE_BASE_URL` 作为兼容/测试回退 |
| `app/opencode.py` | 新增 `OpenCodeRouter`（按 `open_id` / `session_id` 解析客户端）；`upload_file()` 封装 `fs/write`（流式）；`list_dir()` / `read_file()` 封装 `fs/list`、`fs/read` |
| `app/bridge.py` | 路由接入、SSE 每 url 一循环、按用户对账、入站文件/图片处理、出站 outbox 扫描、未开通用户拒绝与管理员通知、删除临时会话逻辑 |
| `app/feishu.py` | 新增 `download_resource()`（入站）与 `upload_image()` / `upload_file()` / `send_image()` / `send_file()`（出站） |
| `app/db.py` | 新增 `outbox_sent` 表（`open_id, sha256, size, sent_at`）及迁移；删除临时会话读写函数 |
| `app/permissions.py` | `sandbox_mode=container` 时从黑名单剔除 `sudo`/`su`/`service`/`systemctl`，保留破坏性命令 |
| `app/slash_commands.py` | 删除 `temp` 命令；新增 `smoke` 命令 |
| `app/main.py` | 按 `users.json` 为每个 url 启动 SSE 循环；关闭时逐个关闭客户端 |
| `deploy/feishu-opencode-bridge@.service` | 标记为 legacy（保留文件，README 注明已由 compose 取代） |
| `README.md` | 架构、隔离边界、文件消息、新用户开通、升级与冒烟测试、部署流程全量更新 |
| `AGENTS.md` | 新增「升级与冒烟测试」强制条款；更新部署章节为 compose 流程 |
| `bridge.env.example` | 新增用户映射/管理员/沙箱/上传相关变量 |
| `backup.sh` | 适配容器化：备份 `data/`、`config/users.json`、`bridge.env` |
| `tests/` | 更新临时会话相关测试；新增路由、users.json 热加载、文件投递、审批放宽的测试 |

### 删除

`app/bridge.py` 中临时会话相关：`_make_temp_dir`、`_remove_temp_dir`、`_sweep_temp_dirs`、`_cleanup_expired_temp_sessions`、TTL 清理循环、`/temp` 命令分支；`app/db.py` 的临时会话读写函数。

数据库列 `sessions.temporary` / `temp_dir` / `expires_at` **保留但不再使用**，避免破坏性迁移。

---

## 5. 配置契约

### `config/users.json`

```json
[
  {
    "open_id": "ou_xxxxxxxx",
    "slug": "alice",
    "url": "http://10.0.0.11:4096",
    "password": "<随机串>",
    "workspace": "/workspace"
  }
]
```

- `workspace` 可选，默认 `/workspace`
- 未登记 `open_id` 一律拒绝，并回复「未开通」提示
- 文件热加载：记录 mtime，变化后重建路由（无需重启进程）
- 出站目录固定为 `<workspace>/outbox`，发送成功后按 sha256 记入已发送清单，不重复发送

### 环境变量

```bash
# 用户映射（容器内路径）
BRIDGE_USERS_FILE=/app/config/users.json

# 未开通用户通知的管理员（逗号分隔 open_id）
BRIDGE_ADMIN_OPEN_IDS=ou_admin1,ou_admin2

# 同一未开通用户的通知冷却时间（秒，默认 3600）
BRIDGE_UNKNOWN_USER_COOLDOWN=3600

# 沙箱模式：container 时放宽 sudo/service 审批
BRIDGE_SANDBOX_MODE=container

# 上传临时目录（默认 $BRIDGE_DATA_DIR/uploads）
BRIDGE_UPLOAD_TMP_DIR=/app/data/uploads

# 上传上限（默认 104857600 = 100MB，这是飞书「获取消息中的资源文件」的硬上限，
# 调大没有意义；超限错误码 234037）
BRIDGE_MAX_UPLOAD_BYTES=104857600

# 任务超时（默认由 3600 调整为 7200，SQL 导入需要）
BRIDGE_TASK_TIMEOUT_SECONDS=7200
```

### 用户 LXC `/etc/opencode/serve.env`

```bash
OPENCODE_PASSWORD=<与 users.json 一致>
```

---

## 6. 关键实现细节

### 6.1 路由

- `OpenCodeRouter` 保持与 `OpenCodeClient` 同名方法，`bridge.py` 中 `self.oc.*` 的会话级调用点基本不改。
- 需要 `open_id` 的方法显式加参数：`create_session(open_id, ...)`、`list_models(open_id)`、`list_agents(directory, open_id)`。
- 会话级方法（`prompt`、`interrupt`、`reply_permission`、`delete_session` 等）通过 `session_id → open_id` 缓存解析；缓存来源：
  - 启动时从 `chats` 表重建
  - 创建/切换会话时写入
- 同步客户端（卡片回调线程）同样走路由。

### 6.2 SSE

- `main.py` 按 `users.json` 中**去重后的 url** 各启动一个 `sse_loop(entry)`。
- 事件处理沿用现有 `_handle_event`；`session_id not in self._session_ids` 已能过滤跨用户事件。
- 断线重连退避保持现状，日志中带上 `slug` 便于定位。

### 6.3 对账与超时

- `_reconcile_active_tasks` 按任务所属 `open_id` 分组，分别调用对应客户端的 `list_active_sessions()`。
- `interrupt` 使用任务所属用户客户端。
- 默认超时 7200 秒；超时提示文案同步更新。

### 6.4 入站：文件与图片

入口（`on_message`）：

1. `message_type == "file"` → content 为 `{"file_key": "...", "file_name": "..."}`
2. `message_type == "image"` → content 为 `{"image_key": "..."}`，文件名生成为 `image-<message_id>.<ext>`（扩展名按下载响应的 Content-Type 推断，默认 `png`）
3. 其他类型（音频/视频/富文本）回复「目前支持文本、文件、图片」

统一处理链：

1. `feishu.download_resource(message_id, key, type="file"|"image")`
   - 飞书侧上限 100 MB（错误码 `234037`），bridge 把该错误翻译成人话：
     `文件超过飞书 100MB 限制，无法接收。请拆分、压缩，或把下载链接发给 agent 让它自己 curl。`
   - SDK 返回 `BytesIO`（内部已整块读入，无法避免一次内存峰值）
2. 落盘：`data/uploads/<open_id>/<message_id>/<safe_name>`
   - `safe_name = os.path.basename(name)`，拒绝 `..`、空名、超长名
   - 分块写，写完 `fsync`
3. 投递：`upload_file(entry, local_path, remote_path)`
   - `POST {entry.url}/api/experimental/fs/write`
   - `params={"path": remote_path, "location[directory]": entry.workspace}`
   - 通过文件对象传入 body（httpx 自动设置 `Content-Length`），**流式**，不整体读入内存
   - 超时放宽：connect 10s / read 300s / write 300s
4. 校验：响应 200 且返回 `data.path` 与期望一致
5. 清理：`finally` 删除本地临时文件
6. Prompt 模板（文件）：

```
已收到文件 <file_name>（<size>），保存在 /workspace/uploads/<date>/<name>。
请按需处理；导入 SQL 请用 psql -f / mysql < 方式，不要 cat 到上下文中。
```

图片的 prompt 追加一句：`这是一张图片，可用 read 工具查看内容。`

并发控制：上传用 `asyncio.Semaphore(2)`，避免 100 MB 文件并发造成内存尖峰。

### 6.5 内存与落盘

- bridge 容器 `mem_limit: 768m`：飞书 SDK 下载时仍有一次整块缓冲，落盘后立即释放。
- 上传阶段从磁盘流式读取，内存恒定；出站 `fs/read` 同样先落盘再交给上传接口。
- 入站上限 100 MB（飞书硬限制），超过直接拒绝并给出拆分/链接建议。

### 6.6 工作区与用户环境

用户 LXC 内：

- `opencode` 以 root 运行（非特权 LXC，映射宿主 100000），配置目录 `/etc/opencode/opencode/`（通过 `XDG_CONFIG_HOME=/etc/opencode` 指定；实施时用 `opencode debug config` 确认实际读取路径）
- `/workspace` 为工作目录；`/workspace/uploads/<date>/` 放入站文件，`/workspace/outbox/` 放出站产物
- `/workspace/AGENTS.md` 写入工作区约定：`DEBIAN_FRONTEND=noninteractive apt install -y`、长任务分片、outbox 用法
- 数据库**不预装**，由用户任务自行 `apt install`，systemd 管理，重启自动恢复
- 默认软件包清单见 §9.1（可裁剪）
- 模板上配置 nftables：仅允许 docker LXC 的 IP 访问 4096
- 模板放置就绪标记 `/etc/opencode/ready`，供 `add-user.sh` 等待
- 出站代理统一由模板注入：`/etc/opencode/proxy.env`（`HTTP_PROXY`/`HTTPS_PROXY` = `http://192.168.110.11:10808`，`ALL_PROXY` = `socks5h://192.168.110.11:10808`，600），systemd 单元通过 `EnvironmentFile` 注入 opencode 进程；另有 `/etc/profile.d/99-proxy.sh`、`/etc/apt/apt.conf.d/99proxy`、`git --system` 与 `/etc/pip.conf`，保证 `pct exec` 和登录 shell 都能出站。`NO_PROXY` 覆盖 `localhost`、`127.0.0.1` 与私有网段。代理配置随克隆复制，`add-user.sh` 只验证不改写
- `/workspace/AGENTS.md` 写明代理用法：优先使用 `$HTTP_PROXY`/`$HTTPS_PROXY`/`$ALL_PROXY`，不写死地址、不清空变量、不改 `/etc/opencode/proxy.env`
- `/etc/opencode/serve.env`（`OPENCODE_PASSWORD`，600）与 `/etc/opencode/service.env`（模型 API Key 等，600）随模板克隆复制；信任模型下可接受，用户量扩大后再改按用户下发

### 6.7 审批放宽

`sandbox_mode == "container"` 时从 `DANGEROUS_SHELL_PATTERNS` 剔除：

- `sudo *`、`su *`、`doas *`、`pkexec *`
- `service * start*|stop*|restart*`、`systemctl * ...*`

保留：`rm *`、`mkfs*`、`dd *`、`fdisk *`、`shutdown *` 等破坏性操作。

### 6.8 未开通用户处理

- 解析到 `open_id` 不在 `UserRegistry` 中：
  - 回复用户：未开通 + 自己的 `open_id` + 联系管理员
  - 通知管理员：卡片含 `open_id`、时间、建议命令
  - 按 `BRIDGE_UNKNOWN_USER_COOLDOWN` 做冷却，避免重复轰炸
- 不创建任何会话、不写数据库

### 6.9 出站：outbox 回传（图片 / 文件）

agent 不能直接调飞书，也不给它 bridge 的凭据。用**目录约定**表达"要发什么"：

1. 用户在飞书说"把截图发我"，agent 把产物保存到 `/workspace/outbox/<name>`
2. 任务进入 idle / `session.execution.completed` 时，bridge 对该用户执行：
   - `GET /api/fs/list?path=/workspace/outbox&location[directory]=/workspace`
   - 忽略目录；按文件名排序，单次最多处理 10 个
3. 逐个 `GET /api/fs/read/<urlencoded path>` → 先落盘到 `data/outbox/<open_id>/<message_id>/`
4. 按扩展名分流：

| 类型 | 扩展名 | 上限 | 发送链路 |
| --- | --- | --- | --- |
| 图片 | png/jpg/jpeg/webp/gif/bmp | **10 MB** | `im/v1/images`（`image_type=message`）→ `image_key` → `msg_type=image` |
| 其他 | 任意 | **30 MB** | `im/v1/files`（`file_type=stream`）→ `file_key` → `msg_type=file` |

5. 发送成功后的清理（opencode 没有删除接口，只有写）：
   - 采用**已发送清单**而不是移动：bridge 在 `bridge.db` 记录 `(open_id, sha256, size, sent_at)`
   - 下一轮 `fs/list` 时，命中清单且 `mtime` 未变的文件直接跳过，`sent_at` 超过 7 天自动清除记录
   - 若 opencode 提供删除/移动接口（实施时确认），优先改为直接删除；否则用上面的清单做去重，**不往 `sent/` 复制**（避免占双倍磁盘）
   - 提供 `/outbox clear` 运维命令（可选）：让 agent 执行 `rm -f /workspace/outbox/*`
6. 本地 `data/outbox/` 在 `finally` 中清理

失败处理：

- 超过 10 MB 的图片：不发，回一条文本「图片 X 超过飞书 10MB 限制，请让 agent 压缩后重试」
- 超过 30 MB 的文件：同上，提示「只发路径」或改发链接
- 上传或发送失败：保留在 outbox，回复错误摘要，用户重说一次即可重试

重复发送防护：以 `(open_id, sha256, size)` 为键，命中且文件 `mtime` 未变则跳过；记录保留 7 天。

### 6.10 文件大小限制对照

| 方向 | 通道 | 硬限制 | 由谁决定 |
| --- | --- | --- | --- |
| 入站 用户→agent | `im/v1/messages/{id}/resources` | **100 MB**（`234037`） | 飞书 |
| 出站 图片 | `im/v1/images` | **10 MB** | 飞书 |
| 出站 文件 | `im/v1/files` | **30 MB** | 飞书 |
| bridge 本地阈值 | `BRIDGE_MAX_UPLOAD_BYTES` | 100 MB | 本方案 |

超限一律给出可执行建议（拆分 / 压缩 / 发链接让 agent `curl`），不要只说"失败"。

---

## 7. 新用户开通流程

### 7.1 为什么不做自动创建

1. **飞书“企业里拉入新用户”不会给 bot 任何事件。** bot 只在用户主动发消息时拿到 `open_id`。监听通讯录事件需要通讯录全量读权限，权限过大，且通讯录成员并不都该拿到 LXC。
2. **首条消息自动克隆 LXC 是资源耗尽入口。** 任何人发一条消息就触发一次 `pct clone`（数 GB 磁盘 + 常驻 opencode 进程）。
3. bridge 跑在 docker LXC 里，**没有也不该有 `pct` 权限**。

因此：首次消息只做「拒绝 + 通知管理员」，克隆由管理员执行脚本。

### 7.2 时间线

**① 用户 `ou_abc` 首次发消息**

bridge 回复用户：

```
你还没有开通工作区。
你的 open_id：ou_abc123
请联系管理员开通后再试。
```

同时给 `BRIDGE_ADMIN_OPEN_IDS` 中的人发卡片：

```
新用户申请开通
open_id: ou_abc123
时间: 2026-11-20 14:03
建议命令:
./deploy/lxc/add-user.sh alice ou_abc123 10.0.0.15
```

同一 `open_id` 在冷却期内只通知一次。

**② 管理员在 PVE 上执行**

```bash
./deploy/lxc/add-user.sh alice ou_abc123 10.0.0.15
```

脚本内部步骤见 §7.3，耗时约 40–90 秒（ZFS 全量克隆通常十几秒）。

**③ bridge 自动感知，不重启**

```
[INFO] users.json changed, reloaded 3 user(s): alice, bob, carol
```

轮询间隔 5 秒。

**④ 用户重试**

消息进入自己的 LXC，`/status` 正常。

### 7.3 `add-user.sh` 步骤与命令骨架

```bash
#!/usr/bin/env bash
set -euo pipefail

SLUG=$1            # ^[a-z][a-z0-9-]{1,20}$
OPEN_ID=$2         # ou_...
IP=$3              # 10.0.0.15
TEMPLATE=900       # 模板 LXC
START_ID=901       # 用户 LXC 从 901 起编号
DOCKER_LXC=100     # 运行 bridge 的 docker LXC
GATEWAY=10.0.0.1

# 1. 校验：slug、IP 空闲、VMID 冲突
[[ "$SLUG" =~ ^[a-z][a-z0-9-]{1,20}$ ]] || die "slug 非法"
ping -c1 -W1 "$IP" >/dev/null 2>&1 && die "IP 已被占用"
NEXT_ID=$(pvesh get /cluster/nextid)
# nextid 小于 START_ID 时，从 START_ID 向上找第一个空闲 VMID

# 2. 从模板克隆
pct clone "$TEMPLATE" "$NEXT_ID" --hostname "oc-$SLUG" --full

# 3. 网络与资源（内存 2048M / swap 1024M）
pct set "$NEXT_ID" \
  --net0 "name=eth0,bridge=vmbr0,ip=$IP/24,gw=$GATEWAY" \
  --memory 2048 --cores 4 --swap 1024 --onboot 1

# 3.1 磁盘：模板 8G，克隆后扩到 24G（ZFS 上是配额，按实际用量增长）
pct resize "$NEXT_ID" rootfs 24G

# 4. 启动并等待模板就绪标记
pct start "$NEXT_ID"
for _ in $(seq 1 60); do
  pct exec "$NEXT_ID" -- test -f /etc/opencode/ready && break
  sleep 1
done

# 5. 生成独立密码
PW=$(openssl rand -hex 24)
pct exec "$NEXT_ID" -- sh -c \
  "umask 077; printf 'OPENCODE_PASSWORD=%s\n' '$PW' > /etc/opencode/serve.env"

# 6. 启动 opencode
pct exec "$NEXT_ID" -- systemctl enable --now opencode-serve

# 7. 等待健康检查
for _ in $(seq 1 60); do
  curl -fsS -u "opencode:$PW" "http://$IP:4096/global/health" >/dev/null && break
  sleep 1
done

# 8. 更新 users.json（读-改-写，推回 docker LXC）
pct exec "$DOCKER_LXC" -- cat /opt/feishu-bridge/config/users.json > /tmp/users.json
python3 - "$OPEN_ID" "$SLUG" "$IP" "$PW" /tmp/users.json <<'PY'
import json, sys
open_id, slug, ip, pw, path = sys.argv[1:6]
users = json.load(open(path))
users = [u for u in users if u["open_id"] != open_id and u["slug"] != slug]
users.append({"open_id": open_id, "slug": slug,
              "url": f"http://{ip}:4096", "password": pw,
              "workspace": "/workspace"})
json.dump(users, open(path, "w"), ensure_ascii=False, indent=2)
PY
pct push "$DOCKER_LXC" /tmp/users.json /opt/feishu-bridge/config/users.json
rm -f /tmp/users.json

# 9. 立即冒烟
./scripts/smoke-test.sh --url "http://$IP:4096" --password "$PW"
```

要求：

- **IP 由管理员显式传入**，脚本不做自动分配（不扫网段、不 DHCP）。脚本只校验：格式合法、`ping` 不通、不在 `users.json` 中已存在
- 资源默认 `2048M / swap 1024M / 24G`；磁盘先克隆 8G 模板盘再 `pct resize`
- 任一步失败即退出，并打印已创建的 VMID，提示手工清理命令（`pct stop && pct destroy`）
- 脚本幂等：`users.json` 中同 `open_id` 或同 `slug` 先删除再追加
- 脚本**不自动决策、不自动扩容、不自动重试**；所有参数必须来自命令行
- 完整用法见 `docs/lxc-scripts.md`（参数表、示例、排错、回滚），实施时随脚本一起交付
- 首次部署需先创建 `/opt/feishu-bridge/config/users.json`（`cp config/users.json.example config/users.json`），否则第 8 步的 `cat` 会失败

### 7.4 `users.json` 的挂载与写入（重要）

**不能按单文件挂载**：

```yaml
# 错误做法
volumes:
  - ./config/users.json:/app/users.json:ro
```

原因：`pct push` 会替换文件（新 inode），Docker 的单文件 bind mount 绑定 inode，容器内仍是旧内容，热加载永远不生效。

正确做法是**挂目录**：

```yaml
volumes:
  - ./config:/app/config:ro
```

- bridge 读取 `/app/config/users.json`
- `add-user.sh` 推送到 `/opt/feishu-bridge/config/users.json`
- mtime 变化正确传播到容器，5 秒内热加载

同时要求写入方保证文件始终是合法 JSON：先写临时文件、校验 `json.load`、再替换目标文件。

### 7.5 用户生命周期

| 场景 | 操作 |
| --- | --- |
| 新增 | `./deploy/lxc/add-user.sh <slug> <open_id> <ip>` |
| 暂停（保留数据） | `pct stop <vmid>` + 从 `users.json` 移除 |
| 删除（离职/清理） | `./deploy/lxc/remove-user.sh <slug>`：停 LXC、`vzdump` 归档、`pct destroy`、移除映射 |
| 环境损坏 | `pct rollback <vmid> <snapshot>` |
| 需要更强隔离 | 把该用户迁到独立 VM，`url` 改成 VM 地址即可，bridge 无改动 |
| 换 IP | 改 LXC 网络 + 更新 `users.json`，bridge 热加载 |

### 7.6 创建 LXC 永远手动（明确约束）

**本方案不实现任何自动创建/自动部署 LXC 的能力，这一条是硬约束。**

- 模板 LXC：由管理员照 `docs/lxc-template-manual.md` 在 PVE 宿主手工创建
- 用户 LXC：只能由管理员在 PVE 宿主手工执行 `add-user.sh`
- bridge **不持有** PVE API Token，不调用任何 PVE 接口，不执行 `pct`
- 不提供"飞书卡片点一下开通"，不提供扫描通讯录、定时补建、自动扩容
- 未开通用户的消息只触发「拒绝 + 通知管理员」，**不触发任何创建动作**

这样设计的原因：

1. 创建 LXC 是宿主级操作，权限很重，不该出现在长期联网的 bridge 进程里；
2. 每个用户 LXC 都消耗内存/磁盘/IP，属于需要人判断的资源决策；
3. 手动执行留下明确的 shell history 和审计记录。

将来若确实需要自动化，应当作为**独立的、由人显式触发**的运维工具实现，与 bridge 进程解耦；本计划范围外。

### 7.7 事件触发表

| 场景 | 触发 | 动作 |
| --- | --- | --- |
| 用户发文本 | `im.message.receive_v1`，`message_type=text` | 路由到该用户 opencode，走原有任务/审批/卡片流程 |
| 用户发文件 | `message_type=file` | 下载 → 落盘 → `fs/write` 投递 → prompt 只给路径 |
| 用户发图片 | `message_type=image` | 同上，落盘为 `image-<msg_id>.<ext>`，prompt 提示可用 read 查看 |
| agent 产出文件/图片 | agent 写入 `/workspace/outbox/` | idle 后 bridge `fs/list` → `fs/read` → 飞书上传 → 发送 → 记入已发送清单 |
| 未开通用户发消息 | `open_id` 不在 `users.json` | 回复「未开通」+ 通知管理员（带冷却），**不创建任何东西** |
| 新用户开通 | 管理员手工执行 | `add-user.sh`（§7.3） |
| 用户删除/离职 | 管理员手工执行 | `remove-user.sh` |
| opencode 升级 | 改模板中的 `OPENCODE_VERSION` | 手工重建模板 → 逐用户升级/重建 → **必须跑冒烟** |
| 用户环境损坏 | 用户反馈 | 手工 `pct rollback` |
| 管理员进入容器 | 运维需要 | 宿主上 `pct enter <vmid>`（无需密码，见 §7.9） |
| bridge 升级 | 镜像重建 | `docker compose up -d --build` → 跑 `/smoke` |

### 7.8 `docs/lxc-scripts.md` 内容结构

单独一份面向管理员的脚本手册，不放进 README（避免主文档过长）：

1. **前置条件**：PVE 版本、存储、网桥、模板 VMID 约定、docker LXC 的 VMID、`users.json` 路径
2. **模板 LXC**
   - 模板不提供脚本，按 `docs/lxc-template-manual.md` 手工创建
   - 该教程逐步给出：`pct create`、apt 依赖、opencode 安装、`/workspace`、systemd 单元、nftables、就绪标记、冒烟
   - 验证方式：`pct config 900`、`pct exec 900 -- opencode --version`
3. **`add-user.sh`**
   - 参数：`<slug>` `<open_id>` `<ip>`，以及可选 `--memory` `--cores` `--disk` `--gateway` `--bridge`
   - 参数校验规则（slug 正则、IP 格式、IP 占用检测、VMID 自动取 nextid）
   - 完整执行流程与耗时
   - 失败时的清理命令
4. **`remove-user.sh`**
   - 参数：`<slug>`，可选 `--keep-disk`（只停不删）
   - 归档位置与恢复步骤
5. **`scripts/smoke-test.sh`**
   - `--users` 与 `--url/--password` 两种用法
   - 输出示例与退出码含义
6. **常见排错**
   - LXC 起不来 / 4096 连不通 / 401 / `ready` 标记不存在 / `users.json` 未生效
7. **附：手工等价命令**
   - 每个脚本步骤给出对应的原始 `pct` 命令，便于排错时逐步执行
8. **管理员访问与 root 密码**
   - `pct enter` / `pct exec` / `pct mount` 用法
   - root 密码策略与 `passwd -S root` 校验（§7.9）

写法要求：所有命令可直接复制执行；每个参数给默认值和取值范围；示例 IP 用 `10.0.0.x` 占位并注明需替换。

### 7.9 管理员访问与 root 密码

**结论：模板不设 root 密码，管理员从 PVE 宿主用 `pct enter` 进入，不需要密码。**

- PVE 官方 Debian 模板默认不给 root 设密码（账号锁定），控制台登录不了；`pct enter <vmid>` 是宿主侧直接切入容器命名空间，**不走登录流程**，所以无需密码。
- 验证模板状态：

  ```bash
  pct exec 900 -- passwd -S root
  # 第二列 L = 锁定（预期）；P = 已设密码（需要在模板里清掉）
  ```

- **不要在模板里设 root 密码**：`/etc/shadow` 会随克隆复制，所有用户 LXC 共用同一个密码，等于共享口令，且用户 agent 以 root 运行、能读到它。要清理：

  ```bash
  pct exec 900 -- passwd -l root      # 锁定 root 密码
  ```

- 用户侧永远不登录 shell：只有飞书 + opencode 两条入口，容器内不装 `openssh-server`，22 端口不监听。所以 root 密码即使被用户自己改掉，也没有远程登录面。
- 确实需要控制台登录时（救援场景），在**单台** LXC 上单独设置，不写进模板：

  ```bash
  pct exec 101 -- passwd            # 交互式设置，仅影响该用户
  ```

- 容器已停止但仍要查文件：`pct mount <vmid>` 挂到宿主 `/var/lib/lxc/<vmid>/rootfs`，用完 `pct unmount <vmid>`。
- 生产运维路径固定为：SSH 到 PVE 宿主 → `pct enter <vmid>`。不通过 LXC 内部 SSH，也不在 LXC 里保存宿主凭据。

`docs/lxc-scripts.md` 需要收录本节内容（管理员访问、root 密码策略、`passwd -S` 校验、救援流程）。

---

## 8. 冒烟测试（强制）

### 8.1 `scripts/smoke-test.sh`

参数：`--users <users.json>` 或 `--url <url> --password <pw>`；可对单个用户或全部用户执行。

每个用户依次验证：

1. `GET /global/health` 返回 200
2. `GET /api/model` 无认证 401、带认证 200（鉴权生效）
3. 生成 2 MB 随机数据 → `POST /api/experimental/fs/write?path=/tmp/opencode-smoke.bin&location[directory]=/tmp`
4. `GET /api/fs/read/%2Ftmp%2Fopencode-smoke.bin` → sha256 与本地一致
5. `GET /api/event` 建立 SSE 成功（收到 HTTP 200 即断开）

任一失败 → 退出码非 0，打印失败用户与失败步骤。

说明：使用 `/tmp` 固定文件名，反复覆盖，避免污染 `/workspace`；API 没有删除接口，固定名可自我清理。

### 8.2 bridge 内 `/smoke` 命令

新增飞书命令 `/smoke`：对当前用户执行一次「写 1 MB → 读回 → 校验 sha256」，用卡片返回结果。

作用：不需要登录服务器即可验证「飞书 → bridge → 用户 LXC」全链路，覆盖 bridge 侧代码路径。

### 8.3 执行时机（写入 AGENTS.md，作为强制规则）

必须执行冒烟测试的场景：

1. 变更 opencode 版本（手工修改模板，见 `docs/lxc-template-manual.md`）
2. 重新构建/更新模板 LXC、克隆新用户
3. 升级 bridge 镜像且涉及 `app/opencode.py`、文件投递逻辑
4. 部署后首次验证

执行方式：

```bash
# 服务器侧
./scripts/smoke-test.sh --users /opt/feishu-bridge/config/users.json
# 或飞书内
/smoke
```

失败则停止发布；不得跳过、不得只测一个用户。

### 8.4 记忆落点

1. 项目 `AGENTS.md`：在「测试与检查（发布前必须全部通过）」中追加冒烟测试条目，并新增「opencode 升级流程」小节。
2. `README.md`：升级章节写明同样的强制要求。
3. `deploy/lxc/add-user.sh`：末尾自动调用冒烟脚本，失败即报错退出；模板创建完成后按教程手工跑一次冒烟。

---

## 9. 部署顺序

### 9.1 模板 LXC（PVE 宿主，手工创建）

**不提供创建脚本**，按 `docs/lxc-template-manual.md` 逐步执行。教程覆盖：

1. `pct create 900 <debian-13-template> --hostname oc-template ...`
2. `apt update && apt install -y <默认软件包清单>`
3. 安装 opencode v2（官方安装脚本，不固定小版本）
4. 创建 `/workspace`、`/workspace/outbox`、`/etc/opencode/`
5. 写入 `opencode-serve.service`、`opencode.jsonc`、`workspace-AGENTS.md`
6. 配置 nftables（仅放行 docker LXC 访问 4096）
7. `systemctl enable opencode-serve` 并验证
8. 写入就绪标记 `/etc/opencode/ready`
9. 跑 `scripts/smoke-test.sh` 验证
10. `pct stop 900` 并转为模板（`pct template 900`）

模板盘 **8G**（克隆快），用户 LXC 克隆后 `pct resize` 到 **24G**。

#### 默认软件包清单（已定稿）

| 包 | 约占用 | opencode 是否依赖 | 说明 |
| --- | --- | --- | --- |
| `ca-certificates` | <1 MB | 必需 | HTTPS 请求 |
| `curl` | ~2 MB | 安装必需 | 安装脚本下载 opencode；agent 也常用来下载 |
| `git` | ~30 MB | 建议保留 | opencode 的快照/撤销依赖 git；用户任务几乎一定会用 |
| `bash` / `coreutils` | 基础 | 必需 | Debian 自带 |
| `python3` + `python3-venv` + `python3-pip` | ~35 MB | 非必需 | 用户任务常用；venv 满足 PEP 668 |
| `jq` | ~1 MB | 非必需 | 处理 JSON 方便，体积小 |
| `unzip` + `xz-utils` | ~2 MB | 非必需 | 解压常见格式 |
| `less` + `file` + `procps` | ~2 MB | 非必需 | 排查问题时的基本工具 |
| `nodejs` / `npm` | ~80 MB | 非必需 | 预装，供 Playwright MCP 使用 |
| `@playwright/mcp` + `playwright` | ~20 MB | 非必需 | 全局安装，agent 截图用 |
| Chromium（`playwright install chromium`） | ~300 MB | 非必需 | 预装到 `/root/.cache/ms-playwright` |
| `fonts-noto-cjk` | ~200 MB | 非必需 | 仅当用户要 playwright 截中文页面才装 |
| ~~`build-essential`~~ | ~200 MB | 不装 | 需要编译时用户自行 `apt install` |
| ~~`postgresql-client`~~ | ~5 MB | 不装 | 用户装服务端时一起装 |
| ~~`default-mysql-client`~~ | ~10 MB | 不装 | 用户装服务端时一起装 |
| ~~`postgresql` / `mariadb-server`~~ | 数百 MB | 不装 | 按用户需求自行 `apt install` |
| ~~`sudo`~~ | — | 不装 | 容器内就是 root |
| ~~`openssh-server`~~ | — | 不装 | 不开放 SSH |

合计约 **450 MB**（不含可选字体；主要是 Chromium）。手工创建模板时按此清单 `apt install`；每一项在教程里单独一行并带注释，方便后续增删。

**opencode 真正需要的只有**：`ca-certificates`、`git`（建议）、以及运行时可执行 `bash`。其余都可以按需装。

模板**不设 root 密码**（账号锁定），管理员用 `pct enter <vmid>` 从宿主进入，不需要密码；原因与校验方式见 §7.9。

### 9.2 用户 LXC（PVE 宿主，手工）

```bash
./deploy/lxc/add-user.sh alice ou_xxxxx 10.0.0.11
```

默认 `2048M` 内存 / `1024M` swap / `24G` 磁盘。IP 必须显式传入。脚本用法见 `docs/lxc-scripts.md`。

### 9.3 bridge 容器

在 docker LXC 内：

```bash
cd /opt/feishu-bridge
cp bridge.env.example bridge.env   # 填密钥与 BRIDGE_ADMIN_OPEN_IDS
cp config/users.json.example config/users.json
docker compose build
docker compose up -d
docker compose logs -f
```

### 9.4 验收

1. 飞书 `/status` 正常
2. 文本任务：让 agent 执行 `apt install postgresql` 并启动
3. 文件任务：发送 `.sql`，确认落盘路径与导入结果
4. 图片任务：发送一张截图，确认 agent 能读出内容
5. 出站任务：让 agent 用 playwright 截图写到 `/workspace/outbox/`，确认飞书收到图片
6. `/smoke` 通过
7. `pct backup 101` 可正常完成

---

## 10. 回滚

| 层 | 方式 |
| --- | --- |
| bridge | `docker compose down` → 切回上一个 image tag → `up -d`；`data/` 提前备份 |
| 用户 LXC | `pct rollback 101 <snapshot>` |
| 用户映射 | `config/users.json` 保留上一版副本 |
| 配置 | `bridge.env` 备份 |

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| `fs/write` 是 experimental 接口 | 升级后文件投递失效 | 不固定小版本；投递封装在单方法；升级必须先跑冒烟 |
| 飞书 100 MB 限制 | 超大 SQL 无法上传 | 明确提示拆分/压缩；文档写明 |
| 非特权 LXC 共享内核 | 隔离弱于 VM | 可信用户模型；`pct rollback`；必要时迁移单个用户到 VM |
| LXC 网络暴露 4096 | 同网段可访问 | nftables 仅放行 docker LXC IP；basic auth |
| 未开通用户刷消息 | 通知轰炸 | 冷却时间 + 限频 |
| 上传并发导致内存尖峰 | OOM | 信号量 2、落盘、`mem_limit 768m` |
| 2G 内存同时跑 PG + MySQL | cgroup OOM 杀进程 | 文档建议单库运行；DB 缓冲参数调小；必要时 `pct set --memory 3072` |
| 出站图片/文件超限 | 发不出去 | 10MB/30MB 前置检查，提示压缩或只留路径 |
| 飞书缺上传权限 | 出站 403 | 应用需开「获取与上传图片或文件资源」并发布新版本；`/smoke` 之外加一次出站自检 |
| outbox 只增不减 | 磁盘占用 | 已发送清单去重 + `/outbox clear`；能删则删，不能删提示用户清理 |
| 截图中文变方块 | 交付物不可用 | 需要时安装 `fonts-noto-cjk`（约 200 MB），默认不装 |
| 用户数据库撑满磁盘 | LXC 磁盘满 | 模板设置磁盘上限；文档提示 `pct resize` |
| Docker LXC 在 PVE 上（嵌套） | 内核升级后可能异常 | 该 LXC 已稳定运行其他容器；保持 PVE 与内核版本受控 |
| `users.json` 单文件挂载 | 热加载失效 | 挂目录（§7.4） |

---

## 12. 实施顺序（Build 阶段）

1. `app/users.py` + `UserRegistry`（含 mtime 热加载）+ 单测
2. `app/opencode.py`：`OpenCodeRouter`、`upload_file()`、`list_dir()`、`read_file()` + 单测
3. `app/feishu.py`：`download_resource()`（入站）、`upload_image()`/`upload_file()`/`send_image()`/`send_file()`（出站）
4. `app/bridge.py`：路由接入、SSE 多实例、对账、入站文件/图片、出站 outbox、未开通用户通知、删临时会话
5. `app/permissions.py`、`app/config.py`、`app/slash_commands.py`、`app/main.py`
6. `scripts/smoke-test.sh` + `/smoke` 命令
7. `Dockerfile`、`.dockerignore`、`docker-compose.yml`、`config/users.json.example`
8. `deploy/lxc/*`：`opencode-serve.service`、`opencode.jsonc`、`workspace-AGENTS.md`、`add-user.sh`、`remove-user.sh`
9. `docs/lxc-template-manual.md`（模板手工教程）、`docs/lxc-scripts.md`（脚本用法、示例、排错、回滚）
10. `tests/` 补齐并全部通过
11. `README.md`、`AGENTS.md`、`bridge.env.example`、`backup.sh`
12. 部署到 docker LXC，跑冒烟，验收

---

## 13. 验收定义（DoD）

- [ ] 两个用户同时使用，互不可见、互不干扰
- [ ] 未开通用户首次发消息得到明确提示，管理员收到含 `open_id` 的卡片
- [ ] 未开通用户的消息**不会**触发任何 LXC 创建或 PVE 调用
- [ ] `add-user.sh` 一条命令完成开通（IP 显式传入），且自动跑冒烟
- [ ] 用户 LXC 默认 2048M 内存 / 1024M swap / 24G 磁盘
- [ ] `docs/lxc-scripts.md` 覆盖全部脚本参数与排错
- [ ] `users.json` 更新后 5 秒内生效，无需重启 bridge
- [ ] 用户 A 安装 PostgreSQL 并导入 50 MB SQL，任务不被超时中断
- [ ] 飞书发送 `.sql` 文件，agent 能直接使用，路径正确
- [ ] 飞书发送图片，agent 能读出图片内容
- [ ] agent 把截图写入 `/workspace/outbox/`，飞书能收到图片消息
- [ ] 出站超限（图片 >10MB、文件 >30MB）时给出可执行提示
- [ ] 入站超限（>100MB）时提示拆分或改用链接
- [ ] `rm -rf` 仍触发审批，`sudo`/`service` 不再触发
- [ ] `/temp` 相关命令与文档已移除
- [ ] 用户 LXC 出站走代理：`/etc/opencode/proxy.env` 生效，`bash -lc 'curl https://api.github.com'` 返回 200
- [ ] `/workspace/AGENTS.md` 含「网络与代理」一节
- [ ] `scripts/smoke-test.sh` 与 `/smoke` 均通过
- [ ] `AGENTS.md` 含强制冒烟测试条款
- [ ] 单测、compileall、`bash -n`、`git diff --check` 全部通过
