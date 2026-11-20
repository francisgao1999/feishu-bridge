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


if __name__ == "__main__":
    sys.exit(unittest.main())
