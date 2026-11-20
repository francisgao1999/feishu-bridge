from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.opencode import (  # noqa: E402
        OpenCodeClient,
        OpenCodeError,
        OpenCodeRouter,
        OpenCodeSyncClient,
        OpenCodeSyncRouter,
        parse_agents,
    )
except ImportError:  # flattened workspace without app/ package
    from opencode import (  # noqa: E402
        OpenCodeClient,
        OpenCodeError,
        OpenCodeRouter,
        OpenCodeSyncClient,
        OpenCodeSyncRouter,
        parse_agents,
    )


class ParseAgentsTest(unittest.TestCase):
    def test_filters_hidden_and_subagents(self):
        data = {
            "data": [
                {
                    "id": "build",
                    "name": "Build",
                    "mode": "primary",
                    "hidden": False,
                },
                {
                    "id": "plan",
                    "name": "Plan",
                    "mode": "primary",
                    "hidden": False,
                },
                {
                    "id": "general",
                    "name": "General",
                    "mode": "subagent",
                    "hidden": False,
                },
                {
                    "id": "compaction",
                    "name": "Compaction",
                    "mode": "primary",
                    "hidden": True,
                },
                {
                    "id": "medical-coder",
                    "name": "Medical Coder",
                    "mode": "all",
                    "hidden": False,
                },
                {"id": "custom", "name": "Custom"},
            ]
        }
        agents = parse_agents(data)
        self.assertEqual(
            [agent["id"] for agent in agents],
            ["build", "plan", "medical-coder", "custom"],
        )
        self.assertEqual(agents[0]["name"], "Build")
        self.assertEqual(agents[2]["mode"], "all")
        self.assertEqual(agents[3]["mode"], "primary")
        self.assertEqual(agents[0]["permissions"], [])

    def test_accepts_list_payload(self):
        agents = parse_agents(
            [
                {
                    "id": "plan",
                    "name": "Plan",
                    "mode": "primary",
                    "permissions": [
                        {
                            "action": "edit",
                            "resource": "*",
                            "effect": "deny",
                        }
                    ],
                }
            ]
        )
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["permissions"][0]["effect"], "deny")

    def test_ignores_invalid_items(self):
        self.assertEqual(parse_agents(None), [])
        self.assertEqual(parse_agents({"data": "nope"}), [])
        self.assertEqual(
            parse_agents({"data": [None, {}, {"id": ""}]}), []
        )


def _entry(
    open_id: str = "ou_alice",
    url: str = "http://10.0.0.11:4096",
    password: str = "pw",
    workspace: str = "/workspace",
) -> SimpleNamespace:
    return SimpleNamespace(
        open_id=open_id,
        slug=open_id.removeprefix("ou_"),
        base_url=url,
        username="opencode",
        password=password,
        workspace=workspace,
    )


class FakeRegistry:
    def __init__(self, entries: dict[str, SimpleNamespace]):
        self._entries = entries
        self.configured = bool(entries)

    def get(self, open_id: str):
        return self._entries.get(open_id)

    def entries(self):
        return list(self._entries.values())

    def by_url(self):
        return {e.base_url: e for e in self._entries.values()}


class RouterTest(unittest.TestCase):
    def test_client_for_caches_per_entry(self):
        registry = FakeRegistry({"ou_alice": _entry()})
        router = OpenCodeRouter(registry)
        self.addCleanup(lambda: asyncio.run(router.close()))

        first = router.client_for("ou_alice")
        second = router.client_for("ou_alice")
        self.assertIs(first, second)
        self.assertEqual(first.base_url, "http://10.0.0.11:4096")

    def test_client_for_unknown_without_default_raises(self):
        router = OpenCodeRouter(FakeRegistry({}))
        self.addCleanup(lambda: asyncio.run(router.close()))
        with self.assertRaises(OpenCodeError):
            router.client_for("ou_nobody")

    def test_client_for_unknown_uses_default(self):
        default = OpenCodeClient("http://127.0.0.1:4096", "u", "p")
        router = OpenCodeRouter(FakeRegistry({}), default=default)
        self.addCleanup(lambda: asyncio.run(router.close()))
        self.assertIs(router.client_for("ou_nobody"), default)

    def test_client_for_session_resolves_owner(self):
        registry = FakeRegistry({"ou_alice": _entry()})
        router = OpenCodeRouter(
            registry, session_resolver=lambda sid: "ou_alice"
        )
        self.addCleanup(lambda: asyncio.run(router.close()))
        client = router.client_for_session("ses_1")
        self.assertEqual(client.base_url, "http://10.0.0.11:4096")

    def test_client_for_session_unknown_falls_back_to_default(self):
        default = OpenCodeClient("http://127.0.0.1:4096", "u", "p")
        router = OpenCodeRouter(
            FakeRegistry({}), default=default, session_resolver=lambda sid: None
        )
        self.addCleanup(lambda: asyncio.run(router.close()))
        self.assertIs(router.client_for_session("ses_1"), default)

    def test_registered_reports_membership(self):
        registry = FakeRegistry({"ou_alice": _entry()})
        router = OpenCodeRouter(registry)
        self.addCleanup(lambda: asyncio.run(router.close()))
        self.assertTrue(router.registered("ou_alice"))
        self.assertFalse(router.registered("ou_bob"))


