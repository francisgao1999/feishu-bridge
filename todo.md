# 未来优化 TODO

> 背景：本项目已从「单用户单服务」改为「每用户一个 PVE LXC」的多用户桥接器：飞书长连接 → bridge 容器 → 按 `open_id` 路由到用户 LXC 的 `opencode serve`。以下优化按「减少 babysitting + 多用户安全/成本」排序，不追求企业级多租户。
> 原则：先做最小可用版，观察 1–2 周后再决定是否上完整架构。
> 更新时间：2026-11-25（按 commit `b5efca8` 核实；188 个单测通过）
> 核实范围：`app/`、`deploy/lxc/`、`scripts/`、`tests/`、`README.md`、`bridge.env.example`。

## 0. 当前已具备（避免重复做）

| 能力 | 状态 | 落点 |
| --- | --- | --- |
| 多用户 LXC 路由 | 已实现 | `app/users.py`、`app/opencode.py` Router |
| `users.json` mtime 热加载 | 已实现 | `app/users.py` |
| 每用户独立 LXC / workspace / `opencode serve` | 已实现 | `deploy/lxc/` |
| 沙箱模式默认放行，仅破坏性操作审批 | 已实现 | `app/permissions.py`、`BRIDGE_SANDBOX_MODE` |
| 定时任务（单次/周期、提醒/任务、stats 推送） | 已实现 | `schedules` 表、`schedule_loop` |
| 同一 session 串行、不同 session 并行 | 已实现 | `tasks` + `get_active_task` |
| 失败/超时/重启中断通知 | 已实现 | `_finish_task_failure`、`_finish_task_interrupted`、`notify_stale_tasks` |
| 运行时任务对账 + idle 消息补发 | 已实现 | `task_watchdog_loop`、`_reconcile_task` |
| 进度卡片实时流式展示 | 已实现 | `app/cards.py`、`app/bridge.py` |
| `/stats` 用量统计 | 已实现 | `app/stats.py` |
| 文件/图片入站与 outbox 出站 | 已实现 | `app/bridge.py`、`app/feishu.py` |
| 模型/Agent 切换 | 已实现 | `/model`、`/agent` |
| 未开通用户通知 + 冷却 | 已实现 | `_notify_unknown_user` |
| 冒烟测试脚本与 `/smoke` | 已实现 | `scripts/smoke-test.sh` |
| 项目 `AGENTS.md` 与 workspace `AGENTS.md` | 已实现 | 仓库根目录、`deploy/lxc/workspace-AGENTS.md` |

## 1. 优先级总览

| 优先级 | 事项 | 预计投入 | 价值 |
| --- | --- | --- | --- |
| P0 | 多用户安全：卡片回调归属校验 + MCP/webfetch 权限 + 脱敏 + 审批 always 限制 | 1–2 天 | 堵住跨用户审批与工具越权 |
| P0 | 多用户成本：每用户 API Key / 配额 | 0.5–1 天 | 防止共享 key 被单个用户烧穿 |
| P0 | 常驻：重启恢复 + 按任务超时 | 1 天 | 长任务/重启不再全丢 |
| P0 | 记忆：按用户 `MEMORY.md` + `/remember` + 注入 | 1 天 | 减少重复交代 |
| P1 | 治理：审计 + 成本 + 预算 + 健康检查 | 2–3 天 | 可复盘、可止损 |
| P1 | 常驻：通用 jobs 队列 + 重试/死信 + 全局并发 | 2–3 天 | 真正的后台任务队列 |
| P1 | 记忆：SQLite FTS5 + session 摘要 + 自动抽取 | 2–3 天 | 跨会话连续性 |
| P1 | 多用户运维：版本清单 + 批量升级 + 用户级权限配置 | 1–2 天 | 多 LXC 不漂移 |
| P2 | 常驻：心跳 + goals/watches + 摘要通知 | 2–3 天 | 主动跟进 |
| P2 | 治理：`/pause` `/resume` + 优雅退出 + 配置自检 | 1–2 天 | 无人值守更安心 |
| P2 | 记忆：混合检索 + memory MCP 工具 | 2–3 天 | 更准的召回 |
| P3 | 常驻：webhook/RSS/文件监听 + agent 自排任务 | 3–5 天 | 事件驱动 |
| P3 | 治理：恢复演练 + 版本固定 + prompt injection 加固 | 2–3 天 | 长期可靠性 |
| P3 | 记忆：冲突/遗忘/加密/统计 | 2–3 天 | 记忆治理 |

