from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.permissions import (  # noqa: E402
        agent_is_read_only,
        build_session_permissions,
    )
except ImportError:  # flattened workspace without app/ package
    from permissions import (  # noqa: E402
        agent_is_read_only,
        build_session_permissions,
    )


EDIT_ASK = {"action": "edit", "resource": "*", "effect": "ask"}


class SessionPermissionsTest(unittest.TestCase):
    def test_build_requires_edit_approval(self):
        rules = build_session_permissions("build")
        self.assertIn(EDIT_ASK, rules)

    def test_plan_does_not_override_agent_edit_deny(self):
        rules = build_session_permissions("plan")
        self.assertNotIn(EDIT_ASK, rules)

    def test_readonly_agents_config(self):
        rules = build_session_permissions(
            "custom", readonly_agents=frozenset({"custom"})
        )
        self.assertNotIn(EDIT_ASK, rules)

    def test_detects_read_only_agent_permissions(self):
        info = {
            "permissions": [
                {"action": "*", "resource": "*", "effect": "allow"},
                {"action": "edit", "resource": "*", "effect": "deny"},
                {
                    "action": "edit",
                    "resource": "~/.opencode/plan/*",
                    "effect": "allow",
                },
            ]
        }
        self.assertTrue(agent_is_read_only(info))
        rules = build_session_permissions("custom", agent_info=info)
        self.assertNotIn(EDIT_ASK, rules)

    def test_non_read_only_agent_keeps_edit_approval(self):
        info = {
            "permissions": [
                {"action": "*", "resource": "*", "effect": "allow"},
            ]
        }
        self.assertFalse(agent_is_read_only(info))
        rules = build_session_permissions("custom", agent_info=info)
        self.assertIn(EDIT_ASK, rules)

    def test_last_matching_edit_rule_wins(self):
        info = {
            "permissions": [
                {"action": "edit", "resource": "*", "effect": "deny"},
                {"action": "edit", "resource": "*", "effect": "allow"},
            ]
        }
        self.assertFalse(agent_is_read_only(info))

    def test_shell_rules_still_present_for_plan(self):
        rules = build_session_permissions("plan")
        self.assertIn(
            {"action": "shell", "resource": "*", "effect": "allow"},
            rules,
        )
        self.assertIn(
            {"action": "shell", "resource": "rm *", "effect": "ask"},
            rules,
        )


if __name__ == "__main__":
    unittest.main()
