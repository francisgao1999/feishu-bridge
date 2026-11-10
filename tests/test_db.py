from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
