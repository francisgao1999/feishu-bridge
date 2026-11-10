from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.db import Database  # noqa: E402
except ImportError:  # flattened workspace without app/ package
    from db import Database  # noqa: E402


class DatabaseAgentTest(unittest.TestCase):
    def test_agent_round_trip(self):
        db = Database(":memory:")
        try:
            db.init()
            db.ensure_chat("chat", "user")
            db.add_session(
                "ses", "chat", "user", "title", agent="plan"
            )
            db.update_chat_agent("chat", "plan")
            self.assertEqual(db.get_chat("chat")["agent"], "plan")
            self.assertEqual(db.get_session("ses")["agent"], "plan")
            db.update_session_agent("ses", "build")
            self.assertEqual(db.get_session("ses")["agent"], "build")
        finally:
            db.close()

    def test_migrates_old_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bridge.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE chats (
                    chat_id TEXT PRIMARY KEY,
                    open_id TEXT NOT NULL,
                    session_id TEXT,
                    session_title TEXT,
                    model_provider TEXT,
                    model_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE sessions (
                    session_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    open_id TEXT NOT NULL DEFAULT '',
                    title TEXT,
                    model_provider TEXT,
                    model_id TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.close()

            db = Database(path)
            try:
                db.init()
            finally:
                db.close()

            conn = sqlite3.connect(path)
            try:
                chat_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(chats)")
                }
                session_columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(sessions)")
                }
            finally:
                conn.close()
            self.assertIn("agent", chat_columns)
            self.assertIn("agent", session_columns)


class DatabaseFormTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.db.init()
        self.db.ensure_chat("chat", "user")
        self.db.add_session("ses", "chat", "user", "title")
        self.task_id = self.db.create_task("chat", "ses", None, "prompt")

    def tearDown(self):
        self.db.close()

    def test_form_round_trip(self):
        fields = [
            {
                "key": "q0",
                "type": "string",
                "title": "选择方案",
                "options": [{"value": "A", "label": "A"}],
            }
        ]
        self.assertTrue(
            self.db.add_form(
                "frm_1",
                "ses",
                "chat",
                self.task_id,
                "Questions",
                fields,
            )
        )
        # Duplicate inserts are ignored.
        self.assertFalse(
            self.db.add_form(
                "frm_1", "ses", "chat", self.task_id, "Questions", fields
            )
        )

        form = self.db.get_form("frm_1")
        self.assertIsNotNone(form)
        self.assertEqual(form["status"], "pending")
        self.assertEqual(form["title"], "Questions")
        self.assertEqual(form["task_id"], self.task_id)
        self.assertIsNone(form["message"])
        self.assertEqual(form["interrupted"], 0)
        self.assertEqual(
            [item["key"] for item in json.loads(form["fields"])],
            ["q0"],
        )
        self.assertTrue(self.db.has_pending_forms("ses"))
        self.assertEqual(
            [item["id"] for item in self.db.list_pending_forms()],
            ["frm_1"],
        )

        self.db.update_form(
            "frm_1",
            status="replied",
            answer='{"q0": "A"}',
            message="已提交",
            interrupted=1,
            card_message_id="om_1",
        )
        form = self.db.get_form("frm_1")
        self.assertEqual(form["status"], "replied")
        self.assertEqual(form["answer"], '{"q0": "A"}')
        self.assertEqual(form["message"], "已提交")
        self.assertEqual(form["interrupted"], 1)
        self.assertEqual(form["card_message_id"], "om_1")
        self.assertFalse(self.db.has_pending_forms("ses"))
        self.assertEqual(self.db.list_pending_forms(), [])

    def test_delete_session_removes_forms(self):
        self.db.add_form("frm_1", "ses", "chat", self.task_id, "Q", [])
        self.db.delete_session("ses")
        self.assertIsNone(self.db.get_form("frm_1"))
        self.assertFalse(self.db.has_pending_forms("ses"))

    def test_migrates_forms_message_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "bridge.db")
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE forms (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    task_id INTEGER,
                    title TEXT NOT NULL DEFAULT '',
                    fields TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answer TEXT,
                    card_message_id TEXT,
                    reminded INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.close()

            db = Database(path)
            try:
                db.init()
                db.update_form("frm_old", message="migrated")
                self.assertIsNone(db.get_form("frm_old"))
            finally:
                db.close()

            conn = sqlite3.connect(path)
            try:
                columns = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(forms)")
                }
            finally:
                conn.close()
            self.assertIn("message", columns)
            self.assertIn("interrupted", columns)


if __name__ == "__main__":
    unittest.main()
