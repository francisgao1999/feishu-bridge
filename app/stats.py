from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

STATS_PATH = "/api/experimental/session/stats"
STATS_TIMEZONE = "Asia/Shanghai"
STATS_TZ = ZoneInfo(STATS_TIMEZONE)


class StatsError(RuntimeError):
    pass


@dataclass(frozen=True)
class StatsQuery:
    """One time range to request from the OpenCode stats endpoint."""

    label: str
    from_ms: int | None = None
    to_ms: int | None = None
    show_models: bool = False


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _day_start(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def _year_query(year: int, now: datetime) -> StatsQuery:
    start = datetime(year, 1, 1, tzinfo=STATS_TZ)
    end = datetime(year + 1, 1, 1, tzinfo=STATS_TZ)
    return StatsQuery(
        f"{year} 年",
        _ms(start),
        _ms(min(end, now)),
        show_models=True,
    )


def build_stats_queries(
    argument: str, now: datetime | None = None
) -> list[StatsQuery] | None:
    """Turn a /stats argument into the ranges to query.

    ``None`` means the argument is not understood; the caller should show
    usage help instead of guessing.
    """

    moment = now or datetime.now(STATS_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=STATS_TZ)
    token = argument.strip().lower()

    if not token:
        today = _day_start(moment)
        return [
            StatsQuery(
                "最近 14 天",
                _ms(today - timedelta(days=13)),
                _ms(moment),
                show_models=True,
            ),
            StatsQuery("本月", _ms(today.replace(day=1)), _ms(moment)),
        ]

    if token in ("all", "lifetime", "全部", "历史"):
        return [StatsQuery("全部", None, None, show_models=True)]

    if token in ("today", "今天", "今日"):
        return [
            StatsQuery(
                "今天", _ms(_day_start(moment)), _ms(moment), show_models=True
            )
        ]

    if token in ("year", "今年", "本年", "年"):
        return [_year_query(moment.year, moment)]

    if token.isdigit() and len(token) == 4:
        return [_year_query(int(token), moment)]

    if token.isdigit():
        days = int(token)
        if days <= 0:
            return None
        start = _day_start(moment) - timedelta(days=days - 1)
        return [
            StatsQuery(
                f"最近 {days} 天",
                _ms(start),
                _ms(moment),
                show_models=True,
            )
        ]

    return None


def _trim_float(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def fmt_int(value: Any) -> str:
    return f"{_as_int(value):,}"


def fmt_tokens(value: Any) -> str:
    count = _as_int(value)
    if count >= 1_000_000_000:
        return f"{_trim_float(count / 1_000_000_000)}b"
    if count >= 1_000_000:
        return f"{_trim_float(count / 1_000_000)}m"
    if count >= 1_000:
        return f"{_trim_float(count / 1_000)}k"
    return str(count)


def total_tokens(tokens: Any) -> int:
    if not isinstance(tokens, dict):
        return 0
    total = sum(
        _as_int(tokens.get(key))
        for key in ("input", "output", "reasoning")
    )
    cache = tokens.get("cache")
    if isinstance(cache, dict):
        total += _as_int(cache.get("read")) + _as_int(cache.get("write"))
    return total


def tool_success_rate(tools: Any) -> str | None:
    if not isinstance(tools, dict):
        return None
    totals = tools.get("totals")
    if not isinstance(totals, dict):
        return None
    calls = _as_int(totals.get("calls"))
    if calls <= 0:
        return None
    succeeded = _as_int(totals.get("succeeded"))
    return f"{succeeded / calls * 100:.1f}%"


def model_label(model: Any) -> str:
    if not isinstance(model, dict):
        return "-"
    provider = str(model.get("providerID") or "").strip()
    model_id = str(model.get("id") or "").strip()
    if provider and model_id:
        return f"{provider}/{model_id}"
    return model_id or provider or "-"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def block_lines(query: StatsQuery, data: dict[str, Any]) -> list[str]:
    """Render one range as markdown lines for a Feishu card."""

    lines = [f"**{query.label}**"]
    summary = f"{_as_int(data.get('sessions'))} 会话"
    subagents = _as_int(data.get("subagents"))
    if subagents:
        summary += f" · {subagents} 子 agent"
    summary += (
        f" · {_as_int(data.get('prompts'))} 提示"
        f" · {fmt_int(data.get('steps'))} 步"
    )
    lines.append(summary)

    detail = f"{fmt_tokens(total_tokens(data.get('tokens')))} tokens"
    rate = tool_success_rate(data.get("tools"))
    if rate:
        detail += f" · 工具成功率 {rate}"
    cost = data.get("cost")
    if isinstance(cost, (int, float)) and cost > 0:
        detail += f" · 费用 ${cost:.2f}"
    lines.append(detail)

    lines.append(
        f"活跃 {_as_int(data.get('activeDays'))} 天"
        f" · 最长连续 {_as_int(data.get('streak'))} 天"
    )

    if query.show_models:
        models = data.get("models")
        if isinstance(models, list):
            top = [item for item in models if isinstance(item, dict)][:3]
            if top:
                lines.append("**模型前三**")
                for index, item in enumerate(top, 1):
                    lines.append(
                        f"{index}. `{_clip(model_label(item.get('model')), 48)}`"
                        f" · {fmt_int(item.get('steps'))} 步"
                        f" · {fmt_tokens(total_tokens(item.get('tokens')))}"
                    )
    return lines


def stats_card(
    *,
    queries: list[StatsQuery],
    results: list[dict[str, Any]],
    title: str = "opencode 用量",
    note: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, (query, data) in enumerate(zip(queries, results)):
        if index:
            elements.append({"tag": "hr"})
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "\n".join(block_lines(query, data)),
                },
            }
        )
    if note:
        elements.append(
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": note}],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }


def stats_help_text() -> str:
    return (
        "用量统计\n"
        "/stats  最近 14 天 + 本月汇总 + 模型前三\n"
        "/stats 7  最近 7 天（也可 14 / 30 / today）\n"
        "/stats year  今年；/stats 2025 指定年份\n"
        "/stats all  全部历史\n"
        "定时推送：/schedule daily 22:00 stats"
    )
