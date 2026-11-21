from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LXC_DIR = REPO_ROOT / "deploy" / "lxc"


def _load_jsonc(path: Path) -> dict:
    """Parse a JSONC file by removing comments outside string literals."""
    text = path.read_text(encoding="utf-8")
    out: list[str] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < len(text):
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            next_char = text[index + 1]
            if next_char == "/":
                index += 2
                while index < len(text) and text[index] != "\n":
                    index += 1
                continue
            if next_char == "*":
                index += 2
                while index + 1 < len(text) and not (
                    text[index] == "*" and text[index + 1] == "/"
                ):
                    index += 1
                index += 2
                continue
        out.append(char)
        index += 1
    return json.loads("".join(out))


class ProxyConfigTest(unittest.TestCase):
    """模板里的代理配置是用户 LXC 出网的唯一通道，改动要有测试兜底。"""

    def test_proxy_env_example_has_expected_keys(self):
        text = (LXC_DIR / "proxy.env.example").read_text(encoding="utf-8")
        for key in (
            "HTTP_PROXY=",
            "HTTPS_PROXY=",
            "ALL_PROXY=",
            "NO_PROXY=",
            "http_proxy=",
            "https_proxy=",
        ):
            self.assertIn(key, text)
        self.assertIn("10808", text)
        self.assertIn("192.168.110.11", text)
        # opencode 依赖本机回环通信，NO_PROXY 必须排除回环地址
        self.assertIn("127.0.0.1", text)

    def test_service_loads_proxy_env(self):
        text = (LXC_DIR / "opencode-serve.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-/etc/opencode/proxy.env", text)

    def test_workspace_agents_mentions_proxy(self):
        text = (LXC_DIR / "workspace-AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("网络与代理", text)
        self.assertIn("HTTP_PROXY", text)
        self.assertIn("proxy.env", text)

    def test_workspace_agents_mentions_customer_proxy(self):
        text = (LXC_DIR / "workspace-AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:40808", text)
        self.assertIn("meshloom", text)

    def test_add_user_verifies_proxy(self):
        text = (LXC_DIR / "add-user.sh").read_text(encoding="utf-8")
        self.assertIn("/etc/opencode/proxy.env", text)
        self.assertIn("出站代理", text)


class PlaywrightMcpConfigTest(unittest.TestCase):
    """模板预装 Playwright MCP，配置改动要保证无显示器、root、走代理都能跑。"""

    def test_opencode_config_enables_playwright_mcp(self):
        text = (LXC_DIR / "opencode.jsonc").read_text(encoding="utf-8")
        self.assertIn('"playwright"', text)
        self.assertIn('"playwright-mcp"', text)
        # 容器内无显示器且以 root 运行
        self.assertIn('"--headless"', text)
        self.assertIn('"--no-sandbox"', text)
        self.assertIn('"--browser"', text)
        self.assertIn('"chromium"', text)
        # 浏览器必须显式走代理，否则打不开外网
        self.assertIn('"--proxy-server"', text)
        self.assertIn("192.168.110.11:10808", text)
        # 截图默认写进 outbox，桥接器才能回传
        self.assertIn("/workspace/outbox", text)

    def test_workspace_agents_points_to_mcp(self):
        text = (LXC_DIR / "workspace-AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Playwright MCP", text)
        self.assertIn("browser_take_screenshot", text)
        self.assertIn("/workspace/outbox", text)
        # 不应再让 agent 临时装 Node.js
        self.assertNotIn("apt-get install -y nodejs npm", text)

    def test_template_manual_installs_playwright(self):
        text = (REPO_ROOT / "docs" / "lxc-template-manual.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("npm install -g @playwright/mcp", text)
        self.assertIn("playwright install --with-deps chromium", text)
        self.assertIn("ms-playwright", text)

    def test_template_manual_installs_node_22_and_ast_grep(self):
        text = (REPO_ROOT / "docs" / "lxc-template-manual.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("setup_22.x", text)
        self.assertIn("npm install -g ast-grep-mcp", text)
        self.assertIn("service.env.example", text)


class McpServersConfigTest(unittest.TestCase):
    """模板内置的 MCP 服务器要和本机一致，远程 MCP 的密钥必须走环境变量。"""

    def setUp(self):
        self.config = _load_jsonc(LXC_DIR / "opencode.jsonc")
        self.servers = self.config["mcp"]["servers"]

    def test_declares_expected_mcp_servers(self):
        self.assertEqual(
            set(self.servers),
            {"context7", "exa", "grep_app", "ast_grep", "playwright"},
        )

    def test_remote_mcp_uses_environment_keys(self):
        context7 = self.servers["context7"]
        self.assertEqual(context7["type"], "remote")
        self.assertEqual(context7["url"], "https://mcp.context7.com/mcp")
        self.assertEqual(
            context7["headers"]["CONTEXT7_API_KEY"], "{env:CONTEXT7_API_KEY}"
        )

        exa = self.servers["exa"]
        self.assertEqual(exa["type"], "remote")
        self.assertEqual(exa["url"], "https://mcp.exa.ai/mcp")
        self.assertEqual(exa["headers"]["x-api-key"], "{env:EXA_API_KEY}")

        grep_app = self.servers["grep_app"]
        self.assertEqual(grep_app["type"], "remote")
        self.assertEqual(grep_app["url"], "https://mcp.grep.app")

    def test_ast_grep_uses_global_command(self):
        ast_grep = self.servers["ast_grep"]
        self.assertEqual(ast_grep["type"], "local")
        self.assertEqual(ast_grep["command"], ["ast-grep-mcp"])

    def test_playwright_uses_global_command(self):
        playwright = self.servers["playwright"]
        self.assertEqual(playwright["type"], "local")
        self.assertEqual(playwright["command"][0], "playwright-mcp")

    def test_no_plaintext_keys_in_config(self):
        text = (LXC_DIR / "opencode.jsonc").read_text(encoding="utf-8")
        markers = (
            "{env:SYSTALYZE_API_KEY}",
            "{env:CONTEXT7_API_KEY}",
            "{env:EXA_API_KEY}",
        )
        for marker in markers:
            self.assertIn(marker, text)
        for prefix in ("stlzi_", "exa_", "ctx7_"):
            self.assertNotIn(prefix, text)


class ServiceEnvExampleTest(unittest.TestCase):
    """service.env 是模型和远程 MCP 密钥的唯一来源。"""

    def test_lists_required_keys(self):
        text = (LXC_DIR / "service.env.example").read_text(encoding="utf-8")
        for key in ("SYSTALYZE_API_KEY=", "CONTEXT7_API_KEY=", "EXA_API_KEY="):
            self.assertIn(key, text)


class AddUserVmidTest(unittest.TestCase):
    """用户 LXC 默认从 901 起编号，避免和基础设施 VMID 混在一起。"""

    def test_default_start_id_is_901(self):
        text = (LXC_DIR / "add-user.sh").read_text(encoding="utf-8")
        self.assertIn("START_ID=901", text)
        self.assertIn("MAX_ID=9999", text)
        self.assertIn("--start-id", text)
        self.assertIn("pvesh get /cluster/nextid", text)
        # 起始号被占用时要继续向上找，不能直接失败
        self.assertIn("for ((id = START_ID; id <= MAX_ID; id++))", text)


class SmokeScriptTest(unittest.TestCase):
    """冒烟脚本是发布前的强制检查，接口路径必须和 opencode v2 保持一致。"""

    def test_read_back_includes_directory_param(self):
        text = (REPO_ROOT / "scripts" / "smoke-test.sh").read_text(encoding="utf-8")
        # v2 的 /api/fs/read 不带 location[directory] 会返回 404
        self.assertIn(
            "/api/fs/read/%2Ftmp%2Fopencode-smoke.bin?location%5Bdirectory%5D=%2Ftmp",
            text,
        )

    def test_write_uses_experimental_endpoint(self):
        text = (REPO_ROOT / "scripts" / "smoke-test.sh").read_text(encoding="utf-8")
        self.assertIn("/api/experimental/fs/write", text)
        self.assertIn("location%5Bdirectory%5D=%2Ftmp", text)


if __name__ == "__main__":
    sys.exit(unittest.main())
