from __future__ import annotations

import fnmatch
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


def _effect(
    rules: list[dict[str, str]], action: str, resource: str
) -> str:
    """Approximate OpenCode rule evaluation: last matching rule wins."""
    effect = "ask"
    for rule in rules:
        if fnmatch.fnmatchcase(action, rule["action"]) and fnmatch.fnmatchcase(
            resource, rule["resource"]
        ):
            effect = rule["effect"]
    return effect



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

    def test_sandbox_mode_relaxes_privilege_patterns(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        resources = {rule["resource"] for rule in rules}
        # Routine and privileged work no longer asks for approval.
        for resource in (
            "sudo *",
            "systemctl *restart*",
            "service * restart*",
            "shutdown *",
            "passwd *",
            "apt install*",
        ):
            self.assertNotIn(resource, resources)
        # Destructive operations stay gated.
        self.assertIn("rm *", resources)
        self.assertIn("git *push*", resources)
        self.assertIn("docker rm*", resources)
        self.assertIn("kubectl delete*", resources)

    def test_sandbox_mode_allows_package_installs(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        self.assertEqual(_effect(rules, "shell", "apt install -y nginx"), "allow")
        self.assertEqual(_effect(rules, "shell", "pip install requests"), "allow")
        self.assertEqual(_effect(rules, "shell", "npm install -g pnpm"), "allow")
        self.assertNotIn(
            {"action": "shell", "resource": "apt install*", "effect": "ask"},
            rules,
        )

    def test_sandbox_mode_allows_edits_and_sensitive_reads(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        self.assertEqual(_effect(rules, "edit", "/workspace/app.py"), "allow")
        self.assertEqual(_effect(rules, "read", "/workspace/.env"), "allow")
        self.assertEqual(
            _effect(rules, "shell", "cat /workspace/.env"), "allow"
        )

    def test_sandbox_mode_allows_external_directory_and_web(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        self.assertEqual(
            _effect(rules, "external_directory", "/etc/*"), "allow"
        )
        self.assertEqual(_effect(rules, "webfetch", "example.com"), "allow")
        self.assertEqual(_effect(rules, "websearch", "*"), "allow")

    def test_sandbox_mode_keeps_destructive_shell_gated(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        for command in (
            "rm -rf /workspace/build",
            "rmdir /workspace/old",
            "dd if=/dev/zero of=/dev/sda",
            "mkfs.ext4 /dev/sdb1",
            "find /workspace -name '*.pyc' -delete",
            "git push origin main",
            "git reset --hard HEAD~1",
            "git clean -fdx",
            "docker rm -f web",
            "docker compose down",
            "podman rmi old:latest",
            "kubectl delete pod web",
            "helm uninstall release",
        ):
            self.assertEqual(
                _effect(rules, "shell", command), "ask", command
            )

    def test_sandbox_mode_non_destructive_shell_allowed(self):
        rules = build_session_permissions("build", sandbox_mode=True)
        for command in (
            "sudo apt update",
            "systemctl restart postgresql",
            "kill -9 1234",
            "chmod 644 /workspace/app.py",
            "docker ps",
            "kubectl get pods",
            "git status",
            "git commit -m test",
        ):
            self.assertEqual(
                _effect(rules, "shell", command), "allow", command
            )

    def test_sandbox_mode_read_only_agent_keeps_edit_deny(self):
        rules = build_session_permissions("plan", sandbox_mode=True)
        self.assertNotIn(
            {"action": "*", "resource": "*", "effect": "allow"}, rules
        )
        self.assertNotIn(EDIT_ASK, rules)
        # No session rule may turn edit into allow; the agent's deny wins.
        self.assertNotEqual(_effect(rules, "edit", "/workspace/app.py"), "allow")
        # Non-write work is still allowed without approval.
        self.assertEqual(_effect(rules, "shell", "ls -la"), "allow")
        self.assertEqual(_effect(rules, "read", "/workspace/README.md"), "allow")
        self.assertEqual(
            _effect(rules, "external_directory", "/etc/*"), "allow"
        )
        self.assertEqual(_effect(rules, "webfetch", "example.com"), "allow")
        self.assertEqual(
            _effect(rules, "shell", "rm -rf /workspace/build"), "ask"
        )

    def test_non_sandbox_keeps_privilege_patterns(self):
        rules = build_session_permissions("build")
        resources = {rule["resource"] for rule in rules}
        self.assertIn("sudo *", resources)
        self.assertIn("apt install*", resources)


if __name__ == "__main__":
    unittest.main()