## 2. 多用户安全与治理

### P0

- [ ] **卡片回调归属校验**（当前最大缺口）：
  - [ ] 审批回调校验 `permission.chat_id` 的 owner `open_id` == 点击者 `open_id`；同时校验 `permission.task_id` 属于该 chat。
  - [ ] `form_submit` / `form_cancel` 同样校验 form 的 `chat_id` owner。
  - [ ] `model` / `agent` 卡片动作同样校验卡片所属会话的 chat owner。
  - [ ] 当前 `_is_allowed()` 只查全局 `FEISHU_ALLOWED_OPEN_IDS`；多用户模式该值为空，等于不校验。README「审批策略」声称“回调会校验点击者是否在白名单内”，与实现不一致，需补代码或改文档。
- [ ] 补全 OpenCode permissions（当前只配置 `shell` / `read` / `edit`，以及沙箱模式的通配放行）：
  - [ ] MCP 工具按当前 OpenCode 版本的 permission action 名称配置 allow/ask/deny（先用事件或 `opencode debug config` 确认 action 名）。
  - [ ] `webfetch` 域名白名单，未知域名 ask。
  - [ ] `websearch` allow。
  - [ ] `subagent` 已知 agent allow，未知 ask。
  - [ ] `skill` 已知 skill allow，未知 ask。
  - [ ] `external_directory` 默认 ask/deny（沙箱模式可放宽）。
- [ ] 审批卡增加“总是允许”（OpenCode `always`），并限制粒度：
  - [ ] 普通读/写/网页抓取可以 always。
  - [ ] 高风险操作（`rm`、`dd`、`mkfs`、`git push`、`docker rm`、`kubectl delete` 等）禁止 always。
- [ ] 审批卡尽量展示 diff / 命令 / 参数；当前只展示 `action`、`resources`、`save`、`prompt`，没有 diff。
- [ ] 日志/卡片/记忆写入前做 secret 脱敏：tool 参数、prompt、错误信息、`resources` 都可能带 token/密码。
- [ ] 评估用 OpenCode 硬拒绝机制（如 `policies` / deny 规则）处理 `sudo`、`rm -rf /`、生产数据库写操作；当前 `DANGEROUS_SHELL_PATTERNS` 只是尽力而为黑名单，不是安全边界。
- [ ] **每用户 API Key / 配额**：当前模板把同一份 `service.env` 复制到所有用户 LXC，README「用户隔离」里的“独立 API Key”并不成立；共享 key 无法按用户限流、计费或止损。至少支持：
  - [ ] `users.json` 每用户覆盖 `service.env` / API Key。
  - [ ] 每用户 token/费用上限，超限停止新任务。
- [ ] 文档纠偏：README「审批策略」「用户隔离」与实现不一致的地方，先修文档或补代码。

### P1

- [ ] `audit_log` 表：tool call、permission、task/job 生命周期；按 `open_id` / `chat_id` 记录，支持按用户查询。
- [ ] `/audit` 查询 + JSONL 导出。
- [ ] 持久化 `session.usage.updated` 到 `usage` 表；当前 `/stats` 是实时读取 OpenCode，不落库。
- [ ] `/cost today|7d|30d|session|user`；多用户模式下按用户汇总。
- [ ] 每日/每月预算，80% 警告，100% 停止新任务；支持全局和每用户。
- [ ] 单任务 token/费用异常检测。
- [ ] `users.json` 支持每用户权限配置：`sandbox_mode`、`readonly_agents`、额外 allow/ask/deny 规则。
- [ ] 跨用户数据隔离测试：确保 `/sessions`、`/switch`、`/delete`、`/schedule`、`/status`、审批、表单、outbox 都不会串用户。

### P2

- [ ] 健康检查：OpenCode（逐用户）、Feishu、SQLite、磁盘。
- [ ] 告警卡片 + 冷却时间；用户 LXC 掉线时通知管理员。
- [ ] `/pause` / `/resume` 全局 kill switch（当前只有 `/schedule pause|resume`）。
- [ ] 优雅退出 + 启动恢复（`shutdown()` 已优雅退出；启动仍 `mark_stale_tasks` 标记失败，未恢复）。
- [ ] 配置自检（必需 env、路径、连通性、users.json 格式）。

### P3

- [ ] `restore.sh --check` 备份恢复演练；当前只有 `backup.sh`，没有恢复脚本。
- [ ] 依赖版本固定：Python 依赖已固定；OpenCode 版本策略是“不固定、装最新 v2”，至少记录已验证版本矩阵并纳入冒烟。
- [ ] prompt injection 防线：研究用只读 agent，执行前审批；对网页/MCP 返回内容做标记。
- [ ] 每日摘要：任务数、审批数、失败数、花费、按用户拆分。

