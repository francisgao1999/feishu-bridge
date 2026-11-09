from __future__ import annotations

from typing import Any


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n…（已截断，共 {len(text)} 字符）"


def _plain(text: str, limit: int = 2000) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "plain_text", "content": _truncate(text, limit)},
    }


def _md(text: str, limit: int = 2000) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {"tag": "lark_md", "content": _truncate(text, limit)},
    }


def _note(text: str, limit: int = 500) -> dict[str, Any]:
    return {
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": _truncate(text, limit)}],
    }


def progress_card(
    *,
    status: str,
    prompt: str,
    tool: str | None = None,
    elapsed: int | None = None,
    final_text: str | None = None,
    error: str | None = None,
    session_id: str | None = None,
    usage: dict[str, Any] | None = None,
    max_final_chars: int = 3000,
    show_result: bool = True,
) -> dict[str, Any]:
    if status in ("已中止", "已中断"):
        template = "grey"
        title = "任务已中止"
    elif error:
        template = "red"
        title = "任务失败"
    elif final_text is not None:
        template = "green"
        title = "任务完成"
    else:
        template = "blue"
        title = "任务执行中"

    elements: list[dict[str, Any]] = [
        _md(f"**任务**\n{_truncate(prompt, 800)}"),
        {"tag": "hr"},
    ]

    status_lines = [f"**状态**: {status}"]
    if tool:
        status_lines.append(f"**当前工具**: {tool}")
    if elapsed is not None:
        status_lines.append(f"**已用时**: {elapsed} 秒")
    if usage:
        tokens = usage.get("tokens") or {}
        status_lines.append(
            "**Token**: 输入 "
            f"{tokens.get('input', 0)} / 输出 {tokens.get('output', 0)}"
        )
        cost = usage.get("cost")
        if cost:
            status_lines.append(f"**费用**: {cost}")
    elements.append(_md("\n".join(status_lines)))

    if error:
        elements.append({"tag": "hr"})
        elements.append(_plain(f"错误: {error}"))
    elif final_text is not None and show_result:
        elements.append({"tag": "hr"})
        elements.append(_md("**结果**"))
        elements.append(_plain(final_text, limit=max_final_chars))

    if session_id:
        elements.append(_note(f"会话 {session_id}"))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": elements,
    }


def approval_card(
    *,
    permission_id: str,
    action: str,
    resources: list[str],
    save: list[str],
    prompt: str,
    session_id: str,
    expire_seconds: int,
) -> dict[str, Any]:
    resource_text = "\n".join(resources) if resources else "(无)"
    elements: list[dict[str, Any]] = [
        _md(f"**动作**: `{action}`"),
        _plain(resource_text),
        {"tag": "hr"},
        _md(f"**任务**\n{_truncate(prompt, 500)}"),
    ]
    if save:
        elements.append(_md(f"**可记住的模式**: `{', '.join(save)}`"))

    elements.extend(
        [
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "允许一次"},
                        "type": "primary",
                        "value": {
                            "action": "permission",
                            "decision": "once",
                            "permission_id": permission_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {
                            "action": "permission",
                            "decision": "reject",
                            "permission_id": permission_id,
                        },
                    },
                ],
            },
            _note(
                "仅允许本次执行；"
                f"{expire_seconds // 60} 分钟内未处理将自动拒绝。"
                f"会话 {session_id}"
            ),
        ]
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "需要审批"},
            "template": "orange",
        },
        "elements": elements,
    }


def approval_resolved_card(
    *,
    action: str,
    resources: list[str],
    decision: str,
    prompt: str,
    session_id: str,
) -> dict[str, Any]:
    if decision == "allowed":
        template = "green"
        title = "已允许一次"
        status = "已允许本次执行"
    elif decision == "rejected":
        template = "red"
        title = "已拒绝"
        status = "已拒绝本次执行"
    else:
        template = "grey"
        title = "审批已过期"
        status = "超时未处理，已自动拒绝"

    resource_text = "\n".join(resources) if resources else "(无)"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            _md(f"**动作**: `{action}`"),
            _plain(resource_text),
            {"tag": "hr"},
            _md(f"**任务**\n{_truncate(prompt, 500)}"),
            _md(f"**结果**: {status}"),
            _note(f"会话 {session_id}"),
        ],
    }


def status_card(
    *,
    chat_id: str,
    session_id: str | None,
    session_title: str | None,
    active_task: dict[str, Any] | None,
    pending: list[dict[str, Any]],
    model: str,
    workspace: str,
    sessions: list[dict[str, Any]] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    lines = [
        f"**会话**: `{session_id or '未创建'}`",
        f"**标题**: {session_title or '-'}",
        f"**模型**: `{model}`",
        f"**工作目录**: `{workspace}`",
    ]
    if active_task:
        lines.append(
            f"**当前任务**: #{active_task['id']} {active_task['status']}"
        )
        lines.append(
            f"**任务内容**: {_truncate(active_task['prompt'], 200)}"
        )
    else:
        lines.append("**当前任务**: 无")

    if pending:
        lines.append(f"**待审批**: {len(pending)} 个")
    else:
        lines.append("**待审批**: 无")

    if sessions:
        lines.append("")
        lines.append(f"**全部会话**（{len(sessions)} 个）")
        for index, session in enumerate(sessions, 1):
            marker = " ← 当前" if session.get("active") else ""
            lines.append(
                f"{index}. {session.get('title') or '(无标题)'}"
                f" [{session.get('state') or '空闲'}]{marker}"
            )

    footer = f"chat {chat_id}"
    if note:
        footer = f"{note}\n{footer}"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "状态"},
            "template": "blue",
        },
        "elements": [_md("\n".join(lines)), _note(footer)],
    }