class SyncRouterTest(unittest.TestCase):
    def test_client_for_caches_and_resolves(self):
        registry = FakeRegistry({"ou_alice": _entry()})
        router = OpenCodeSyncRouter(registry)
        self.addCleanup(router.close)

        first = router.client_for("ou_alice")
        second = router.client_for("ou_alice")
        self.assertIs(first, second)

        router2 = OpenCodeSyncRouter(
            registry, session_resolver=lambda sid: "ou_alice"
        )
        self.addCleanup(router2.close)
        self.assertEqual(
            router2.client_for_session("ses_1")._client.base_url.host,
            "10.0.0.11",
        )

    def test_unknown_without_default_raises(self):
        router = OpenCodeSyncRouter(FakeRegistry({}))
        self.addCleanup(router.close)
        with self.assertRaises(OpenCodeError):
            router.client_for("ou_nobody")


class SyncClientTransferTest(unittest.TestCase):
    def _client(self, handler):
        client = OpenCodeSyncClient("http://10.0.0.11:4096", "u", "p")
        client._client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://10.0.0.11:4096",
            auth=("u", "p"),
        )
        self.addCleanup(client.close)
        return client

    def test_upload_file_streams_body(self):
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["query"] = dict(request.url.params)
            seen["body"] = request.read()
            return httpx.Response(200, json={"data": {"path": "/workspace/a"}})

        client = self._client(handler)
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"hello-world")
            local = handle.name
        self.addCleanup(lambda: Path(local).unlink(missing_ok=True))

        client.upload_file(
            local, "/workspace/uploads/a.txt", "/workspace/uploads"
        )
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["path"], "/api/experimental/fs/write")
        self.assertEqual(seen["query"]["path"], "/workspace/uploads/a.txt")
        self.assertEqual(
            seen["query"]["location[directory]"], "/workspace/uploads"
        )
        self.assertEqual(seen["body"], b"hello-world")

    def test_download_file_writes_bytes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                request.url.raw_path.split(b"?")[0],
                b"/api/fs/read/%2Fworkspace%2Fa.bin",
            )
            return httpx.Response(200, content=b"abc123")

        client = self._client(handler)
        with tempfile.TemporaryDirectory() as tmp:
            target = str(Path(tmp) / "out.bin")
            size = client.download_file(
                "/workspace/a.bin", target, "/workspace"
            )
            self.assertEqual(size, 6)
            self.assertEqual(Path(target).read_bytes(), b"abc123")

    def test_download_file_raises_on_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "missing"})

        client = self._client(handler)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OpenCodeError) as ctx:
                client.download_file(
                    "/workspace/missing.bin", str(Path(tmp) / "x"), "/workspace"
                )
            self.assertEqual(ctx.exception.status_code, 404)


class ListDirTest(unittest.TestCase):
    def test_list_dir_returns_entries(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"path": "/workspace/outbox/a.png", "type": "file"},
                        {"path": "/workspace/outbox/sub", "type": "directory"},
                    ]
                },
            )

        client = OpenCodeClient("http://10.0.0.11:4096", "u", "p")
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="http://10.0.0.11:4096",
            auth=("u", "p"),
        )
        self.addCleanup(lambda: asyncio.run(client.close()))

        entries = asyncio.run(
            client.list_dir("/workspace/outbox", "/workspace")
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["type"], "file")


if __name__ == "__main__":
    unittest.main()
