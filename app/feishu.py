from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

log = logging.getLogger(__name__)

RETRYABLE_CODES = {99991400, 99991401, 99991402}

# Feishu's documented hard limit for message resources.
FEISHU_MAX_RESOURCE_BYTES = 100 * 1024 * 1024
FEISHU_TOO_LARGE_CODE = 234037


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

    # ------------------------------------------------------------------
    # inbound resources (user -> agent)
    # ------------------------------------------------------------------
    def download_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        dest_path: str,
        *,
        max_bytes: int = FEISHU_MAX_RESOURCE_BYTES,
    ) -> str:
        """Download a message resource to ``dest_path``.

        ``resource_type`` is ``"image"`` or ``"file"``. Returns the file name
        reported by Feishu (falls back to the destination basename).
        """
        request = (
            GetMessageResourceRequest.builder()
            .message_id(message_id)
            .file_key(file_key)
            .type(resource_type)
            .build()
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = self._client.im.v1.message_resource.get(request)
                raw = getattr(response, "raw", None)
                status = getattr(raw, "status_code", None)
                handle = getattr(response, "file", None)
                if response.code == 0 and handle is not None:
                    handle.seek(0, os.SEEK_END)
                    size = handle.tell()
                    handle.seek(0)
                    if size > max_bytes:
                        raise FeishuError(
                            f"文件过大（{size} 字节，上限 {max_bytes} 字节），"
                            f"飞书拒绝下载（错误码 {FEISHU_TOO_LARGE_CODE}）"
                        )
                    written = 0
                    with open(dest_path, "wb") as out:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            written += len(chunk)
                    log.info(
                        "downloaded %s %s -> %s (%d bytes)",
                        resource_type,
                        file_key,
                        dest_path,
                        written,
                    )
                    return response.file_name or os.path.basename(dest_path)
                last_error = FeishuError(
                    f"Feishu download error code={response.code} "
                    f"status={status} msg={response.msg}"
                )
                if response.code in RETRYABLE_CODES:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                break
            except FeishuError:
                raise
            except Exception as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def upload_image(self, path: str) -> str:
        """Upload a local image and return its ``image_key``."""
        with open(path, "rb") as handle:
            request = (
                CreateImageRequest.builder()
                .request_body(
                    CreateImageRequestBody.builder()
                    .image(handle)
                    .image_type("message")
                    .build()
                )
                .build()
            )
            response = self._retry(self._client.im.v1.image.create, request)
        key = response.data.image_key if response.data else None
        if not key:
            raise FeishuError("Feishu image upload returned no image_key")
        return key

    def upload_file(self, path: str, file_name: str | None = None) -> str:
        """Upload a local file and return its ``file_key``."""
        name = file_name or os.path.basename(path)
        with open(path, "rb") as handle:
            request = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file(handle)
                    .file_type("stream")
                    .file_name(name)
                    .build()
                )
                .build()
            )
            response = self._retry(self._client.im.v1.file.create, request)
        key = response.data.file_key if response.data else None
        if not key:
            raise FeishuError("Feishu file upload returned no file_key")
        return key

    # ------------------------------------------------------------------
    # outbound resources (agent -> user)
    # ------------------------------------------------------------------
    def send_image(self, chat_id: str, image_key: str) -> str | None:
        content = json.dumps({"image_key": image_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("image")
                .content(content)
                .build()
            )
            .build()
        )
        response = self._retry(self._client.im.v1.message.create, request)
        return response.data.message_id if response.data else None

    def send_file(self, chat_id: str, file_key: str) -> str | None:
        content = json.dumps({"file_key": file_key}, ensure_ascii=False)
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("file")
                .content(content)
                .build()
            )
            .build()
        )
        response = self._retry(self._client.im.v1.message.create, request)
        return response.data.message_id if response.data else None

    async def download_resource_async(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        dest_path: str,
        *,
        max_bytes: int = FEISHU_MAX_RESOURCE_BYTES,
    ) -> str:
        return await asyncio.to_thread(
            self.download_resource,
            message_id,
            file_key,
            resource_type,
            dest_path,
            max_bytes=max_bytes,
        )

    async def upload_image_async(self, path: str) -> str:
        return await asyncio.to_thread(self.upload_image, path)

    async def upload_file_async(
        self, path: str, file_name: str | None = None
    ) -> str:
        return await asyncio.to_thread(self.upload_file, path, file_name)

    async def send_image_async(
        self, chat_id: str, image_key: str
    ) -> str | None:
        return await asyncio.to_thread(self.send_image, chat_id, image_key)

    async def send_file_async(
        self, chat_id: str, file_key: str
    ) -> str | None:
        return await asyncio.to_thread(self.send_file, chat_id, file_key)

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
