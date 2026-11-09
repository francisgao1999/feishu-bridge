from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

log = logging.getLogger(__name__)

RETRYABLE_CODES = {99991400, 99991401, 99991402}


class FeishuError(RuntimeError):
    pass


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )

    def _retry(self, fn, *args: Any, **kwargs: Any):
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = fn(*args, **kwargs)
                if response.code == 0:
                    return response
                last_error = FeishuError(
                    f"Feishu API error {response.code}: {response.msg}"
                )
                if response.code in RETRYABLE_CODES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def send_text(self, chat_id: str, text: str) -> str | None:
        content = json.dumps({"text": text}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(content)
                .build()
            )
            .build()
        )
        response = self._retry(self._client.im.v1.message.create, request)
        return response.data.message_id if response.data else None

    def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        content = json.dumps(card, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(content)
                .build()
            )
            .build()
        )
        response = self._retry(self._client.im.v1.message.create, request)
        return response.data.message_id if response.data else None

    def update_card(self, message_id: str, card: dict[str, Any]) -> None:
        content = json.dumps(card, ensure_ascii=False)
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder().content(content).build()
            )
            .build()
        )
        response = self._retry(self._client.im.v1.message.patch, request)
        log.debug(
            "feishu update_card %s code=%s msg=%s",
            message_id,
            response.code,
            response.msg,
        )

    async def send_text_async(self, chat_id: str, text: str) -> str | None:
        return await asyncio.to_thread(self.send_text, chat_id, text)

    async def send_card_async(
        self, chat_id: str, card: dict[str, Any]
    ) -> str | None:
        return await asyncio.to_thread(self.send_card, chat_id, card)

    async def update_card_async(
        self, message_id: str, card: dict[str, Any]
    ) -> None:
        await asyncio.to_thread(self.update_card, message_id, card)

    def send_long_text(
        self,
        chat_id: str,
        text: str,
        chunk_size: int = 3000,
        max_chunks: int = 5,
    ) -> None:
        chunks = split_text(text, chunk_size)
        for chunk in chunks[:max_chunks]:
            self.send_text(chat_id, chunk)
        if len(chunks) > max_chunks:
            self.send_text(
                chat_id,
                f"（输出过长，已截断，共 {len(chunks)} 段）",
            )

    async def send_long_text_async(
        self,
        chat_id: str,
        text: str,
        chunk_size: int = 3000,
        max_chunks: int = 5,
    ) -> None:
        await asyncio.to_thread(
            self.send_long_text, chat_id, text, chunk_size, max_chunks
        )


def split_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if len(line) > size:
            if current:
                chunks.append("".join(current))
                current = []
                current_len = 0
            for index in range(0, len(line), size):
                chunks.append(line[index : index + size])
            continue

        if current_len + len(line) > size and current:
            chunks.append("".join(current))
            current = []
            current_len = 0

        current.append(line)
        current_len += len(line)

    if current:
        chunks.append("".join(current))
    return chunks
