from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

from bridge import Bridge, _TaskProgress  # noqa: E402
from config import load_config  # noqa: E402
from db import Database  # noqa: E402


class FakeOC:
    def __init__(self) -> None:
        self.shell_chunks: list[dict] = []
        self.shell_calls: list[tuple] = []

    async def shell_output(
        self,
        session_id: str,
        shell_id: str,
        *,
        directory: str | None = None,
        cursor: int = 0,
        limit: int = 65536,
    ) -> dict:
        self.shell_calls.append((session_id, shell_id, directory, cursor, limit))
        if self.shell_chunks:
            return self.shell_chunks.pop(0)
        return {"output": "", "cursor": cursor, "size": cursor, "truncated": False}

    async def list_dir(
        self, open_id: str, path: str, directory: str | None = None
    ) -> list[dict]:
        return []


class FakeFeishu:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict]] = []
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, dict]] = []

    async def update_card_async(self, message_id: str, card: dict) -> None:
        self.updates.append((message_id, card))

    async def send_text_async(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))

    async def send_card_async(self, chat_id: str, card: dict) -> str:
        self.cards.append((chat_id, card))
        return "result-card"

    async def send_long_text_async(self, chat_id: str, text: str) -> None:
        self.texts.append((chat_id, text))


def _card_text(card: dict) -> str:
    parts: list[str] = []
    for element in card.get("elements") or []:
        text = element.get("text") or {}
        if text.get("content"):
            parts.append(str(text["content"]))
        for child in element.get("elements") or []:
            child_text = child.get("text") or {}
            if child_text.get("content"):
                parts.append(str(child_text["content"]))
    return "\n".join(parts)


class TaskProgressStateTest(unittest.TestCase):
    def test_output_is_kept_in_tail_window(self):
        state = _TaskProgress()
        state.add_output("one\ntwo\nthree\n")
        window = state.window(3)
        self.assertEqual(window, ["  one", "  two", "  three"])

    def test_split_chunk_keeps_line_whole(self):
        state = _TaskProgress()
        state.add_output("hel")
        state.add_output("lo\n")
        self.assertEqual(state.window(3), ["  hello"])
        state.flush_output()
        self.assertEqual(state.lines, ["  hello"])

    def test_pending_line_is_shown_before_newline(self):
        state = _TaskProgress()
        state.add_output("running 10%")
        self.assertEqual(state.window(3), ["  running 10%"])

    def test_trim_keeps_memory_bounded(self):
        state = _TaskProgress()
        for i in range(300):
            state.add_output(f"line {i}\n")
        self.assertLessEqual(len(state.lines), 200)
        self.assertTrue(state.truncated)
        self.assertIn("line 299", state.full_log())


class BridgeProgressEventTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        env = {
            "BRIDGE_HOME": str(base),
            "FEISHU_APP_ID": "app",
            "FEISHU_APP_SECRET": "secret",
            "OPENCODE_PASSWORD": "pw",
            "BRIDGE_PROGRESS_MIN_INTERVAL": "1",
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = load_config()
        self.db = Database(str(base / "bridge.db"))
        self.db.init()
        self.addCleanup(self.db.close)
        self.db.add_session("sess1", "chat1", "ou_alice", "title")
        self.oc = FakeOC()
        self.feishu = FakeFeishu()
        self.bridge = Bridge(
            self.cfg, self.db, self.oc, self.oc, self.feishu
        )
        self.task_id = self.db.create_task("chat1", "sess1", "m1", "do it")
        self.db.update_task(self.task_id, card_message_id="card1")
        self.db.mark_task_running(self.task_id)

    async def _dispatch(self, event: dict) -> None:
        await self.bridge._handle_event(event)
        # Let the throttled background flush run.
        await asyncio.sleep(0.05)

    def test_execution_started_renders_log(self) -> None:
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.execution.started",
                    "data": {"sessionID": "sess1"},
                }
            )
        )
        self.assertTrue(self.feishu.updates)
        text = _card_text(self.feishu.updates[-1][1])
        self.assertIn("开始执行", text)

    def test_tool_call_and_shell_output_are_rendered(self) -> None:
        self.oc.shell_chunks = [
            {
                "output": "hello\nworld\n",
                "cursor": 12,
                "size": 12,
                "truncated": False,
            }
        ]
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.tool.input.started",
                    "data": {
                        "sessionID": "sess1",
                        "id": "call1",
                        "name": "bash",
                    },
                }
            )
        )
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.tool.called",
                    "data": {
                        "sessionID": "sess1",
                        "id": "call1",
                        "input": {"command": "echo hi"},
                    },
                }
            )
        )
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.shell.started",
                    "data": {
                        "sessionID": "sess1",
                        "shell": {
                            "id": "sh1",
                            "command": "echo hi",
                            "cwd": "/workspace",
                            "status": "running",
                        },
                    },
                    "location": {"directory": "/workspace"},
                }
            )
        )
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.tool.progress",
                    "data": {
                        "sessionID": "sess1",
                        "id": "call1",
                        "metadata": {"shellID": "sh1"},
                    },
                    "location": {"directory": "/workspace"},
                }
            )
        )
        # 节流间隔内事件只标脏，手动触发一次刷新来验证 shell 输出。
        asyncio.run(self.bridge._flush_progress(self.task_id))
        text = _card_text(self.feishu.updates[-1][1])
        self.assertIn("bash", text)
        self.assertIn("hello", text)
        self.assertTrue(self.oc.shell_calls)

    def test_shell_ended_appends_only_unread_suffix(self) -> None:
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.shell.started",
                    "data": {
                        "sessionID": "sess1",
                        "shell": {
                            "id": "sh1",
                            "command": "echo hi",
                            "cwd": "/workspace",
                            "status": "running",
                        },
                    },
                    "location": {"directory": "/workspace"},
                }
            )
        )
        state = self.bridge._progress_state(self.task_id)
        state.cursor = 5
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.shell.ended",
                    "data": {
                        "sessionID": "sess1",
                        "shell": {
                            "id": "sh1",
                            "command": "echo hi",
                            "cwd": "/workspace",
                            "status": "exited",
                            "exit": 0,
                        },
                        "output": {
                            "output": "hello world\n",
                            "cursor": 12,
                            "size": 12,
                            "truncated": False,
                        },
                    },
                }
            )
        )
        self.assertIsNone(state.shell_id)
        asyncio.run(self.bridge._flush_progress(self.task_id))
        text = _card_text(self.feishu.updates[-1][1])
        self.assertIn("world", text)
        # 已读过的 "hello" 前缀不应重复追加。
        self.assertNotIn("hello world", text)

    def test_execution_succeeded_clears_state(self) -> None:
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.execution.started",
                    "data": {"sessionID": "sess1"},
                }
            )
        )
        asyncio.run(
            self._dispatch(
                {
                    "type": "session.execution.succeeded",
                    "data": {"sessionID": "sess1"},
                }
            )
        )
        self.assertNotIn(self.task_id, self.bridge._progress)
        self.assertTrue(self.feishu.cards)


if __name__ == "__main__":
    unittest.main()
