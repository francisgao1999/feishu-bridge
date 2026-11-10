# 未来优化 TODO

> 背景：本项目是单用户使用的飞书 → OpenCode 桥接器。以下优化按“减少 babysitting”排序，不追求多租户隔离。
> 原则：先做最小可用版，观察 1–2 周后再决定是否上完整架构。
> 更新时间：2026-11-10

## 优先级总览

| 优先级 | 事项 | 预计投入 | 价值 |
| --- | --- | --- | --- |
| P0 | 治理最小版：MCP 策略 + 审批 always + 归属校验 + 脱敏 | 0.5–1 天 | 堵住最大安全洞，减少审批疲劳 |
| P0 | 常驻最小版：超时 + 重启恢复 + 失败通知 | 0.5–1 天 | 长任务/重启不再全丢 |
| P0 | 记忆最小版：AGENTS.md/MEMORY.md + /remember + 注入 | 0.5–1 天 | 减少重复交代 |
| P1 | 治理：审计日志 + 成本统计 + 预算 | 2–3 天 | 可复盘、可控成本 |
| P1 | 常驻：jobs 表 + worker + 租约 + 并发 | 2–3 天 | 真正的后台任务队列 |
| P1 | 记忆：SQLite FTS5 + session 摘要 + 自动抽取 | 2–3 天 | 跨会话连续性 |
| P2 | 常驻：心跳 + goals/watches + 摘要通知 | 2–3 天 | 主动跟进 |
| P2 | 治理：健康检查 + /pause /resume + 优雅退出 | 2–3 天 | 无人值守更安心 |
| P2 | 记忆：混合检索 + memory MCP 工具 | 2–3 天 | 更准的召回 |
| P3 | 常驻：webhook/RSS/文件监听 + agent 自排任务 | 3–5 天 | 事件驱动 |
| P3 | 治理：恢复演练 + 依赖固定 + prompt injection 加固 | 2–3 天 | 长期可靠性 |
| P3 | 记忆：冲突/遗忘/加密/统计 | 2–3 天 | 记忆治理 |

## 1. 治理（最高优先级）

### P0
- [ ] 补全 OpenCode permissions：
  - [ ] MCP 工具按 `<server>_<tool>` 设置 allow/ask/deny
  - [ ] `webfetch` 域名白名单，未知域名 ask
  - [ ] `websearch` allow
  - [ ] `subagent` 已知 agent allow，未知 ask
  - [ ] `skill` 已知 skill allow，未知 ask
  - [ ] `external_directory` 默认 ask/deny
- [ ] 审批卡增加“总是允许”（OpenCode `always`）
- [ ] 审批回调校验 `permission.chat_id == 点击者 chat_id`
- [ ] 日志/卡片/记忆写入前做 secret 脱敏
- [ ] 高风险操作禁止 `always`
- [ ] 评估用 `policies` 硬拒绝 `sudo`、`rm -rf /`、生产数据库写操作

### P1
- [ ] `audit_log` 表：tool call、permission、task/job 生命周期
- [ ] `/audit` 查询 + JSONL 导出
- [ ] 持久化 `session.usage.updated` 到 `usage` 表
- [ ] `/cost today|7d|30d|session`
- [ ] 每日/每月预算，80% 警告，100% 停止新任务
- [ ] 单任务 token/费用异常检测

### P2
- [ ] 健康检查：OpenCode、Feishu、SQLite、磁盘
- [ ] 告警卡片 + 冷却时间
- [ ] `/pause` / `/resume` kill switch
- [ ] 优雅退出 + 启动恢复
- [ ] 配置自检（必需 env、路径、连通性）

### P3
- [ ] `restore.sh --check` 备份恢复演练
- [ ] 依赖版本固定（requirements、OpenCode、MCP server）
- [ ] prompt injection 防线：研究用只读 agent，执行前审批
- [ ] 每日摘要：任务数、审批数、失败数、花费

## 2. 常驻（后台工作台）

### P0
- [ ] `BRIDGE_TASK_TIMEOUT_SECONDS` 改为可按任务配置，或默认取消 60 分钟硬超时
- [ ] 重启恢复：不要无脑 `mark_stale_tasks`；检查 OpenCode active/idle，能恢复就恢复
- [ ] 任务失败/超时/重启中断时发送明确通知
- [ ] 瞬时错误重试（指数退避）