def switch_card(
    *,
    title: str,
    session_id: str,
    model: str,
    state: str,
    last_task: dict[str, Any] | None = None,
    empty_text: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        _md(f"**会话**: {title}"),
        _md(f"**Session**: `{session_id}`"),
        _md(f"**模型**: `{model}`"),
        _md(f"**状态**: {state}"),
    ]

    if last_task:
        elements.append({"tag": "hr"})
        heading = (
            f"**上次任务**: #{last_task['id']} "
            f"{last_task.get('status') or ''}"
        ).rstrip()
        if last_task.get("finished"):
            heading += f" · {last_task['finished']}"
        elements.append(_md(heading))
        if last_task.get("prompt"):
            elements.append(
                _plain(f"任务：{last_task['prompt']}", limit=300)
            )
        if last_task.get("result") is not None:
            elements.append(_md("**结果摘要**"))
            elements.append(_plain(last_task["result"], limit=400))
        if last_task.get("error") is not None:
            elements.append(_md("**错误**"))
            elements.append(_plain(last_task["error"], limit=400))
        if last_task.get("note"):
            elements.append(_md(last_task["note"]))
    elif empty_text:
        elements.append({"tag": "hr"})
        elements.append(_md(empty_text))

    footer = "后续消息将发送到该会话。"
    if last_task and last_task.get("status") in ("完成", "失败"):
        footer += " 发送 /last 查看完整结果。"
    if note:
        footer = f"{note}\n{footer}"
    elements.append(_note(footer))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "已切换会话"},
            "template": "green",
        },
        "elements": elements,
    }


def model_select_card(
    *,
    models: list[dict[str, str]],
    current_provider: str,
    current_model: str,
    session_id: str | None = None,
    session_title: str | None = None,
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        _md(f"**当前模型**: `{current_provider}/{current_model}`"),
        {"tag": "hr"},
    ]

    groups: dict[str, list[dict[str, str]]] = {}
    for model in models:
        groups.setdefault(model["provider_id"], []).append(model)

    for provider_id, items in groups.items():
        provider_name = items[0].get("provider_name") or provider_id
        elements.append(_md(f"**{provider_name}**"))
        actions: list[dict[str, Any]] = []
        for model in items:
            selected = (
                model["provider_id"] == current_provider
                and model["model_id"] == current_model
            )
            label = model["model_name"]
            if selected:
                label = f"✓ {label}"
            actions.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": label},
                    "type": "primary" if selected else "default",
                    "value": {
                        "action": "model",
                        "provider_id": model["provider_id"],
                        "model_id": model["model_id"],
                        **(
                            {"session_id": session_id}
                            if session_id
                            else {}
                        ),
                    },
                }
            )
        for start in range(0, len(actions), 4):
            elements.append(
                {
                    "tag": "action",
                    "actions": actions[start : start + 4],
                }
            )

    note = "点击按钮切换模型；切换后对该会话的新任务生效。"
    if session_title:
        note = f"会话：{session_title}\n{note}"
    elements.append(_note(note))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "选择模型"},
            "template": "blue",
        },
        "elements": elements,
    }


def model_resolved_card(
    *,
    provider_id: str,
    model_id: str,
    model_name: str,
    session_label: str | None = None,
) -> dict[str, Any]:
    elements = [
        _md(f"**当前模型**: `{provider_id}/{model_id}`"),
        _md(f"**显示名称**: {model_name}"),
    ]
    if session_label:
        elements.append(_md(f"**会话**: {session_label}"))
    elements.append(_note("该会话的后续新任务将使用该模型。"))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "模型已切换"},
            "template": "green",
        },
        "elements": elements,
    }


def reminder_card(
    *,
    prompt: str,
    schedule_id: int | None = None,
    when: str | None = None,
    label: str = "单次提醒",
) -> dict[str, Any]:
    elements: list[dict[str, Any]] = [
        _md("**提醒内容**"),
        _plain(prompt, limit=1500),
    ]
    if when:
        elements.append(_md(f"**原定时间**: {when}"))
    note = label
    if schedule_id:
        note += f" #{schedule_id}"
    elements.append(_note(note))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⏰ 提醒时间到"},
            "template": "orange",
        },
        "elements": elements,
    }


def schedule_help_text() -> str:
    return (
        "定时任务命令：\n"
        "/schedule 13:38 提醒我【去接水】  单次提醒\n"
        "/schedule 明天 09:00 <任务>       单次任务\n"
        "/schedule 2026-11-10 13:38 <任务> 指定日期单次任务\n"
        "/schedule once 13:38 <任务>       单次任务（也可写 单次/一次性）\n"
        "/schedule daily 10:00 <任务>      每天 10:00 执行（也可写 每日）\n"
        "/schedule weekdays 09:30 <任务>   工作日执行\n"
        "/schedule weekly mon 10:00 <任务> 每周一执行（也可写 每周一）\n"
        "/schedule add daily 10:00 <任务>  添加周期任务\n"
        "/schedule list                    查看定时任务\n"
        "/schedule pause <id>              暂停\n"
        "/schedule resume <id>             恢复\n"
        "/schedule remove <id>             删除（也可写 delete）\n"
        "/schedule run <id>                立即执行一次\n"
        "子命令也支持中文：列表/暂停/恢复/删除/执行/单次/一次性/添加；"
        "周期可写 每天/每日、工作日、每周一\n\n"
        "也可以直接发送自然语言，例如：\n"
        "每天早上10点给我发送昨日到现在 linux.do 福利羊毛板块的帖子摘要\n"
        "13:38分提醒我【去接水】\n\n"
        "单次提醒到点直接发提醒卡片，不创建会话；"
        "单次任务使用独立会话，结果会作为新卡片发到本聊天。"
    )


def help_text(temp_ttl_text: str = "空闲 30 分钟后自动清理") -> str:
    return (
        "可用命令\n"
        "\n"
        "【会话管理】\n"
        "/new [任务]  创建并切换到新会话；带任务时立即执行\n"
        "/temp [任务]  创建并切换到临时会话；带任务时立即执行"
        "（别名 /t、/临时）\n"
        "/temp end  结束当前临时会话并删除临时目录"
        "（也可写 /temp 结束、/temp stop）\n"
        "/temp list  查看临时会话、剩余时间和目录"
        "（也可写 /temp 列表）\n"
        "/sessions  列出全部会话及运行状态（别名 /list）\n"
        "/switch <序号|session_id|标题>  切换当前会话（别名 /use）\n"
        "/last [序号|session_id]  重发最后一次任务结果"
        "（别名 /result）\n"
        "/delete <序号|session_id|标题>  删除空闲会话"
        "（别名 /remove、/删除）\n"
        "/clean  列出空闲会话；/clean confirm 确认清理"
        "（不含当前会话；confirm 也可写 确认/yes/y；别名 /清理）\n"
        "/status  查看当前会话、全部会话、任务和审批状态\n"
        "/whoami  显示 open_id / chat_id / session_id / 会话标题 / 模型\n"
        "\n"
        "【任务与模型】\n"
        "直接发送文本  在当前会话派任务\n"
        "/stop [序号|session_id]  中止任务并清理待审批，默认当前会话\n"
        "/model  打开模型选择卡片\n"
        "/model <序号>  按列表序号切换模型\n"
        "/model <供应商>/<模型>  直接切换模型\n"
        "\n"
        "【定时任务】\n"
        "/schedule  查看定时任务用法（别名 /定时；也可 /schedule help）\n"
        "/schedule 13:38 提醒我【去接水】  单次提醒\n"
        "/schedule 明天 09:00 <任务>  单次任务\n"
        "/schedule 2026-11-10 13:38 <任务>  指定日期单次任务\n"
        "/schedule once 13:38 <任务>  单次任务"
        "（once 也可写 单次/一次性）\n"
        "/schedule daily 10:00 <任务>  每天执行（也可写 每日）\n"
        "/schedule weekdays 09:30 <任务>  工作日执行\n"
        "/schedule weekly mon 10:00 <任务>  每周一执行"
        "（weekly 也可写 每周一；mon…sun 或 周一…周日）\n"
        "/schedule add <周期|时间> <任务>  创建周期/单次任务，"
        "例如 add daily 10:00 <任务>\n"
        "/schedule list  查看定时任务\n"
        "/schedule pause <id>  暂停\n"
        "/schedule resume <id>  恢复\n"
        "/schedule remove <id>  删除（remove 也可写 delete）\n"
        "/schedule run <id>  立即执行一次\n"
        "子命令也支持中文：列表/暂停/恢复/删除/执行/单次/一次性/添加；"
        "周期可写 每天/每日、工作日、每周一\n"
        "也可直接发送自然语言，例如：\n"
        "每天早上10点给我发送昨日到现在 linux.do 福利羊毛板块的帖子摘要\n"
        "13:38分提醒我【去接水】\n"
        "\n"
        "【说明】\n"
        "多个会话可以同时执行任务：在 A 会话跑任务时，发送 "
        "/new B任务 会创建 B 会话并并行执行；之后用 /switch 切回 A。\n"
        "临时会话使用独立空目录，适合一次性实验；"
        f"{temp_ttl_text}，发送 /temp end 可立即结束。\n"
        "执行 shell 或修改文件时会发审批卡片，"
        "只支持「允许一次」或「拒绝」。\n"
        "/help  显示本帮助（别名 /start）"
    )
