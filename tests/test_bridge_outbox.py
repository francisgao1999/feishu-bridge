from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

from bridge import Bridge  # noqa: E402
from config import load_config  # noqa: E402
from db import Database  # noqa: E402
from users import UserRegistry  # noqa: E402


class FakeOC:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []
        self.files: dict[str, bytes] = {}
        self.uploaded: list[tuple[str, str, str]] = []
        self.downloaded: list[tuple[str, str, str]] = []

    async def list_models(self, open_id: str) -> list[dict[str, str]]:
        return [{"id": "m1"}]

    async def list_dir(
        self, open_id: str, path: str, directory: str | None = None
    ) -> list[dict[str, str]]:
        return self.entries

    async def upload_file(
        self,
        open_id: str,
        local_path: str,
        remote_path: str,
        directory: str,
    ) -> dict:
        data = Path(local_path).read_bytes()
        self.files[remote_path] = data
        self.uploaded.append((open_id, remote_path, directory))
        return {}

    async def download_file(
        self,
        open_id: str,
        remote_path: str,
        local_path: str,
        directory: str,
    ) -> int:
        data = self.files.get(remote_path, b"")
        Path(local_path).write_bytes(data)
        self.downloaded.append((open_id, remote_path, directory))
        return len(data)


class FakeFeishu:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def upload_image_async(self, path: str) -> str:
        self.calls.append(("upload_image", path))
        return "img_key"

    async def send_image_async(self, chat_id: str, image_key: str) -> None:
        self.calls.append(("send_image", chat_id, image_key))

    async def upload_file_async(self, path: str, name: str) -> str:
        self.calls.append(("upload_file", path, name))
        return "file_key"

    async def send_file_async(self, chat_id: str, file_key: str) -> None:
        self.calls.append(("send_file", chat_id, file_key))

    async def send_text_async(self, chat_id: str, text: str) -> None:
        self.calls.append(("send_text", chat_id, text))


class BridgeOutboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        env = {
            "BRIDGE_HOME": str(base),
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "OPENCODE_PASSWORD": "pw",
            "BRIDGE_ADMIN_OPEN_IDS": "ou_admin",
            "BRIDGE_UPLOAD_TMP_DIR": str(base / "uploads"),
            "BRIDGE_OUTBOX_SUBDIR": "outbox",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = load_config()
        self.db = Database(str(base / "bridge.db"))
        self.db.init()
        self.addCleanup(self.db.close)
        users_path = base / "users.json"
        users_path.write_text(
            '[{"open_id": "ou_alice", "slug": "alice", '
            '"url": "http://10.0.0.11:4096", "password": "pw", '
            '"workspace": "/workspace"}]',
            encoding="utf-8",
        )
        self.users = UserRegistry(str(users_path))
        self.users.load()
        self.oc = FakeOC()
        self.feishu = FakeFeishu()
        self.bridge = Bridge(
            self.cfg,
            self.db,
            self.oc,
            self.oc,
            self.feishu,
            users=self.users,
        )

    def _rebuild(self, cfg) -> Bridge:
        return Bridge(
            cfg, self.db, self.oc, self.oc, self.feishu, users=self.users
        )

    def _call_kinds(self) -> list[str]:
        return [call[0] for call in self.feishu.calls]

    def test_deliver_outbox_image(self) -> None:
        self.oc.entries = [{"type": "file", "path": "a.png"}]
        self.oc.files["/workspace/outbox/a.png"] = (
            b"\x89PNG\r\n\x1a\n" + b"x" * 16
        )
        asyncio.run(self.bridge._deliver_outbox("chat1", "ou_alice"))
        kinds = self._call_kinds()
        self.assertIn("upload_image", kinds)
        self.assertIn("send_image", kinds)
        self.assertNotIn("upload_file", kinds)

