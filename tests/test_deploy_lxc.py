from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LXC_DIR = REPO_ROOT / "deploy" / "lxc"


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