### P1
- [ ] `jobs` 表：kind、status、priority、attempts、lease、deadline、idempotency_key
- [ ] worker pool + 租约 + 全局并发（`BRIDGE_MAX_CONCURRENT_JOBS`）
- [ ] 同一 session 串行，不同 session 并行
- [ ] `/jobs`、`/job <id>`、`/cancel <id>`、`/retry <id>`
- [ ] 死信队列 + 通知
- [ ] 保留 `tasks` 作为执行记录，`jobs` 作为调度状态

### P2
- [ ] 心跳循环（15–30 分钟）
- [ ] `goals` 表：长期目标、cadence、next_check_at
- [ ] `watches` 表：GitHub/RSS/文件/HTTP/日志条件
- [ ] 摘要通知：urgent 立即，普通合并 digest
- [ ] 安静时间

### P3
- [ ] HTTP webhook `127.0.0.1:8787/trigger`
- [ ] RSS/Atom 轮询
- [ ] 文件监听（watchdog/inotify）
- [ ] bridge MCP server：`schedule_job`、`create_goal`、`watch`、`notify`
- [ ] 指标：jobs 状态/时长/token/费用，`/stats`

## 3. 记忆（跨会话连续性）

### P0
- [ ] `AGENTS.md`：项目约定、部署命令、代码风格
- [ ] `MEMORY.md`：用户偏好、长期事实、决策
- [ ] `/remember <text>` 追加到 `MEMORY.md`
- [ ] 新会话/新任务注入 `AGENTS.md` + `MEMORY.md` 的相关内容
- [ ] 控制注入 token 预算（<3k）

### P1
- [ ] `memories` 表 + `memories_fts`（FTS5）
- [ ] `session_summaries` 表：任务完成/会话空闲时生成摘要
- [ ] 任务完成自动抽取候选记忆
- [ ] 去重/合并/冲突处理
- [ ] `/memory`、`/memory edit/delete`、`/forget`
- [ ] `/summary [session]`

### P2
- [ ] 混合检索：FTS5 + 向量（sqlite-vec 或外部）
- [ ] memory MCP 工具：`memory_search`、`memory_write`、`memory_delete`
- [ ] profile/preferences 自动注入
- [ ] 记忆使用统计（last_used_at、命中率）

### P3
- [ ] 冲突消解 + supersedes
- [ ] TTL/遗忘/归档
- [ ] 记忆加密（可选）
- [ ] `/memory stats`

## 4. 场景与裁剪建议

| 场景 | 优先做 | 可以晚做/不做 |
| --- | --- | --- |
| 远程编码/运维 | 治理 P0、常驻 P0 | 主动触发、向量记忆 |
| 定时聚合/提醒 | 常驻 P0/P1、失败通知 | goals/watches、复杂记忆 |
| 多项目并行 | 记忆 P0、常驻 P1 | 审计/成本（除非多项目烧钱） |
| 临时问答 | 治理 P0 | 常驻/记忆完整版 |
| 无人值守 | 治理 P0/P2、常驻 P2 | 记忆 P2+ |
| 第二大脑 | 记忆 P0/P1 | 常驻 P2+ |

## 5. 验收标准

### 治理
- [ ] 任何 MCP 写操作/发消息/删除都必须审批或 deny
- [ ] 审批卡能看 diff/命令/参数，可以 `always` 并生效
- [ ] `/audit` 能查某任务的所有工具调用和审批
- [ ] `/cost` 能看到今天/本周花费；超预算自动停
- [ ] OpenCode/Feishu 挂了 5 分钟内收到告警
- [ ] 日志/卡片/记忆里没有明文 token

### 常驻
- [ ] 2 小时任务期间可以继续聊天，完成后通知
- [ ] 重启 bridge 后任务不丢
- [ ] goal 到点自动跑，结果去重
- [ ] agent 能自己排后续任务
- [ ] 失败任务有重试/死信/通知

### 记忆
- [ ] 新会话能答“上次部署怎么做的”
- [ ] 说“以后用中文/Python 3.13”后新会话遵守
- [ ] 记忆可查/可改/可删
- [ ] 注入 token 在预算内

## 6. 暂不做

- [ ] 多租户隔离 / 每用户容器（单用户，暂不需要）
- [ ] 完整向量数据库（先用 FTS5 + 静态 MEMORY.md）
- [ ] 复杂 goals/watches（除非确实需要主动发现）
- [ ] 企业级 RBAC / Agent 365 集成
- [ ] 自动学习/微调

## 7. 决策记录

- 2026-11-10：确定单用户前提，隔离暂不考虑；优先治理 → 常驻 → 记忆，每项先做最小可用版。
