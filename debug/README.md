# 调试工具

这里的脚本是独立调试工具，不属于桥接器运行代码，`app/main.py` 不会加载它们。

## 凭据

脚本从环境变量读取凭据，**不要**把 App ID / App Secret / chat_id 硬编码到文件里：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_TEST_CHAT_ID`（仅 `card_probe.py` 需要）

可以临时从 `bridge.env` 导入：

```bash
cd /home/oc/feishu-opencode-bridge
set -a
source bridge.env
set +a
export FEISHU_TEST_CHAT_ID=oc_xxx
```

## probe.py

验证飞书长连接和 `im.message.receive_v1` 事件订阅：

```bash
.venv/bin/python debug/probe.py
```

启动后给机器人发一条消息，终端会打印原始事件 JSON。

## card_probe.py

验证卡片按钮回调：

```bash
.venv/bin/python debug/card_probe.py
```

脚本会向 `FEISHU_TEST_CHAT_ID` 发送一张测试审批卡，并打印原始 `CARD` 帧和按钮回调。
它 monkey-patch 了 `lark-oapi` 的 WebSocket 客户端，仅用于调试；生产环境使用
`app/ws_patch.py` 中的补丁。

## 安全

- 不要把 `bridge.env`、`service.env` 或任何真实密钥提交到 Git。
- 如果密钥曾经被硬编码或分享，请在飞书开放平台轮换 App Secret。
