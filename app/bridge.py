from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from httpx_sse import aconnect_sse
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from cards import (
    agent_resolved_card,
    agent_select_card,
    approval_card,
    approval_resolved_card,
    help_text,
    model_resolved_card,
    model_select_card,
    progress_card,
    reminder_card,
    schedule_help_text,
    sessions_card,
    status_card,
    switch_card,
)
from config import Config
from db import Database
from feishu import FeishuClient
from forms import (
    answer_from_form_value,
    build_form_card,
    build_form_resolved_card,
    parse_answer,
    parse_fields,
)
from opencode import OpenCodeClient, OpenCodeError, OpenCodeSyncClient
from permissions import build_session_permissions

log = logging.getLogger(__name__)


def _toast(
    kind: str,
    content: str,
    card: dict[str, Any] | None = None,
) -> P2CardActionTriggerResponse:
    response = P2CardActionTriggerResponse({})
    response.toast = {"type": kind, "content": content}
    if card is not None:
        response.card = {"type": "raw", "data": card}
    return response


def _short(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _task_state(task: dict[str, Any] | None) -> str:
    if not task:
        return "空闲"
    state = "执行中" if task["status"] == "running" else "排队中"
    return f"{state} #{task['id']}"


def _task_status_label(status: str) -> str:
    return {
        "queued": "排队中",
        "running": "执行中",
        "succeeded": "完成",
        "failed": "失败",
        "interrupted": "已中止",
    }.get(status, status)


SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_GRACE_SECONDS = 30 * 60

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEKDAY_ALIASES = {
    "一": 0, "周一": 0, "星期一": 0, "周1": 0,
    "mon": 0, "monday": 0, "1": 0,
    "二": 1, "周二": 1, "星期二": 1, "周2": 1,
    "tue": 1, "tuesday": 1, "2": 1,
    "三": 2, "周三": 2, "星期三": 2, "周3": 2,
    "wed": 2, "wednesday": 2, "3": 2,
    "四": 3, "周四": 3, "星期四": 3, "周4": 3,
    "thu": 3, "thursday": 3, "4": 3,
    "五": 4, "周五": 4, "星期五": 4, "周5": 4,
    "fri": 4, "friday": 4, "5": 4,
    "六": 5, "周六": 5, "星期六": 5, "周6": 5,
    "sat": 5, "saturday": 5, "6": 5,
    "日": 6, "天": 6, "周日": 6, "周天": 6,
    "星期日": 6, "星期天": 6, "周7": 6, "周0": 6,
    "sun": 6, "sunday": 6, "7": 6, "0": 6,
}


def _normalize_time(
    hour: int, minute: int, period: str | None
) -> tuple[int, int] | None:
    if not (0 <= minute <= 59):
        return None
    if period == "晚上" and hour == 12:
        hour = 0
    elif period in ("下午", "晚上", "中午"):
        if hour < 12:
            hour += 12
    elif period in ("早上", "上午", "凌晨"):
        if hour == 12:
            hour = 0
    if not (0 <= hour <= 23):
        return None
    return hour, minute


def _compute_next_run(
    kind: str,
    time_of_day: str,
    weekday: int | None,
    after: datetime | None = None,
) -> int:
    now = after or datetime.now(SCHEDULE_TZ)
    hour, minute = (int(part) for part in time_of_day.split(":"))
    candidate = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if kind == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
    elif kind == "weekdays":
        if candidate <= now:
            candidate += timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
    elif kind == "weekly":
        target = 0 if weekday is None else weekday
        days_ahead = (target - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= now:
            candidate += timedelta(days=7)
    else:
        raise ValueError(f"unknown schedule kind: {kind}")
    return int(candidate.timestamp())


def _schedule_label(
    kind: str, time_of_day: str, weekday: int | None
) -> str:
    if kind == "daily":
        return f"每天 {time_of_day}"
    if kind == "weekdays":
        return f"工作日 {time_of_day}"
    if kind == "weekly":
        name = (
            WEEKDAY_NAMES[weekday]
            if weekday is not None and 0 <= weekday < 7
            else "周一"
        )
        return f"每{name} {time_of_day}"
    return f"{kind} {time_of_day}"


_SCHEDULE_TEXT_RE = re.compile(
    r"^\s*(?P<kind>每天|每日|每周(?P<weekday>[一二三四五六日天])"
    r"|每星期(?P<weekday2>[一二三四五六日天])|工作日)"
    r"\s*(?P<period>早上|上午|中午|下午|晚上|凌晨)?\s*"
    r"(?P<hour>\d{1,2})\s*"
    r"(?:[:：点]\s*(?P<minute>\d{1,2})?\s*分?)?\s*"
    r"(?P<prompt>.*)$",
    re.S,
)


def _normalize_half_hour(text: str) -> str:
    """Turn the first ``10点半`` into ``10点30分``."""
    return re.sub(r"(\d{1,2})\s*点\s*半", r"\1点30分", text, count=1)


def parse_schedule_text(text: str) -> dict[str, Any] | None:
    """Parse a natural-language schedule request.

    Only messages that start with 每天/每日/每周X/工作日 and contain a
    concrete time are recognized, to avoid misclassifying normal tasks.
    """
    text = _normalize_half_hour(text)
    match = _SCHEDULE_TEXT_RE.match(text)
    if not match:
        return None

    kind_raw = match.group("kind")
    if kind_raw in ("每天", "每日"):
        kind = "daily"
        weekday = None
    elif kind_raw == "工作日":
        kind = "weekdays"
        weekday = None
    else:
        kind = "weekly"
        weekday_raw = match.group("weekday") or match.group("weekday2")
        weekday = WEEKDAY_ALIASES.get(weekday_raw or "")
        if weekday is None:
            return None

    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    normalized = _normalize_time(hour, minute, match.group("period"))
    if normalized is None:
        return None
    hour, minute = normalized

    prompt = match.group("prompt").strip()
    while True:
        cleaned = prompt.lstrip("，,。.：:；;、 ")
        cleaned = re.sub(
            r"^(?:(?:给我|帮我|请|麻烦)\s*)+", "", cleaned
        )
        if cleaned == prompt:
            break
        prompt = cleaned
    if not prompt:
        return None

    mode = "task"
    reminder = _parse_reminder(prompt)
    if reminder:
        mode = "reminder"
        prompt = reminder
    elif _REMINDER_RE.match(prompt):
        return None

    return {
        "kind": kind,
        "time_of_day": f"{hour:02d}:{minute:02d}",
        "weekday": weekday,
        "prompt": prompt,
        "mode": mode,
    }


def _looks_like_schedule(text: str) -> bool:
    stripped = text.strip()
    if not re.match(r"^(每天|每日|每周|每星期|工作日)", stripped):
        return False
    return bool(re.search(r"\d{1,2}\s*[:：点]", stripped))


def _parse_time_and_prompt(text: str) -> tuple[str, str] | None:
    text = _normalize_half_hour(text.strip())
    match = re.match(
        r"^(\d{1,2})(?:[:：点](\d{1,2})?\s*分?)?"
        r"(?=\s|[^\d\s分:：点])\s*(.+)$",
        text,
        re.S,
    )
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    prompt = match.group(3).strip()
    if not prompt:
        return None
    return f"{hour:02d}:{minute:02d}", prompt


_ONCE_PREFIX_RE = re.compile(r"^(?:once|单次|一次性)\s*", re.I)
_ONCE_RELATIVE_RE = re.compile(r"^(今天|明天|后天|大后天)\s*")
_ONCE_FULL_DATE_RE = re.compile(
    r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\s*"
)
_ONCE_SHORT_DATE_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})\s*")
_ONCE_CN_DATE_RE = re.compile(r"^(\d{1,2})月(\d{1,2})[日号]?\s*")
_ONCE_TIME_RE = re.compile(
    r"^(?P<period>早上|上午|中午|下午|晚上|凌晨)?\s*"
    r"(?P<hour>\d{1,2})\s*"
    r"(?:[:：点]\s*(?P<minute>\d{1,2})?\s*分?)?\s*"
)
_ONCE_TIME_START_RE = re.compile(
    r"^(?:早上|上午|中午|下午|晚上|凌晨)?\s*\d{1,2}\s*[:：点]"
)
_REMINDER_RE = re.compile(
    r"^(?:请|麻烦|帮我|给我)?\s*"
    r"(?:提醒我一下|提醒我|提醒一下|记得|别忘了|不要忘了|别忘记)"
    r"\s*[:：,，。]?\s*(?P<content>.*)$",
    re.S,
)


def _strip_wrapping(text: str) -> str:
    text = text.strip().rstrip("。.!！?？")
    pairs = [
        ("【", "】"),
        ("[", "]"),
        ("「", "」"),
        ("『", "』"),
        ("“", "”"),
        ('"', '"'),
        ("'", "'"),
        ("（", "）"),
        ("(", ")"),
    ]
    for left, right in pairs:
        if text.startswith(left) and text.endswith(right):
            return text[len(left) : -len(right)].strip()
    return text


def _parse_reminder(text: str) -> str | None:
    """Return reminder content when text is a reminder phrase."""
    match = _REMINDER_RE.match(text.strip())
    if not match:
        return None
    content = _strip_wrapping(match.group("content"))
    return content or None


def _looks_like_once(text: str) -> bool:
    """Conservatively detect a one-shot reminder/task request."""
    stripped = text.strip()
    if not stripped:
        return False
    if _ONCE_PREFIX_RE.match(stripped):
        return True
    if not re.search(r"\d{1,2}\s*[:：点]", stripped):
        return False
    if _ONCE_RELATIVE_RE.match(stripped):
        return True
    if _ONCE_FULL_DATE_RE.match(stripped):
        return True
    if _ONCE_CN_DATE_RE.match(stripped):
        return True
    if _ONCE_SHORT_DATE_RE.match(stripped):
        return True
    if _ONCE_TIME_START_RE.match(stripped):
        return bool(
            re.search(
                r"提醒我|提醒一下|记得|别忘了|不要忘了|别忘记",
                stripped,
            )
        )
    return False


def _starts_with_once_time(text: str) -> bool:
    """Whether a /schedule argument starts with a one-shot time/date."""
    stripped = text.strip()
    if not stripped:
        return False
    if _ONCE_PREFIX_RE.match(stripped):
        return True
    if _ONCE_RELATIVE_RE.match(stripped):
        return True
    if _ONCE_FULL_DATE_RE.match(stripped):
        return True
    if _ONCE_CN_DATE_RE.match(stripped):
        return True
    if _ONCE_SHORT_DATE_RE.match(stripped):
        return True
    return bool(_ONCE_TIME_START_RE.match(stripped))


