from __future__ import annotations

import json
import math
import re
from typing import Any


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（已截断，共 {len(text)} 字符）"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _md(text: str, limit: int = 2000) -> dict[str, Any]:
    return {"tag": "markdown", "content": _truncate(text, limit)}


def _note(text: str, limit: int = 500) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": _truncate(text, limit),
            "text_size": "notation",
            "text_color": "grey",
        },
    }


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def parse_fields(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def parse_answer(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def _options(options: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, option in enumerate(options):
        if not isinstance(option, dict):
            continue
        label = str(
            option.get("label")
            or option.get("value")
            or f"选项 {index + 1}"
        )
        result.append(
            {
                "text": {
                    "tag": "plain_text",
                    "content": _clip(label, 100),
                },
                "value": str(index),
            }
        )
    return result


def _option_label(field: dict[str, Any], value: Any) -> str | None:
    options = field.get("options") or []
    try:
        index = int(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else None
    if 0 <= index < len(options) and isinstance(options[index], dict):
        option = options[index]
        return str(
            option.get("value") or option.get("label") or value
        )
    return str(value) if value is not None else None


def _field_component(
    field: dict[str, Any], key: str
) -> dict[str, Any] | None:
    field_type = str(field.get("type") or "string")
    title = str(field.get("title") or "")
    description = str(field.get("description") or "")
    placeholder = description or title or "请输入"
    options = field.get("options") or []

    if field_type == "external":
        url = str(field.get("url") or "")
        label = title or "打开链接"
        if url:
            return _md(f"[{label}]({url})")
        return _md(label)

    if field_type == "multiselect":
        if options:
            return {
                "tag": "multi_select_static",
                "name": key,
                "placeholder": {
                    "tag": "plain_text",
                    "content": placeholder,
                },
                "width": "fill",
                "options": _options(options),
            }
        return {
            "tag": "input",
            "name": key,
            "placeholder": {
                "tag": "plain_text",
                "content": "多个答案用逗号分隔",
            },
            "width": "fill",
        }

    if field_type == "boolean":
        return {
            "tag": "select_static",
            "name": key,
            "placeholder": {"tag": "plain_text", "content": "请选择"},
            "width": "fill",
            "options": [
                {
                    "text": {"tag": "plain_text", "content": "是"},
                    "value": "true",
                },
                {
                    "text": {"tag": "plain_text", "content": "否"},
                    "value": "false",
                },
            ],
        }

    if field_type in ("number", "integer"):
        return {
            "tag": "input",
            "name": key,
            "placeholder": {
                "tag": "plain_text",
                "content": "请输入数字",
            },
            "width": "fill",
        }

    if options:
        return {
            "tag": "select_static",
            "name": key,
            "placeholder": {"tag": "plain_text", "content": placeholder},
            "width": "fill",
            "options": _options(options),
        }
    return {
        "tag": "input",
        "name": key,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "width": "fill",
    }


def _option_notes(field: dict[str, Any]) -> dict[str, Any] | None:
    options = field.get("options") or []
    lines: list[str] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        description = str(option.get("description") or "").strip()
        if not description:
            continue
        label = str(
            option.get("label") or option.get("value") or "选项"
        )
        lines.append(f"- **{_clip(label, 60)}**: {_clip(description, 200)}")
    if not lines:
        return None
    return _md("\n".join(lines))


def build_form_card(
    *,
    form_id: str,
    title: str,
    fields: list[dict[str, Any]],
    prompt: str = "",
    session_id: str = "",
    expire_seconds: int = 1800,
) -> dict[str, Any]:
    safe_id = _safe_name(form_id)
    elements: list[dict[str, Any]] = []
    if prompt:
        elements.append(_md(f"**任务**\n{_truncate(prompt, 500)}"))
        elements.append({"tag": "hr"})

    form_elements: list[dict[str, Any]] = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict) or field.get("hidden"):
            continue
        key = str(field.get("key") or f"q{index}")
        field_type = str(field.get("type") or "string")
        field_title = str(field.get("title") or "")
        description = str(field.get("description") or "")

        if field_title or description:
            lines: list[str] = []
            if field_title:
                marker = " *" if field.get("required") else ""
                lines.append(f"**{field_title}{marker}**")
            if description:
                lines.append(description)
            form_elements.append(_md("\n".join(lines)))

        component = _field_component(field, key)
        if component:
            form_elements.append(component)
        option_notes = _option_notes(field)
        if option_notes:
            form_elements.append(option_notes)

        if (
            field.get("custom")
            and field_type in ("string", "multiselect")
            and field.get("options")
        ):
            custom_placeholder = (
                "其他答案（可选，多个用逗号分隔）"
                if field_type == "multiselect"
                else "其他答案（可选，填写后优先使用）"
            )
            form_elements.append(
                {
                    "tag": "input",
                    "name": f"{key}__custom",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": custom_placeholder,
                    },
                    "width": "fill",
                }
            )

    form_elements.append(
        {
            "tag": "column_set",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "提交",
                            },
                            "type": "primary",
                            "form_action_type": "submit",
                            "name": f"submit_{form_id}",
                        }
                    ],
                }
            ],
        }
    )

    elements.append(
        {
            "tag": "form",
            "name": f"form_{safe_id}",
            "elements": form_elements,
        }
    )
    elements.append(
        {
            "tag": "column_set",
            "columns": [
                {
                    "tag": "column",
                    "width": "auto",
                    "elements": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "取消提问",
                            },
                            "type": "default",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "form_cancel",
                                        "form_id": form_id,
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    if expire_seconds > 0:
        note = (
            f"{max(1, expire_seconds // 60)} 分钟内未处理将自动取消"
            "并中断任务。"
        )
    else:
        note = "提问不会自动过期。"
    if session_id:
        note += f" 会话 {session_id}"
    elements.append(_note(note))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": "OpenCode 需要你的输入",
            },
            "subtitle": {
                "tag": "plain_text",
                "content": _clip(title or "提问", 80),
            },
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def build_form_resolved_card(
    *,
    title: str,
    fields: list[dict[str, Any]],
    status: str,
    answer: dict[str, Any] | None = None,
    message: str | None = None,
    prompt: str = "",
    session_id: str = "",
    interrupted: bool = False,
) -> dict[str, Any]:
    if status == "replied":
        template = "green"
        header_title = "提问已提交"
    elif status == "cancelled":
        template = "grey"
        header_title = "提问已取消"
    elif status == "expired":
        template = "orange"
        header_title = "提问已过期"
    else:
        template = "grey"
        header_title = "提问已处理"

    elements: list[dict[str, Any]] = []
    if prompt:
        elements.append(_md(f"**任务**\n{_truncate(prompt, 500)}"))
        elements.append({"tag": "hr"})

    values = answer or {}
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        if field.get("hidden") or field.get("type") == "external":
            continue
        key = str(field.get("key") or f"q{index}")
        field_title = str(field.get("title") or key)
        value = values.get(key)
        if isinstance(value, list):
            text = ", ".join(str(item) for item in value) or "(未回答)"
        elif isinstance(value, bool):
            text = "是" if value else "否"
        elif value is None or value == "":
            text = "(未回答)"
        else:
            text = str(value)
        elements.append(_md(f"**{field_title}**\n{text}"))

    if message:
        elements.append({"tag": "hr"})
        elements.append(_md(f"**说明**: {message}"))

    if status == "replied":
        elements.append(_md("已提交，OpenCode 将继续执行。"))
    elif status == "expired":
        if interrupted:
            elements.append(_md("任务已中断。请重新发送任务。"))
        else:
            elements.append(_md("提问已过期。"))
    elif status == "cancelled":
        if interrupted:
            elements.append(_md("提问已取消，任务已中断。"))
        else:
            elements.append(_md("提问已取消。"))

    if session_id:
        elements.append(_note(f"会话 {session_id}"))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "subtitle": {
                "tag": "plain_text",
                "content": _clip(title or "提问", 80),
            },
            "template": template,
        },
        "body": {"elements": elements},
    }