## 3. 常驻（后台工作台）

### P0

- [ ] **重启恢复**：不要无脑 `mark_stale_tasks`；启动时检查 OpenCode active/idle，能恢复就恢复，至少区分“可恢复/不可恢复”。当前启动仍全部标记失败，只有运行时 `_reconcile_task` 会从 idle 消息补发结果。
- [ ] `BRIDGE_TASK_TIMEOUT_SECONDS` 支持按任务/按用户配置；当前是全局配置，默认 7200 秒（2 小时）。
- [ ] 瞬时错误重试（指数退避）：SSE 已指数退避；飞书为线性重试；OpenCode 请求无重试。

### P1

- [ ] `jobs` 表：kind、status、priority、attempts、lease、deadline、idempotency_key；当前 `schedules` 已有原子 claim，但只覆盖定时任务。
- [ ] worker pool + 租约 + 全局并发（`BRIDGE_MAX_CONCURRENT_JOBS`）；当前只有上传 `Semaphore(2)`，任务没有全局并发上限。
- [ ] `/jobs`、`/job <id>`、`/cancel <id>`、`/retry <id>`。
- [ ] 死信队列 + 通知。
- [ ] 保留 `tasks` 作为执行记录，`jobs` 作为调度状态。
- [ ] 定时任务补强：失败重试、执行超时、连续失败自动暂停、结果去重。

### P2

- [ ] 心跳循环（15–30 分钟）。
- [ ] `goals` 表：长期目标、cadence、next_check_at。
- [ ] `watches` 表：GitHub/RSS/文件/HTTP/日志条件。
- [ ] 摘要通知：urgent 立即，普通合并 digest。
- [ ] 安静时间。

### P3

- [ ] HTTP webhook `127.0.0.1:8787/trigger`。
- [ ] RSS/Atom 轮询。
- [ ] 文件监听（watchdog/inotify）。
- [ ] bridge MCP server：`schedule_job`、`create_goal`、`watch`、`notify`。
- [ ] 指标：jobs 状态/时长/token/费用，`/stats` 扩展。

## 4. 记忆（跨会话连续性）

> 多用户模式下记忆按用户隔离：`MEMORY.md` 放在各用户 LXC 的 `/workspace`，通过该用户的 OpenCode 客户端读写；不要放 bridge 容器的共享目录，也不要用全局单文件注入。SQLite 记忆表按 `open_id` 过滤。

### P0

- [x] `AGENTS.md`：项目约定、部署命令、代码风格。
- [ ] `MEMORY.md`：用户偏好、长期事实、决策（每用户 LXC 的 `/workspace/MEMORY.md`）。
- [ ] `/remember <text>` 追加到该用户的 `MEMORY.md`。
- [ ] 新会话/新任务注入 `AGENTS.md` + `MEMORY.md` 的相关内容（OpenCode 会自动加载项目 `AGENTS.md`；`MEMORY.md` 需由 bridge 用该用户的 OpenCode 客户端 `fs/read` 读取后注入，尚未实现）。
- [ ] 控制注入 token 预算（<3k）。

### P1

- [ ] `memories` 表 + `memories_fts`（FTS5），按 `open_id` 隔离。
- [ ] `session_summaries` 表：任务完成/会话空闲时生成摘要。
- [ ] 任务完成自动抽取候选记忆。
- [ ] 去重/合并/冲突处理。
- [ ] `/memory`、`/memory edit/delete`、`/forget`。
- [ ] `/summary [session]`。

### P2

- [ ] 混合检索：FTS5 + 向量（sqlite-vec 或外部）。
- [ ] memory MCP 工具：`memory_search`、`memory_write`、`memory_delete`。
- [ ] profile/preferences 自动注入。
- [ ] 记忆使用统计（last_used_at、命中率）。

### P3

- [ ] 冲突消解 + supersedes。
- [ ] TTL/遗忘/归档。
- [ ] 记忆加密（可选）。
- [ ] `/memory stats`。

## 5. 多用户运维

### P1

