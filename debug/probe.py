"""飞书长连接消息探针。

独立调试脚本，不属于桥接器运行代码。用于验证飞书应用是否成功建立
WebSocket 长连接，以及 ``im.message.receive_v1`` 事件是否正常推送。

凭据从环境变量读取，不要硬编码到文件里：

    set -a
    source /home/oc/feishu-opencode-bridge/bridge.env
    set +a
    /home/oc/feishu-opencode-bridge/.venv/bin/python debug/probe.py
"""

from __future__ import annotations

import os

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1


def on_message(data: P2ImMessageReceiveV1) -> None:
    print("=== MESSAGE ===", flush=True)
    print(lark.JSON.marshal(data, indent=2), flush=True)


def main() -> None:
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    cli = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    cli.start()


if __name__ == "__main__":
    main()
