from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
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
    PROGRESS_LOG_LINES,
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
from feishu import FeishuClient, FeishuError
from forms import (
    answer_from_form_value,
    build_form_card,
    build_form_resolved_card,
    parse_answer,
    parse_fields,
)
from opencode import OpenCodeClient, OpenCodeError, OpenCodeSyncClient
from permissions import build_session_permissions
from stats import (
    STATS_TIMEZONE,
    build_stats_queries,
    stats_card,
    stats_help_text,
)
from users import UserRegistry

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


_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
}

# playwright MCP 的运行时产物，不是交付物。它们会落在 outbox 里，
# 但不能回传给用户：0 字节的 page-*.yml 还会让飞书上传接口报 234001。
_OUTBOX_ARTIFACT_RE = re.compile(
    r"^(?:console|page)-\d{4}-\d{2}-\d{2}T[^/]*\.(?:log|yml|yaml)$"
)

# 单个 outbox 文件投递失败后的最大尝试次数，超过就暂时跳过，
# 避免一个永远传不上去的文件把 outbox 扫描拖成每分钟死循环。
_OUTBOX_MAX_ATTEMPTS = 3


def _guess_image_ext(path: str) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return ".bin"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"BM"):
        return ".bmp"
    return ".bin"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{int(size)} B"


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
    if _is_stats_request(prompt):
        mode = "stats"
        prompt = "用量统计"
    else:
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


_STATS_REQUESTS = {
    "stats",
    "usage",
    "opencode stats",
    "用量",
    "用量统计",
    "统计用量",
}


def _is_stats_request(text: str) -> bool:
    """Whether a schedule prompt asks for the usage-stats card."""
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"(?:卡片|报告)$", "", normalized).strip()
    while True:
        stripped = re.sub(
            r"^(?:发送|推送|生成|查看|给我|帮我|请|麻烦|发|来|看)\s*",
            "",
            normalized,
        )
        if stripped == normalized:
            break
        normalized = stripped
    return normalized in _STATS_REQUESTS


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

    if _is_stats_request(rest):
        mode = "stats"
        prompt = "用量统计"
    else:
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


# 进度卡实时日志的内存上限：飞书卡片 JSON 有体积上限，超长日志会撞上限。
_PROGRESS_LOG_MAX_LINES = 200
_PROGRESS_LOG_MAX_BYTES = 12000
_PROGRESS_PARTIAL_MAX_CHARS = 4000
_PROGRESS_SHELL_CHUNK = 65536