- [ ] 版本清单：记录每个用户 LXC 的 opencode 版本、配置 hash、镜像/模板版本。
- [ ] 批量升级脚本：按用户滚动升级，每台升级后跑冒烟；失败自动回滚。
- [ ] 用户级权限/沙箱配置（见治理 P1）。
- [ ] 用户级成本/配额（见治理 P0/P1）。
- [ ] 备份策略：bridge 数据 + 每用户 LXC 的 `vzdump` 周期；当前 `backup.sh` 只备份 bridge 侧。
- [ ] `remove-user.sh` 已有归档/删除流程；补充“删除前确认 + 数据保留期 + 恢复演练”。

### P2

- [ ] 管理员命令：`/admin users`、`/admin health`、`/admin cost`（仅 `BRIDGE_ADMIN_OPEN_IDS`）。
- [ ] 用户 LXC 健康巡检：逐用户 `/global/health`，异常通知管理员。
- [ ] 数据库保留策略：`tasks` / `permissions` / `forms` 目前只随会话删除；增加按时间归档/清理 + `VACUUM`。
- [ ] outbox 清理：当前只做发送去重和 7 天清理 `outbox_sent`，没有 `/outbox clear` 或磁盘占用告警。

## 6. 场景与裁剪建议

| 场景 | 优先做 | 可以晚做/不做 |
| --- | --- | --- |
| 内部小团队（2–10 人） | 安全 P0、成本 P0/P1、运维 P1 | 复杂 goals/watches、向量记忆 |
| 外部客户/不可信用户 | 每用户 key、审计、prompt injection、必要时每用户 VM | 共享 bridge 的便利功能 |
| 定时聚合/提醒 | 定时任务补强、失败重试/死信 | goals/watches、复杂记忆 |
| 多项目并行 | 记忆 P0、每用户成本 | 审计/成本（除非多项目烧钱） |
| 临时问答 | 安全 P0 | 常驻/记忆完整版 |
| 无人值守 | 安全 P0/P2、健康检查、pause/resume | 记忆 P2+ |
| 第二大脑 | 记忆 P0/P1 | 常驻 P2+ |

## 7. 验收标准

### 多用户安全与治理

- [ ] 用户 A 无法点击/批准用户 B 的审批卡、表单卡、模型/Agent 卡。
- [ ] 任何 MCP 写操作/发消息/删除都必须审批或 deny。
- [ ] 审批卡能看 diff/命令/参数，可以 `always` 并生效；高风险操作不能 always。
- [ ] `/audit` 能查某用户/某任务的所有工具调用和审批。
- [ ] `/cost` 能看到今天/本周/每用户花费；超预算自动停。
- [ ] OpenCode/Feishu/用户 LXC 挂了 5 分钟内收到告警。
- [ ] 日志/卡片/记忆里没有明文 token。
- [ ] 每个用户使用独立 API Key，单个用户超限不影响其他人。

### 常驻

- [x] 2 小时任务期间可以继续聊天，完成后通知（同一聊天可执行 `/status` 等命令，不同聊天可并行任务；结果卡会通知）。
- [ ] 重启 bridge 后任务不丢，或至少能恢复可恢复的任务。
- [ ] 定时任务到点自动跑，失败有重试/死信/通知，结果去重。
- [ ] goal 到点自动跑，结果去重。
- [ ] agent 能自己排后续任务。
- [ ] 全局并发可控，不会因为多用户同时跑任务打爆 bridge/飞书 API。

### 记忆

- [ ] 新会话能答“上次部署怎么做的”。
- [ ] 说“以后用中文/Python 3.13”后新会话遵守。
- [ ] 用户 A 的记忆不会出现在用户 B 的会话里。
- [ ] 记忆可查/可改/可删。
- [ ] 注入 token 在预算内。

## 8. 暂不做

以下事项明确不在当前规划内，保留为记录：

- 每用户独立 bridge 进程（共享 bridge 是当前设计选择；隔离靠每用户 LXC）。
- 完整向量数据库（先用 FTS5 + 静态 `MEMORY.md`）。
- 复杂 goals/watches（除非确实需要主动发现）。
- 企业级 RBAC / Agent 365 集成。
- 自动学习/微调。
- 自动创建/删除 LXC（创建 LXC 永远由管理员手工执行；`remove-user.sh` 是管理员脚本）。

## 9. 决策记录

- 2026-11-10：确定单用户前提，隔离暂不考虑；优先治理 → 常驻 → 记忆，每项先做最小可用版。
- 2026-11-25：架构已改为每用户一个 LXC，单用户前提作废；优先级调整为「多用户安全/成本 → 常驻 → 记忆 → 多用户运维」。补充卡片回调归属校验、每用户 API Key/配额、版本清单/批量升级、数据库保留/outbox 清理等事项。
