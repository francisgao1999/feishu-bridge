from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_USERNAME = "opencode"


class UserRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class UserEntry:
    """One Feishu user mapped to one OpenCode server."""

    open_id: str
    slug: str
    url: str
    password: str
    workspace: str = DEFAULT_WORKSPACE
    username: str = DEFAULT_USERNAME

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    def public_dict(self) -> dict[str, str]:
        """Serialisable form without the password (safe for logs)."""
        return {
            "open_id": self.open_id,
            "slug": self.slug,
            "url": self.base_url,
            "workspace": self.workspace,
        }


def _require_str(item: dict[str, Any], key: str, where: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UserRegistryError(f"{where}: missing or empty '{key}'")
    return value.strip()


def parse_users(data: Any) -> dict[str, UserEntry]:
    """Validate a parsed users.json payload and index it by open_id."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise UserRegistryError(f"invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        # Accept {"users": [...]} as well as a bare list.
        data = data.get("users")
    if not isinstance(data, list):
        raise UserRegistryError("users.json must contain a JSON array")

    users: dict[str, UserEntry] = {}
    slugs: set[str] = set()
    for index, item in enumerate(data):
        where = f"users[{index}]"
        if not isinstance(item, dict):
            raise UserRegistryError(f"{where}: must be an object")
        open_id = _require_str(item, "open_id", where)
        slug = _require_str(item, "slug", where)
        url = _require_str(item, "url", where)
        password = _require_str(item, "password", where)
        workspace = item.get("workspace") or DEFAULT_WORKSPACE
        username = item.get("username") or DEFAULT_USERNAME
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise UserRegistryError(
                f"{where}: 'workspace' must be an absolute path"
            )
        if not isinstance(username, str):
            raise UserRegistryError(f"{where}: 'username' must be a string")
        if not SLUG_RE.match(slug):
            raise UserRegistryError(
                f"{where}: 'slug' must match {SLUG_RE.pattern}"
            )
        if not (url.startswith("http://") or url.startswith("https://")):
            raise UserRegistryError(
                f"{where}: 'url' must start with http:// or https://"
            )
        if open_id in users:
            raise UserRegistryError(f"{where}: duplicate open_id {open_id}")
        if slug in slugs:
            raise UserRegistryError(f"{where}: duplicate slug {slug}")
        users[open_id] = UserEntry(
            open_id=open_id,
            slug=slug,
            url=url.rstrip("/"),
            password=password,
            workspace=workspace,
            username=username,
        )
        slugs.add(slug)
    return users


class UserRegistry:
    """Loads users.json and reloads it when the file's mtime changes.

    The file is mounted as a directory in Docker (single-file bind mounts keep
    the old inode after ``pct push``), so mtime polling is the only reliable
    change signal.
    """

    def __init__(
        self,
        path: str | None = None,
        *,
        reload_interval: float = 5.0,
    ):
        self._path = path
        self._reload_interval = reload_interval
        self._users: dict[str, UserEntry] = {}
        self._mtime_ns: int | None = None
        self._last_check = 0.0
        self._loaded = False
        self._exists = False

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def configured(self) -> bool:
        """True when users.json exists (an empty array means nobody is open)."""
        self.load()
        return self._exists

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self, *, force: bool = False) -> bool:
        """Reload if needed. Returns True when the table actually changed."""
        if not self._path:
            return False
        now = time.monotonic()
        if (
            not force
            and self._loaded
            and now - self._last_check < self._reload_interval
        ):
            return False
        self._last_check = now
        try:
            stat = os.stat(self._path)
        except FileNotFoundError:
            was_configured = self._exists
            self._exists = False
            if self._loaded and not self._users and not was_configured:
                return False
            log.warning("users file not found: %s", self._path)
            self._users = {}
            self._mtime_ns = None
            self._loaded = True
            return True
        except OSError as exc:
            log.warning("cannot stat users file %s: %s", self._path, exc)
            return False
        self._exists = True
        if self._loaded and self._mtime_ns == stat.st_mtime_ns:
            return False
        try:
            raw = Path(self._path).read_text(encoding="utf-8")
            users = parse_users(raw)
        except (OSError, UserRegistryError) as exc:
            # Keep the previous table: a half-written file must not kick
            # every user out.
            log.error("cannot load users file %s: %s", self._path, exc)
            return False
        self._users = users
        self._mtime_ns = stat.st_mtime_ns
        self._loaded = True
        log.info(
            "loaded %d user(s) from %s: %s",
            len(users),
            self._path,
            ", ".join(entry.slug for entry in users.values()),
        )
        return True

    def get(self, open_id: str) -> UserEntry | None:
        self.load()
        return self._users.get(open_id)

    def entries(self) -> list[UserEntry]:
        self.load()
        return list(self._users.values())

    def open_ids(self) -> set[str]:
        self.load()
        return set(self._users)

    def slugs(self) -> set[str]:
        self.load()
        return {entry.slug for entry in self._users.values()}

    def by_url(self) -> dict[str, UserEntry]:
        """Unique servers keyed by base URL (one SSE loop per server)."""
        self.load()
        seen: dict[str, UserEntry] = {}
        for entry in self._users.values():
            seen.setdefault(entry.base_url, entry)
        return seen

    def __len__(self) -> int:
        self.load()
        return len(self._users)

    def __contains__(self, open_id: object) -> bool:
        if not isinstance(open_id, str):
            return False
        return self.get(open_id) is not None
