from __future__ import annotations

import base64
import http
import time

from lark_oapi.core.const import UTF_8
from lark_oapi.core.json import JSON
from lark_oapi.core.log import logger
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

_original_handle_data_frame = WSClient._handle_data_frame


async def _handle_data_frame_with_card(self, frame):
    headers = frame.headers
    frame_type = _get_by_key(headers, HEADER_TYPE)
    if frame_type != MessageType.CARD.value:
        return await _original_handle_data_frame(self, frame)

    message_id = _get_by_key(headers, HEADER_MESSAGE_ID)
    total = _get_by_key(headers, HEADER_SUM)
    sequence = _get_by_key(headers, HEADER_SEQ)
    payload = frame.payload
    if int(total) > 1:
        payload = self._combine(
            message_id, int(total), int(sequence), payload
        )
        if payload is None:
            return

    response = Response(code=http.HTTPStatus.OK)
    try:
        start = int(round(time.time() * 1000))
        result = self._event_handler._do_without_validation(payload)
        end = int(round(time.time() * 1000))
        header = headers.add()
        header.key = HEADER_BIZ_RT
        header.value = str(end - start)
        if result is not None:
            response.data = base64.b64encode(
                JSON.marshal(result).encode(UTF_8)
            )
    except Exception as exc:
        logger.exception("card frame handling failed: %s", exc)
        response = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

    frame.payload = JSON.marshal(response).encode(UTF_8)
    await self._write_message(frame.SerializeToString())


def patch_ws_client() -> None:
    if getattr(WSClient, "_feishu_bridge_card_patch", False):
        return
    WSClient._handle_data_frame = _handle_data_frame_with_card
    WSClient._feishu_bridge_card_patch = True