    def test_deliver_outbox_regular_file(self) -> None:
        self.oc.entries = [{"type": "file", "path": "report.txt"}]
        self.oc.files["/workspace/outbox/report.txt"] = b"hello"
        asyncio.run(self.bridge._deliver_outbox("chat1", "ou_alice"))
        kinds = self._call_kinds()
        self.assertIn("upload_file", kinds)
        self.assertIn("send_file", kinds)
        self.assertNotIn("upload_image", kinds)

    def test_deliver_outbox_dedupes_same_content(self) -> None:
        self.oc.entries = [{"type": "file", "path": "report.txt"}]
        self.oc.files["/workspace/outbox/report.txt"] = b"hello"
        asyncio.run(self.bridge._deliver_outbox("chat1", "ou_alice"))
        asyncio.run(self.bridge._deliver_outbox("chat1", "ou_alice"))
        self.assertEqual(self._call_kinds().count("send_file"), 1)
        self.assertEqual(len(self.oc.downloaded), 2)

    def test_deliver_outbox_oversized_file_warns(self) -> None:
        cfg = dataclasses.replace(self.cfg, max_outbox_file_bytes=1)
        bridge = self._rebuild(cfg)
        self.oc.entries = [{"type": "file", "path": "big.bin"}]
        self.oc.files["/workspace/outbox/big.bin"] = b"too big"
        asyncio.run(bridge._deliver_outbox("chat1", "ou_alice"))
        kinds = self._call_kinds()
        self.assertIn("send_text", kinds)
        self.assertNotIn("send_file", kinds)
        text = [c[2] for c in self.feishu.calls if c[0] == "send_text"][0]
        self.assertIn("big.bin", text)
        # 同一份超大文件只提醒一次，避免每次任务结束都重复提示。
        self.feishu.calls.clear()
        asyncio.run(bridge._deliver_outbox("chat1", "ou_alice"))
        self.assertEqual(self._call_kinds(), [])

    def test_deliver_outbox_skips_directories(self) -> None:
        self.oc.entries = [{"type": "directory", "path": "sub"}]
        asyncio.run(self.bridge._deliver_outbox("chat1", "ou_alice"))
        self.assertEqual(self.feishu.calls, [])

    def test_notify_unknown_user_and_cooldown(self) -> None:
        asyncio.run(
            self.bridge._notify_unknown_user("chat1", "ou_unknown")
        )
        texts = [c for c in self.feishu.calls if c[0] == "send_text"]
        self.assertEqual(len(texts), 2)
        self.assertEqual(texts[0][1], "chat1")
        self.assertEqual(texts[1][1], "ou_admin")