def _parse_once_text(text: str) -> dict[str, Any] | None:
    """Parse a one-shot request such as ``13:38分提醒我【去接水】``.

    Returns ``kind="once"`` with an absolute ``next_run_at`` timestamp.
    A time without a date that has already passed rolls to tomorrow;
    an explicit date/time in the past is rejected.
    """
    text = _normalize_half_hour(text.strip())
    text = _ONCE_PREFIX_RE.sub("", text, count=1).strip()
    if not text:
        return None

    now = datetime.now(SCHEDULE_TZ)
    explicit_date = False
    base_date = now.date()

    match = _ONCE_RELATIVE_RE.match(text)
    if match:
        days = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
        base_date = (now + timedelta(days=days[match.group(1)])).date()
        explicit_date = True
        text = text[match.end() :]
    else:
        match = _ONCE_FULL_DATE_RE.match(text)
        if match:
            try:
                base_date = datetime(
                    int(match.group(1)),
                    int(match.group(2)),
                    int(match.group(3)),
                ).date()
            except ValueError:
                return None
            explicit_date = True
            text = text[match.end() :]
        else:
            match = _ONCE_CN_DATE_RE.match(text) or _ONCE_SHORT_DATE_RE.match(
                text
            )
            if match:
                month = int(match.group(1))
                day = int(match.group(2))
                year = now.year
                try:
                    base_date = datetime(year, month, day).date()
                except ValueError:
                    return None
                if base_date < now.date():
                    try:
                        base_date = datetime(year + 1, month, day).date()
                    except ValueError:
                        return None
                explicit_date = True
                text = text[match.end() :]

    match = _ONCE_TIME_RE.match(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    normalized = _normalize_time(hour, minute, match.group("period"))
    if normalized is None:
        return None
    hour, minute = normalized

    candidate = datetime(
        base_date.year,
        base_date.month,
        base_date.day,
        hour,
        minute,
        tzinfo=SCHEDULE_TZ,
    )
    if not explicit_date and candidate <= now:
        candidate += timedelta(days=1)
    if candidate <= now:
        return None

    rest = text[match.end() :].strip()
    rest = rest.lstrip("，,。.：:；;、-— ")
    if not rest:
        return None

    reminder = _parse_reminder(rest)
    if reminder:
        mode = "reminder"
        prompt = reminder
    elif _REMINDER_RE.match(rest):
        # 明确写了「提醒我」但没有内容，按无效处理，避免变成普通任务。
        return None
    else:
        mode = "task"
        prompt = rest
    if not prompt:
        return None

    return {
        "kind": "once",
        "mode": mode,
        "prompt": prompt,
        "next_run_at": int(candidate.timestamp()),
        "time_of_day": f"{hour:02d}:{minute:02d}",
        "when": candidate,
    }


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except Exception:
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def _summarize_input(tool: str, data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    for key in (
        "command",
        "filePath",
        "path",
        "pattern",
        "url",
        "query",
        "description",
    ):
        if key in data:
            return _short(str(data[key]))
    return _short(json.dumps(data, ensure_ascii=False))


def _summarize_output(data: dict[str, Any]) -> str:
    content = data.get("content")
    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if texts:
            return _short(" ".join(texts))
    for key in ("error", "message", "reason"):
        if data.get(key):
            return _short(str(data[key]))
    return ""


def _structured_error(data: Any) -> str:
    """Extract a human-readable message from an event/error payload."""
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
            if message:
                return str(message)
        elif error:
            return str(error)
        for key in ("message", "reason"):
            value = data.get(key)
            if isinstance(value, dict):
                message = value.get("message") or value.get("type")
                if message:
                    return str(message)
            elif value:
                return str(value)
        return ""
    if data:
        return str(data)
    return ""


def _assistant_text(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    parts: list[str] = []
    for part in message.get("content") or []:
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text")
            if text:
                parts.append(str(text))
    return "\n\n".join(parts)


class Bridge:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        oc: OpenCodeClient,
        oc_sync: OpenCodeSyncClient,
        feishu: FeishuClient,
    ):
        self.cfg = cfg
        self.db = db
        self.oc = oc
        self.oc_sync = oc_sync
        self.feishu = feishu

        self._session_ids = db.get_session_ids()
        self._final_text: dict[str, str] = {}
        self._usage: dict[str, dict[str, Any]] = {}
        self._tool_names: dict[str, str] = {}
        self._progress_last: dict[int, float] = {}
        self._started_at: dict[int, float] = {}
        self._stale_tasks: list[dict[str, Any]] = []
        self._model_catalog: list[dict[str, str]] = []
        self._agent_catalog: list[dict[str, Any]] = []
        self._agent_catalog_dir: str | None = None
        self._agent_catalog_at: float = 0.0
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._clean_pending: dict[str, set[str]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False

    # ------------------------------------------------------------------
    # startup / shutdown
    # ------------------------------------------------------------------
    def startup(self) -> None:
        removed = self.db.cleanup_processed_events()
        if removed:
            log.info("cleaned %s old processed event(s)", removed)
        self._stale_tasks = self.db.mark_stale_tasks()
        if self._stale_tasks:
            log.warning(
                "marked %s stale task(s) as failed", len(self._stale_tasks)
            )
        self._recover_schedules()
        self._sweep_temp_dirs()

    async def notify_stale_tasks(self) -> None:
        for task in self._stale_tasks:
            try:
                if task.get("card_message_id"):
                    await self.feishu.update_card_async(
                        task["card_message_id"],
                        progress_card(
                            status="已中断",
                            prompt=task["prompt"],
                            error="桥接器重启，任务已中断",
                            session_id=task["session_id"],
                        ),
                    )
                await self.feishu.send_text_async(
                    task["chat_id"],
                    f"桥接器重启，任务 #{task['id']} 已中断，请重新发送。",
                )
                log.info("notified stale task %s", task["id"])
            except Exception:
                log.exception("notify stale task %s failed", task["id"])
        self._stale_tasks = []
        await self._recover_forms()

    async def _recover_forms(self) -> None:
        """Cancel forms whose task is no longer active after a restart."""
        for form in self.db.list_pending_forms():
            task_id = form.get("task_id")
            task = self.db.get_task(task_id) if task_id else None
            if task and task["status"] in ("queued", "running"):
                continue
            try:
                await self.oc.cancel_form(
                    form["session_id"],
                    form["id"],
                    "桥接器重启，提问已失效",
                )
            except OpenCodeError as exc:
                if exc.status_code == 409:
                    current = await self._refresh_form(form)
                    if current["status"] != "pending":
                        await self._update_form_card(current)
                        continue
                log.warning(
                    "cancel orphan form %s failed: %s",
                    form["id"],
                    exc,
                )
            except Exception as exc:
                log.warning(
                    "cancel orphan form %s failed: %s",
                    form["id"],
                    exc,
                )
            self.db.update_form(
                form["id"],
                status="expired",
                message="桥接器重启，提问已失效",
                interrupted=1
                if task and task["status"] in ("failed", "interrupted")
                else 0,
            )
            current = self.db.get_form(form["id"])
            if current:
                await self._update_form_card(current)

    async def shutdown(self) -> None:
        self._stopping = True
        await self.oc.close()

    # ------------------------------------------------------------------
    # inbound messages
    # ------------------------------------------------------------------
    def on_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            event = data.event
            if event is None or event.message is None or event.sender is None:
                return
            message = event.message
            sender = event.sender
            open_id = (
                sender.sender_id.open_id if sender.sender_id is not None else None
            )
            if not open_id:
                return
            if not self._is_allowed(open_id):
                log.warning("rejected message from open_id=%s", open_id)
                return
            if message.chat_type != "p2p":
                log.info(
                    "ignored non-p2p message chat_type=%s chat_id=%s",
                    message.chat_type,
                    message.chat_id,
                )
                return
            if not self.db.mark_event_processed(message.message_id):
                log.info("duplicate message %s", message.message_id)
                return

            text = self._extract_text(message)
            if text is None:
                asyncio.create_task(
                    self.feishu.send_text_async(
                        message.chat_id, "目前只支持文本消息。"
                    )
                )
                return
            asyncio.create_task(
                self._handle_text(
                    message.chat_id, open_id, message.message_id, text
                )
            )
        except Exception:
            log.exception("on_message failed")

    def _is_allowed(self, open_id: str) -> bool:
        if not self.cfg.allowed_open_ids:
            return True
        return open_id in self.cfg.allowed_open_ids

    def _session_model(
        self,
        chat: dict[str, Any] | None,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        if session_id:
            session = self.db.get_session(session_id)
            if (
                session
                and session.get("model_provider")
                and session.get("model_id")
            ):
                return (
                    str(session["model_provider"]),
                    str(session["model_id"]),
                )
        if chat and chat.get("model_provider") and chat.get("model_id"):
            return str(chat["model_provider"]), str(chat["model_id"])
        return self.cfg.model_provider, self.cfg.model_id

    def _session_agent(
        self,
        chat: dict[str, Any] | None,
        session_id: str | None = None,
    ) -> str:
        if session_id:
            session = self.db.get_session(session_id)
            if session and session.get("agent"):
                return str(session["agent"])
        if chat and chat.get("agent"):
            return str(chat["agent"])
        return self.cfg.agent

    def _agent_directory(
        self,
        chat: dict[str, Any] | None,
        session_id: str | None = None,
    ) -> str:
        if session_id:
            session = self.db.get_session(session_id)
            if session and session.get("temporary") and session.get("temp_dir"):
                return str(session["temp_dir"])
        return self.cfg.workspace_dir

    async def _get_agents(
        self, directory: str, *, refresh: bool = False
    ) -> list[dict[str, Any]]:
        now = time.time()
        if (
            not refresh
            and self._agent_catalog
            and self._agent_catalog_dir == directory
            and now - self._agent_catalog_at < 60
        ):
            return self._agent_catalog
        agents = await self.oc.list_agents(directory)
        self._agent_catalog = agents
        self._agent_catalog_dir = directory
        self._agent_catalog_at = now
        return agents

    def _extract_text(self, message: Any) -> str | None:
        try:
            content = json.loads(message.content)
        except Exception:
            return None

        if message.message_type == "text":
            return content.get("text", "")

        if message.message_type == "post":
            parts: list[str] = []
            title = content.get("title")
            if title:
                parts.append(str(title) + "\n")
            for line in content.get("content", []):
                for element in line:
                    tag = element.get("tag")
                    if tag in ("text", "a"):
                        parts.append(str(element.get("text", "")))
                    elif tag == "at":
                        parts.append("@" + str(element.get("user_name", "")))
                parts.append("\n")
            return "".join(parts).strip()

        return None

    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _handle_text(
        self, chat_id: str, open_id: str, message_id: str, text: str
    ) -> None:
        text = text.strip()
        if not text:
            return
        # 同一聊天的消息串行处理，避免 /new 尚未创建完会话时，
        # 紧随其后的任务消息被派到旧会话。
        async with self._chat_lock(chat_id):
            chat = self.db.get_chat(chat_id)
            if chat and chat["session_id"]:
                session = self.db.get_session(chat["session_id"])
                if session and session.get("temporary"):
                    self._touch_session(chat["session_id"])
            if text.startswith("/"):
                await self._handle_command(chat_id, open_id, text)
                return
            parsed = parse_schedule_text(text)
            if parsed:
                await self._create_schedule(
                    chat_id,
                    open_id,
                    kind=parsed["kind"],
                    time_of_day=parsed["time_of_day"],
                    weekday=parsed["weekday"],
                    prompt=parsed["prompt"],
                    mode=parsed.get("mode", "task"),
                )
                return
            if _looks_like_once(text):
                parsed_once = _parse_once_text(text)
                if parsed_once:
                    await self._create_schedule(
                        chat_id,
                        open_id,
                        kind="once",
                        time_of_day=parsed_once["time_of_day"],
                        weekday=None,
                        prompt=parsed_once["prompt"],
                        mode=parsed_once["mode"],
                        next_run_at=parsed_once["next_run_at"],
                    )
                else:
                    await self.feishu.send_text_async(
                        chat_id,
                        "这看起来是单次提醒/任务，但没能识别出时间。\n"
                        "请使用：/schedule 13:38 提醒我【去接水】\n"
                        "或：/schedule 明天 09:00 <任务>",
                    )
                return
            if _looks_like_schedule(text):
                await self.feishu.send_text_async(
                    chat_id,
                    "这看起来是定时任务，但没能识别出时间。\n"
                    "请使用：/schedule daily 10:00 <任务内容>\n"
                    "或：/schedule weekly mon 10:00 <任务内容>",
                )
                return
            await self._handle_prompt(
                chat_id, open_id, message_id, text
            )

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    async def _handle_command(
        self, chat_id: str, open_id: str, text: str
    ) -> None:
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        argument = parts[1].strip() if len(parts) > 1 else ""

        if command in ("/help", "/start"):
            await self.feishu.send_text_async(
                chat_id, help_text(self._temp_ttl_text())
            )
        elif command == "/new":
            await self._cmd_new(chat_id, open_id, argument)
        elif command in ("/temp", "/t", "/临时"):
            await self._cmd_temp(chat_id, open_id, argument)
        elif command in ("/sessions", "/list"):
            await self._cmd_sessions(chat_id)
        elif command in ("/switch", "/use"):
            await self._cmd_switch(chat_id, argument)
        elif command in ("/last", "/result"):
            await self._cmd_last(chat_id, argument)
        elif command in ("/delete", "/remove", "/删除"):
            await self._cmd_delete(chat_id, argument)
        elif command in ("/clean", "/清理"):
            await self._cmd_clean(chat_id, argument)
        elif command in ("/schedule", "/定时"):
            await self._cmd_schedule(chat_id, open_id, argument)
        elif command == "/status":
            await self._cmd_status(chat_id)
        elif command == "/model":
            await self._cmd_model(chat_id, open_id, argument)
        elif command == "/agent":
            await self._cmd_agent(chat_id, open_id, argument)
        elif command == "/stop":
            await self._cmd_stop(chat_id, argument)
        elif command == "/whoami":
            chat = self.db.get_chat(chat_id)
            session_id = chat["session_id"] if chat else None
            session = (
                self.db.get_session(session_id) if session_id else None
            )
            title = (
                (session.get("title") if session else None)
                or (chat.get("session_title") if chat else None)
                or "-"
            )
            provider_id, model_id = self._session_model(chat, session_id)
            agent = self._session_agent(chat, session_id)
            await self.feishu.send_text_async(
                chat_id,
                f"open_id: {open_id}\n"
                f"chat_id: {chat_id}\n"
                f"session_id: {session_id or '-'}\n"
                f"会话标题: {title}\n"
                f"模型: {provider_id}/{model_id}\n"
                f"Agent: {agent}",
            )
        else:
            await self.feishu.send_text_async(
                chat_id, "未知命令。发送 /help 查看可用命令。"
            )

    async def _cmd_new(
        self,
        chat_id: str,
        open_id: str,
        argument: str,
    ) -> None:
        # /new 只创建会话；参数是标题，不发给模型。
        title = _short(argument, 40) if argument else None
        try:
            session_id = await self._create_session(
                chat_id, open_id, title
            )
        except Exception as exc:
            log.exception("create session failed")
            await self.feishu.send_text_async(
                chat_id, f"创建会话失败: {exc}"
            )
            return

        session = self.db.get_session(session_id)
        title_text = (
            (session.get("title") if session else None)
            or title
            or session_id
        )
        try:
            await self.feishu.send_text_async(
                chat_id,
                f"已创建会话【{title_text}】\n"
                f"{session_id}\n"
                "直接发送内容开始对话。",
            )
        except Exception:
            log.exception("send new session notice failed")

    async def _cmd_temp(
        self,
        chat_id: str,
        open_id: str,
        argument: str,
    ) -> None:
        action = argument.strip().lower()
        if action in ("end", "结束", "stop"):
            await self._cmd_temp_end(chat_id)
            return
        if action in ("list", "列表"):
            await self._cmd_temp_list(chat_id)
            return

        chat = self.db.get_chat(chat_id)
        current_id = chat["session_id"] if chat else None
        current = self.db.get_session(current_id) if current_id else None
        if current and current.get("temporary") and not argument:
            current_title = current.get("title") or current_id
            await self.feishu.send_text_async(
                chat_id,
                f"当前已经是临时会话【{current_title}】。\n"
                "直接发送内容即可继续；发送 /temp end 结束并删除临时目录。",
            )
            return

        title = (
            _short(argument, 40)
            if argument
            else f"临时-{datetime.now().strftime('%m%d-%H%M%S')}"
        )
        try:
            session_id = await self._create_session(
                chat_id,
                open_id,
                title,
                temporary=True,
                activate=True,
            )
        except Exception as exc:
            log.exception("create temp session failed")
            await self.feishu.send_text_async(
                chat_id, f"创建临时会话失败: {exc}"
            )
            return

        session = self.db.get_session(session_id)
        temp_dir = session.get("temp_dir") if session else None
        title_text = (
            (session.get("title") if session else None)
            or title
            or session_id
        )
        try:
            await self.feishu.send_text_async(
                chat_id,
                f"已创建临时会话【{title_text}】\n"
                f"{session_id}\n"
                f"临时工作目录：{temp_dir or '-'}\n"
                f"{self._temp_ttl_text()}；发送 /temp end 立即结束。\n"
                "直接发送内容开始对话。",
            )
        except Exception:
            log.exception("send temp session notice failed")

    async def _cmd_temp_end(self, chat_id: str) -> None:
        chat = self.db.get_chat(chat_id)
        session_id = chat["session_id"] if chat else None
        session = self.db.get_session(session_id) if session_id else None
        if not session or not session.get("temporary"):
            await self.feishu.send_text_async(
                chat_id, "当前不是临时会话。"
            )
            return

        active = self.db.get_active_task(session_id)
        if active:
            await self.feishu.send_text_async(
                chat_id,
                f"临时会话还有任务 #{active['id']} 执行中，"
                "请先 /stop 或等待完成后再结束。",
            )
            return

        # 先清空当前会话，避免删除后 chats.session_id 指向不存在的会话。
        self.db.clear_active_session(chat_id)
        ok, message = await self._delete_session(session, chat_id)
        if ok:
            await self.feishu.send_text_async(
                chat_id,
                "已结束临时会话并删除临时工作目录。\n"
                "当前会话已清空，发送任意消息会自动创建普通会话。",
            )
        else:
            # 手动结束失败时把到期时间提前，让 watchdog 下一轮立即重试。
            self.db.touch_session(session_id, temp_ttl=0)
            await self.feishu.send_text_async(
                chat_id,
                f"已退出临时会话，但清理失败：{message}\n"
                "后台会自动重试。",
            )

    async def _cmd_temp_list(self, chat_id: str) -> None:
        sessions = self.db.list_temp_sessions(chat_id)
        if not sessions:
            await self.feishu.send_text_async(
                chat_id, "当前没有临时会话。发送 /temp 创建。"
            )
            return

        chat = self.db.get_chat(chat_id)
        active_id = chat["session_id"] if chat else None
        now = int(time.time())
        lines = [f"临时会话（共 {len(sessions)} 个）"]
        for index, session in enumerate(sessions, 1):
            session_id = session["session_id"]
            marker = " [当前]" if session_id == active_id else ""
            expires_at = session.get("expires_at") or now
            remaining = max(0, int(expires_at - now))
            state = _task_state(
                self.db.get_active_task(session_id)
            )
            lines.append(
                f"{index}. {session.get('title') or '(无标题)'}{marker}"
                f"  [{state} / 剩 {remaining // 60} 分钟]"
            )
            lines.append(f"   {session_id}")
            if session.get("temp_dir"):
                lines.append(f"   目录: {session['temp_dir']}")
        lines.append("")
        lines.append(
            "发送 /temp end 结束当前临时会话；"
            "/delete <session_id> 删除其他临时会话。"
        )
        await self.feishu.send_text_async(chat_id, "\n".join(lines))

    def _list_sessions_with_current(
        self, chat_id: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        """列出会话，并保证当前会话一定在列表里。

        当前会话可能因为历史迁移、手工建会话等原因不在 sessions 表，
        这里用 chats 里的信息补一条，避免 /sessions 看不到自己。
        """
        chat = self.db.get_chat(chat_id)
        active_id = chat["session_id"] if chat else None
        sessions = self.db.list_sessions(chat_id)
        if active_id and not any(
            session["session_id"] == active_id for session in sessions
        ):
            current = self.db.get_session(active_id)
            if not current:
                now = int(time.time())
                current = {
                    "session_id": active_id,
                    "chat_id": chat_id,
                    "open_id": chat.get("open_id") if chat else "",
                    "title": chat.get("session_title") if chat else None,
                    "model_provider": (
                        chat.get("model_provider") if chat else None
                    ),
                    "model_id": chat.get("model_id") if chat else None,
                    "created_at": (
                        chat.get("created_at") if chat else now
                    ),
                    "updated_at": (
                        chat.get("updated_at") if chat else now
                    ),
                }
            sessions.append(current)
            sessions.sort(
                key=lambda item: (
                    item.get("created_at") or 0,
                    item["session_id"],
                )
            )
        return sessions, active_id

    async def _cmd_sessions(self, chat_id: str) -> None:
        sessions, active_id = self._list_sessions_with_current(chat_id)
        if not sessions:
            await self.feishu.send_card_async(
                chat_id,
                sessions_card(
                    sessions=[],
                    active_id=None,
                    active_title=None,
                ),
            )
            return

        pending = self.db.list_pending_permissions()
        pending_forms = self.db.list_pending_forms()
        items: list[dict[str, Any]] = []
        active_title: str | None = None
        for index, session in enumerate(sessions, 1):
            session_id = session["session_id"]
            active_task = self.db.get_active_task(session_id)
            pending_count = sum(
                1
                for item in pending
                if item["session_id"] == session_id
            )
            form_count = sum(
                1
                for item in pending_forms
                if item["session_id"] == session_id
            )
            flags: list[str] = []
            if session.get("temporary"):
                flags.append("临时")
                expires_at = session.get("expires_at")
                if expires_at:
                    remaining = max(
                        0, int(expires_at - time.time())
                    )
                    flags.append(f"剩 {remaining // 60} 分钟")
            flags.append(_task_state(active_task))
            if pending_count:
                flags.append(f"待审批 {pending_count}")
            if form_count:
                flags.append(f"待回答 {form_count}")
            title = session.get("title") or "(无标题)"
            if session_id == active_id:
                active_title = title
            updated = datetime.fromtimestamp(
                session["updated_at"]
            ).strftime("%m-%d %H:%M")
            items.append(
                {
                    "index": index,
                    "session_id": session_id,
                    "title": title,
                    "flags": flags,
                    "updated": updated,
                    "active": session_id == active_id,
                }
            )
        await self.feishu.send_card_async(
            chat_id,
            sessions_card(
                sessions=items,
                active_id=active_id,
                active_title=active_title,
            ),
        )

    def _resolve_session(
        self, chat_id: str, argument: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        sessions, _ = self._list_sessions_with_current(chat_id)
        if not sessions:
            return None, "当前没有会话。"
        argument = " ".join(argument.split())
        if not argument:
            return None, "请提供会话序号或 session_id。"

        if argument.isdigit():
            index = int(argument) - 1
            if 0 <= index < len(sessions):
                return sessions[index], None
            return None, f"序号超出范围（1-{len(sessions)}）。"

        lowered = argument.lower()
        for session in sessions:
            if session["session_id"].lower() == lowered:
                return session, None

        matches = [
            session
            for session in sessions
            if session["session_id"].lower().startswith(lowered)
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "匹配到多个会话，请使用更长的 session_id。"

        matches = [
            session
            for session in sessions
            if (session.get("title") or "") == argument
        ]
        if len(matches) == 1:
            return matches[0], None

        matches = [
            session
            for session in sessions
            if lowered in (session.get("title") or "").lower()
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "匹配到多个会话，请使用序号或 session_id。"
        return None, "未找到该会话。发送 /sessions 查看列表。"

    async def _cmd_switch(self, chat_id: str, argument: str) -> None:
        if not argument:
            await self._cmd_sessions(chat_id)
            return
        session, error = self._resolve_session(chat_id, argument)
        if error or not session:
            await self.feishu.send_text_async(
                chat_id, error or "未找到该会话。"
            )
            return

        session_id = session["session_id"]
        title = session.get("title") or session_id
        self.db.set_active_session(chat_id, session_id, title)
        self._touch_session(session_id)
        if session.get("model_provider") and session.get("model_id"):
            self.db.update_chat_model(
                chat_id,
                str(session["model_provider"]),
                str(session["model_id"]),
            )
        if session.get("agent"):
            self.db.update_chat_agent(chat_id, str(session["agent"]))
        self._session_ids.add(session_id)

        active_task = self.db.get_active_task(session_id)
        state = _task_state(active_task)
        provider_id, model_id = self._session_model(
            self.db.get_chat(chat_id), session_id
        )
        agent = self._session_agent(
            self.db.get_chat(chat_id), session_id
        )
        last_task = self.db.get_last_task(session_id)
        last_info: dict[str, Any] | None = None
        empty_text: str | None = None
        if last_task and last_task["status"] not in ("queued", "running"):
            label = _task_status_label(last_task["status"])
            finished = datetime.fromtimestamp(
                last_task["updated_at"]
            ).strftime("%m-%d %H:%M")
            last_info = {
                "id": last_task["id"],
                "status": label,
                "finished": finished,
                "prompt": _short(last_task["prompt"], 120),
            }
            if last_task["status"] == "succeeded":
                last_info["result"] = (
                    _short(last_task.get("final_text") or "", 200)
                    or "（无文本输出）"
                )
            elif last_task["status"] == "failed":
                last_info["error"] = (
                    _short(last_task.get("error") or "", 200)
                    or "（未提供错误信息）"
                )
            elif last_task["status"] == "interrupted":
                last_info["note"] = "该任务已中止。"
        elif not last_task:
            empty_text = "该会话还没有任务记录。"

        card = switch_card(
            title=title,
            session_id=session_id,
            model=f"{provider_id}/{model_id}",
            agent=agent,
            state=state,
            last_task=last_info,
            empty_text=empty_text,
            note=(
                f"临时会话：{self._temp_ttl_text()}。"
                if session.get("temporary")
                else None
            ),
        )
        await self.feishu.send_card_async(chat_id, card)

    async def _cmd_last(self, chat_id: str, argument: str) -> None:
        if argument:
            session, error = self._resolve_session(chat_id, argument)
            if error or not session:
                await self.feishu.send_text_async(
                    chat_id, error or "未找到该会话。"
                )
                return
            session_id = session["session_id"]
            title = session.get("title") or session_id
        else:
            chat = self.db.get_chat(chat_id)
            if not chat or not chat["session_id"]:
                await self.feishu.send_text_async(
                    chat_id, "当前没有会话。"
                )
                return
            session_id = chat["session_id"]
            title = chat.get("session_title") or session_id

        task = self.db.get_last_task(session_id)
        if not task:
            await self.feishu.send_text_async(
                chat_id, f"会话 {title} 还没有任务记录。"
            )
            return

        status = task["status"]
        label = _task_status_label(status)
        if status in ("queued", "running"):
            await self.feishu.send_text_async(
                chat_id,
                f"会话 {title} 的最后一次任务 #{task['id']} 仍在{label}，"
                "完成后会自动发送结果卡。",
            )
            return

        elapsed = max(0, int(task["updated_at"] - task["created_at"]))
        if status == "succeeded":
            card = progress_card(
                status="完成",
                prompt=task["prompt"],
                elapsed=elapsed,
                final_text=task.get("final_text") or "（无文本输出）",
                session_id=session_id,
                max_final_chars=self.cfg.max_final_chars,
            )
        elif status == "failed":
            card = progress_card(
                status="失败",
                prompt=task["prompt"],
                elapsed=elapsed,
                error=task.get("error") or "执行失败（未提供错误信息）",
                session_id=session_id,
            )
        else:
            await self.feishu.send_text_async(
                chat_id,
                f"会话 {title} 的最后一次任务 #{task['id']} 已中止。\n"
                f"任务：{_short(task['prompt'], 200)}",
            )
            return
        await self.feishu.send_card_async(chat_id, card)

    async def _cmd_delete(self, chat_id: str, argument: str) -> None:
        if not argument:
            await self.feishu.send_text_async(
                chat_id,
                "用法：/delete <序号|session_id|标题>\n"
                "只能删除空闲会话；当前会话不能删除。\n"
                "发送 /sessions 查看序号。",
            )
            return
        session, error = self._resolve_session(chat_id, argument)
        if error or not session:
            await self.feishu.send_text_async(
                chat_id, error or "未找到该会话。"
            )
            return
        chat = self.db.get_chat(chat_id)
        if (
            chat
            and chat["session_id"] == session["session_id"]
            and session.get("temporary")
        ):
            await self.feishu.send_text_async(
                chat_id,
                "当前是临时会话，请发送 /temp end 结束并删除临时目录。",
            )
            return
        ok, message = await self._delete_session(session, chat_id)
        await self.feishu.send_text_async(chat_id, message)

    async def _delete_session(
        self, session: dict[str, Any], chat_id: str
    ) -> tuple[bool, str]:
        session_id = session["session_id"]
        title = session.get("title") or session_id
        chat = self.db.get_chat(chat_id)
        if chat and chat["session_id"] == session_id:
            return (
                False,
                f"会话 {title} 是当前会话，不能删除。"
                "请先 /switch 到其他会话。",
            )
        active = self.db.get_active_task(session_id)
        if active:
            return (
                False,
                f"会话 {title} 还有任务 #{active['id']} 正在执行，不能删除。"
                "请等待完成或 /stop 后再试。",
            )

        # 删除前先拒绝该会话的待审批，避免 OpenCode 里留下悬挂请求。
        for permission in self.db.list_pending_permissions():
            if permission["session_id"] != session_id:
                continue
            try:
                await self.oc.reply_permission(
                    session_id, permission["id"], "reject"
                )
            except Exception as exc:
                log.warning(
                    "reject permission %s before deleting session %s "
                    "failed: %s",
                    permission["id"],
                    session_id,
                    exc,
                )
            self.db.update_permission(permission["id"], status="expired")
            await self._update_permission_card(permission, "expired")

        await self._cancel_forms_for_session(
            session_id, "会话已删除，提问自动取消"
        )

        try:
            await self.oc.delete_session(session_id)
        except Exception as exc:
            log.exception("delete OpenCode session %s failed", session_id)
            return False, f"删除会话 {title} 失败：{exc}"

        temp_dir = session.get("temp_dir")
        if temp_dir:
            await asyncio.to_thread(self._remove_temp_dir, temp_dir)

        self.db.delete_session(session_id)
        self._session_ids.discard(session_id)
        self._final_text.pop(session_id, None)
        self._usage.pop(session_id, None)
        log.info("deleted session %s (%s)", session_id, title)
        return True, f"已删除会话：{title}\n{session_id}"

    async def _cmd_clean(self, chat_id: str, argument: str) -> None:
        action = argument.strip().lower()
        if action in ("confirm", "确认", "yes", "y"):
            pending = self._clean_pending.pop(chat_id, None)
            if pending is None:
                await self.feishu.send_text_async(
                    chat_id,
                    "请先发送 /clean 查看待清理的会话，"
                    "再发送 /clean confirm。",
                )
                return

            sessions, active_id = self._list_sessions_with_current(chat_id)
            by_id = {
                session["session_id"]: session for session in sessions
            }
            deleted = 0
            failed: list[str] = []
            for session_id in pending:
                session = by_id.get(session_id)
                if not session:
                    continue
                if session_id == active_id:
                    failed.append(
                        f"{session.get('title') or session_id}：当前会话"
                    )
                    continue
                active = self.db.get_active_task(session_id)
                if active:
                    failed.append(
                        f"{session.get('title') or session_id}："
                        f"任务 #{active['id']} 执行中"
                    )
                    continue
                ok, message = await self._delete_session(session, chat_id)
                if ok:
                    deleted += 1
                else:
                    failed.append(message)

            lines = [f"已清理 {deleted} 个空闲会话。"]
            if failed:
                lines.append("")
                lines.append("未删除：")
                lines.extend(f"- {item}" for item in failed)
            await self.feishu.send_text_async(chat_id, "\n".join(lines))
            return

        if action:
            await self.feishu.send_text_async(
                chat_id,
                "用法：/clean 查看待清理会话，/clean confirm 确认删除。",
            )
            return

        sessions, active_id = self._list_sessions_with_current(chat_id)
        candidates: list[dict[str, Any]] = []
        skipped: list[tuple[dict[str, Any], str]] = []
        for session in sessions:
            session_id = session["session_id"]
            if session_id == active_id:
                skipped.append((session, "当前会话"))
                continue
            active = self.db.get_active_task(session_id)
            if active:
                skipped.append(
                    (session, f"任务 #{active['id']} 执行中")
                )
                continue
            candidates.append(session)

        if not candidates:
            await self.feishu.send_text_async(
                chat_id, "没有可清理的空闲会话。"
            )
            return

        self._clean_pending[chat_id] = {
            session["session_id"] for session in candidates
        }
        lines = [f"将删除 {len(candidates)} 个空闲会话："]
        for index, session in enumerate(candidates, 1):
            title = session.get("title") or "(无标题)"
            if session.get("temporary"):
                title += "（临时）"
            lines.append(
                f"{index}. {title}  {session['session_id']}"
            )
        if skipped:
            lines.append("")
            lines.append("保留：")
            for session, reason in skipped:
                title = session.get("title") or "(无标题)"
                lines.append(f"- {title}（{reason}）")
        lines.append("")
        lines.append("确认请发送：/clean confirm")
        await self.feishu.send_text_async(chat_id, "\n".join(lines))

    async def _cmd_status(self, chat_id: str) -> None:
        chat = self.db.get_chat(chat_id)
        session_id = chat["session_id"] if chat else None
        active = (
            self.db.get_active_task(session_id) if session_id else None
        )
        pending = [
            item
            for item in self.db.list_pending_permissions()
            if item["chat_id"] == chat_id
        ]
        current_provider, current_model = self._session_model(
            chat, session_id
        )
        current_agent = self._session_agent(chat, session_id)
        current_session = (
            self.db.get_session(session_id) if session_id else None
        )
        workspace = self.cfg.workspace_dir
        note: str | None = None
        if current_session and current_session.get("temporary"):
            workspace = (
                current_session.get("temp_dir") or workspace
            )
            note = f"临时会话：{self._temp_ttl_text()}。"
        sessions = []
        all_sessions, _ = self._list_sessions_with_current(chat_id)
        for session in all_sessions:
            task = self.db.get_active_task(session["session_id"])
            state = _task_state(task)
            if session.get("temporary"):
                state = f"临时 / {state}"
            sessions.append(
                {
                    "session_id": session["session_id"],
                    "title": session.get("title") or "(无标题)",
                    "state": state,
                    "active": session["session_id"] == session_id,
                }
            )
        card = status_card(
            chat_id=chat_id,
            session_id=session_id,
            session_title=chat["session_title"] if chat else None,
            active_task=active,
            pending=pending,
            model=f"{current_provider}/{current_model}",
            agent=current_agent,
            workspace=workspace,
            sessions=sessions,
            note=note,
        )
        await self.feishu.send_card_async(chat_id, card)

    async def _cmd_model(
        self, chat_id: str, open_id: str, argument: str
    ) -> None:
        self.db.ensure_chat(chat_id, open_id)
        try:
            models = await self.oc.list_models()
        except Exception as exc:
            log.exception("list models failed")
            await self.feishu.send_text_async(
                chat_id, f"获取模型列表失败: {exc}"
            )
            return
        if not models:
            await self.feishu.send_text_async(
                chat_id,
                "没有找到可用模型，请检查 OpenCode 的模型配置。",
            )
            return
        self._model_catalog = models

        chat = self.db.get_chat(chat_id)
        current_provider, current_model = self._session_model(
            chat, chat["session_id"] if chat else None
        )

        if argument:
            selected = self._resolve_model_argument(models, argument)
            if not selected:
                await self.feishu.send_text_async(
                    chat_id,
                    "未找到该模型。发送 /model 查看列表，"
                    "也可以用 /model 序号 或 /model 供应商/模型。",
                )
                return
            await self._switch_model(
                chat_id, selected["provider_id"], selected["model_id"]
            )
            return

        card = model_select_card(
            models=models,
            current_provider=current_provider,
            current_model=current_model,
            session_id=chat["session_id"] if chat else None,
            session_title=chat.get("session_title") if chat else None,
        )
        await self.feishu.send_card_async(chat_id, card)

    def _resolve_model_argument(
        self, models: list[dict[str, str]], argument: str
    ) -> dict[str, str] | None:
        argument = argument.strip()
        if not argument:
            return None
        if argument.isdigit():
            index = int(argument) - 1
            if 0 <= index < len(models):
                return models[index]
            return None
        if "/" in argument:
            provider_id, model_id = argument.split("/", 1)
            for model in models:
                if (
                    model["provider_id"] == provider_id
                    and model["model_id"] == model_id
                ):
                    return model
            return None
        lowered = argument.lower()
        for model in models:
            if (
                model["model_id"].lower() == lowered
                or model["model_name"].lower() == lowered
            ):
                return model
        return None

    async def _switch_model(
        self, chat_id: str, provider_id: str, model_id: str
    ) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            await self.feishu.send_text_async(
                chat_id, "请先发送一条消息创建会话。"
            )
            return
        session_id = chat.get("session_id")
        if session_id:
            active = self.db.get_active_task(session_id)
            if active:
                await self.feishu.send_text_async(
                    chat_id,
                    f"当前任务 #{active['id']} 还在执行，"
                    "请先 /stop 或等待完成。",
                )
                return
            try:
                await self.oc.switch_model(session_id, provider_id, model_id)
            except Exception as exc:
                log.exception("switch model failed")
                await self.feishu.send_text_async(
                    chat_id, f"切换模型失败: {exc}"
                )
                return
        self.db.update_chat_model(chat_id, provider_id, model_id)
        if session_id:
            self.db.update_session_model(
                session_id, provider_id, model_id
            )
        label = chat.get("session_title") or session_id or "当前会话"
        await self.feishu.send_text_async(
            chat_id, f"已切换 {label} 的模型：{provider_id}/{model_id}"
        )

    async def _cmd_agent(
        self, chat_id: str, open_id: str, argument: str
    ) -> None:
        self.db.ensure_chat(chat_id, open_id)
        chat = self.db.get_chat(chat_id)
        session_id = chat["session_id"] if chat else None
        directory = self._agent_directory(chat, session_id)
        try:
            agents = await self._get_agents(directory, refresh=True)
        except Exception as exc:
            log.exception("list agents failed")
            await self.feishu.send_text_async(
                chat_id, f"获取 Agent 列表失败: {exc}"
            )
            return
        if not agents:
            await self.feishu.send_text_async(
                chat_id,
                "没有找到可用的 primary agent，"
                "请检查 OpenCode 的 agent 配置。",
            )
            return

        current_agent = self._session_agent(chat, session_id)
        if argument:
            selected = self._resolve_agent_argument(agents, argument)
            if not selected:
                await self.feishu.send_text_async(
                    chat_id,
                    "未找到该 Agent。发送 /agent 查看列表，"
                    "也可以用 /agent 序号 或 /agent <agent_id>。",
                )
                return
            await self._switch_agent(chat_id, selected)
            return

        card = agent_select_card(
            agents=agents,
            current_agent=current_agent,
            session_id=session_id,
            session_title=chat.get("session_title") if chat else None,
        )
        await self.feishu.send_card_async(chat_id, card)

    def _resolve_agent_argument(
        self, agents: list[dict[str, Any]], argument: str
    ) -> dict[str, Any] | None:
        argument = argument.strip()
        if not argument:
            return None
        if argument.isdigit():
            index = int(argument) - 1
            if 0 <= index < len(agents):
                return agents[index]
            return None
        lowered = argument.lower()
        for agent in agents:
            if (
                agent["id"].lower() == lowered
                or agent["name"].lower() == lowered
            ):
                return agent
        return None

    async def _switch_agent(
        self, chat_id: str, agent: dict[str, Any]
    ) -> None:
        chat = self.db.get_chat(chat_id)
        if not chat:
            await self.feishu.send_text_async(
                chat_id, "请先发送一条消息创建会话。"
            )
            return
        agent_id = str(agent["id"])
        session_id = chat.get("session_id")
        if session_id:
            active = self.db.get_active_task(session_id)
            if active:
                await self.feishu.send_text_async(
                    chat_id,
                    f"当前任务 #{active['id']} 还在执行，"
                    "请先 /stop 或等待完成。",
                )
                return
            previous_agent_id = self._session_agent(chat, session_id)
            try:
                await self.oc.switch_agent(session_id, agent_id)
                await self.oc.set_session_permissions(
                    session_id,
                    build_session_permissions(
                        agent_id,
                        readonly_agents=self.cfg.readonly_agents,
                        agent_info=agent,
                    ),
                )
            except Exception as exc:
                log.exception("switch agent failed")
                # Best effort: put the previous agent back so a failed
                # permission update cannot leave a read-only agent with the
                # bridge's normal edit-approval rules.
                try:
                    await self.oc.switch_agent(
                        session_id, previous_agent_id
                    )
                except Exception:
                    log.exception("rollback agent switch failed")
                await self.feishu.send_text_async(
                    chat_id, f"切换 Agent 失败: {exc}"
                )
                return
        self.db.update_chat_agent(chat_id, agent_id)
        if session_id:
            self.db.update_session_agent(session_id, agent_id)
        label = chat.get("session_title") or session_id or "当前会话"
        await self.feishu.send_text_async(
            chat_id,
            f"已切换 {label} 的 Agent："
            f"{agent['name']}（{agent_id}）",
        )

    async def _cmd_stop(self, chat_id: str, argument: str = "") -> None:
        chat = self.db.get_chat(chat_id)
        if argument:
            session, error = self._resolve_session(chat_id, argument)
            if error or not session:
                await self.feishu.send_text_async(
                    chat_id, error or "未找到该会话。"
                )
                return
            session_id = session["session_id"]
            title = session.get("title") or session_id
        else:
            if not chat or not chat["session_id"]:
                await self.feishu.send_text_async(
                    chat_id, "当前没有会话。"
                )
                return
            session_id = chat["session_id"]
            title = chat.get("session_title") or session_id
        active = self.db.get_active_task(session_id)
        if not active:
            await self.feishu.send_text_async(
                chat_id, f"会话 {title} 当前没有正在执行的任务。"
            )
            return
        try:
            await self.oc.interrupt(session_id)
        except Exception as exc:
            log.exception("interrupt failed")
            await self.feishu.send_text_async(chat_id, f"中止失败: {exc}")
            return

        if not self.db.complete_task(
            active["id"], "interrupted", error="user interrupted"
        ):
            await self.feishu.send_text_async(
                chat_id, f"会话 {title} 的任务已经结束。"
            )
            return
        self._touch_session(session_id)
        for permission in self.db.list_pending_permissions():
            if permission["session_id"] != session_id:
                continue
            try:
                await self.oc.reply_permission(
                    session_id, permission["id"], "reject"
                )
            except Exception as exc:
                log.warning(
                    "reject pending permission %s failed: %s",
                    permission["id"],
                    exc,
                )
            self.db.update_permission(permission["id"], status="expired")
            await self._update_permission_card(permission, "expired")
        await self._cancel_forms_for_session(
            session_id, "用户中止任务，提问自动取消"
        )
        if active.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    active["card_message_id"],
                    progress_card(
                        status="已中止",
                        prompt=active["prompt"],
                        error="用户中止",
                        session_id=session_id,
                    ),
                )
            except Exception:
                log.exception("update interrupted card failed")
        await self.feishu.send_text_async(
            chat_id, f"已向会话 {title} 发送中止请求。"
        )

    # ------------------------------------------------------------------
    # schedules
    # ------------------------------------------------------------------
    async def _cmd_schedule(
        self, chat_id: str, open_id: str, argument: str
    ) -> None:
        argument = argument.strip()
        if not argument or argument.lower() in ("help", "帮助"):
            await self.feishu.send_text_async(
                chat_id, schedule_help_text()
            )
            return

        parts = argument.split(maxsplit=1)
        action = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action in ("list", "列表"):
            await self._schedule_list(chat_id)
        elif action in ("pause", "暂停"):
            await self._schedule_toggle(chat_id, rest, enabled=False)
        elif action in ("resume", "恢复"):
            await self._schedule_toggle(chat_id, rest, enabled=True)
        elif action in ("remove", "delete", "删除"):
            await self._schedule_remove(chat_id, rest)
        elif action in ("run", "执行"):
            await self._schedule_run(chat_id, rest)
        elif action in ("once", "单次", "一次性"):
            await self._schedule_add_once(chat_id, open_id, rest)
        elif action in ("add", "添加"):
            if _starts_with_once_time(rest):
                await self._schedule_add_once(chat_id, open_id, rest)
            else:
                await self._schedule_add(chat_id, open_id, rest)
        elif action in (
            "daily",
            "每天",
            "每日",
            "weekdays",
            "工作日",
            "weekly",
            "每周",
        ) or action.startswith("每周"):
            await self._schedule_add(chat_id, open_id, argument)
        elif _starts_with_once_time(argument):
            await self._schedule_add_once(chat_id, open_id, argument)
        else:
            await self.feishu.send_text_async(
                chat_id,
                "未知的定时任务命令。\n" + schedule_help_text(),
            )

    async def _schedule_add(
        self, chat_id: str, open_id: str, text: str
    ) -> None:
        text = text.strip()
        if not text:
            await self.feishu.send_text_async(
                chat_id, "用法：/schedule daily 10:00 <任务>"
            )
            return

        parts = text.split(maxsplit=1)
        kind_token = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if kind_token in ("daily", "每天", "每日"):
            kind, weekday = "daily", None
        elif kind_token in ("weekdays", "工作日"):
            kind, weekday = "weekdays", None
        elif kind_token in ("weekly", "每周"):
            sub = rest.split(maxsplit=1)
            if len(sub) < 2:
                await self.feishu.send_text_async(
                    chat_id,
                    "每周任务需要指定星期，例如："
                    "/schedule weekly mon 10:00 <任务>",
                )
                return
            weekday = WEEKDAY_ALIASES.get(sub[0].lower())
            if weekday is None:
                await self.feishu.send_text_async(
                    chat_id,
                    f"无法识别星期：{sub[0]}。"
                    "可用 mon/tue/wed/thu/fri/sat/sun 或 周一…周日。",
                )
                return
            rest = sub[1].strip()
            kind = "weekly"
        elif kind_token.startswith("每周"):
            weekday = WEEKDAY_ALIASES.get(kind_token[2:])
            if weekday is None:
                await self.feishu.send_text_async(
                    chat_id, f"无法识别星期：{kind_token[2:]}。"
                )
                return
            kind = "weekly"
        else:
            await self.feishu.send_text_async(
                chat_id,
                "无法识别周期。用法：\n"
                "/schedule daily 10:00 <任务>\n"
                "/schedule weekdays 10:00 <任务>\n"
                "/schedule weekly mon 10:00 <任务>",
            )
            return

        parsed = _parse_time_and_prompt(rest)
        if not parsed:
            await self.feishu.send_text_async(
                chat_id,
                "无法识别时间。请使用 10:00、10点 或 10点30 这样的格式，"
                "并在时间后写任务内容。",
            )
            return
        time_of_day, prompt = parsed
        mode = "task"
        reminder = _parse_reminder(prompt)
        if reminder:
            mode = "reminder"
            prompt = reminder
        elif _REMINDER_RE.match(prompt):
            await self.feishu.send_text_async(
                chat_id,
                "提醒内容不能为空。例如："
                "/schedule daily 10:00 提醒我【喝水】",
            )
            return
        await self._create_schedule(
            chat_id,
            open_id,
            kind=kind,
            time_of_day=time_of_day,
            weekday=weekday,
            prompt=prompt,
            mode=mode,
        )

    async def _schedule_add_once(
        self, chat_id: str, open_id: str, text: str
    ) -> None:
        parsed = _parse_once_text(text)
        if not parsed:
            await self.feishu.send_text_async(
                chat_id,
                "无法识别单次时间或提醒内容，或时间已经过去。用法：\n"
                "/schedule 13:38 提醒我【去接水】\n"
                "/schedule 明天 09:00 <任务>\n"
                "/schedule 2026-11-10 13:38 <任务>\n"
                "/schedule once 13:38 <任务>",
            )
            return
        await self._create_schedule(
            chat_id,
            open_id,
            kind="once",
            time_of_day=parsed["time_of_day"],
            weekday=None,
            prompt=parsed["prompt"],
            mode=parsed["mode"],
            next_run_at=parsed["next_run_at"],
        )

    async def _schedule_list(self, chat_id: str) -> None:
        schedules = self.db.list_schedules(chat_id)
        if not schedules:
            await self.feishu.send_text_async(
                chat_id,
                "还没有定时任务。\n"
                "发送 /schedule daily 10:00 <任务> 创建。",
            )
            return
        lines = [f"定时任务（共 {len(schedules)} 个）"]
        for schedule in schedules:
            mode = schedule.get("mode") or "task"
            if schedule["kind"] == "once":
                if schedule.get("last_run_at"):
                    status = "已完成"
                elif not schedule["enabled"]:
                    if schedule["next_run_at"] <= int(time.time()):
                        status = "已错过"
                    else:
                        status = "已暂停"
                else:
                    status = "待执行"
                label = "单次提醒" if mode == "reminder" else "单次任务"
                when = datetime.fromtimestamp(
                    schedule["next_run_at"], SCHEDULE_TZ
                ).strftime("%m-%d %H:%M")
                lines.append(
                    f"#{schedule['id']} {schedule['title']}  [{status}]"
                )
                lines.append(f"   {label} · {when}")
                lines.append(
                    f"   {'提醒' if mode == 'reminder' else '任务'}："
                    f"{_short(schedule['prompt'], 120)}"
                )
                if mode == "task" and schedule.get("last_task_id"):
                    last_task = self.db.get_task(schedule["last_task_id"])
                    if last_task:
                        label = _task_status_label(last_task["status"])
                        lines.append(
                            f"   上次任务：#{last_task['id']} {label}"
                        )
                continue

            status = "启用" if schedule["enabled"] else "已暂停"
            next_run = datetime.fromtimestamp(
                schedule["next_run_at"], SCHEDULE_TZ
            ).strftime("%m-%d %H:%M")
            label = _schedule_label(
                schedule["kind"],
                schedule["time_of_day"],
                schedule["weekday"],
            )
            lines.append(
                f"#{schedule['id']} {schedule['title']}  [{status}]"
            )
            lines.append(f"   {label} · 下次 {next_run}")
            lines.append(
                f"   {'提醒' if mode == 'reminder' else '任务'}："
                f"{_short(schedule['prompt'], 120)}"
            )
            if mode == "task" and schedule.get("last_task_id"):
                last_task = self.db.get_task(schedule["last_task_id"])
                if last_task:
                    label = _task_status_label(last_task["status"])
                    lines.append(
                        f"   上次任务：#{last_task['id']} {label}"
                    )
                else:
                    lines.append(
                        f"   上次任务：#{schedule['last_task_id']}"
                    )
        lines.append("")
        lines.append(
            "发送 /schedule pause|resume|remove|run #<id> 管理。"
        )
        await self.feishu.send_text_async(chat_id, "\n".join(lines))

    def _get_schedule_for_chat(
        self, chat_id: str, argument: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        argument = argument.strip()
        if not argument:
            return None, "请提供定时任务编号，例如：/schedule pause 3"
        try:
            schedule_id = int(argument.split()[0])
        except ValueError:
            return None, "定时任务编号必须是数字。"
        schedule = self.db.get_schedule(schedule_id)
        if not schedule or schedule["chat_id"] != chat_id:
            return None, f"未找到定时任务 #{schedule_id}。"
        return schedule, None

    async def _schedule_toggle(
        self, chat_id: str, argument: str, *, enabled: bool
    ) -> None:
        schedule, error = self._get_schedule_for_chat(chat_id, argument)
        if error or not schedule:
            await self.feishu.send_text_async(
                chat_id, error or "未找到定时任务。"
            )
            return
        if schedule["kind"] == "once":
            noun = (
                "单次提醒"
                if schedule.get("mode") == "reminder"
                else "单次任务"
            )
            if enabled:
                if schedule.get("last_run_at"):
                    await self.feishu.send_text_async(
                        chat_id,
                        f"{noun} #{schedule['id']} 已完成，不能恢复。"
                        "如需再次执行，请重新创建。",
                    )
                    return
                if schedule["next_run_at"] <= int(time.time()):
                    await self.feishu.send_text_async(
                        chat_id,
                        f"{noun} #{schedule['id']} 的执行时间已过，"
                        "不能恢复。如需再次执行，请重新创建。",
                    )
                    return
                self.db.update_schedule(schedule["id"], enabled=1)
                when = datetime.fromtimestamp(
                    schedule["next_run_at"], SCHEDULE_TZ
                ).strftime("%m-%d %H:%M")
                await self.feishu.send_text_async(
                    chat_id,
                    f"已恢复{noun} #{schedule['id']}："
                    f"{schedule['title']}\n执行时间：{when}",
                )
            else:
                self.db.update_schedule(schedule["id"], enabled=0)
                await self.feishu.send_text_async(
                    chat_id,
                    f"已暂停{noun} #{schedule['id']}："
                    f"{schedule['title']}",
                )
            return

        if enabled:
            next_run_at = _compute_next_run(
                schedule["kind"],
                schedule["time_of_day"],
                schedule["weekday"],
            )
            self.db.update_schedule(
                schedule["id"], enabled=1, next_run_at=next_run_at
            )
            next_run = datetime.fromtimestamp(
                next_run_at, SCHEDULE_TZ
            ).strftime("%m-%d %H:%M")
            noun = (
                "定时提醒"
                if schedule.get("mode") == "reminder"
                else "定时任务"
            )
            await self.feishu.send_text_async(
                chat_id,
                f"已恢复{noun} #{schedule['id']}：{schedule['title']}\n"
                f"下次执行：{next_run}",
            )
        else:
            self.db.update_schedule(schedule["id"], enabled=0)
            noun = (
                "定时提醒"
                if schedule.get("mode") == "reminder"
                else "定时任务"
            )
            await self.feishu.send_text_async(
                chat_id,
                f"已暂停{noun} #{schedule['id']}：{schedule['title']}",
            )

    async def _schedule_remove(
        self, chat_id: str, argument: str
    ) -> None:
        schedule, error = self._get_schedule_for_chat(chat_id, argument)
        if error or not schedule:
            await self.feishu.send_text_async(
                chat_id, error or "未找到定时任务。"
            )
            return
        self.db.delete_schedule(schedule["id"])
        noun = (
            "定时提醒"
            if schedule.get("mode") == "reminder"
            else "定时任务"
        )
        await self.feishu.send_text_async(
            chat_id,
            f"已删除{noun} #{schedule['id']}：{schedule['title']}\n"
            "对应的会话和聊天记录会保留（如有）。",
        )

    async def _schedule_run(
        self, chat_id: str, argument: str
    ) -> None:
        schedule, error = self._get_schedule_for_chat(chat_id, argument)
        if error or not schedule:
            await self.feishu.send_text_async(
                chat_id, error or "未找到定时任务。"
            )
            return
        if schedule["kind"] == "once":
            noun = (
                "单次提醒"
                if schedule.get("mode") == "reminder"
                else "单次任务"
            )
            if schedule.get("last_run_at"):
                await self.feishu.send_text_async(
                    chat_id,
                    f"{noun} #{schedule['id']} 已完成，不能重复执行。",
                )
                return
            if not schedule["enabled"]:
                self.db.update_schedule(schedule["id"], enabled=1)
            if not self.db.claim_once_schedule(
                schedule["id"], schedule["next_run_at"]
            ):
                await self.feishu.send_text_async(
                    chat_id,
                    f"{noun} #{schedule['id']} 状态已变化，"
                    "请发送 /schedule list 查看。",
                )
                return
        action = (
            "发送提醒"
            if schedule.get("mode") == "reminder"
            else "立即执行定时任务"
        )
        await self.feishu.send_text_async(
            chat_id,
            f"正在{action} #{schedule['id']}：{schedule['title']}",
        )
        await self._run_schedule(schedule)

    async def _create_schedule(
        self,
        chat_id: str,
        open_id: str,
        *,
        kind: str,
        time_of_day: str,
        weekday: int | None,
        prompt: str,
        mode: str = "task",
        next_run_at: int | None = None,
    ) -> None:
        title = _short(" ".join(prompt.split()), 40)
        if next_run_at is None:
            next_run_at = _compute_next_run(kind, time_of_day, weekday)
        schedule_id = self.db.add_schedule(
            chat_id,
            open_id,
            title,
            prompt,
            kind,
            time_of_day,
            weekday,
            next_run_at,
            mode=mode,
        )
        session_id: str | None = None
        if mode != "reminder":
            try:
                session_id = await self._create_session(
                    chat_id,
                    open_id,
                    f"定时: {title}",
                    activate=False,
                )
                self.db.set_schedule_session(schedule_id, session_id)
            except Exception as exc:
                log.exception("create schedule session failed")
                noun = "单次任务" if kind == "once" else "定时任务"
                await self.feishu.send_text_async(
                    chat_id,
                    f"{noun} #{schedule_id} 已创建，但独立会话创建失败："
                    f"{exc}\n首次执行时会自动重试。",
                )

        when = datetime.fromtimestamp(
            next_run_at, SCHEDULE_TZ
        ).strftime("%m-%d %H:%M")
        if kind == "once":
            if mode == "reminder":
                lines = [
                    f"已创建单次提醒 #{schedule_id}：{title}",
                    f"执行时间：{when}",
                    f"内容：{_short(prompt, 200)}",
                    "到点会直接发送提醒卡片，不创建会话。",
                ]
            else:
                lines = [
                    f"已创建单次任务 #{schedule_id}：{title}",
                    f"执行时间：{when}",
                    f"任务：{_short(prompt, 200)}",
                ]
                if session_id:
                    lines.append(f"独立会话：{session_id}")
                lines.append("到点会在独立会话执行，结果发到本聊天。")
        else:
            if mode == "reminder":
                lines = [
                    f"已创建定时提醒 #{schedule_id}：{title}",
                    f"周期：{_schedule_label(kind, time_of_day, weekday)}",
                    f"下次执行：{when}",
                    f"提醒：{_short(prompt, 200)}",
                    "到点会直接发送提醒卡片，不创建会话。",
                ]
            else:
                lines = [
                    f"已创建定时任务 #{schedule_id}：{title}",
                    f"周期：{_schedule_label(kind, time_of_day, weekday)}",
                    f"下次执行：{when}",
                    f"任务：{_short(prompt, 200)}",
                ]
                if session_id:
                    lines.append(f"独立会话：{session_id}")
        lines.append("发送 /schedule list 查看全部定时任务。")
        await self.feishu.send_text_async(chat_id, "\n".join(lines))

    def _recover_schedules(self) -> None:
        schedules = self.db.list_schedules()
        enabled = [item for item in schedules if item["enabled"]]
        if enabled:
            log.info("loaded %s enabled schedule(s)", len(enabled))
        now = int(time.time())
        overdue = [item for item in enabled if item["next_run_at"] <= now]
        if overdue:
            log.info(
                "%s schedule(s) due at startup; scheduler will process them",
                len(overdue),
            )

    async def _ensure_schedule_session(
        self, schedule: dict[str, Any]
    ) -> str | None:
        session_id = schedule.get("session_id")
        if session_id and self.db.get_session(session_id):
            self._session_ids.add(session_id)
            return session_id
        try:
            session_id = await self._create_session(
                schedule["chat_id"],
                schedule["open_id"],
                f"定时: {schedule['title']}",
                activate=False,
            )
        except Exception as exc:
            log.exception(
                "create session for schedule %s failed", schedule["id"]
            )
            noun = (
                "单次任务"
                if schedule["kind"] == "once"
                else "定时任务"
            )
            await self.feishu.send_text_async(
                schedule["chat_id"],
                f"{noun} #{schedule['id']} 创建会话失败：{exc}",
            )
            return None
        self.db.set_schedule_session(schedule["id"], session_id)
        return session_id

    async def _run_schedule(self, schedule: dict[str, Any]) -> None:
        if schedule.get("mode") == "reminder":
            when = datetime.fromtimestamp(
                schedule["next_run_at"], SCHEDULE_TZ
            ).strftime("%m-%d %H:%M")
            card = reminder_card(
                prompt=schedule["prompt"],
                schedule_id=schedule["id"],
                when=when,
                label=(
                    "单次提醒"
                    if schedule["kind"] == "once"
                    else "定时提醒"
                ),
            )
            await self.feishu.send_card_async(schedule["chat_id"], card)
            log.info("schedule %s sent reminder", schedule["id"])
            return

        session_id = await self._ensure_schedule_session(schedule)
        if not session_id:
            return
        active = self.db.get_active_task(session_id)
        if active:
            log.info(
                "schedule %s skipped: task %s still active in session %s",
                schedule["id"],
                active["id"],
                session_id,
            )
            noun = (
                "单次任务"
                if schedule["kind"] == "once"
                else "定时任务"
            )
            await self.feishu.send_text_async(
                schedule["chat_id"],
                f"{noun} #{schedule['id']} 上次任务 #{active['id']} "
                "还在执行，本次跳过。",
            )
            return
        task_id = await self._submit_task(
            schedule["chat_id"],
            schedule["open_id"],
            None,
            schedule["prompt"],
            session_id=session_id,
            notify_busy=False,
        )
        if task_id is None:
            return
        self.db.set_schedule_task(schedule["id"], task_id)
        log.info(
            "schedule %s started task %s in session %s",
            schedule["id"],
            task_id,
            session_id,
        )

    async def _run_due_schedules(self) -> None:
        now = int(time.time())
        for schedule in self.db.due_schedules(now):
            try:
                if schedule["kind"] == "once":
                    missed = now - schedule["next_run_at"]
                    if not self.db.claim_once_schedule(
                        schedule["id"],
                        schedule["next_run_at"],
                        ran=missed <= SCHEDULE_GRACE_SECONDS,
                    ):
                        continue
                    if missed > SCHEDULE_GRACE_SECONDS:
                        log.info(
                            "once schedule %s missed by %s seconds, skipped",
                            schedule["id"],
                            missed,
                        )
                        noun = (
                            "单次提醒"
                            if schedule.get("mode") == "reminder"
                            else "单次任务"
                        )
                        await self.feishu.send_text_async(
                            schedule["chat_id"],
                            f"{noun} #{schedule['id']} 错过执行时间"
                            f"（{_short(schedule['title'], 40)}），"
                            "已取消。如需重新提醒，请重新创建。",
                        )
                        continue
                    await self._run_schedule(schedule)
                    continue

                next_run_at = _compute_next_run(
                    schedule["kind"],
                    schedule["time_of_day"],
                    schedule["weekday"],
                )
                if not self.db.claim_schedule(
                    schedule["id"],
                    schedule["next_run_at"],
                    next_run_at,
                ):
                    continue
                missed = now - schedule["next_run_at"]
                if missed > SCHEDULE_GRACE_SECONDS:
                    log.info(
                        "schedule %s missed by %s seconds, skipped",
                        schedule["id"],
                        missed,
                    )
                    noun = (
                        "定时提醒"
                        if schedule.get("mode") == "reminder"
                        else "定时任务"
                    )
                    await self.feishu.send_text_async(
                        schedule["chat_id"],
                        f"{noun} #{schedule['id']} 错过执行时间"
                        f"（{_short(schedule['title'], 40)}），"
                        "已跳过本次。",
                    )
                    continue
                await self._run_schedule(schedule)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "run schedule %s failed", schedule.get("id")
                )

    async def schedule_loop(self) -> None:
        while not self._stopping:
            try:
                await self._run_due_schedules()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("schedule loop failed")
            await asyncio.sleep(30)

    # ------------------------------------------------------------------
    # prompts / sessions
    # ------------------------------------------------------------------
    async def _handle_prompt(
        self, chat_id: str, open_id: str, message_id: str, text: str
    ) -> None:
        await self._submit_task(
            chat_id,
            open_id,
            message_id,
            text,
            notify_busy=True,
        )

    async def _submit_task(
        self,
        chat_id: str,
        open_id: str,
        message_id: str | None,
        text: str,
        *,
        session_id: str | None = None,
        notify_busy: bool = True,
    ) -> int | None:
        if session_id is None:
            try:
                session_id = await self._ensure_session(
                    chat_id, open_id, _short(text, 40) if text else None
                )
            except Exception as exc:
                log.exception("ensure session failed")
                await self.feishu.send_text_async(
                    chat_id, f"创建会话失败: {exc}"
                )
                return None

        active = self.db.get_active_task(session_id)
        if active:
            if notify_busy:
                if self.db.has_pending_forms(session_id):
                    message = (
                        f"当前任务 #{active['id']} 正在等待你回答提问。"
                        "请先处理提问卡片，或发送 /stop 中止。"
                    )
                else:
                    message = (
                        f"当前任务 #{active['id']} 还在执行"
                        f"（{active['status']}）。"
                        "请等待完成，或发送 /stop 中止。"
                    )
                await self.feishu.send_text_async(chat_id, message)
            return None

        await self._cancel_forms_for_session(
            session_id, "新任务已提交，旧提问自动取消"
        )

        for permission in self.db.list_pending_permissions():
            if permission["session_id"] != session_id:
                continue
            try:
                await self.oc.reply_permission(
                    session_id, permission["id"], "reject"
                )
            except Exception as exc:
                log.warning(
                    "reject stale permission %s failed: %s",
                    permission["id"],
                    exc,
                )
            self.db.update_permission(permission["id"], status="expired")
            await self._update_permission_card(permission, "expired")

        task_id = self.db.create_task(
            chat_id, session_id, message_id, text
        )
        self._touch_session(session_id)
        self._started_at[task_id] = time.time()
        # 清理上一个任务可能残留的最终文本/用量，避免串到新任务。
        self._final_text.pop(session_id, None)
        self._usage.pop(session_id, None)

        card_message_id: str | None = None
        try:
            card_message_id = await self.feishu.send_card_async(
                chat_id,
                progress_card(
                    status="已排队", prompt=text, session_id=session_id
                ),
            )
        except Exception:
            log.exception("send progress card failed")

        self.db.update_task(task_id, card_message_id=card_message_id)

        try:
            await self.oc.prompt(session_id, text)
        except Exception as exc:
            log.exception("prompt failed")
            self.db.update_task(task_id, status="failed", error=str(exc))
            if card_message_id:
                try:
                    await self.feishu.update_card_async(
                        card_message_id,
                        progress_card(
                            status="失败",
                            prompt=text,
                            error=str(exc),
                            session_id=session_id,
                        ),
                    )
                except Exception:
                    log.exception("update failed card failed")
            await self.feishu.send_text_async(
                chat_id, f"任务提交失败: {exc}"
            )
            return None

        log.info(
            "task %s queued in session %s for chat %s",
            task_id,
            session_id,
            chat_id,
        )
        return task_id

    async def _ensure_session(
        self, chat_id: str, open_id: str, title: str | None = None
    ) -> str:
        chat = self.db.get_chat(chat_id)
        if chat and chat["session_id"]:
            self._session_ids.add(chat["session_id"])
            return chat["session_id"]
        return await self._create_session(chat_id, open_id, title)

    async def _create_session(
        self,
        chat_id: str,
        open_id: str,
        title: str | None,
        *,
        activate: bool = True,
        temporary: bool = False,
    ) -> str:
        now = datetime.now().strftime("%m%d-%H%M%S")
        session_title = title or f"feishu-{chat_id[-6:]}-{now}"
        self.db.ensure_chat(chat_id, open_id)
        chat = self.db.get_chat(chat_id)
        provider_id, model_id = self._session_model(
            chat, chat["session_id"] if chat else None
        )
        agent = self._session_agent(
            chat, chat["session_id"] if chat else None
        )

        temp_dir: str | None = None
        directory = self.cfg.workspace_dir
        expires_at: int | None = None
        if temporary:
            temp_dir = self._make_temp_dir()
            directory = temp_dir
            expires_at = int(time.time()) + max(
                self.cfg.temp_session_ttl_seconds, 60
            )

        agent_info: dict[str, Any] | None = None
        try:
            agents = await self._get_agents(directory)
            agent_info = next(
                (item for item in agents if item["id"] == agent), None
            )
            if agents and agent_info is None:
                fallback = self.cfg.agent
                if not any(item["id"] == fallback for item in agents):
                    fallback = str(agents[0]["id"])
                log.warning(
                    "stored agent %s is not available; falling back to %s",
                    agent,
                    fallback,
                )
                agent = fallback
                agent_info = next(
                    (item for item in agents if item["id"] == agent),
                    None,
                )
        except Exception:
            log.exception("load agents for session creation failed")

        permissions = build_session_permissions(
            agent,
            readonly_agents=self.cfg.readonly_agents,
            agent_info=agent_info,
        )

        try:
            session_id = await self.oc.create_session(
                title=session_title,
                directory=directory,
                agent=agent,
                provider_id=provider_id,
                model_id=model_id,
                permissions=permissions,
            )
        except Exception:
            if temp_dir:
                await asyncio.to_thread(
                    self._remove_temp_dir, temp_dir
                )
            raise

        try:
            self.db.add_session(
                session_id,
                chat_id,
                open_id,
                session_title,
                provider_id,
                model_id,
                agent,
                temporary=temporary,
                temp_dir=temp_dir,
                expires_at=expires_at,
            )
            if activate:
                self.db.upsert_chat(
                    chat_id, open_id, session_id, session_title
                )
                self.db.update_chat_agent(chat_id, agent)
        except Exception:
            try:
                self.db.delete_session(session_id)
            except Exception:
                log.exception(
                    "cleanup DB session %s after failure failed",
                    session_id,
                )
            try:
                await self.oc.delete_session(session_id)
            except Exception:
                log.exception(
                    "cleanup OpenCode session %s after DB failure failed",
                    session_id,
                )
            if temp_dir:
                await asyncio.to_thread(
                    self._remove_temp_dir, temp_dir
                )
            raise

        self._session_ids.add(session_id)
        log.info(
            "created session %s for chat %s (activate=%s, temporary=%s)",
            session_id,
            chat_id,
            activate,
            temporary,
        )
        return session_id

    def _touch_session(self, session_id: str) -> None:
        self.db.touch_session(
            session_id, temp_ttl=self.cfg.temp_session_ttl_seconds
        )

    def _temp_ttl_text(self) -> str:
        ttl = self.cfg.temp_session_ttl_seconds
        if ttl <= 0:
            return "空闲后立即清理"
        return f"空闲 {max(1, ttl // 60)} 分钟后自动清理"

    def _make_temp_dir(self) -> str:
        root = Path(self.cfg.temp_workspace_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            log.warning("chmod temp root %s failed", root)
        return tempfile.mkdtemp(prefix="temp-", dir=root)

    def _remove_temp_dir(self, temp_dir: str | None) -> bool:
        if not temp_dir:
            return True
        root = Path(self.cfg.temp_workspace_root).expanduser().resolve()
        path = Path(temp_dir)
        try:
            parent = path.parent.resolve()
        except OSError:
            return False
        if not parent.is_relative_to(root):
            log.error(
                "refusing to remove temp dir outside root: %s", temp_dir
            )
            return False
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
                return True
            resolved = path.resolve()
            if resolved == root or not resolved.is_relative_to(root):
                log.error(
                    "refusing to remove temp dir outside root: %s",
                    temp_dir,
                )
                return False
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            log.exception("remove temp dir %s failed", temp_dir)
            return False

    def _sweep_temp_dirs(self) -> None:
        root = Path(self.cfg.temp_workspace_root).expanduser().resolve()
        if not root.exists():
            return
        known: set[Path] = set()
        for value in self.db.list_temp_dirs():
            try:
                known.add(Path(value).resolve())
            except OSError:
                continue
        try:
            children = list(root.iterdir())
        except OSError:
            log.exception("list temp root %s failed", root)
            return
        for child in children:
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in known or resolved == root.resolve():
                continue
            log.info("removing orphan temp path %s", child)
            self._remove_temp_dir(str(child))

    # ------------------------------------------------------------------
    # card actions
    # ------------------------------------------------------------------
    def _resolved_card(self, permission: dict[str, Any]) -> dict[str, Any]:
        status = permission.get("status") or "expired"
        decision = status if status in ("allowed", "rejected") else "expired"
        return approval_resolved_card(
            action=permission["action"],
            resources=_json_list(permission["resources"]),
            decision=decision,
            prompt=permission.get("prompt") or "",
            session_id=permission["session_id"],
        )

    def _on_model_action(
        self, event: Any, value: dict[str, Any]
    ) -> P2CardActionTriggerResponse:
        chat_id = event.context.open_chat_id if event.context else None
        if not chat_id:
            return _toast("error", "无法确定会话")

        provider_id = value.get("provider_id")
        model_id = value.get("model_id")
        if not provider_id or not model_id:
            return _toast("error", "无效模型")

        catalog = self._model_catalog
        if not catalog:
            try:
                catalog = self.oc_sync.list_models()
                self._model_catalog = catalog
            except Exception as exc:
                log.exception("load model catalog failed")
                return _toast("error", f"获取模型列表失败: {exc}")

        selected = next(
            (
                model
                for model in catalog
                if model["provider_id"] == provider_id
                and model["model_id"] == model_id
            ),
            None,
        )
        if not selected:
            return _toast("error", "模型不在列表中，请重新发送 /model")

        chat = self.db.get_chat(chat_id)
        if not chat:
            operator = event.operator.open_id if event.operator else None
            if not operator:
                return _toast("error", "无法确定用户")
            self.db.ensure_chat(chat_id, operator)
            chat = self.db.get_chat(chat_id)

        requested_session_id = value.get("session_id")
        if requested_session_id:
            session = self.db.get_session(str(requested_session_id))
            if not session or session["chat_id"] != chat_id:
                return _toast("error", "会话不存在或不属于当前聊天")
            session_id = str(requested_session_id)
        else:
            session_id = chat.get("session_id") if chat else None

        if session_id:
            active = self.db.get_active_task(session_id)
            if active:
                return _toast(
                    "info",
                    f"任务 #{active['id']} 执行中，请先 /stop 或等待完成",
                )
            try:
                self.oc_sync.switch_model(
                    session_id, provider_id, model_id
                )
            except Exception as exc:
                log.exception("switch model failed")
                return _toast("error", f"切换失败: {exc}")

        if session_id:
            self.db.update_session_model(
                session_id, provider_id, model_id
            )
        if not session_id or (
            chat and chat.get("session_id") == session_id
        ):
            self.db.update_chat_model(chat_id, provider_id, model_id)
        log.info(
            "chat %s session %s switched model to %s/%s",
            chat_id,
            session_id,
            provider_id,
            model_id,
        )
        session = self.db.get_session(session_id) if session_id else None
        label = (
            (session.get("title") if session else None)
            or session_id
            or "当前会话"
        )
        card = model_resolved_card(
            provider_id=provider_id,
            model_id=model_id,
            model_name=selected["model_name"],
            session_label=label,
        )
        return _toast(
            "success",
            f"已切换 {label} 的模型：{selected['model_name']}",
            card=card,
        )

    def _on_agent_action(
        self, event: Any, value: dict[str, Any]
    ) -> P2CardActionTriggerResponse:
        chat_id = event.context.open_chat_id if event.context else None
        if not chat_id:
            return _toast("error", "无法确定会话")

        agent_id = value.get("agent_id")
        if not agent_id:
            return _toast("error", "无效 Agent")

        chat = self.db.get_chat(chat_id)
        if not chat:
            operator = event.operator.open_id if event.operator else None
            if not operator:
                return _toast("error", "无法确定用户")
            self.db.ensure_chat(chat_id, operator)
            chat = self.db.get_chat(chat_id)

        requested_session_id = value.get("session_id")
        if requested_session_id:
            session = self.db.get_session(str(requested_session_id))
            if not session or session["chat_id"] != chat_id:
                return _toast("error", "会话不存在或不属于当前聊天")
            session_id = str(requested_session_id)
        else:
            session_id = chat.get("session_id") if chat else None

        directory = self._agent_directory(chat, session_id)
        catalog = self._agent_catalog
        if not catalog or self._agent_catalog_dir != directory:
            try:
                catalog = self.oc_sync.list_agents(directory)
                self._agent_catalog = catalog
                self._agent_catalog_dir = directory
                self._agent_catalog_at = time.time()
            except Exception as exc:
                log.exception("load agent catalog failed")
                return _toast("error", f"获取 Agent 列表失败: {exc}")

        selected = next(
            (
                agent
                for agent in catalog
                if agent["id"] == str(agent_id)
            ),
            None,
        )
        if not selected:
            return _toast("error", "Agent 不在列表中，请重新发送 /agent")

        if session_id:
            active = self.db.get_active_task(session_id)
            if active:
                return _toast(
                    "info",
                    f"任务 #{active['id']} 执行中，请先 /stop 或等待完成",
                )
            previous_agent_id = self._session_agent(chat, session_id)
            try:
                self.oc_sync.switch_agent(session_id, str(agent_id))
                self.oc_sync.set_session_permissions(
                    session_id,
                    build_session_permissions(
                        str(agent_id),
                        readonly_agents=self.cfg.readonly_agents,
                        agent_info=selected,
                    ),
                )
            except Exception as exc:
                log.exception("switch agent failed")
                try:
                    self.oc_sync.switch_agent(
                        session_id, previous_agent_id
                    )
                except Exception:
                    log.exception("rollback agent switch failed")
                return _toast("error", f"切换失败: {exc}")

        if session_id:
            self.db.update_session_agent(session_id, str(agent_id))
        if not session_id or (
            chat and chat.get("session_id") == session_id
        ):
            self.db.update_chat_agent(chat_id, str(agent_id))
        log.info(
            "chat %s session %s switched agent to %s",
            chat_id,
            session_id,
            agent_id,
        )
        session = self.db.get_session(session_id) if session_id else None
        label = (
            (session.get("title") if session else None)
            or session_id
            or "当前会话"
        )
        card = agent_resolved_card(
            agent_id=str(agent_id),
            agent_name=selected["name"],
            session_label=label,
        )
        return _toast(
            "success",
            f"已切换 {label} 的 Agent：{selected['name']}",
            card=card,
        )

    def _on_form_submit(
        self,
        data: P2CardActionTrigger,
        event: Any,
        value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        form_id = value.get("form_id")
        if not form_id:
            return _toast("error", "无效提问")
        form = self.db.get_form(str(form_id))
        if not form:
            return _toast("error", "提问不存在")
        if form["status"] != "pending":
            return _toast(
                "info",
                "该提问已处理",
                card=self._resolved_form_card(form),
            )

        fields = parse_fields(form.get("fields"))
        form_value = event.action.form_value if event.action else None
        answer, error = answer_from_form_value(fields, form_value)
        if error:
            return _toast("error", error)

        event_id = data.header.event_id if data.header else None
        if event_id:
            self.db.mark_event_processed(event_id)

        log.info(
            "form submit form_id=%s card_message_id=%s",
            form_id,
            form.get("card_message_id"),
        )
        try:
            self.oc_sync.reply_form(
                form["session_id"], str(form_id), answer or {}
            )
        except Exception as exc:
            log.exception("form reply failed")
            return _toast("error", f"提交失败: {exc}")

        self.db.update_form(
            str(form_id),
            status="replied",
            answer=json.dumps(answer or {}, ensure_ascii=False),
        )
        current = self.db.get_form(str(form_id)) or form
        return _toast(
            "success",
            "已提交",
            card=self._resolved_form_card(current),
        )

    def _on_form_cancel(
        self,
        data: P2CardActionTrigger,
        event: Any,
        value: dict[str, Any],
    ) -> P2CardActionTriggerResponse:
        form_id = value.get("form_id")
        if not form_id:
            return _toast("error", "无效提问")
        form = self.db.get_form(str(form_id))
        if not form:
            return _toast("error", "提问不存在")
        if form["status"] != "pending":
            return _toast(
                "info",
                "该提问已处理",
                card=self._resolved_form_card(form),
            )

        event_id = data.header.event_id if data.header else None
        if event_id:
            self.db.mark_event_processed(event_id)

        log.info(
            "form cancel form_id=%s card_message_id=%s",
            form_id,
            form.get("card_message_id"),
        )
        try:
            self.oc_sync.cancel_form(
                form["session_id"],
                str(form_id),
                "用户在飞书中取消了提问",
            )
        except Exception as exc:
            log.exception("form cancel failed")
            return _toast("error", f"取消失败: {exc}")

        self.db.update_form(
            str(form_id), status="cancelled", message="用户取消"
        )
        current = self.db.get_form(str(form_id)) or form
        # The card callback runs on the event loop but is synchronous, so
        # schedule the interrupt instead of blocking the callback.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(
                self._finish_form_cancel(str(form_id), "提问已取消")
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return _toast(
            "success",
            "已取消提问，任务将中断",
            card=self._resolved_form_card(current),
        )

    async def _finish_form_cancel(
        self, form_id: str, reason: str
    ) -> None:
        form = self.db.get_form(form_id)
        if not form:
            return
        await self._interrupt_task_for_form(form, reason)
        current = self.db.get_form(form_id)
        if current:
            await self._update_form_card(current)

    def on_card_action(
        self, data: P2CardActionTrigger
    ) -> P2CardActionTriggerResponse:
        try:
            event = data.event
            if event is None:
                return _toast("error", "无效回调")

            operator = (
                event.operator.open_id if event.operator else None
            )
            if not operator or not self._is_allowed(operator):
                return _toast("error", "无权限")

            value = event.action.value if event.action else None
            if not isinstance(value, dict):
                value = {}
            action = value.get("action")
            if action is None and event.action:
                name = event.action.name or ""
                if name.startswith("submit_"):
                    action = "form_submit"
                    value = {
                        "action": action,
                        "form_id": name[len("submit_") :],
                    }
            if action is None and event.action and event.action.tag in (
                "select_static",
                "multi_select_static",
                "input",
                "date_picker",
                "picker_time",
                "picker_datetime",
                "checker",
            ):
                # Form components emit callbacks on every change. The
                # final values arrive with the submit button, so ignore
                # these intermediate events without showing a toast.
                return P2CardActionTriggerResponse({})
            if action == "model":
                return self._on_model_action(event, value)
            if action == "agent":
                return self._on_agent_action(event, value)
            if action == "form_submit":
                return self._on_form_submit(data, event, value)
            if action == "form_cancel":
                return self._on_form_cancel(data, event, value)
            if action != "permission":
                return _toast("info", "未知操作")

            permission_id = value.get("permission_id")
            decision = value.get("decision")
            if decision not in ("once", "reject"):
                return _toast("error", "无效审批动作")

            event_id = data.header.event_id if data.header else None
            if event_id:
                self.db.mark_event_processed(event_id)

            permission = self.db.get_permission(permission_id)
            if not permission:
                return _toast("error", "审批不存在")
            if permission["status"] != "pending":
                return _toast(
                    "info",
                    "该审批已处理",
                    card=self._resolved_card(permission),
                )

            log.info(
                "card action permission=%s decision=%s card_message_id=%s",
                permission_id,
                decision,
                permission.get("card_message_id"),
            )
            try:
                self.oc_sync.reply_permission(
                    permission["session_id"], permission_id, decision
                )
            except Exception as exc:
                log.exception("permission reply failed")
                return _toast("error", f"处理失败: {exc}")

            status = "allowed" if decision == "once" else "rejected"
            self.db.update_permission(permission_id, status=status)

            resolved_card = self._resolved_card(
                {**permission, "status": status}
            )
            log.info(
                "returning resolved card in callback response for %s",
                permission_id,
            )
            return _toast(
                "success",
                "已允许一次" if decision == "once" else "已拒绝",
                card=resolved_card,
            )
        except Exception:
            log.exception("on_card_action failed")
            return _toast("error", "处理失败")

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    async def sse_loop(self) -> None:
        backoff = 1
        while not self._stopping:
            try:
                await self._consume_sse()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("SSE disconnected: %s", exc)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _consume_sse(self) -> None:
        timeout = httpx.Timeout(
            connect=5.0, read=None, write=10.0, pool=5.0
        )
        async with httpx.AsyncClient(
            base_url=self.cfg.opencode_base_url,
            auth=(self.cfg.opencode_username, self.cfg.opencode_password),
            timeout=timeout,
        ) as client:
            async with aconnect_sse(
                client, "GET", "/api/event"
            ) as source:
                log.info("SSE connected")
                async for sse in source.aiter_sse():
                    if self._stopping:
                        return
                    if not sse.data:
                        continue
                    try:
                        event = json.loads(sse.data)
                    except json.JSONDecodeError:
                        continue
                    try:
                        await self._handle_event(event)
                    except Exception:
                        log.exception(
                            "handle event failed: %s", event.get("type")
                        )

    async def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "form.created":
            form = data.get("form") or {}
            session_id = (
                form.get("sessionID") if isinstance(form, dict) else None
            )
        else:
            session_id = data.get("sessionID")

        if session_id and session_id not in self._session_ids:
            return

        if event_type == "permission.asked":
            await self._on_permission_asked(data)
        elif event_type == "permission.replied":
            await self._on_permission_replied(data)
        elif event_type == "form.created":
            await self._on_form_created(data.get("form") or {})
        elif event_type == "form.replied":
            await self._on_form_replied(data)
        elif event_type == "form.cancelled":
            await self._on_form_cancelled(data)
        elif event_type == "session.execution.started":
            await self._on_execution_started(session_id)
        elif event_type == "session.execution.succeeded":
            await self._on_execution_finished(session_id, True)
        elif event_type == "session.execution.failed":
            await self._on_execution_finished(
                session_id, False, data
            )
        elif event_type == "session.execution.interrupted":
            await self._on_execution_finished(
                session_id, False, data, interrupted=True
            )
        elif event_type == "session.retry.scheduled":
            await self._on_retry_scheduled(session_id, data)
        elif event_type == "session.tool.input.started":
            self._tool_names[data.get("id", "")] = data.get("name", "")
        elif event_type == "session.tool.called":
            await self._on_tool_event(session_id, "called", data)
        elif event_type == "session.tool.success":
            await self._on_tool_event(session_id, "success", data)
        elif event_type == "session.tool.failed":
            await self._on_tool_event(session_id, "failed", data)
        elif event_type == "session.text.ended":
            self._final_text[session_id] = data.get("text") or ""
        elif event_type == "session.usage.updated":
            self._usage[session_id] = data
        elif event_type == "session.error":
            await self._on_session_error(session_id, data)

    # ------------------------------------------------------------------
    # execution lifecycle
    # ------------------------------------------------------------------
    async def _on_execution_started(self, session_id: str) -> None:
        task = self.db.get_active_task(session_id)
        if not task:
            return
        if not self.db.mark_task_running(task["id"]):
            return
        await self._update_progress(task, status="执行中", force=True)

    async def _on_tool_event(
        self, session_id: str, phase: str, data: dict[str, Any]
    ) -> None:
        task = self.db.get_active_task(session_id)
        if not task:
            return

        call_id = data.get("id", "")
        tool = self._tool_names.get(call_id, "")
        if not tool:
            tool = "tool"

        if phase == "called":
            summary = _summarize_input(tool, data.get("input") or {})
            status = "调用工具"
        elif phase == "success":
            summary = _summarize_output(data)
            status = "工具完成"
        else:
            summary = _summarize_output(data)
            status = "工具失败"

        label = f"{tool} {summary}".strip()
        await self._update_progress(task, status=status, tool=label)

    async def _update_progress(
        self,
        task: dict[str, Any],
        *,
        status: str,
        tool: str | None = None,
        force: bool = False,
    ) -> None:
        now = time.time()
        last = self._progress_last.get(task["id"], 0.0)
        if not force and now - last < self.cfg.progress_min_interval:
            return
        self._progress_last[task["id"]] = now

        elapsed = int(now - self._started_at.get(task["id"], now))
        card = progress_card(
            status=status,
            prompt=task["prompt"],
            tool=tool,
            elapsed=elapsed,
            session_id=task["session_id"],
        )
        if not task.get("card_message_id"):
            return
        try:
            await self.feishu.update_card_async(
                task["card_message_id"], card
            )
        except Exception:
            log.exception("update progress card failed")

    async def _on_execution_finished(
        self,
        session_id: str,
        succeeded: bool,
        data: dict[str, Any] | None = None,
        *,
        interrupted: bool = False,
    ) -> None:
        task = self.db.get_active_task(session_id)
        if not task:
            return
        self._touch_session(session_id)

        final_text = self._final_text.pop(session_id, "")
        usage = self._usage.pop(session_id, None)

        if succeeded:
            await self._finish_task_success(task, final_text, usage)
            return
        if interrupted:
            reason = _structured_error(data) or "执行被中止"
            await self._finish_task_interrupted(task, reason)
            return
        error = _structured_error(data) or "执行失败（未提供错误信息）"
        await self._finish_task_failure(task, error)

    async def _on_retry_scheduled(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        task = self.db.get_active_task(session_id)
        if not task:
            return
        attempt = data.get("attempt") or 1
        error = _structured_error(data) or "上游模型报错"
        log.warning(
            "session %s retry #%s for task %s: %s",
            session_id,
            attempt,
            task["id"],
            error,
        )
        await self._update_progress(
            task,
            status=f"上游报错，重试中（第 {attempt} 次）",
            force=True,
        )
        if attempt == 1:
            await self.feishu.send_text_async(
                task["chat_id"],
                f"上游模型报错，正在自动重试：{error}",
            )

    async def _on_session_error(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        error = _structured_error(data) or "会话错误"
        log.error("session error in %s: %s", session_id, data)
        task = self.db.get_active_task(session_id)
        if not task:
            return
        await self._finish_task_failure(task, error)

    def _task_elapsed(self, task: dict[str, Any]) -> int:
        return int(
            time.time()
            - self._started_at.get(task["id"], task["created_at"])
        )

    async def _finish_task_success(
        self,
        task: dict[str, Any],
        final_text: str,
        usage: dict[str, Any] | None,
    ) -> None:
        if not self.db.complete_task(
            task["id"], "succeeded", final_text=final_text
        ):
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        elapsed = self._task_elapsed(task)
        result_card = progress_card(
            status="完成",
            prompt=task["prompt"],
            elapsed=elapsed,
            final_text=final_text or "（无文本输出）",
            session_id=session_id,
            usage=usage,
            max_final_chars=self.cfg.max_final_chars,
        )
        # 原进度卡只收尾成「完成」，结果单独在聊天底部发一张新卡。
        # 飞书更新卡片不会触发消息通知，只更新原卡用户很容易错过结果；
        # 审批卡也可能把结果压在更上面，所以统一发新卡。
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"],
                    progress_card(
                        status="完成",
                        prompt=task["prompt"],
                        elapsed=elapsed,
                        final_text="",
                        show_result=False,
                        session_id=session_id,
                        usage=usage,
                    ),
                )
            except Exception:
                log.exception("update completion card failed")
        try:
            await self.feishu.send_card_async(
                task["chat_id"], result_card
            )
        except Exception:
            log.exception("send completion card failed")
        if len(final_text) > self.cfg.max_final_chars:
            await self.feishu.send_long_text_async(
                task["chat_id"],
                final_text,
                self.cfg.max_text_chunk,
            )
        log.info("task %s succeeded", task["id"])

    async def _finish_task_failure(
        self, task: dict[str, Any], error: str
    ) -> None:
        if not self.db.complete_task(
            task["id"], "failed", error=error
        ):
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        elapsed = self._task_elapsed(task)
        card = progress_card(
            status="失败",
            prompt=task["prompt"],
            elapsed=elapsed,
            error=error,
            session_id=session_id,
        )
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"], card
                )
            except Exception:
                log.exception("update failure card failed")
        await self.feishu.send_text_async(
            task["chat_id"], f"任务失败: {error}"
        )
        log.info("task %s failed: %s", task["id"], error)

    async def _finish_task_interrupted(
        self, task: dict[str, Any], reason: str
    ) -> None:
        if not self.db.complete_task(
            task["id"], "interrupted", error=reason
        ):
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        elapsed = self._task_elapsed(task)
        card = progress_card(
            status="已中止",
            prompt=task["prompt"],
            elapsed=elapsed,
            error=reason,
            session_id=session_id,
        )
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"], card
                )
            except Exception:
                log.exception("update interrupted card failed")
        await self.feishu.send_text_async(
            task["chat_id"], f"任务已中止: {reason}"
        )
        log.info("task %s interrupted: %s", task["id"], reason)

    # ------------------------------------------------------------------
    # task watchdog
    # ------------------------------------------------------------------
    async def task_watchdog_loop(self) -> None:
        while not self._stopping:
            try:
                await self._reconcile_active_tasks()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("task watchdog failed")
            try:
                await self._cleanup_expired_temp_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("temp session cleanup failed")
            await asyncio.sleep(self.cfg.task_reconcile_seconds)

    async def _cleanup_expired_temp_sessions(self) -> None:
        sessions = self.db.list_expired_temp_sessions(int(time.time()))
        for session in sessions:
            session_id = session["session_id"]
            chat_id = session["chat_id"]
            async with self._chat_lock(chat_id):
                current = self.db.get_session(session_id)
                if not current or not current.get("temporary"):
                    continue
                expires_at = current.get("expires_at")
                if expires_at and expires_at > int(time.time()):
                    # 等待锁期间被用户消息或 /switch 续期了。
                    continue
                if self.db.get_active_task(session_id):
                    continue
                if self.db.has_pending_permissions(session_id):
                    continue
                if self.db.has_pending_forms(session_id):
                    continue

                chat = self.db.get_chat(chat_id)
                was_current = bool(
                    chat and chat["session_id"] == session_id
                )
                if was_current:
                    self.db.clear_active_session(chat_id)
                ok, message = await self._delete_session(
                    current, chat_id
                )
                if ok:
                    log.info(
                        "cleaned expired temp session %s", session_id
                    )
                    if was_current:
                        await self.feishu.send_text_async(
                            chat_id,
                            "临时会话已到期并清理，当前会话已清空。\n"
                            "发送任意消息会自动创建普通会话。",
                        )
                else:
                    # 失败后 5 分钟再试，避免每 60 秒刷日志。
                    self.db.touch_session(session_id, temp_ttl=300)
                    log.warning(
                        "clean expired temp session %s failed: %s",
                        session_id,
                        message,
                    )

    async def _reconcile_active_tasks(self) -> None:
        tasks = self.db.list_active_tasks()
        if not tasks:
            return
        try:
            active_sessions = await self.oc.list_active_sessions()
        except Exception as exc:
            log.warning("list active sessions failed: %s", exc)
            return

        now = time.time()
        for task in tasks:
            try:
                await self._reconcile_task(task, active_sessions, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile task %s failed", task["id"])

    async def _reconcile_task(
        self,
        task: dict[str, Any],
        active_sessions: set[str],
        now: float,
    ) -> None:
        age = now - task["created_at"]
        if age < self.cfg.task_reconcile_grace_seconds:
            return

        session_id = task["session_id"]
        if self.db.has_pending_forms(session_id):
            # The task is legitimately waiting for the user to answer a
            # question. The form expiry loop owns this timeout.
            return
        if session_id in active_sessions:
            if age >= self.cfg.task_timeout_seconds:
                log.warning(
                    "task %s timed out after %s seconds; interrupting",
                    task["id"],
                    int(age),
                )
                try:
                    await self.oc.interrupt(session_id)
                except Exception as exc:
                    log.warning(
                        "interrupt timed out task %s failed: %s",
                        task["id"],
                        exc,
                    )
                await self._finish_task_failure(
                    task,
                    "任务超时（超过 "
                    f"{self.cfg.task_timeout_seconds // 60} 分钟）",
                )
            return

        # 会话已不在执行中。idle 消息是 OpenCode 的权威完成记录，
        # 用它补发 SSE 断线期间错过的结果。
        try:
            idle = await self.oc.last_idle_message(
                session_id, task["created_at"]
            )
        except OpenCodeError as exc:
            if exc.status_code == 404:
                await self._finish_task_failure(
                    task, "会话已不存在（可能已被删除）"
                )
                return
            log.warning(
                "fetch idle message for task %s failed: %s",
                task["id"],
                exc,
            )
            return
        except Exception as exc:
            log.warning(
                "fetch idle message for task %s failed: %s",
                task["id"],
                exc,
            )
            return

        if idle:
            outcome = idle.get("outcome")
            error = _structured_error(idle)
            if outcome == "succeeded":
                final_text = ""
                try:
                    message = await self.oc.last_assistant_message(
                        session_id, task["created_at"]
                    )
                    final_text = _assistant_text(message)
                except Exception:
                    log.exception(
                        "fetch result for task %s failed", task["id"]
                    )
                log.info(
                    "recovered succeeded task %s from idle message",
                    task["id"],
                )
                await self._finish_task_success(task, final_text, None)
            elif outcome == "failed":
                log.info(
                    "recovered failed task %s from idle message",
                    task["id"],
                )
                await self._finish_task_failure(
                    task, error or "上游执行失败（未收到完成事件）"
                )
            elif outcome == "interrupted":
                log.info(
                    "recovered interrupted task %s from idle message",
                    task["id"],
                )
                await self._finish_task_interrupted(
                    task, error or "执行被中止（未收到完成事件）"
                )
            return

        if age >= self.cfg.task_timeout_seconds:
            log.warning(
                "task %s has no idle message after %s seconds; failing",
                task["id"],
                int(age),
            )
            await self._finish_task_failure(
                task,
                "任务超时（OpenCode 未报告执行结果）",
            )

    # ------------------------------------------------------------------
    # permissions
    # ------------------------------------------------------------------
    async def _on_permission_asked(
        self, data: dict[str, Any]
    ) -> None:
        permission_id = data.get("id")
        session_id = data.get("sessionID")
        if not permission_id or not session_id:
            return
        if session_id not in self._session_ids:
            return

        chat = self.db.get_chat_by_session(session_id)
        if not chat:
            log.warning(
                "permission for unknown session %s", session_id
            )
            return

        task = self.db.get_active_task(session_id)
        if not task:
            log.info(
                "rejecting stale permission %s for session %s (no active task)",
                permission_id,
                session_id,
            )
            try:
                await self.oc.reply_permission(
                    session_id, permission_id, "reject"
                )
            except OpenCodeError as exc:
                if exc.status_code != 404:
                    log.warning(
                        "reject stale permission %s failed: %s",
                        permission_id,
                        exc,
                    )
            except Exception as exc:
                log.warning(
                    "reject stale permission %s failed: %s",
                    permission_id,
                    exc,
                )
            existing = self.db.get_permission(permission_id)
            if existing and existing["status"] == "pending":
                self.db.update_permission(permission_id, status="expired")
                await self._update_permission_card(existing, "expired")
            return

        existing = self.db.get_permission(permission_id)
        if existing and existing["status"] != "pending":
            try:
                await self.oc.reply_permission(
                    session_id, permission_id, "reject"
                )
            except Exception:
                pass
            return

        task_id = task["id"]
        prompt = task["prompt"]
        action = data.get("action", "")
        resources = data.get("resources") or []
        save = data.get("save") or []
        source = data.get("source") or {}

        inserted = self.db.add_permission(
            permission_id=permission_id,
            task_id=task_id,
            chat_id=chat["chat_id"],
            session_id=session_id,
            action=action,
            resources=resources,
            save=save,
            source=source,
            prompt=prompt,
        )
        if not inserted:
            return

        card = approval_card(
            permission_id=permission_id,
            action=action,
            resources=resources,
            save=save,
            prompt=prompt,
            session_id=session_id,
            expire_seconds=self.cfg.permission_expire_seconds,
        )
        try:
            log.info(
                "sending approval card for %s to %s",
                permission_id,
                chat["chat_id"],
            )
            card_message_id = await self.feishu.send_card_async(
                chat["chat_id"], card
            )
            log.info(
                "approval card sent for %s: %s",
                permission_id,
                card_message_id,
            )
            if card_message_id:
                self.db.update_permission(
                    permission_id, card_message_id=card_message_id
                )
        except Exception:
            log.exception("send approval card failed")

    async def _on_permission_replied(
        self, data: dict[str, Any]
    ) -> None:
        permission_id = data.get("requestID") or data.get("id")
        if not permission_id:
            return
        permission = self.db.get_permission(permission_id)
        if not permission or permission["status"] != "pending":
            return

        reply = data.get("reply") or data.get("decision")
        status = "allowed" if reply == "once" else "rejected"
        self.db.update_permission(permission_id, status=status)

        if permission.get("card_message_id"):
            card = approval_resolved_card(
                action=permission["action"],
                resources=_json_list(permission["resources"]),
                decision=status,
                prompt=permission.get("prompt") or "",
                session_id=permission["session_id"],
            )
            try:
                await self.feishu.update_card_async(
                    permission["card_message_id"], card
                )
            except Exception:
                log.exception("update approval card failed")

    # ------------------------------------------------------------------
    # forms
    # ------------------------------------------------------------------
    def _form_prompt(self, form: dict[str, Any]) -> str:
        task_id = form.get("task_id")
        if not task_id:
            return ""
        task = self.db.get_task(task_id)
        if not task:
            return ""
        return task.get("prompt") or ""

    def _resolved_form_card(
        self, form: dict[str, Any]
    ) -> dict[str, Any]:
        return build_form_resolved_card(
            title=form.get("title") or "提问",
            fields=parse_fields(form.get("fields")),
            status=form.get("status") or "expired",
            answer=parse_answer(form.get("answer")),
            message=form.get("message"),
            prompt=self._form_prompt(form),
            session_id=form.get("session_id") or "",
            interrupted=bool(form.get("interrupted")),
        )

    async def _send_form_card(self, form: dict[str, Any]) -> None:
        card = build_form_card(
            form_id=form["id"],
            title=form.get("title") or "提问",
            fields=parse_fields(form.get("fields")),
            prompt=self._form_prompt(form),
            session_id=form.get("session_id") or "",
            expire_seconds=self.cfg.form_expire_seconds,
        )
        try:
            log.info(
                "sending form card for %s to %s",
                form["id"],
                form["chat_id"],
            )
            card_message_id = await self.feishu.send_card_async(
                form["chat_id"], card
            )
            log.info(
                "form card sent for %s: %s",
                form["id"],
                card_message_id,
            )
            if card_message_id:
                self.db.update_form(
                    form["id"], card_message_id=card_message_id
                )
        except Exception:
            log.exception("send form card failed")

    async def _update_form_card(self, form: dict[str, Any]) -> None:
        if not form.get("card_message_id"):
            return
        try:
            await self.feishu.update_card_async(
                form["card_message_id"], self._resolved_form_card(form)
            )
        except Exception:
            log.exception("update form card failed")

    async def _refresh_form(
        self, form: dict[str, Any]
    ) -> dict[str, Any]:
        """Refresh a form from OpenCode after a 409 (already settled)."""
        try:
            detail = await self.oc.get_form(
                form["session_id"], form["id"]
            )
        except Exception as exc:
            log.warning("refresh form %s failed: %s", form["id"], exc)
            return form
        if not detail:
            return form
        state = detail.get("state") or {}
        status = state.get("status")
        if status == "answered":
            self.db.update_form(
                form["id"],
                status="replied",
                answer=json.dumps(
                    state.get("answer") or {}, ensure_ascii=False
                ),
            )
        elif status == "cancelled":
            self.db.update_form(
                form["id"],
                status="cancelled",
                message=state.get("message") or "已取消",
            )
        return self.db.get_form(form["id"]) or form

    async def _on_form_created(
        self, form: dict[str, Any]
    ) -> None:
        form_id = form.get("id")
        session_id = form.get("sessionID")
        if not form_id or not session_id:
            return
        if session_id not in self._session_ids:
            return

        chat = self.db.get_chat_by_session(session_id)
        if not chat:
            log.warning("form for unknown session %s", session_id)
            return

        task = self.db.get_active_task(session_id)
        if not task:
            log.info(
                "cancelling stale form %s for session %s "
                "(no active task)",
                form_id,
                session_id,
            )
            try:
                await self.oc.cancel_form(
                    session_id, form_id, "任务已结束，提问自动取消"
                )
            except Exception as exc:
                log.warning(
                    "cancel stale form %s failed: %s", form_id, exc
                )
            return

        existing = self.db.get_form(form_id)
        if existing:
            if existing["status"] == "pending" and not existing.get(
                "card_message_id"
            ):
                await self._send_form_card(existing)
            return

        inserted = self.db.add_form(
            form_id=form_id,
            session_id=session_id,
            chat_id=chat["chat_id"],
            task_id=task["id"],
            title=str(form.get("title") or "提问"),
            fields=parse_fields(form.get("fields")),
        )
        if not inserted:
            return
        stored = self.db.get_form(form_id)
        if stored:
            await self._send_form_card(stored)

    async def _on_form_replied(
        self, data: dict[str, Any]
    ) -> None:
        form_id = data.get("id")
        if not form_id:
            return
        form = self.db.get_form(form_id)
        if not form:
            return
        if form["status"] == "pending":
            answer = data.get("answer") or {}
            self.db.update_form(
                form_id,
                status="replied",
                answer=json.dumps(answer, ensure_ascii=False),
            )
            form = self.db.get_form(form_id)
        if form:
            await self._update_form_card(form)

    async def _on_form_cancelled(
        self, data: dict[str, Any]
    ) -> None:
        form_id = data.get("id")
        if not form_id:
            return
        form = self.db.get_form(form_id)
        if not form:
            return
        was_pending = form["status"] == "pending"
        if was_pending:
            self.db.update_form(
                form_id, status="cancelled", message="已取消"
            )
            form = self.db.get_form(form_id)
        if not form:
            return
        if was_pending:
            await self._interrupt_task_for_form(form, "提问已取消")
        current = self.db.get_form(form_id)
        if current:
            await self._update_form_card(current)

    async def _interrupt_task_for_form(
        self, form: dict[str, Any], reason: str
    ) -> bool:
        task_id = form.get("task_id")
        if not task_id:
            return False
        task = self.db.get_task(task_id)
        if not task or task["status"] not in ("queued", "running"):
            return False
        try:
            await self.oc.interrupt(form["session_id"])
        except Exception as exc:
            log.warning(
                "interrupt task %s after form cancel failed: %s",
                task_id,
                exc,
            )
        if not self.db.complete_task(
            task_id, "interrupted", error=reason
        ):
            current_task = self.db.get_task(task_id)
            if current_task and current_task["status"] == "interrupted":
                self.db.update_form(form["id"], interrupted=1)
            return False
        self.db.update_form(form["id"], interrupted=1)
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"],
                    progress_card(
                        status="已中止",
                        prompt=task["prompt"],
                        error=reason,
                        session_id=task["session_id"],
                    ),
                )
            except Exception:
                log.exception("update interrupted task card failed")
        await self.feishu.send_text_async(
            task["chat_id"], f"任务 #{task_id} 已中断：{reason}"
        )
        return True

    async def _cancel_forms_for_session(
        self,
        session_id: str,
        reason: str,
        *,
        interrupt: bool = False,
    ) -> None:
        for form in self.db.list_pending_forms():
            if form["session_id"] != session_id:
                continue
            try:
                await self.oc.cancel_form(
                    session_id, form["id"], reason
                )
            except OpenCodeError as exc:
                if exc.status_code == 409:
                    current = await self._refresh_form(form)
                    if current["status"] != "pending":
                        await self._update_form_card(current)
                        continue
                log.warning(
                    "cancel form %s failed: %s", form["id"], exc
                )
            except Exception as exc:
                log.warning(
                    "cancel form %s failed: %s", form["id"], exc
                )
            self.db.update_form(
                form["id"], status="cancelled", message=reason
            )
            current = self.db.get_form(form["id"])
            if not current:
                continue
            if interrupt:
                await self._interrupt_task_for_form(current, reason)
                current = self.db.get_form(form["id"]) or current
            await self._update_form_card(current)

    async def form_poll_loop(self) -> None:
        while not self._stopping:
            try:
                for task in self.db.list_active_tasks():
                    session_id = task["session_id"]
                    if session_id not in self._session_ids:
                        continue
                    try:
                        forms = await self.oc.list_session_forms(
                            session_id
                        )
                    except Exception as exc:
                        log.warning(
                            "list forms for %s failed: %s",
                            session_id,
                            exc,
                        )
                        continue
                    for form in forms:
                        try:
                            await self._on_form_created(form)
                        except Exception:
                            log.exception(
                                "handle polled form %s failed",
                                form.get("id"),
                            )

                for form in self.db.list_pending_forms():
                    try:
                        detail = await self.oc.get_form(
                            form["session_id"], form["id"]
                        )
                    except Exception as exc:
                        log.warning(
                            "get form %s failed: %s", form["id"], exc
                        )
                        continue
                    if detail is None:
                        self.db.update_form(
                            form["id"],
                            status="expired",
                            message="提问已不存在",
                        )
                        current = self.db.get_form(form["id"])
                        if current:
                            await self._interrupt_task_for_form(
                                current, "提问已不存在，任务已中断"
                            )
                            current = (
                                self.db.get_form(form["id"]) or current
                            )
                            await self._update_form_card(current)
                        continue
                    state = detail.get("state") or {}
                    status = state.get("status")
                    if status == "answered":
                        self.db.update_form(
                            form["id"],
                            status="replied",
                            answer=json.dumps(
                                state.get("answer") or {},
                                ensure_ascii=False,
                            ),
                        )
                        current = self.db.get_form(form["id"])
                        if current:
                            await self._update_form_card(current)
                    elif status == "cancelled":
                        self.db.update_form(
                            form["id"],
                            status="cancelled",
                            message=state.get("message") or "已取消",
                        )
                        current = self.db.get_form(form["id"])
                        if current:
                            await self._interrupt_task_for_form(
                                current, "提问已取消"
                            )
                            current = (
                                self.db.get_form(form["id"]) or current
                            )
                            await self._update_form_card(current)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("form poll failed")
            await asyncio.sleep(30)

    async def form_expiry_loop(self) -> None:
        while not self._stopping:
            try:
                now = time.time()
                for form in self.db.list_pending_forms():
                    age = now - form["created_at"]
                    if (
                        self.cfg.form_expire_seconds > 0
                        and age >= self.cfg.form_expire_seconds
                    ):
                        current = self.db.get_form(form["id"])
                        if (
                            not current
                            or current["status"] != "pending"
                        ):
                            continue
                        reason = "提问超时，已自动取消"
                        try:
                            await self.oc.cancel_form(
                                current["session_id"],
                                current["id"],
                                reason,
                            )
                        except OpenCodeError as exc:
                            if exc.status_code == 409:
                                current = await self._refresh_form(
                                    current
                                )
                                if current["status"] == "replied":
                                    await self._update_form_card(
                                        current
                                    )
                                    continue
                                if current["status"] == "cancelled":
                                    await self._interrupt_task_for_form(
                                        current, "提问已取消"
                                    )
                                    current = (
                                        self.db.get_form(current["id"])
                                        or current
                                    )
                                    await self._update_form_card(
                                        current
                                    )
                                    continue
                            log.warning(
                                "auto cancel form %s failed: %s",
                                current["id"],
                                exc,
                            )
                        except Exception as exc:
                            log.warning(
                                "auto cancel form %s failed: %s",
                                current["id"],
                                exc,
                            )
                        self.db.update_form(
                            current["id"],
                            status="expired",
                            message=reason,
                        )
                        current = self.db.get_form(current["id"])
                        if not current:
                            continue
                        await self._interrupt_task_for_form(
                            current, reason
                        )
                        current = (
                            self.db.get_form(current["id"]) or current
                        )
                        await self._update_form_card(current)
                    elif (
                        self.cfg.form_remind_seconds > 0
                        and age >= self.cfg.form_remind_seconds
                        and not form["reminded"]
                    ):
                        self.db.update_form(form["id"], reminded=1)
                        await self.feishu.send_text_async(
                            form["chat_id"],
                            "提问还在等待回答，"
                            "请在卡片中提交或取消。",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("form expiry loop failed")
            await asyncio.sleep(60)

    async def permission_poll_loop(self) -> None:
        while not self._stopping:
            try:
                pending = await self.oc.list_pending_permissions()
                for permission in pending:
                    if permission.get("sessionID") in self._session_ids:
                        await self._on_permission_asked(permission)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("permission poll failed: %s", exc)
            await asyncio.sleep(30)

    async def permission_expiry_loop(self) -> None:
        while not self._stopping:
            try:
                now = time.time()
                for permission in self.db.list_pending_permissions():
                    age = now - permission["created_at"]
                    if age >= self.cfg.permission_expire_seconds:
                        current = self.db.get_permission(permission["id"])
                        if not current or current["status"] != "pending":
                            continue
                        rejected = False
                        try:
                            await self.oc.reply_permission(
                                current["session_id"],
                                current["id"],
                                "reject",
                            )
                            rejected = True
                        except OpenCodeError as exc:
                            log.warning(
                                "auto reject failed for %s: %s",
                                current["id"],
                                exc,
                            )
                            if exc.status_code == 404:
                                rejected = True
                        except Exception as exc:
                            log.warning(
                                "auto reject failed for %s: %s",
                                current["id"],
                                exc,
                            )
                        current = self.db.get_permission(permission["id"])
                        if not current or current["status"] != "pending":
                            continue
                        if not rejected:
                            continue
                        self.db.update_permission(
                            permission["id"], status="expired"
                        )
                        await self._update_permission_card(
                            permission, "expired"
                        )
                        await self._fail_task_for_permission(permission)
                        await self.feishu.send_text_async(
                            permission["chat_id"],
                            "审批超时，已自动拒绝。",
                        )
                    elif (
                        age >= self.cfg.permission_remind_seconds
                        and not permission["reminded"]
                    ):
                        self.db.update_permission(
                            permission["id"], reminded=1
                        )
                        await self.feishu.send_text_async(
                            permission["chat_id"],
                            "审批还在等待处理，请点击审批卡片上的按钮。",
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("permission expiry loop failed")
            await asyncio.sleep(60)

    async def _update_permission_card(
        self, permission: dict[str, Any], status: str
    ) -> None:
        if not permission.get("card_message_id"):
            return
        card = approval_resolved_card(
            action=permission["action"],
            resources=_json_list(permission["resources"]),
            decision=status,
            prompt=permission.get("prompt") or "",
            session_id=permission["session_id"],
        )
        try:
            await self.feishu.update_card_async(
                permission["card_message_id"], card
            )
        except Exception:
            log.exception("update permission card failed")

    async def _fail_task_for_permission(
        self, permission: dict[str, Any]
    ) -> None:
        task_id = permission.get("task_id")
        if not task_id:
            return
        task = self.db.get_task(task_id)
        if not task or task["status"] not in ("queued", "running"):
            return
        try:
            await self.oc.interrupt(permission["session_id"])
        except Exception as exc:
            log.warning(
                "interrupt after permission timeout failed: %s", exc
            )
        self.db.update_task(
            task_id, status="failed", error="审批超时，已自动拒绝"
        )
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"],
                    progress_card(
                        status="失败",
                        prompt=task["prompt"],
                        error="审批超时，已自动拒绝",
                        session_id=task["session_id"],
                    ),
                )
            except Exception:
                log.exception("update timeout task card failed")
