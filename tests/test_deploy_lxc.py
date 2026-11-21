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