@dataclass
class _TaskProgress:
    """单个任务的实时进度状态，仅在内存中维护，不落库。"""

    status: str = "执行中"
    phase: str = ""
    tool: str = ""
    step: int = 0
    lines: list[str] = field(default_factory=list)
    used_bytes: int = 0
    pending: str = ""
    partial: str = ""
    partial_kind: str = ""
    shell_id: str | None = None
    shell_dir: str | None = None
    cursor: int = 0
    truncated: bool = False
    dirty: bool = False
    closed: bool = False
    collecting: bool = False
    last_push: float = 0.0
    timer: asyncio.Task[Any] | None = None

    def add_line(self, text: str) -> None:
        for line in text.splitlines() or [""]:
            self.lines.append(line)
            self.used_bytes += len(line.encode("utf-8")) + 1
        self._trim()

    def add_output(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # shell 输出可能在一行中间被切块，先把不完整的尾行留在 pending。
        text = self.pending + text
        parts = text.split("\n")
        self.pending = parts.pop()
        for line in parts:
            if not line.strip():
                continue
            rendered = "  " + line
            self.lines.append(rendered)
            self.used_bytes += len(rendered.encode("utf-8")) + 1
        self._trim()

    def flush_output(self) -> None:
        if self.pending.strip():
            rendered = "  " + self.pending
            self.lines.append(rendered)
            self.used_bytes += len(rendered.encode("utf-8")) + 1
        self.pending = ""
        self._trim()

    def _trim(self) -> None:
        while self.lines and (
            len(self.lines) > _PROGRESS_LOG_MAX_LINES
            or self.used_bytes + len(self.pending.encode("utf-8"))
            > _PROGRESS_LOG_MAX_BYTES
        ):
            removed = self.lines.pop(0)
            self.used_bytes -= len(removed.encode("utf-8")) + 1
            self.truncated = True
        if self.used_bytes < 0:
            self.used_bytes = 0

    def set_partial(self, text: str) -> None:
        if len(text) > _PROGRESS_PARTIAL_MAX_CHARS:
            text = "…" + text[-_PROGRESS_PARTIAL_MAX_CHARS:]
        self.partial = text

    def window(self, count: int = PROGRESS_LOG_LINES) -> list[str]:
        lines = list(self.lines)
        if self.pending.strip():
            lines.append("  " + self.pending)
        if self.partial:
            lines.extend(self.partial.splitlines())
        return lines[-count:]

    def full_log(self) -> str:
        lines = list(self.lines)
        if self.pending.strip():
            lines.append("  " + self.pending)
        if self.partial:
            lines.extend(self.partial.splitlines())
        text = "\n".join(lines)
        if self.truncated:
            text = "…（更早的输出已省略）\n" + text
        return text


class Bridge:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        oc: OpenCodeClient,
        oc_sync: OpenCodeSyncClient,
        feishu: FeishuClient,
        users: UserRegistry | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.oc = oc
        self.oc_sync = oc_sync
        self.feishu = feishu
        self.users = users or UserRegistry(None)

        self._session_ids = db.get_session_ids()
        self._final_text: dict[str, str] = {}
        self._usage: dict[str, dict[str, Any]] = {}
        self._tool_names: dict[str, str] = {}
        self._progress: dict[int, _TaskProgress] = {}
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
        # --- sandbox / multi-user state ------------------------------
        self._pending_attachments: dict[str, list[dict[str, Any]]] = {}
        self._upload_sem = asyncio.Semaphore(2)
        self._outbox_dirty: set[str] = set()
        # (open_id, 文件名) -> 连续投递失败次数
        self._outbox_failures: dict[tuple[str, str], int] = {}

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
            if self.users.configured and self.users.get(open_id) is None:
                asyncio.create_task(
                    self._notify_unknown_user(message.chat_id, open_id)
                )
                return

            text = self._extract_text(message)
            attachments = self._extract_attachments(message)
            if attachments:
                asyncio.create_task(
                    self._handle_inbound(
                        message.chat_id,
                        open_id,
                        message.message_id,
                        text,
                        attachments,
                    )
                )
                return
            if text is None:
                asyncio.create_task(
                    self.feishu.send_text_async(
                        message.chat_id,
                        "目前支持文本、文件和图片消息。",
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

    # ------------------------------------------------------------------
    # multi-user helpers
    # ------------------------------------------------------------------
    def _workspace_for(self, open_id: str) -> str:
        entry = self.users.get(open_id)
        if entry is not None:
            return entry.workspace
        return self.cfg.workspace_dir

    def _open_id_for_chat(self, chat_id: str) -> str | None:
        chat = self.db.get_chat(chat_id)
        return chat.get("open_id") if chat else None

    def _open_id_for_session(self, session_id: str) -> str | None:
        session = self.db.get_session(session_id)
        return session.get("open_id") if session else None

    async def _notify_unknown_user(
        self, chat_id: str, open_id: str
    ) -> None:
        """Tell the user they are not provisioned and ping the admin."""
        try:
            await self.feishu.send_text_async(
                chat_id,
                "你还没有开通使用权限。\n"
                f"请把下面这行发给管理员：open_id={open_id}",
            )
        except Exception:
            log.exception("send unknown-user notice failed")

        key = f"unknown_user_notified:{open_id}"
        now = time.time()
        try:
            last = float(self.db.get_setting(key) or 0)
        except (TypeError, ValueError):
            last = 0.0
        if now - last < self.cfg.unknown_user_cooldown_seconds:
            return
        self.db.set_setting(key, str(int(now)))
        for admin in self.cfg.admin_open_ids:
            try:
                await self.feishu.send_text_async(
                    admin,
                    "有未开通用户尝试使用桥接器：\n"
                    f"open_id={open_id}\nchat_id={chat_id}\n"
                    "如需开通，请把 open_id 加入 config/users.json。",
                )
            except Exception:
                log.exception("notify admin %s failed", admin)

    async def _handle_inbound(
        self,
        chat_id: str,
        open_id: str,
        message_id: str,
        text: str | None,
        attachments: list[dict[str, str]],
    ) -> None:
        """Download attachments first, then hand the text to the task handler."""
        for attachment in attachments:
            try:
                await self._handle_attachment(
                    chat_id,
                    open_id,
                    message_id,
                    attachment["type"],
                    attachment["file_key"],
                    attachment.get("file_name"),
                )
            except Exception:
                log.exception("handle attachment failed")
        if text and text.strip():
            await self._handle_text(chat_id, open_id, message_id, text)

    async def _handle_attachment(
        self,
        chat_id: str,
        open_id: str,
        message_id: str,
        message_type: str,
        file_key: str | None,
        file_name: str | None = None,
    ) -> None:
        """Download a Feishu file/image and push it into the user workspace."""
        if not file_key:
            await self.feishu.send_text_async(
                chat_id, "无法读取附件信息，请重新发送。"
            )
            return

        upload_dir = Path(self.cfg.upload_tmp_dir)
        try:
            upload_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot create upload tmp dir %s: %s", upload_dir, exc)
            await self.feishu.send_text_async(
                chat_id, "服务器临时目录不可用，请稍后再试。"
            )
            return

        local_path = str(upload_dir / f"{message_id}-{file_key}")
        async with self._upload_sem:
            try:
                reported = await self.feishu.download_resource_async(
                    message_id,
                    file_key,
                    "image" if message_type == "image" else "file",
                    local_path,
                    max_bytes=self.cfg.max_upload_bytes,
                )
            except FeishuError as exc:
                await self.feishu.send_text_async(
                    chat_id, f"附件下载失败：{exc}"
                )
                return
            except Exception as exc:
                log.exception("download attachment failed")
                await self.feishu.send_text_async(
                    chat_id, f"附件下载失败：{exc}"
                )
                return

            try:
                size = os.path.getsize(local_path)
                if size > self.cfg.max_upload_bytes:
                    await self.feishu.send_text_async(
                        chat_id,
                        "附件超过 100 MB 上限，无法处理。",
                    )
                    return
                name = reported or file_name or str(file_key)
                name = os.path.basename(str(name)) or "attachment"
                if message_type == "image":
                    stem = os.path.splitext(name)[0] or "image"
                    name = stem + _guess_image_ext(local_path)
                digest = _sha256_file(local_path)
                remote_path = await self._upload_to_workspace(
                    open_id, local_path, name
                )
            except Exception as exc:
                log.exception("upload attachment to workspace failed")
                await self.feishu.send_text_async(
                    chat_id, f"附件写入工作区失败：{exc}"
                )
                return
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass

        self._pending_attachments.setdefault(chat_id, []).append(
            {
                "path": remote_path,
                "name": name,
                "size": size,
                "sha256": digest,
            }
        )
        kind = "图片" if message_type == "image" else "文件"
        await self.feishu.send_text_async(
            chat_id,
            f"已收到{kind}：{remote_path}（{_human_size(size)}）\n"
            "下一条消息会附带该文件路径，直接说要我做什么即可。",
        )

    async def _upload_to_workspace(
        self, open_id: str, local_path: str, name: str
    ) -> str:
        """Upload to <workspace>/uploads/<name>; fall back to the workspace root."""
        workspace = self._workspace_for(open_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        remote_name = f"{stamp}-{name}"
        uploads_dir = f"{workspace.rstrip('/')}/{self.cfg.upload_subdir}"
        remote_path = f"{uploads_dir}/{remote_name}"
        try:
            await self.oc.upload_file(
                open_id, local_path, remote_path, uploads_dir
            )
            return remote_path
        except OpenCodeError as exc:
            log.warning(
                "upload to %s failed (%s); retrying in workspace root",
                uploads_dir,
                exc,
            )
        remote_path = f"{workspace.rstrip('/')}/{remote_name}"
        await self.oc.upload_file(
            open_id, local_path, remote_path, workspace
        )
        return remote_path

    def _attachment_prefix(self, chat_id: str) -> str:
        attachments = self._pending_attachments.pop(chat_id, [])
        if not attachments:
            return ""
        lines = ["用户上传了以下文件："]
        for item in attachments:
            lines.append(
                f"- {item['path']}（{_human_size(item['size'])}，"
                f"sha256 {item['sha256'][:16]}…）"
            )
        return "\n".join(lines) + "\n\n"

    async def _deliver_outbox(
        self, chat_id: str, open_id: str | None = None
    ) -> None:
        """Send files from <workspace>/outbox back to the user."""
        open_id = open_id or self._open_id_for_chat(chat_id)
        if not open_id:
            return
        # 任务还在跑的时候 outbox 里多半是中间产物（模型会反复重写同名
        # xlsx、playwright 会追加日志），此时投递会把半成品反复发出去。
        # 统一推迟到任务结束后由 outbox_loop 补投。
        if self.db.has_active_task(chat_id):
            self._outbox_dirty.add(open_id)
            return
        workspace = self._workspace_for(open_id)
        outbox = f"{workspace.rstrip('/')}/{self.cfg.outbox_subdir}"
        try:
            entries = await self.oc.list_dir(open_id, outbox, workspace)
        except OpenCodeError as exc:
            if exc.status_code in (400, 404):
                return
            log.warning("list outbox failed for %s: %s", open_id, exc)
            return
        except Exception as exc:
            log.warning("list outbox failed for %s: %s", open_id, exc)
            return
        names = []
        for entry in entries:
            if entry.get("type") != "file":
                continue
            name = str(entry.get("path") or "")
            if not name or name.endswith("/"):
                continue
            base = os.path.basename(name)
            if base:
                names.append(base)
        # 清理已经不存在的失败记录，避免字典无限增长。
        self._outbox_failures = {
            key: count
            for key, count in self._outbox_failures.items()
            if key[0] != open_id or key[1] in names
        }
        names = [
            name
            for name in names
            if not _OUTBOX_ARTIFACT_RE.match(name)
        ]
        # 单轮最多处理 10 个，避免一次任务结束触发大量下载。
        names.sort()
        batch = names[:10]
        if len(names) > len(batch):
            self._outbox_dirty.add(open_id)
        for name in batch:
            key = (open_id, name)
            if (
                self._outbox_failures.get(key, 0)
                >= _OUTBOX_MAX_ATTEMPTS
            ):
                continue
            remote_path = f"{outbox}/{name}"
            try:
                await self._deliver_outbox_file(
                    chat_id, open_id, remote_path, name
                )
            except Exception:
                attempts = self._outbox_failures.get(key, 0) + 1
                self._outbox_failures[key] = attempts
                if attempts < _OUTBOX_MAX_ATTEMPTS:
                    log.exception(
                        "deliver outbox file %s failed (attempt %s/%s)",
                        name,
                        attempts,
                        _OUTBOX_MAX_ATTEMPTS,
                    )
                    # Retry on the next outbox scan.
                    self._outbox_dirty.add(open_id)
                else:
                    log.error(
                        "giving up outbox file %s after %s attempts; "
                        "it will be retried after restart",
                        name,
                        attempts,
                    )
            else:
                self._outbox_failures.pop(key, None)

    async def _deliver_outbox_file(
        self, chat_id: str, open_id: str, remote_path: str, name: str
    ) -> None:
        workspace = self._workspace_for(open_id)
        tmpdir = Path(self.cfg.upload_tmp_dir)
        tmpdir.mkdir(parents=True, exist_ok=True)
        local_path = str(tmpdir / f"outbox-{os.getpid()}-{time.time_ns()}")
        try:
            size = await self.oc.download_file(
                open_id, remote_path, local_path, workspace
            )
            if size <= 0:
                # 空文件飞书会返回 234001，直接跳过，等有内容了再投递。
                log.info("skip empty outbox file %s", name)
                return
            digest = _sha256_file(local_path)
            if self.db.is_outbox_sent(open_id, digest, size):
                return
            ext = os.path.splitext(name)[1].lower()
            if ext in _IMAGE_EXTENSIONS and size <= self.cfg.max_outbox_image_bytes:
                image_key = await self.feishu.upload_image_async(local_path)
                await self.feishu.send_image_async(chat_id, image_key)
            elif size <= self.cfg.max_outbox_file_bytes:
                file_key = await self.feishu.upload_file_async(
                    local_path, name
                )
                await self.feishu.send_file_async(chat_id, file_key)
            else:
                await self.feishu.send_text_async(
                    chat_id,
                    f"outbox 文件过大，未发送：{name}"
                    f"（{_human_size(size)}，上限 30 MB）",
                )
                # 记入已发送清单，避免每次任务结束都重复提醒；
                # 文件内容变化后 sha256 不同，会重新提醒。
                self.db.mark_outbox_sent(open_id, digest, size, name)
                return
            self.db.mark_outbox_sent(open_id, digest, size, name)
            log.info("delivered outbox file %s to %s", name, open_id)
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass

    async def outbox_loop(self) -> None:
        """Retry undelivered outbox files once the chat is idle."""
        while not self._stopping:
            try:
                await asyncio.sleep(self.cfg.outbox_scan_seconds)
            except asyncio.CancelledError:
                raise
            try:
                self.db.prune_outbox_sent(int(time.time()) - 7 * 24 * 3600)
            except Exception:
                log.exception("prune outbox dedupe failed")
            targets = set(self._outbox_dirty)
            self._outbox_dirty.clear()
            for open_id in targets:
                chat = self.db.get_chat_by_open_id(open_id)
                if not chat:
                    continue
                try:
                    await self._deliver_outbox(chat["chat_id"], open_id)
                except Exception:
                    log.exception("outbox scan failed for %s", open_id)

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
        open_id: str | None = None,
    ) -> str:
        return self._workspace_for(open_id) if open_id else self.cfg.workspace_dir

    async def _get_agents(
        self,
        open_id: str,
        directory: str,
        *,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        now = time.time()
        if (
            not refresh
            and self._agent_catalog
            and self._agent_catalog_dir == directory
            and now - self._agent_catalog_at < 60
        ):
            return self._agent_catalog
        agents = await self.oc.list_agents(open_id, directory)
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

    def _extract_attachments(self, message: Any) -> list[dict[str, str]]:
        """Return image/file keys carried by a message.

        Feishu wraps an image or file sent with a caption in a ``post``
        message: images live in ``content`` elements tagged ``img`` and files
        in the top-level ``files`` array. Pure ``image``/``file`` messages
        carry the key directly. Both shapes must be handled, otherwise the
        caption path silently drops the attachment.
        """
        try:
            content = json.loads(message.content)
        except Exception:
            return []
        if not isinstance(content, dict):
            return []

        attachments: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(kind: str, key: Any, name: Any = None) -> None:
            if not isinstance(key, str) or not key or key in seen:
                return
            seen.add(key)
            item = {"type": kind, "file_key": key}
            if isinstance(name, str) and name:
                item["file_name"] = name
            attachments.append(item)

        message_type = message.message_type
        if message_type == "image":
            add("image", content.get("image_key"))
        elif message_type == "file":
            add("file", content.get("file_key"), content.get("file_name"))
        elif message_type == "post":
            for line in content.get("content") or []:
                if not isinstance(line, list):
                    continue
                for element in line:
                    if not isinstance(element, dict):
                        continue
                    tag = element.get("tag")
                    if tag == "img":
                        add("image", element.get("image_key"))
                    elif tag == "file":
                        add(
                            "file",
                            element.get("file_key"),
                            element.get("file_name"),
                        )
            for item in content.get("files") or []:
                if isinstance(item, dict):
                    add(
                        "file",
                        item.get("file_key"),
                        item.get("file_name"),
                    )
        return attachments

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
            if text.startswith("/"):
                await self._handle_command(chat_id, open_id, text)
                return
            prefix = self._attachment_prefix(chat_id)
            if prefix:
                text = prefix + text
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
            await self.feishu.send_text_async(chat_id, help_text())
        elif command == "/new":
            await self._cmd_new(chat_id, open_id, argument)
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
        elif command in ("/stats", "/stat", "/用量"):
            await self._cmd_stats(chat_id, open_id, argument)
        elif command == "/model":
            await self._cmd_model(chat_id, open_id, argument)
        elif command == "/agent":
            await self._cmd_agent(chat_id, open_id, argument)
        elif command == "/stop":
            await self._cmd_stop(chat_id, argument)
        elif command == "/smoke":
            await self._cmd_smoke(chat_id, open_id)
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
            note=None,
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
        workspace = self._workspace_for(chat.get("open_id") if chat else "")
        note: str | None = None
        sessions = []
        all_sessions, _ = self._list_sessions_with_current(chat_id)
        for session in all_sessions:
            task = self.db.get_active_task(session["session_id"])
            state = _task_state(task)
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

    async def _cmd_stats(
        self, chat_id: str, open_id: str, argument: str
    ) -> None:
        queries = build_stats_queries(argument)
        if queries is None:
            await self.feishu.send_text_async(chat_id, stats_help_text())
            return

        results: list[dict[str, Any]] = []
        try:
            for query in queries:
                results.append(
                    await self.oc.session_stats(
                        open_id,
                        from_ms=query.from_ms,
                        to_ms=query.to_ms,
                        timezone=STATS_TIMEZONE,
                    )
                )
        except OpenCodeError as exc:
            log.exception("session stats failed")
            if exc.status_code == 404:
                await self.feishu.send_text_async(
                    chat_id,
                    "当前 OpenCode 版本不支持用量统计接口"
                    "（/api/experimental/session/stats）。"
                    "请升级用户 LXC 内的 opencode。",
                )
            else:
                await self.feishu.send_text_async(
                    chat_id, f"获取用量统计失败: {exc}"
                )
            return
        except Exception as exc:
            log.exception("session stats failed")
            await self.feishu.send_text_async(
                chat_id, f"获取用量统计失败: {exc}"
            )
            return

        card = stats_card(
            queries=queries,
            results=results,
            note=f"统计范围：用户 OpenCode 会话 · 时区 {STATS_TIMEZONE}",
        )
        await self.feishu.send_card_async(chat_id, card)

    async def _cmd_model(
        self, chat_id: str, open_id: str, argument: str
    ) -> None:
        self.db.ensure_chat(chat_id, open_id)
        try:
            models = await self.oc.list_models(open_id)
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
        directory = self._agent_directory(chat, session_id, open_id)
        try:
            agents = await self._get_agents(
                open_id, directory, refresh=True
            )
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
                        sandbox_mode=self.cfg.sandbox_mode,
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
                    self._final_progress_card(
                        active, status="已中止", error="用户中止", with_tail=True
                    ),
                )
            except Exception:
                log.exception("update interrupted card failed")
        self._clear_progress(active["id"])
        await self.feishu.send_text_async(
            chat_id, f"已向会话 {title} 发送中止请求。"
        )

    async def _cmd_smoke(self, chat_id: str, open_id: str) -> None:
        """Run a minimal end-to-end check against the user's OpenCode server."""
        lines = ["冒烟测试"]
        ok = True
        workspace = self._workspace_for(open_id)
        lines.append(f"工作区: {workspace}")

        try:
            models = await self.oc.list_models(open_id)
            lines.append(f"✅ 连接 OpenCode 成功（{len(models)} 个模型）")
        except Exception as exc:
            ok = False
            lines.append(f"❌ 连接 OpenCode 失败: {exc}")

        if ok:
            tmpdir = Path(self.cfg.upload_tmp_dir)
            tmpdir.mkdir(parents=True, exist_ok=True)
            stamp = int(time.time())
            local = str(tmpdir / f"smoke-{stamp}.bin")
            remote = f"{workspace.rstrip('/')}/.smoke-{stamp}.bin"
            payload = os.urandom(1024 * 1024)
            readback = str(tmpdir / f"smoke-{stamp}.out")
            try:
                with open(local, "wb") as handle:
                    handle.write(payload)
                await self.oc.upload_file(
                    open_id, local, remote, workspace
                )
                await self.oc.download_file(
                    open_id, remote, readback, workspace
                )
                with open(readback, "rb") as handle:
                    got = handle.read()
                if got == payload:
                    lines.append("✅ 文件写入/读取一致")
                else:
                    ok = False
                    lines.append("❌ 文件写入/读取内容不一致")
            except Exception as exc:
                ok = False
                lines.append(f"❌ 文件写入/读取失败: {exc}")
            finally:
                for path in (local, readback):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

        lines.append("结果: " + ("通过" if ok else "失败"))
        await self.feishu.send_text_async(chat_id, "\n".join(lines))

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
        if _is_stats_request(prompt):
            mode = "stats"
            prompt = "用量统计"
        else:
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
                if mode == "reminder":
                    label, prompt_label = "单次提醒", "提醒"
                elif mode == "stats":
                    label, prompt_label = "单次用量统计", "统计"
                else:
                    label, prompt_label = "单次任务", "任务"
                when = datetime.fromtimestamp(
                    schedule["next_run_at"], SCHEDULE_TZ
                ).strftime("%m-%d %H:%M")
                lines.append(
                    f"#{schedule['id']} {schedule['title']}  [{status}]"
                )
                lines.append(f"   {label} · {when}")
                lines.append(
                    f"   {prompt_label}：{_short(schedule['prompt'], 120)}"
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
            if mode == "reminder":
                prompt_label = "提醒"
            elif mode == "stats":
                prompt_label = "统计"
            else:
                prompt_label = "任务"
            lines.append(
                f"   {prompt_label}：{_short(schedule['prompt'], 120)}"
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
            if schedule.get("mode") == "reminder":
                noun = "单次提醒"
            elif schedule.get("mode") == "stats":
                noun = "单次用量统计"
            else:
                noun = "单次任务"
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
        if schedule.get("mode") == "reminder":
            action = "发送提醒"
        elif schedule.get("mode") == "stats":
            action = "发送用量统计"
        else:
            action = "立即执行定时任务"
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
        if mode not in ("reminder", "stats"):
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
            elif mode == "stats":
                lines = [
                    f"已创建单次用量统计 #{schedule_id}：{title}",
                    f"执行时间：{when}",
                    "到点会直接发送用量统计卡片，不创建会话。",
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
            elif mode == "stats":
                lines = [
                    f"已创建定时用量统计 #{schedule_id}：{title}",
                    f"周期：{_schedule_label(kind, time_of_day, weekday)}",
                    f"下次执行：{when}",
                    "到点会直接发送用量统计卡片，不创建会话。",
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
        if schedule.get("mode") == "stats":
            await self._cmd_stats(
                schedule["chat_id"], schedule["open_id"], ""
            )
            log.info("schedule %s sent stats", schedule["id"])
            return
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
        directory = self._workspace_for(open_id)

        agent_info: dict[str, Any] | None = None
        try:
            agents = await self._get_agents(open_id, directory)
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
            sandbox_mode=self.cfg.sandbox_mode,
        )

        session_id = await self.oc.create_session(
            open_id,
            title=session_title,
            directory=directory,
            agent=agent,
            provider_id=provider_id,
            model_id=model_id,
            permissions=permissions,
        )

        try:
            self.db.add_session(
                session_id,
                chat_id,
                open_id,
                session_title,
                provider_id,
                model_id,
                agent,
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
            raise

        self._session_ids.add(session_id)
        log.info(
            "created session %s for chat %s (activate=%s)",
            session_id,
            chat_id,
            activate,
        )
        return session_id

    def _touch_session(self, session_id: str) -> None:
        self.db.touch_session(session_id)

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

        chat = self.db.get_chat(chat_id)
        if not chat:
            operator = event.operator.open_id if event.operator else None
            if not operator:
                return _toast("error", "无法确定用户")
            self.db.ensure_chat(chat_id, operator)
            chat = self.db.get_chat(chat_id)
        open_id = chat.get("open_id") if chat else None
        if not open_id:
            return _toast("error", "无法确定用户")

        catalog = self._model_catalog
        if not catalog:
            try:
                catalog = self.oc_sync.list_models(open_id)
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

        open_id = chat.get("open_id") if chat else None
        if not open_id:
            return _toast("error", "无法确定用户")

        directory = self._agent_directory(chat, session_id, open_id)
        catalog = self._agent_catalog
        if not catalog or self._agent_catalog_dir != directory:
            try:
                catalog = self.oc_sync.list_agents(open_id, directory)
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
                        sandbox_mode=self.cfg.sandbox_mode,
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
        """Keep one SSE connection per unique OpenCode server."""
        tasks: dict[str, asyncio.Task[Any]] = {}
        while not self._stopping:
            try:
                servers = self._sse_servers()
                for base_url, auth in servers.items():
                    task = tasks.get(base_url)
                    if task is None or task.done():
                        tasks[base_url] = asyncio.create_task(
                            self._sse_server_loop(base_url, auth)
                        )
                for base_url in list(tasks):
                    if base_url not in servers:
                        tasks[base_url].cancel()
                        del tasks[base_url]
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("sse supervisor failed")
            await asyncio.sleep(5)

    def _sse_servers(self) -> dict[str, tuple[str, str]]:
        """Return base_url -> (username, password) for every server."""
        if not self.users.configured:
            return {
                self.cfg.opencode_base_url.rstrip("/"): (
                    self.cfg.opencode_username,
                    self.cfg.opencode_password,
                )
            }
        servers: dict[str, tuple[str, str]] = {}
        for entry in self.users.by_url().values():
            servers[entry.base_url] = (entry.username, entry.password)
        return servers

    async def _sse_server_loop(
        self, base_url: str, auth: tuple[str, str]
    ) -> None:
        backoff = 1
        while not self._stopping:
            try:
                await self._consume_sse(base_url, auth)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("SSE disconnected from %s: %s", base_url, exc)
            if self._stopping:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _consume_sse(
        self, base_url: str, auth: tuple[str, str]
    ) -> None:
        timeout = httpx.Timeout(
            connect=5.0, read=None, write=10.0, pool=5.0
        )
        async with httpx.AsyncClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
        ) as client:
            async with aconnect_sse(
                client, "GET", "/api/event"
            ) as source:
                log.info("SSE connected to %s", base_url)
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

        location = event.get("location") or {}
        directory = (
            location.get("directory") if isinstance(location, dict) else None
        )

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
        elif event_type == "session.step.started":
            await self._on_step_started(session_id, data)
        elif event_type == "session.step.first-token":
            await self._on_step_first_token(session_id)
        elif event_type == "session.reasoning.delta":
            await self._on_stream_delta(session_id, "reasoning", data)
        elif event_type == "session.reasoning.ended":
            await self._on_stream_ended(session_id, "reasoning", data)
        elif event_type == "session.text.delta":
            await self._on_stream_delta(session_id, "text", data)
        elif event_type == "session.tool.input.started":
            self._tool_names[data.get("id", "")] = data.get("name", "")
            await self._on_tool_input_started(session_id, data)
        elif event_type == "session.tool.called":
            await self._on_tool_event(session_id, "called", data)
        elif event_type == "session.tool.progress":
            await self._on_tool_progress(session_id, data, directory)
        elif event_type == "session.tool.success":
            await self._on_tool_event(session_id, "success", data)
        elif event_type == "session.tool.failed":
            await self._on_tool_event(session_id, "failed", data)
        elif event_type == "session.shell.started":
            await self._on_shell_started(session_id, data, directory)
        elif event_type == "session.shell.ended":
            await self._on_shell_ended(session_id, data)
        elif event_type == "session.text.ended":
            await self._on_text_ended(session_id, data)
        elif event_type == "session.usage.updated":
            self._usage[session_id] = data
            await self._on_usage_updated(session_id, data)
        elif event_type == "session.compaction.started":
            await self._on_compaction(session_id, "started")
        elif event_type == "session.compaction.ended":
            await self._on_compaction(session_id, "ended")
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
        state = self._progress_state(task["id"])
        state.status = "执行中"
        state.phase = "启动"
        state.add_line("▶ 开始执行")
        await self._schedule_progress(task, force=True)

    # ------------------------------------------------------------------
    # realtime progress state
    # ------------------------------------------------------------------
    def _progress_state(self, task_id: int) -> _TaskProgress:
        state = self._progress.get(task_id)
        if state is None:
            state = _TaskProgress()
            self._progress[task_id] = state
        return state

    def _active_progress(
        self, session_id: str | None
    ) -> tuple[dict[str, Any] | None, _TaskProgress | None]:
        if not session_id:
            return None, None
        task = self.db.get_active_task(session_id)
        if not task:
            return None, None
        return task, self._progress_state(task["id"])

    async def _schedule_progress(
        self, task: dict[str, Any], *, force: bool = False
    ) -> None:
        """节流刷新：事件只改内存状态，卡片更新放到后台任务里做。

        这样 SSE 消费循环不会被飞书/OpenCode 的网络请求阻塞。
        """
        state = self._progress_state(task["id"])
        state.dirty = True
        if state.closed:
            return
        now = time.time()
        interval = max(1, self.cfg.progress_min_interval)
        deadline = state.last_push + interval
        delay = 0.0 if force or now >= deadline else deadline - now
        if state.timer is not None and not state.timer.done():
            # 已有刷新任务在跑，它结束后会检查 dirty 并补一次尾随刷新。
            return
        timer = asyncio.create_task(self._flush_later(task["id"], delay))
        state.timer = timer
        self._background_tasks.add(timer)
        timer.add_done_callback(self._background_tasks.discard)

    async def _flush_later(self, task_id: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._flush_progress(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("flush progress for task %s failed", task_id)

    async def _flush_progress(self, task_id: int) -> None:
        state = self._progress.get(task_id)
        if state is None or not state.dirty or state.closed:
            return
        task = self.db.get_task(task_id)
        if not task or task["status"] not in ("queued", "running"):
            return
        state.dirty = False
        state.last_push = time.time()
        await self._collect_shell_output(task, state)
        if state.closed:
            return
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"], self._progress_card(task, state)
                )
            except Exception:
                log.exception("update progress card failed")
        if state.shell_id and not state.closed:
            # 命令还在跑：即使没有新事件，也按节流间隔继续拉取输出。
            state.dirty = True
        if state.dirty and not state.closed:
            # 当前刷新任务还没结束，先清掉引用，才能排下一次尾随刷新。
            state.timer = None
            await self._schedule_progress(task)

    async def _collect_shell_output(
        self, task: dict[str, Any], state: _TaskProgress
    ) -> bool:
        if not state.shell_id or state.collecting:
            return False
        state.collecting = True
        try:
            return await self._collect_shell_output_locked(task, state)
        finally:
            state.collecting = False

    async def _collect_shell_output_locked(
        self, task: dict[str, Any], state: _TaskProgress
    ) -> bool:
        if not state.shell_id:
            return False
        method = getattr(self.oc, "shell_output", None)
        if method is None:
            return False
        for _ in range(4):
            cursor_before = state.cursor
            try:
                chunk = await method(
                    task["session_id"],
                    state.shell_id,
                    directory=state.shell_dir,
                    cursor=cursor_before,
                    limit=_PROGRESS_SHELL_CHUNK,
                )
            except OpenCodeError as exc:
                if exc.status_code == 404:
                    state.shell_id = None
                return False
            except Exception as exc:
                log.debug("read shell output failed: %s", exc)
                return False
            if state.cursor != cursor_before:
                # 另一条路径已经推进过游标，丢弃这次重复结果。
                return False
            if not isinstance(chunk, dict):
                return False
            output = chunk.get("output")
            if isinstance(output, str) and output:
                state.add_output(output)
            previous = state.cursor
            cursor = chunk.get("cursor")
            if isinstance(cursor, int):
                state.cursor = cursor
            if chunk.get("truncated"):
                state.truncated = True
            if not chunk.get("truncated") or state.cursor == previous:
                break
        return True

    def _progress_card(
        self, task: dict[str, Any], state: _TaskProgress
    ) -> dict[str, Any]:
        return progress_card(
            status=state.status,
            prompt=task["prompt"],
            tool=state.tool or None,
            phase=state.phase or None,
            elapsed=self._task_elapsed(task),
            session_id=task["session_id"],
            usage=self._usage.get(task["session_id"]),
            log_tail=state.window(),
            log_full=state.full_log(),
        )

    def _clear_progress(self, task_id: int) -> None:
        state = self._progress.pop(task_id, None)
        if state is None:
            return
        state.closed = True
        if state.timer is not None:
            state.timer.cancel()

    def _commit_partial(self, state: _TaskProgress, marker: str = "💬") -> None:
        """把尚未结束的流式片段落成一行日志，避免工具开始后残留在窗口里。"""
        text = state.partial.strip()
        state.partial = ""
        state.partial_kind = ""
        if text:
            state.add_line(marker + " " + _short(text.replace("\n", " "), 200))

    async def _on_step_started(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        self._commit_partial(state)
        state.step += 1
        state.status = "执行中"
        state.phase = "模型思考中"
        state.add_line(f"◆ 步骤 {state.step} 开始")
        await self._schedule_progress(task)

    async def _on_step_first_token(self, session_id: str) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        state.phase = "模型输出中"
        await self._schedule_progress(task)

    async def _on_stream_delta(
        self, session_id: str, kind: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        state.phase = "模型思考中" if kind == "reasoning" else "模型输出中"
        state.partial_kind = kind
        state.set_partial(state.partial + delta)
        await self._schedule_progress(task)

    async def _on_stream_ended(
        self, session_id: str, kind: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        text = data.get("text")
        if not isinstance(text, str) or not text:
            text = state.partial
        state.partial = ""
        state.partial_kind = ""
        if kind == "reasoning":
            state.phase = "思考完成"
            if text:
                state.add_line("💭 " + _short(text.replace("\n", " "), 200))
        else:
            state.phase = "输出完成"
            if text:
                state.add_line("💬 " + _short(text.replace("\n", " "), 200))
        await self._schedule_progress(task)

    async def _on_text_ended(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        self._final_text[session_id] = data.get("text") or ""
        await self._on_stream_ended(session_id, "text", data)

    async def _on_tool_input_started(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        self._commit_partial(state)
        name = data.get("name") or "tool"
        state.tool = str(name)
        state.phase = f"准备调用 {name}"
        await self._schedule_progress(task)

    async def _on_tool_progress(
        self,
        session_id: str,
        data: dict[str, Any],
        directory: str | None,
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        metadata = data.get("metadata") or {}
        shell_id = None
        if isinstance(metadata, dict):
            shell_id = (
                metadata.get("shellID")
                or metadata.get("shellId")
                or metadata.get("shell_id")
            )
            if not shell_id:
                shell = metadata.get("shell")
                if isinstance(shell, dict):
                    shell_id = shell.get("id")
        if not shell_id:
            return
        shell_id = str(shell_id)
        # 同一个 shell 会多次上报 progress，只有换了 shell 才重置游标，
        # 否则会把已经读过的输出重复追加一遍。
        if state.shell_id != shell_id:
            state.cursor = 0
        state.shell_id = shell_id
        state.shell_dir = directory or state.shell_dir
        state.status = "执行中"
        state.phase = "命令执行中"
        state.tool = "shell"
        await self._schedule_progress(task)

    async def _on_shell_started(
        self,
        session_id: str,
        data: dict[str, Any],
        directory: str | None,
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        shell = data.get("shell") or {}
        if not isinstance(shell, dict):
            return
        shell_id = shell.get("id")
        if not shell_id:
            return
        self._commit_partial(state)
        state.shell_id = str(shell_id)
        state.shell_dir = directory or shell.get("cwd") or state.shell_dir
        state.cursor = 0
        state.status = "执行中"
        state.phase = "命令执行中"
        state.tool = "shell"
        command = shell.get("command")
        if command:
            state.add_line("$ " + _short(str(command), 200))
        await self._schedule_progress(task)

    async def _on_shell_ended(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        # 先做一次增量读取，避免漏掉最后一段输出。
        await self._collect_shell_output(task, state)
        output = data.get("output") or {}
        if isinstance(output, dict):
            text = output.get("output")
            if isinstance(text, str) and text:
                # 事件里带的是完整输出；按已读游标只取后缀，避免重复。
                start = state.cursor
                if start > 0:
                    raw = text.encode("utf-8")
                    text = raw[start:].decode("utf-8", "ignore")
                if text:
                    state.add_output(text)
            if output.get("truncated"):
                state.truncated = True
        shell = data.get("shell") or {}
        shell_status = None
        exit_code = None
        if isinstance(shell, dict):
            shell_status = shell.get("status")
            exit_code = shell.get("exit")
        state.flush_output()
        state.shell_id = None
        state.cursor = 0
        failed = shell_status in ("timeout", "killed") or (
            isinstance(exit_code, (int, float)) and exit_code != 0
        )
        if failed:
            state.phase = "命令失败"
            state.add_line(
                f"✗ 命令结束（status={shell_status}, exit={exit_code}）"
            )
        else:
            state.phase = "命令结束"
        await self._schedule_progress(task)

    async def _on_usage_updated(
        self, session_id: str, data: dict[str, Any]
    ) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        await self._schedule_progress(task)

    async def _on_compaction(self, session_id: str, phase: str) -> None:
        task, state = self._active_progress(session_id)
        if not task or not state:
            return
        if phase == "started":
            state.phase = "压缩上下文"
            state.add_line("⟳ 开始压缩上下文")
        else:
            state.phase = "上下文已压缩"
            state.add_line("✓ 上下文压缩完成")
        await self._schedule_progress(task)

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
            marker = "▶"
        elif phase == "success":
            summary = _summarize_output(data)
            status = "工具完成"
            marker = "✓"
        else:
            summary = _summarize_output(data)
            status = "工具失败"
            marker = "✗"

        label = f"{tool} {summary}".strip()
        state = self._progress_state(task["id"])
        if phase == "called":
            self._commit_partial(state)
        state.add_line(f"{marker} {label}")
        await self._update_progress(task, status=status, tool=label)

    async def _update_progress(
        self,
        task: dict[str, Any],
        *,
        status: str,
        tool: str | None = None,
        phase: str | None = None,
        force: bool = False,
    ) -> None:
        state = self._progress_state(task["id"])
        state.status = status
        if tool is not None:
            state.tool = tool
        if phase is not None:
            state.phase = phase
        await self._schedule_progress(task, force=force)

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
        state = self._progress_state(task["id"])
        state.add_line(f"⟳ 上游报错，重试中（第 {attempt} 次）")
        await self._update_progress(
            task,
            status=f"上游报错，重试中（第 {attempt} 次）",
            phase="等待重试",
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
        state = self._progress_state(task["id"])
        state.add_line("✗ 会话错误: " + _short(error, 200))
        await self._finish_task_failure(task, error)

    def _task_elapsed(self, task: dict[str, Any]) -> int:
        return int(
            time.time()
            - self._started_at.get(task["id"], task["created_at"])
        )

    def _final_progress_card(
        self,
        task: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
        final_text: str | None = None,
        show_result: bool = True,
        usage: dict[str, Any] | None = None,
        with_tail: bool = True,
    ) -> dict[str, Any]:
        state = self._progress.get(task["id"])
        if state is not None:
            # 终态卡片一旦开始构建，就冻结实时状态，避免在途的节流刷新
            # 用「执行中」卡片覆盖最终结果。
            state.closed = True
            if state.timer is not None:
                state.timer.cancel()
        return progress_card(
            status=status,
            prompt=task["prompt"],
            tool=state.tool if state else None,
            phase=state.phase if state else None,
            elapsed=self._task_elapsed(task),
            final_text=final_text,
            error=error,
            session_id=task["session_id"],
            usage=usage,
            max_final_chars=self.cfg.max_final_chars,
            show_result=show_result,
            log_tail=state.window() if state and with_tail else None,
            log_full=state.full_log() if state else None,
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
            self._clear_progress(task["id"])
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        result_card = self._final_progress_card(
            task,
            status="完成",
            final_text=final_text or "（无文本输出）",
            usage=usage,
            with_tail=False,
        )
        # 原进度卡只收尾成「完成」，结果单独在聊天底部发一张新卡。
        # 飞书更新卡片不会触发消息通知，只更新原卡用户很容易错过结果；
        # 审批卡也可能把结果压在更上面，所以统一发新卡。
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"],
                    self._final_progress_card(
                        task,
                        status="完成",
                        final_text="",
                        show_result=False,
                        usage=usage,
                        with_tail=True,
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
        try:
            open_id = self._open_id_for_session(session_id)
            await self._deliver_outbox(task["chat_id"], open_id)
        except Exception:
            log.exception("deliver outbox failed for task %s", task["id"])
        self._clear_progress(task["id"])
        log.info("task %s succeeded", task["id"])

    async def _finish_task_failure(
        self, task: dict[str, Any], error: str
    ) -> None:
        if not self.db.complete_task(
            task["id"], "failed", error=error
        ):
            self._clear_progress(task["id"])
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        card = self._final_progress_card(
            task, status="失败", error=error, with_tail=True
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
        self._clear_progress(task["id"])
        log.info("task %s failed: %s", task["id"], error)

    async def _finish_task_interrupted(
        self, task: dict[str, Any], reason: str
    ) -> None:
        if not self.db.complete_task(
            task["id"], "interrupted", error=reason
        ):
            self._clear_progress(task["id"])
            return
        session_id = task["session_id"]
        self._touch_session(session_id)
        card = self._final_progress_card(
            task, status="已中止", error=reason, with_tail=True
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
        self._clear_progress(task["id"])
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
            await asyncio.sleep(self.cfg.task_reconcile_seconds)

    async def _reconcile_active_tasks(self) -> None:
        tasks = self.db.list_active_tasks()
        if not tasks:
            return
        by_user: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            open_id = self._open_id_for_session(task["session_id"])
            if not open_id:
                continue
            by_user.setdefault(open_id, []).append(task)

        now = time.time()
        for open_id, user_tasks in by_user.items():
            try:
                active_sessions = await self.oc.list_active_sessions(open_id)
            except Exception as exc:
                log.warning(
                    "list active sessions failed for %s: %s", open_id, exc
                )
                continue
            for task in user_tasks:
                try:
                    await self._reconcile_task(
                        task, active_sessions, now
                    )
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
            self._clear_progress(task_id)
            return False
        self.db.update_form(form["id"], interrupted=1)
        if task.get("card_message_id"):
            try:
                await self.feishu.update_card_async(
                    task["card_message_id"],
                    self._final_progress_card(
                        task, status="已中止", error=reason, with_tail=True
                    ),
                )
            except Exception:
                log.exception("update interrupted task card failed")
        await self.feishu.send_text_async(
            task["chat_id"], f"任务 #{task_id} 已中断：{reason}"
        )
        self._clear_progress(task_id)
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
                    self._final_progress_card(
                        task,
                        status="失败",
                        error="审批超时，已自动拒绝",
                        with_tail=True,
                    ),
                )
            except Exception:
                log.exception("update timeout task card failed")
        self._clear_progress(task_id)
