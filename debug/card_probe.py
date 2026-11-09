"""飞书卡片回调探针。

独立调试脚本，不属于桥接器运行代码。它会：

1. 向 ``FEISHU_TEST_CHAT_ID`` 发送一张测试审批卡；
2. monkey-patch ``lark-oapi`` 的 WebSocket 客户端，打印原始 ``CARD`` 帧；
3. 处理按钮回调并返回 toast。

用于调试飞书卡片按钮回调，以及 ``lark-oapi 1.7.5`` 丢弃 ``CARD`` 帧的问题。
生产环境使用 ``app/ws_patch.py`` 中的补丁，不要运行本脚本。

凭据从环境变量读取，不要硬编码到文件里：

    cd /path/to/feishu-opencode-bridge
    set -a
    source bridge.env
    set +a
    export FEISHU_TEST_CHAT_ID=oc_xxx
    .venv/bin/python debug/card_probe.py
"""

from __future__ import annotations

import base64
import http
import json
import os
import time

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
)
from lark_oapi.core.const import UTF_8
from lark_oapi.core.json import JSON
from lark_oapi.core.log import logger
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)
from lark_oapi.ws.client import (
    Client as WSClient,
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
    _get_by_key,
)
from lark_oapi.ws.enum import MessageType
from lark_oapi.ws.model import Response

CARD = {
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "审批测试"},
        "template": "blue",
    },
    "elements": [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "是否允许执行 `echo hello`？\n\n这是桥接器的卡片回调测试。",
            },
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "允许一次"},
                    "type": "primary",
                    "value": {
                        "action": "permission",
                        "decision": "once",
                        "permission_id": "test-perm-1",
                    },
                },
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "拒绝"},
                    "type": "danger",
                    "value": {
                        "action": "permission",
                        "decision": "reject",
                        "permission_id": "test-perm-1",
                    },
                },
            ],
        },
    ],
}


def send_card() -> None:
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    chat_id = os.environ["FEISHU_TEST_CHAT_ID"]

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(CARD, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    print("send card:", resp.code, resp.msg, flush=True)
    if resp.code == 0:
        print("message_id:", resp.data.message_id, flush=True)


_orig_handle_data_frame = WSClient._handle_data_frame


async def _patched_handle_data_frame(self, frame):
    hs = frame.headers
    type_ = _get_by_key(hs, HEADER_TYPE)
    if type_ == MessageType.CARD.value:
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return
        print("=== RAW CARD FRAME ===", flush=True)
        print(pl.decode(UTF_8), flush=True)
        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(time.time() * 1000))
            result = self._event_handler._do_without_validation(pl)
            end = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            logger.exception("card frame handling failed: %s", e)
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)
        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())
        return
    return await _orig_handle_data_frame(self, frame)


WSClient._handle_data_frame = _patched_handle_data_frame


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    print("=== CARD ACTION ===", flush=True)
    print(JSON.marshal(data, indent=2), flush=True)
    resp = P2CardActionTriggerResponse({})
    resp.toast = {"type": "success", "content": "已收到点击"}
    return resp


def main() -> None:
    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]

    send_card()
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
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
