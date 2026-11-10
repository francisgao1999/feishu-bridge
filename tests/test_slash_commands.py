from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.slash_commands import (  # noqa: E402
        COMMANDS_PATH,
        SLASH_COMMANDS,
        TOKEN_PATH,
        sync_slash_commands,
    )
except ImportError:  # flattened workspace without app/ package
    from slash_commands import (  # noqa: E402
        COMMANDS_PATH,
        SLASH_COMMANDS,
        TOKEN_PATH,
        sync_slash_commands,
    )


EXPECTED_COMMANDS = {
    "help",
    "new",
    "temp",
    "sessions",
    "switch",
    "last",
    "delete",
    "clean",
    "schedule",
    "status",
    "model",
    "agent",
    "stop",
    "whoami",
}


class FakeFeishu:
    def __init__(
        self,
        items: list[dict] | None = None,
        *,
        token_code: int = 0,
        list_code: int = 0,
        create_code: int = 0,
        patch_code: int = 0,
    ):
        self.items = list(items or [])
        self.token_code = token_code
        self.list_code = list_code
        self.create_code = create_code
        self.patch_code = patch_code
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200,
                json={
                    "code": self.token_code,
                    "msg": "ok" if self.token_code == 0 else "token failed",
                    "tenant_access_token": (
                        "token" if self.token_code == 0 else ""
                    ),
                },
            )

        if request.headers.get("Authorization") != "Bearer token":
            return httpx.Response(
                200, json={"code": 99991663, "msg": "invalid token"}
            )

        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": self.list_code,
                    "msg": "ok" if self.list_code == 0 else "list failed",
                    "data": {"items": self.items},
                },
            )

        if request.method == "POST":
            body = json.loads(request.content)
            if self.create_code != 0:
                return httpx.Response(
                    200,
                    json={"code": self.create_code, "msg": "create failed"},
                )
            command = body["command"]
            self.items.append(
                {
                    "command_id": f"id-{command}",
                    "command": command,
                    "description": body["description"],
                }
            )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "success",
                    "data": {"command_id": f"id-{command}"},
                },
            )

        if request.method == "PATCH":
            body = json.loads(request.content)
            command_id = request.url.path.rsplit("/", 1)[-1]
            if self.patch_code != 0:
                return httpx.Response(
                    200,
                    json={"code": self.patch_code, "msg": "patch failed"},
                )
            for item in self.items:
                if item["command_id"] == command_id:
                    item["description"] = body["description"]
                    break
            return httpx.Response(
                200, json={"code": 0, "msg": "success", "data": {}}
            )

        return httpx.Response(404)


def existing_items() -> list[dict]:
    return [
        {
            "command_id": f"id-{item.command}",
            "command": item.command,
            "description": {
                "default_value": item.description,
                "i18n": {
                    "zh_cn": item.description,
                    "en_us": item.description_en,
                },
            },
        }
        for item in SLASH_COMMANDS
    ]


class SlashCommandCatalogTest(unittest.TestCase):
    def test_catalog_covers_all_top_level_commands(self):
        names = [item.command for item in SLASH_COMMANDS]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), EXPECTED_COMMANDS)

    def test_descriptions_are_not_empty(self):
        for item in SLASH_COMMANDS:
            self.assertTrue(item.description.strip())
            self.assertTrue(item.description_en.strip())


class SlashCommandSyncTest(unittest.IsolatedAsyncioTestCase):
    async def sync(self, fake: FakeFeishu):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake)
        ) as client:
            return await sync_slash_commands(
                "cli_test", "secret", client=client
            )

    async def test_creates_missing_commands(self):
        fake = FakeFeishu()
        result = await self.sync(fake)

        self.assertTrue(result.ok)
        self.assertEqual(set(result.created), EXPECTED_COMMANDS)
        self.assertEqual(result.updated, [])
        self.assertEqual(result.failed, [])

        posts = [
            r
            for r in fake.requests
            if r.method == "POST" and r.url.path == COMMANDS_PATH
        ]
        self.assertEqual(len(posts), len(SLASH_COMMANDS))
        for request in posts:
            body = json.loads(request.content)
            self.assertIn(body["command"], EXPECTED_COMMANDS)
            self.assertTrue(body["description"]["default_value"])

    async def test_updates_changed_description(self):
        items = existing_items()[:1]
        items[0]["description"] = {
            "default_value": "旧描述",
            "i18n": {"zh_cn": "旧描述", "en_us": "old"},
        }
        fake = FakeFeishu(items)
        result = await self.sync(fake)

        self.assertTrue(result.ok)
        self.assertEqual(result.updated, [SLASH_COMMANDS[0].command])
        self.assertEqual(len(result.created), len(SLASH_COMMANDS) - 1)

        patches = [r for r in fake.requests if r.method == "PATCH"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0].url.path.endswith("/id-new"))
        body = json.loads(patches[0].content)
        self.assertEqual(
            body["description"]["default_value"],
            SLASH_COMMANDS[0].description,
        )

    async def test_skips_unchanged_commands(self):
        fake = FakeFeishu(existing_items())
        result = await self.sync(fake)

        self.assertTrue(result.ok)
        self.assertEqual(result.created, [])
        self.assertEqual(result.updated, [])
        self.assertEqual(set(result.unchanged), EXPECTED_COMMANDS)
        self.assertFalse(
            [
                r
                for r in fake.requests
                if r.method in ("POST", "PATCH")
                and r.url.path.startswith(COMMANDS_PATH)
            ]
        )

    async def test_permission_error_is_returned(self):
        fake = FakeFeishu(list_code=99991672)
        result = await self.sync(fake)

        self.assertFalse(result.ok)
        self.assertIn("99991672", result.error or "")
        self.assertEqual(result.created, [])

    async def test_token_error_is_returned(self):
        fake = FakeFeishu(token_code=99991663)
        result = await self.sync(fake)

        self.assertFalse(result.ok)
        self.assertIn("99991663", result.error or "")

    async def test_create_failure_is_reported(self):
        fake = FakeFeishu(create_code=40000000)
        result = await self.sync(fake)

        self.assertFalse(result.ok)
        self.assertEqual(set(result.failed), EXPECTED_COMMANDS)
        self.assertIsNone(result.error)

    async def test_create_permission_error_stops_early(self):
        fake = FakeFeishu(create_code=99991672)
        result = await self.sync(fake)

        self.assertFalse(result.ok)
        self.assertIn("99991672", result.error or "")
        self.assertEqual(result.failed, [])
        posts = [
            r
            for r in fake.requests
            if r.method == "POST" and r.url.path == COMMANDS_PATH
        ]
        self.assertEqual(len(posts), 1)


if __name__ == "__main__":
    unittest.main()