def answer_from_form_value(
    fields: list[dict[str, Any]],
    form_value: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert Feishu ``form_value`` into an OpenCode form answer.

    Returns ``(answer, error)``. ``answer`` is ``None`` when validation
    fails; ``error`` is a user-facing message suitable for a toast.
    """
    values = form_value or {}
    answer: dict[str, Any] = {}

    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            continue
        if field.get("hidden") or field.get("type") == "external":
            continue

        field_type = str(field.get("type") or "string")
        key = str(field.get("key") or f"q{index}")
        title = str(field.get("title") or key)
        raw = values.get(key)
        custom = values.get(f"{key}__custom")
        custom_text = str(custom).strip() if custom is not None else ""

        if field_type == "multiselect":
            if isinstance(raw, list):
                selected = raw
            elif isinstance(raw, str) and re.search(r"[,，\n]", raw):
                selected = [
                    item.strip()
                    for item in re.split(r"[,，\n]", raw)
                    if item.strip()
                ]
            elif raw:
                selected = [raw]
            else:
                selected = []
            result: list[str] = []
            for item in selected:
                label = _option_label(field, item)
                if label is not None and label not in result:
                    result.append(label)
            if custom_text:
                for item in re.split(r"[,，\n]", custom_text):
                    item = item.strip()
                    if item and item not in result:
                        result.append(item)
            if not result and field.get("required"):
                return None, f"请回答：{title}"
            answer[key] = result
            continue

        if field_type == "boolean":
            if raw is None or raw == "":
                if field.get("required"):
                    return None, f"请回答：{title}"
                continue
            answer[key] = str(raw).lower() in (
                "true",
                "1",
                "yes",
                "是",
            )
            continue

        if field_type in ("number", "integer"):
            text = custom_text or (
                str(raw).strip() if raw is not None else ""
            )
            if not text:
                if field.get("required"):
                    return None, f"请回答：{title}"
                continue
            try:
                number = float(text)
            except ValueError:
                return None, f"{title} 需要数字"
            if not math.isfinite(number):
                return None, f"{title} 需要有效数字"
            if field_type == "integer":
                if not number.is_integer():
                    return None, f"{title} 需要整数"
                answer[key] = int(number)
            else:
                answer[key] = number
            continue

        # string
        if field.get("options"):
            text = custom_text or _option_label(field, raw) or ""
        elif isinstance(raw, list):
            text = custom_text or ", ".join(str(item) for item in raw)
        else:
            text = custom_text or (
                str(raw).strip() if raw is not None else ""
            )
        if not text:
            if field.get("required"):
                return None, f"请回答：{title}"
            continue
        answer[key] = text

    return answer, None
