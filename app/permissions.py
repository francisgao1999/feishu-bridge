from __future__ import annotations

import re
from typing import Any

# Shell commands that require approval.
#
# OpenCode matches the whole command string with simple wildcards:
#   *  zero or more characters (including spaces and /)
#   ?  exactly one character
# A pattern ending in " *" also matches the command without arguments.
#
# This is a best-effort denylist, not a security boundary. Commands that hide
# their behavior behind interpreters (python -c, perl -e, bash script.sh, ...)
# or uncommon tools can still be destructive. For stronger isolation, run the
# bot in a container/VM or switch to a narrow allowlist.
DANGEROUS_SHELL_PATTERNS: tuple[str, ...] = (
    # Destructive file operations
    "rm *",
    "rmdir *",
    "unlink *",
    "shred *",
    "truncate *",
    "dd *",
    "mkfs*",
    "wipefs *",
    "fdisk *",
    "parted *",
    "sgdisk *",
    "mkswap *",
    "swapoff *",
    "find * -delete*",
    "find * -exec rm*",
    "find * -exec */rm*",
    "xargs * rm*",
    # Privilege and system control
    "sudo *",
    "su *",
    "doas *",
    "pkexec *",
    "systemctl *start*",
    "systemctl *stop*",
    "systemctl *restart*",
    "systemctl *reload*",
    "systemctl *enable*",
    "systemctl *disable*",
    "systemctl *mask*",
    "systemctl *unmask*",
    "systemctl *daemon-reload*",
    "systemctl *daemon-reexec*",
    "systemctl *isolate*",
    "systemctl *kill*",
    "systemctl *set-property*",
    "systemctl *edit*",
    "systemctl *revert*",
    "service * start*",
    "service * stop*",
    "service * restart*",
    "service * reload*",
    "shutdown *",
    "reboot *",
    "poweroff *",
    "halt *",
    "kill *",
    "pkill *",
    "killall *",
    "chmod *",
    "chown *",
    "chattr *",
    "setfacl *",
    "mount *",
    "umount *",
    "iptables *",
    "nft *",
    "ufw *",
    "firewall-cmd *",
    "useradd *",
    "userdel *",
    "usermod *",
    "groupadd *",
    "groupdel *",
    "passwd *",
    "crontab *",
    "at *",
    # Package managers
    "apt install*",
    "apt remove*",
    "apt purge*",
    "apt upgrade*",
    "apt full-upgrade*",
    "apt dist-upgrade*",
    "apt autoremove*",
    "apt-get install*",
    "apt-get remove*",
    "apt-get purge*",
    "apt-get upgrade*",
    "apt-get dist-upgrade*",
    "apt-get autoremove*",
    "yum install*",
    "yum remove*",
    "yum erase*",
    "yum update*",
    "yum upgrade*",
    "yum autoremove*",
    "dnf install*",
    "dnf remove*",
    "dnf erase*",
    "dnf update*",
    "dnf upgrade*",
    "dnf autoremove*",
    "pacman -S*",
    "pacman -R*",
    "pacman -U*",
    "snap install*",
    "snap remove*",
    "snap refresh*",
    "snap revert*",
    "pip install*",
    "pip3 install*",
    "pip uninstall*",
    "pip3 uninstall*",
    "npm install -g*",
    "npm uninstall -g*",
    "npm update -g*",
    "cargo install*",
    "cargo uninstall*",
    "go install*",
    # Containers / orchestration
    "docker rm*",
    "docker rmi*",
    "docker * prune*",
    "docker volume rm*",
    "docker network rm*",
    "docker compose down*",
    "docker-compose down*",
    "podman rm*",
    "podman rmi*",
    "podman * prune*",
    "podman volume rm*",
    "podman network rm*",
    "podman-compose down*",
    "kubectl delete*",
    "kubectl apply*",
    "kubectl create*",
    "kubectl replace*",
    "kubectl patch*",
    "kubectl edit*",
    "kubectl scale*",
    "kubectl rollout*",
    "helm uninstall*",
    "helm upgrade*",
    "helm install*",
    # Destructive git operations
    "git *push*",
    "git *reset --hard*",
    "git *clean*",
    "git *checkout --*",
    "git *restore*",
    "git *branch -D*",
    "git *tag -d*",
    "git *remote remove*",
    "git *stash drop*",
    "git *stash clear*",
    "git *filter-branch*",
    "git *filter-repo*",
    # Sensitive files read through common shell commands
    "cat *.ssh/id_rsa",
    "cat *.ssh/id_ed25519",
    "cat *.ssh/id_ecdsa",
    "cat *.ssh/id_dsa",
    "cat *.ssh/authorized_keys",
    "cat *.env",
    "cat *.env.*",
)


def _wildcard_match(value: str, pattern: str) -> bool:
    """Match OpenCode's simple ``*`` / ``?`` wildcard syntax."""
    regex = (
        "^"
        + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
        + "$"
    )
    return re.match(regex, value) is not None


def agent_is_read_only(agent: dict[str, Any] | None) -> bool:
    """Whether an agent's own rules deny generic file edits.

    OpenCode merges agent rules before session rules and lets the last match
    win, so a session-level ``edit * ask`` would override a read-only agent's
    ``edit * deny``. Detecting this lets the bridge avoid injecting that rule
    for read-only agents such as ``plan``.
    """
    if not agent:
        return False
    rules = agent.get("permissions")
    if not isinstance(rules, list):
        return False
    effect: str | None = None
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        action = str(rule.get("action") or "")
        resource = str(rule.get("resource") or "")
        if _wildcard_match("edit", action) and _wildcard_match("*", resource):
            effect = str(rule.get("effect") or "ask")
    return effect == "deny"


def build_session_permissions(
    agent: str = "build",
    *,
    readonly_agents: frozenset[str] | set[str] = frozenset({"plan"}),
    agent_info: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return the ordered permission rules for a new bridge session.

    Read-only agents (``plan`` by default) must not receive the session-level
    ``edit * ask`` rule, otherwise it would override the agent's own
    ``edit * deny`` rule.
    """
    read_only = agent in readonly_agents or agent_is_read_only(agent_info)
    rules: list[dict[str, str]] = [
        # Allow routine shell commands. Specific rules below override this.
        {"action": "shell", "resource": "*", "effect": "allow"},
    ]
    rules.extend(
        {"action": "shell", "resource": pattern, "effect": "ask"}
        for pattern in DANGEROUS_SHELL_PATTERNS
    )
    # .env.example is safe to read even though it matches cat *.env.*.
    rules.append(
        {
            "action": "shell",
            "resource": "cat *.env.example",
            "effect": "allow",
        }
    )
    if not read_only:
        # File edits still require approval for normal agents.
        rules.append({"action": "edit", "resource": "*", "effect": "ask"})
    rules.extend(
        [
            # Sensitive reads still require approval.
            {"action": "read", "resource": "*.env", "effect": "ask"},
            {"action": "read", "resource": "*.env.*", "effect": "ask"},
            {"action": "read", "resource": "*.env.example", "effect": "allow"},
        ]
    )
    return rules
