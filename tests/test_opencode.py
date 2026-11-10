from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.opencode import parse_agents  # noqa: E402
except ImportError:  # flattened workspace without app/ package
    from opencode import parse_agents  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
