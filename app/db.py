from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    open_id TEXT NOT NULL,
    session_id TEXT,
    session_title TEXT,
    model_provider TEXT,
    model_id TEXT,
    agent TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    open_id TEXT NOT NULL DEFAULT '',
    title TEXT,
    model_provider TEXT,
    model_id TEXT,
    agent TEXT,
    temporary INTEGER NOT NULL DEFAULT 0,
    temp_dir TEXT,
    expires_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    user_message_id TEXT,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    final_text TEXT,
    error TEXT,
    card_message_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    task_id INTEGER,
    chat_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resources TEXT NOT NULL,
    save TEXT NOT NULL,
    source TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    card_message_id TEXT,
    reminded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forms (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    task_id INTEGER,
    title TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    message TEXT,
    interrupted INTEGER NOT NULL DEFAULT 0,
    card_message_id TEXT,
    reminded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    open_id TEXT NOT NULL,
    session_id TEXT,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    kind TEXT NOT NULL,
    time_of_day TEXT NOT NULL,
    weekday INTEGER,
    mode TEXT NOT NULL DEFAULT 'task',
    enabled INTEGER NOT NULL DEFAULT 1,
    next_run_at INTEGER NOT NULL,
    last_run_at INTEGER,
    last_task_id INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_chat
    ON sessions(chat_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_tasks_session_status
    ON tasks(session_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_chat
    ON tasks(chat_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_permissions_status
    ON permissions(status);
CREATE INDEX IF NOT EXISTS idx_forms_status
    ON forms(status);
CREATE INDEX IF NOT EXISTS idx_forms_session
    ON forms(session_id);
CREATE INDEX IF NOT EXISTS idx_schedules_due
    ON schedules(enabled, next_run_at);
"""


class Database:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def init(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(chats)")
            }
            if "model_provider" not in columns:
                self._conn.execute(
                    "ALTER TABLE chats ADD COLUMN model_provider TEXT"
                )
            if "model_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE chats ADD COLUMN model_id TEXT"
                )
            if "agent" not in columns:
                self._conn.execute(
                    "ALTER TABLE chats ADD COLUMN agent TEXT"
                )
            session_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            if "agent" not in session_columns:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN agent TEXT"
                )
            if "temporary" not in session_columns:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN temporary INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            if "temp_dir" not in session_columns:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN temp_dir TEXT"
                )
            if "expires_at" not in session_columns:
                self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN expires_at INTEGER"
                )
            schedule_columns = {
                row["name"]
                for row in self._conn.execute(
                    "PRAGMA table_info(schedules)"
                )
            }
            if "mode" not in schedule_columns:
                self._conn.execute(
                    "ALTER TABLE schedules ADD COLUMN mode TEXT "
                    "NOT NULL DEFAULT 'task'"
                )
            form_columns = {
                row["name"]
                for row in self._conn.execute("PRAGMA table_info(forms)")
            }
            if "message" not in form_columns:
                self._conn.execute(
                    "ALTER TABLE forms ADD COLUMN message TEXT"
                )
            if "interrupted" not in form_columns:
                self._conn.execute(
                    "ALTER TABLE forms ADD COLUMN interrupted INTEGER "
                    "NOT NULL DEFAULT 0"
                )
            self._migrate_sessions()

    def _migrate_sessions(self) -> None:
        """Backfill the sessions table from chats and historical tasks."""
        self._conn.execute(
            """
            INSERT OR IGNORE INTO sessions(
                session_id, chat_id, open_id, title,
                model_provider, model_id, agent, created_at, updated_at
            )
            SELECT session_id, chat_id, open_id, session_title,
                   model_provider, model_id, agent, created_at, updated_at
            FROM chats
            WHERE session_id IS NOT NULL
            """
        )
        rows = self._conn.execute(
            """
            SELECT t.session_id, t.chat_id,
                   MIN(t.id) AS first_id,
                   MIN(t.created_at) AS created_at,
                   MAX(t.updated_at) AS updated_at
            FROM tasks t
            LEFT JOIN sessions s ON s.session_id = t.session_id
            WHERE s.session_id IS NULL
            GROUP BY t.session_id, t.chat_id
            """
        ).fetchall()
        for row in rows:
            first = self._conn.execute(
                "SELECT prompt FROM tasks WHERE id=?", (row["first_id"],)
            ).fetchone()
            chat = self._conn.execute(
                "SELECT open_id, model_provider, model_id, agent FROM chats "
                "WHERE chat_id=?",
                (row["chat_id"],),
            ).fetchone()
            title = (
                " ".join(first["prompt"].split())[:40]
                if first
                else "历史会话"
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO sessions(
                    session_id, chat_id, open_id, title,
                    model_provider, model_id, agent, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["session_id"],
                    row["chat_id"],
                    chat["open_id"] if chat else "",
                    title,
                    chat["model_provider"] if chat else None,
                    chat["model_id"] if chat else None,
                    chat["agent"] if chat else None,
                    row["created_at"],
                    row["updated_at"],
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # chats
    # ------------------------------------------------------------------
    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_chat_by_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT session_id, chat_id, open_id, title AS session_title,
                       model_provider, model_id, agent
                FROM sessions WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
            if row:
                return dict(row)
            row = self._conn.execute(
                "SELECT * FROM chats WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def ensure_chat(self, chat_id: str, open_id: str) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO chats(
                    chat_id, open_id, session_id, session_title,
                    created_at, updated_at
                )
                VALUES (?, ?, NULL, NULL, ?, ?)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (chat_id, open_id, now, now),
            )

    def upsert_chat(
        self,
        chat_id: str,
        open_id: str,
        session_id: str,
        session_title: str,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO chats(chat_id, open_id, session_id, session_title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    open_id=excluded.open_id,
                    session_id=excluded.session_id,
                    session_title=excluded.session_title,
                    updated_at=excluded.updated_at
                """,
                (chat_id, open_id, session_id, session_title, now, now),
            )

    def get_session_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT session_id FROM chats WHERE session_id IS NOT NULL
                UNION
                SELECT session_id FROM sessions
                """
            ).fetchall()
            return {row["session_id"] for row in rows}

    # ------------------------------------------------------------------
    # sessions
    # ------------------------------------------------------------------
    def add_session(
        self,
        session_id: str,
        chat_id: str,
        open_id: str,
        title: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        agent: str | None = None,
        *,
        temporary: bool = False,
        temp_dir: str | None = None,
        expires_at: int | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO sessions(
                    session_id, chat_id, open_id, title,
                    model_provider, model_id, agent, temporary, temp_dir,
                    expires_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    open_id=excluded.open_id,
                    title=excluded.title,
                    model_provider=COALESCE(
                        excluded.model_provider, sessions.model_provider
                    ),
                    model_id=COALESCE(
                        excluded.model_id, sessions.model_id
                    ),
                    agent=COALESCE(excluded.agent, sessions.agent),
                    temporary=excluded.temporary,
                    temp_dir=excluded.temp_dir,
                    expires_at=excluded.expires_at,
                    updated_at=excluded.updated_at
                """,
                (
                    session_id,
                    chat_id,
                    open_id,
                    title,
                    provider_id,
                    model_id,
                    agent,
                    1 if temporary else 0,
                    temp_dir,
                    expires_at,
                    now,
                    now,
                ),
            )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_sessions(self, chat_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE chat_id=?
                ORDER BY created_at ASC, session_id ASC
                """,
                (chat_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_active_session(
        self, chat_id: str, session_id: str, title: str | None
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE chats
                SET session_id=?, session_title=?, updated_at=?
                WHERE chat_id=?
                """,
                (session_id, title, now, chat_id),
            )

    def touch_session(
        self, session_id: str, temp_ttl: int | None = None
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            if temp_ttl is None:
                self._conn.execute(
                    "UPDATE sessions SET updated_at=? WHERE session_id=?",
                    (now, session_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET updated_at=?,
                        expires_at=CASE
                            WHEN temporary=1 THEN ?
                            ELSE expires_at
                        END
                    WHERE session_id=?
                    """,
                    (now, now + temp_ttl, session_id),
                )

    def list_temp_sessions(self, chat_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE chat_id=? AND temporary=1
                ORDER BY created_at ASC, session_id ASC
                """,
                (chat_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_expired_temp_sessions(
        self, now: int
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM sessions
                WHERE temporary=1
                  AND expires_at IS NOT NULL
                  AND expires_at <= ?
                ORDER BY expires_at ASC
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_temp_dirs(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT temp_dir FROM sessions
                WHERE temp_dir IS NOT NULL AND temp_dir <> ''
                """
            ).fetchall()
            return [str(row["temp_dir"]) for row in rows]

    def has_pending_permissions(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM permissions
                WHERE session_id=? AND status='pending'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return row is not None

    def clear_active_session(self, chat_id: str) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE chats
                SET session_id=NULL, session_title=NULL, updated_at=?
                WHERE chat_id=?
                """,
                (now, chat_id),
            )

    def delete_session(self, session_id: str) -> None:
        """Remove a session, its tasks/permissions, and schedule references.

        Deleting the task rows is important: ``_migrate_sessions`` rebuilds
        missing sessions from historical tasks on startup, so keeping them
        would make deleted sessions reappear after a restart.
        """
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE schedules
                SET last_task_id=NULL, updated_at=?
                WHERE last_task_id IN (
                    SELECT id FROM tasks WHERE session_id=?
                )
                """,
                (now, session_id),
            )
            self._conn.execute(
                """
                UPDATE schedules
                SET session_id=NULL, updated_at=?
                WHERE session_id=?
                """,
                (now, session_id),
            )
            self._conn.execute(
                "DELETE FROM permissions WHERE session_id=?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM forms WHERE session_id=?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM tasks WHERE session_id=?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id=?", (session_id,)
            )

    def update_session_model(
        self, session_id: str, provider_id: str, model_id: str
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE sessions
                SET model_provider=?, model_id=?, updated_at=?
                WHERE session_id=?
                """,
                (provider_id, model_id, now, session_id),
            )

    def update_chat_model(
        self, chat_id: str, provider_id: str, model_id: str
    ) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE chats
                SET model_provider=?, model_id=?, updated_at=?
                WHERE chat_id=?
                """,
                (provider_id, model_id, now, chat_id),
            )

    def update_session_agent(self, session_id: str, agent: str) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE sessions
                SET agent=?, updated_at=?
                WHERE session_id=?
                """,
                (agent, now, session_id),
            )

    def update_chat_agent(self, chat_id: str, agent: str) -> None:
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute(
                """
                UPDATE chats
                SET agent=?, updated_at=?
                WHERE chat_id=?
                """,
                (agent, now, chat_id),
            )

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    def create_task(
        self,
        chat_id: str,
        session_id: str,
        user_message_id: str | None,
        prompt: str,
    ) -> int:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO tasks(chat_id, session_id, user_message_id, prompt, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (chat_id, session_id, user_message_id, prompt, now, now),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_active_task(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE session_id=? AND status IN ('queued','running')
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_last_task(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE session_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_active_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN ('queued','running')
                ORDER BY id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def complete_task(
        self,
        task_id: int,
        status: str,
        *,
        final_text: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Finish a task only if it is still queued/running.

        Returns True when this caller won the race, so notifications are sent
        exactly once even if an SSE event and the watchdog both report the
        same completion.
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status=?, final_text=?, error=?, updated_at=?
                WHERE id=? AND status IN ('queued','running')
                """,
                (
                    status,
                    final_text,
                    error,
                    int(time.time()),
                    task_id,
                ),
            )
            return cur.rowcount == 1

    def update_task(self, task_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        columns = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values())
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE tasks SET {columns} WHERE id=?", (*values, task_id)
            )

    def mark_task_running(self, task_id: int) -> bool:
        """Move a queued task to running without resurrecting a finished one."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE tasks
                SET status='running', updated_at=?
                WHERE id=? AND status IN ('queued','running')
                """,
                (int(time.time()), task_id),
            )
            return cur.rowcount == 1

    def mark_stale_tasks(self) -> list[dict[str, Any]]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','running')"
            ).fetchall()
            if not rows:
                return []
            self._conn.execute(
                """
                UPDATE tasks
                SET status='failed', error='bridge restarted', updated_at=?
                WHERE status IN ('queued','running')
                """,
                (int(time.time()),),
            )
            return [dict(row) for row in rows]

    def recent_tasks(self, chat_id: str, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # permissions
    # ------------------------------------------------------------------
    def add_permission(
        self,
        permission_id: str,
        task_id: int | None,
        chat_id: str,
        session_id: str,
        action: str,
        resources: list[str],
        save: list[str],
        source: dict[str, Any],
        prompt: str,
    ) -> bool:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO permissions(
                    id, task_id, chat_id, session_id, action, resources, save,
                    source, prompt, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    permission_id,
                    task_id,
                    chat_id,
                    session_id,
                    action,
                    json.dumps(resources, ensure_ascii=False),
                    json.dumps(save, ensure_ascii=False),
                    json.dumps(source, ensure_ascii=False),
                    prompt,
                    now,
                    now,
                ),
            )
            return cur.rowcount == 1

    def get_permission(self, permission_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM permissions WHERE id=?", (permission_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_permission(self, permission_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        columns = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values())
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE permissions SET {columns} WHERE id=?",
                (*values, permission_id),
            )

    def list_pending_permissions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM permissions WHERE status='pending' ORDER BY created_at ASC"
            ).fetchall()
            return [dict(row) for row in rows]

    def has_permission_cards(self, task_id: int) -> bool:
        """Whether approval cards were sent after this task's progress card."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM permissions
                WHERE task_id=? AND card_message_id IS NOT NULL
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # forms
    # ------------------------------------------------------------------
    def add_form(
        self,
        form_id: str,
        session_id: str,
        chat_id: str,
        task_id: int | None,
        title: str,
        fields: list[dict[str, Any]],
    ) -> bool:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT OR IGNORE INTO forms(
                    id, session_id, chat_id, task_id, title, fields,
                    status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    form_id,
                    session_id,
                    chat_id,
                    task_id,
                    title,
                    json.dumps(fields, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            return cur.rowcount == 1

    def get_form(self, form_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forms WHERE id=?", (form_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_form(self, form_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        columns = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values())
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE forms SET {columns} WHERE id=?",
                (*values, form_id),
            )

    def list_pending_forms(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM forms
                WHERE status='pending'
                ORDER BY created_at ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def has_pending_forms(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM forms
                WHERE session_id=? AND status='pending'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return row is not None

    # ------------------------------------------------------------------
    # schedules
    # ------------------------------------------------------------------
    def add_schedule(
        self,
        chat_id: str,
        open_id: str,
        title: str,
        prompt: str,
        kind: str,
        time_of_day: str,
        weekday: int | None,
        next_run_at: int,
        mode: str = "task",
    ) -> int:
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO schedules(
                    chat_id, open_id, title, prompt, kind,
                    time_of_day, weekday, mode, enabled, next_run_at,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    chat_id,
                    open_id,
                    title,
                    prompt,
                    kind,
                    time_of_day,
                    weekday,
                    mode,
                    next_run_at,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def list_schedules(
        self, chat_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._lock:
            if chat_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM schedules ORDER BY id ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM schedules WHERE chat_id=? ORDER BY id ASC",
                    (chat_id,),
                ).fetchall()
            return [dict(row) for row in rows]

    def get_schedule(self, schedule_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
            return dict(row) if row else None

    def update_schedule(self, schedule_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        columns = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values())
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE schedules SET {columns} WHERE id=?",
                (*values, schedule_id),
            )

    def delete_schedule(self, schedule_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM schedules WHERE id=?", (schedule_id,)
            )

    def due_schedules(self, now: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM schedules
                WHERE enabled=1 AND next_run_at <= ?
                ORDER BY next_run_at ASC
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_schedule(
        self,
        schedule_id: int,
        expected_next_run_at: int,
        next_run_at: int,
    ) -> bool:
        """Atomically claim a due schedule and advance its next run time."""
        now = int(time.time())
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE schedules
                SET next_run_at=?, last_run_at=?, updated_at=?
                WHERE id=? AND enabled=1 AND next_run_at=?
                """,
                (next_run_at, now, now, schedule_id, expected_next_run_at),
            )
            return cur.rowcount == 1

    def claim_once_schedule(
        self,
        schedule_id: int,
        expected_next_run_at: int,
        *,
        ran: bool = True,
    ) -> bool:
        """Atomically claim a one-shot schedule and mark it finished.

        ``ran=False`` is used when the schedule was missed and cancelled;
        ``last_run_at`` stays NULL so the list can show ``已错过`` instead
        of ``已完成``.
        """
        now = int(time.time())
        last_run_at = now if ran else None
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                UPDATE schedules
                SET enabled=0, last_run_at=?, updated_at=?
                WHERE id=? AND enabled=1 AND next_run_at=?
                """,
                (last_run_at, now, schedule_id, expected_next_run_at),
            )
            return cur.rowcount == 1

    def set_schedule_session(
        self, schedule_id: int, session_id: str
    ) -> None:
        self.update_schedule(schedule_id, session_id=session_id)

    def set_schedule_task(self, schedule_id: int, task_id: int) -> None:
        self.update_schedule(schedule_id, last_task_id=task_id)

    # ------------------------------------------------------------------
    # dedup / settings
    # ------------------------------------------------------------------
    def mark_event_processed(self, event_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed_events(event_id, created_at) VALUES (?, ?)",
                (event_id, int(time.time())),
            )
            return cur.rowcount == 1

    def cleanup_processed_events(
        self, older_than_seconds: int = 7 * 86400
    ) -> int:
        cutoff = int(time.time()) - older_than_seconds
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM processed_events WHERE created_at < ?",
                (cutoff,),
            )
            return cur.rowcount

    def get_setting(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )
