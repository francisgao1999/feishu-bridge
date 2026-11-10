from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://open.feishu.cn"
TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
COMMANDS_PATH = "/open-apis/application/v7/app_slash_commands"


class SlashCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class SlashCommand:
    command: str
    description: str
    description_en: str


# 只注册顶层命令。别名（/t、/list、/use 等）和子命令仍可手打，
# 但不重复占用面板条目。
SLASH_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "new",
        "新建会话，参数为会话标题",
        "Create a session; the argument is its title",
    ),
    SlashCommand(
        "temp",
        "新建临时会话；end 结束 / list 查看",
        "Create a temporary session; end to finish, list to view",
    ),
    SlashCommand(
        "sessions",
        "列出全部会话及运行状态",
        "List all sessions and their status",
    ),
    SlashCommand(
        "switch",
        "切换当前会话",
        "Switch the active session",
    ),
    SlashCommand(
        "status",
        "查看会话、任务和审批状态",
        "Show session, task and approval status",
    ),
    SlashCommand(
        "stop",
        "中止任务并清理待审批",
        "Stop the task and clear pending approvals",
    ),
    SlashCommand(
        "model",
        "查看或切换模型",
        "Show or switch the model",
    ),
    SlashCommand(
        "agent",
        "查看或切换 Agent",
        "Show or switch the agent",
    ),
    SlashCommand(
        "schedule",
        "定时任务：list / pause / resume / remove / run",
        "Scheduled tasks: list / pause / resume / remove / run",
    ),
    SlashCommand(
        "help",
        "显示完整命令帮助",
        "Show full command help",
    ),
    SlashCommand(
        "last",
        "重发最后一次任务结果",
        "Resend the last task result",
    ),
    SlashCommand(
        "delete",
        "删除空闲会话",
        "Delete an idle session",
    ),
    SlashCommand(
        "clean",
        "列出或清理空闲会话",
        "List or clean idle sessions",
    ),
    SlashCommand(
        "whoami",
        "显示当前身份、会话、模型和 Agent",
        "Show current identity, session, model and agent",
    ),
)


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.failed


def _description_payload(item: SlashCommand) -> dict[str, Any]:
    return {
        "default_value": item.description,
        "i18n": {
            "zh_cn": item.description,
            "en_us": item.description_en,
        },
    }


def _description_matches(
    existing: dict[str, Any], item: SlashCommand
) -> bool:
    description = existing.get("description") or {}
    if description.get("default_value") != item.description:
        return False
    i18n = description.get("i18n") or {}
    if not isinstance(i18n, dict):
        return False
    return (
        i18n.get("zh_cn") == item.description
        and i18n.get("en_us") == item.description_en
    )


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise SlashCommandError(
            f"invalid response ({response.status_code}): "
            f"{response.text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise SlashCommandError(f"unexpected response: {data!r}")
    return data


async def _tenant_access_token(
    client: httpx.AsyncClient,
    base_url: str,
    app_id: str,
    app_secret: str,
) -> str:
    response = await client.post(
        base_url + TOKEN_PATH,
        json={"app_id": app_id, "app_secret": app_secret},
    )
    data = _response_json(response)
    token = data.get("tenant_access_token")
    if data.get("code") != 0 or not token:
        raise SlashCommandError(
            "tenant_access_token failed: "
            f"{data.get('code')} {data.get('msg')}"
        )
    return str(token)


async def _list_commands(
    client: httpx.AsyncClient, base_url: str, token: str
) -> list[dict[str, Any]]:
    response = await client.get(
        base_url + COMMANDS_PATH,
        headers={"Authorization": f"Bearer {token}"},
    )
    data = _response_json(response)
    if data.get("code") != 0:
        raise SlashCommandError(
            "list slash commands failed: "
            f"{data.get('code')} {data.get('msg')}"
        )
    items = (data.get("data") or {}).get("items") or []
    return [item for item in items if isinstance(item, dict)]


async def _create_command(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    item: SlashCommand,
) -> None:
    response = await client.post(
        base_url + COMMANDS_PATH,
        headers={"Authorization": f"Bearer {token}"},
        json={
            "command": item.command,
            "description": _description_payload(item),
        },
    )
    data = _response_json(response)
    if data.get("code") != 0:
        raise SlashCommandError(
            f"create /{item.command} failed: "
            f"{data.get('code')} {data.get('msg')}"
        )


async def _update_command(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    command_id: str,
    item: SlashCommand,
) -> None:
    response = await client.patch(
        f"{base_url}{COMMANDS_PATH}/{command_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"description": _description_payload(item)},
    )
    data = _response_json(response)
    if data.get("code") != 0:
        raise SlashCommandError(
            f"update /{item.command} failed: "
            f"{data.get('code')} {data.get('msg')}"
        )


def _is_permission_error(exc: Exception) -> bool:
    message = str(exc)
    return "99991672" in message or "99991640" in message


async def sync_slash_commands(
    app_id: str,
    app_secret: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.AsyncClient | None = None,
    commands: tuple[SlashCommand, ...] = SLASH_COMMANDS,
) -> SyncResult:
    """Create missing commands and update changed descriptions.

    Best-effort: failures are logged and returned, never raised, so the
    bridge can start even when the app lacks the slash-command scope.
    Commands that are not in ``commands`` are left untouched.
    """
    result = SyncResult()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
    try:
        token = await _tenant_access_token(
            client, base_url, app_id, app_secret
        )
        existing_items = await _list_commands(client, base_url, token)
        existing = {
            str(item["command"]): item
            for item in existing_items
            if item.get("command")
        }

        for item in commands:
            current = existing.get(item.command)
            try:
                if current is None:
                    await _create_command(client, base_url, token, item)
                    result.created.append(item.command)
                elif not _description_matches(current, item):
                    command_id = str(current.get("command_id") or "")
                    if not command_id:
                        raise SlashCommandError(
                            f"missing command_id for /{item.command}"
                        )
                    await _update_command(
                        client, base_url, token, command_id, item
                    )
                    result.updated.append(item.command)
                else:
                    result.unchanged.append(item.command)
            except Exception as exc:
                if _is_permission_error(exc):
                    result.error = str(exc)
                    log.warning(
                        "sync slash commands stopped: %s "
                        "(需要 application:app_slash_command:write "
                        "权限并发布新版本)",
                        exc,
                    )
                    break
                log.warning(
                    "sync slash command /%s failed: %s", item.command, exc
                )
                result.failed.append(item.command)

        log.info(
            "slash commands synced: created=%s updated=%s "
            "unchanged=%s failed=%s",
            len(result.created),
            len(result.updated),
            len(result.unchanged),
            len(result.failed),
        )
    except Exception as exc:
        result.error = str(exc)
        log.warning(
            "sync slash commands failed: %s "
            "(需要 application:app_slash_command:read/write 权限并发布新版本)",
            exc,
        )
    finally:
        if owns_client:
            await client.aclose()
    return result