        self.feishu.calls.clear()
        asyncio.run(
            self.bridge._notify_unknown_user("chat1", "ou_unknown")
        )
        texts = [c for c in self.feishu.calls if c[0] == "send_text"]
        # 用户提示每次都发，管理员通知在冷却期内不再发。
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0][1], "chat1")

    def _message(self, message_type: str, content: dict) -> SimpleNamespace:
        return SimpleNamespace(
            message_type=message_type,
            content=json.dumps(content, ensure_ascii=False),
        )

    def test_extract_attachments_from_image_post(self) -> None:
        message = self._message(
            "post",
            {
                "title": "",
                "content": [
                    [
                        {
                            "tag": "img",
                            "image_key": "img_v3_123",
                            "width": 1280,
                            "height": 720,
                        }
                    ],
                    [{"tag": "text", "text": "描述一下这张图片", "style": []}],
                ],
            },
        )
        self.assertEqual(
            self.bridge._extract_attachments(message),
            [{"type": "image", "file_key": "img_v3_123"}],
        )

    def test_extract_attachments_from_file_post(self) -> None:
        message = self._message(
            "post",
            {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "这个文件是什么内容？", "style": []}]
                ],
                "files": [
                    {
                        "file_key": "file_v3_123",
                        "file_name": "高昌端个人简历.docx",
                        "is_folder": False,
                    }
                ],
            },
        )
        self.assertEqual(
            self.bridge._extract_attachments(message),
            [
                {
                    "type": "file",
                    "file_key": "file_v3_123",
                    "file_name": "高昌端个人简历.docx",
                }
            ],
        )

    def test_extract_attachments_from_pure_image_and_file(self) -> None:
        image = self._message("image", {"image_key": "img_v3_456"})
        self.assertEqual(
            self.bridge._extract_attachments(image),
            [{"type": "image", "file_key": "img_v3_456"}],
        )
        file_message = self._message(
            "file", {"file_key": "file_v3_456", "file_name": "a.txt"}
        )
        self.assertEqual(
            self.bridge._extract_attachments(file_message),
            [
                {
                    "type": "file",
                    "file_key": "file_v3_456",
                    "file_name": "a.txt",
                }
            ],
        )

    def test_extract_attachments_dedupes_keys(self) -> None:
        message = self._message(
            "post",
            {
                "content": [
                    [
                        {"tag": "img", "image_key": "img_v3_dup"},
                        {"tag": "img", "image_key": "img_v3_dup"},
                    ]
                ],
            },
        )
        self.assertEqual(
            self.bridge._extract_attachments(message),
            [{"type": "image", "file_key": "img_v3_dup"}],
        )

    def test_handle_inbound_downloads_attachment_before_text(self) -> None:
        calls: list[str] = []

        async def fake_attachment(*args, **kwargs) -> None:
            calls.append("attachment")

        async def fake_text(*args, **kwargs) -> None:
            calls.append("text")

        with mock.patch.object(
            self.bridge, "_handle_attachment", new=fake_attachment
        ), mock.patch.object(self.bridge, "_handle_text", new=fake_text):
            asyncio.run(
                self.bridge._handle_inbound(
                    "chat1",
                    "ou_alice",
                    "om_1",
                    "描述一下这张图片",
                    [{"type": "image", "file_key": "img_v3_123"}],
                )
            )
        self.assertEqual(calls, ["attachment", "text"])

    def test_handle_inbound_skips_empty_text(self) -> None:
        calls: list[str] = []

        async def fake_attachment(*args, **kwargs) -> None:
            calls.append("attachment")

        async def fake_text(*args, **kwargs) -> None:
            calls.append("text")

        with mock.patch.object(
            self.bridge, "_handle_attachment", new=fake_attachment
        ), mock.patch.object(self.bridge, "_handle_text", new=fake_text):
            asyncio.run(
                self.bridge._handle_inbound(
                    "chat1",
                    "ou_alice",
                    "om_1",
                    "   ",
                    [{"type": "image", "file_key": "img_v3_123"}],
                )
            )
        self.assertEqual(calls, ["attachment"])

    def test_attachment_prefix_formats_and_clears(self) -> None:
        self.bridge._pending_attachments["chat1"] = [
            {
                "path": "/workspace/uploads/a.txt",
                "name": "a.txt",
                "size": 10,
                "sha256": "abcdef0123456789",
            }
        ]
        prefix = self.bridge._attachment_prefix("chat1")
        self.assertIn("/workspace/uploads/a.txt", prefix)
        self.assertIn("10 B", prefix)
        self.assertIn("abcdef0123456789", prefix)
        self.assertEqual(self.bridge._attachment_prefix("chat1"), "")

    def test_cmd_smoke_roundtrip(self) -> None:
        asyncio.run(self.bridge._cmd_smoke("chat1", "ou_alice"))
        text = [c[2] for c in self.feishu.calls if c[0] == "send_text"][0]
        self.assertIn("✅ 连接 OpenCode 成功", text)
        self.assertIn("✅ 文件写入/读取一致", text)
        self.assertIn("结果: 通过", text)
        self.assertEqual(len(self.oc.uploaded), 1)
        self.assertEqual(len(self.oc.downloaded), 1)


if __name__ == "__main__":
    unittest.main()
