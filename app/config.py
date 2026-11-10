from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


_APP_DIR = Path(__file__).resolve().parent
if (_APP_DIR.parent / "app" / "config.py").is_file():
    _DEFAULT_BRIDGE_HOME = _APP_DIR.parent
else:
    _DEFAULT_BRIDGE_HOME = _APP_DIR


def _load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"missing required env: {name}")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _bridge_home() -> Path:
    raw = os.environ.get("BRIDGE_HOME")
    return Path(raw).expanduser() if raw else _DEFAULT_BRIDGE_HOME


def _data_dir(bridge_home: Path) -> Path:
    raw = os.environ.get("BRIDGE_DATA_DIR")
    value = Path(raw).expanduser() if raw else bridge_home
    if not value.is_absolute():
        value = bridge_home / value
    return value


def _path(name: str, default: Path, base: Path) -> str:
    raw = os.environ.get(name)
    value = Path(raw).expanduser() if raw else default
    if not value.is_absolute():
        value = base / value
    return str(value)


@dataclass(frozen=True)
class Config:
    bridge_home: str
    data_dir: str
    feishu_app_id: str
    feishu_app_secret: str
    allowed_open_ids: frozenset[str]
    opencode_base_url: str
    opencode_username: str
    opencode_password: str
    workspace_dir: str
    agent: str
    readonly_agents: frozenset[str]
    model_provider: str
    model_id: str
    db_path: str
    log_path: str
    progress_min_interval: int
    permission_remind_seconds: int
    permission_expire_seconds: int
    form_remind_seconds: int
    form_expire_seconds: int
    task_reconcile_seconds: int
    task_reconcile_grace_seconds: int
    task_timeout_seconds: int
    max_final_chars: int
    max_text_chunk: int
    temp_session_ttl_seconds: int
    temp_workspace_root: str


def load_config() -> Config:
    env_file = os.environ.get("BRIDGE_ENV_FILE")
    if env_file:
        env_file_path = Path(env_file).expanduser()
        if not env_file_path.is_absolute():
            env_file_path = _bridge_home() / env_file_path
    else:
        env_file_path = _bridge_home() / "bridge.env"
    _load_env_file(str(env_file_path))

    # BRIDGE_HOME / BRIDGE_DATA_DIR may also be set inside bridge.env.
    bridge_home = _bridge_home()
    data_dir = _data_dir(bridge_home)

    allowed = frozenset(
        item.strip()
        for item in os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").split(",")
        if item.strip()
    )

    # 默认与仓库同级：~/feishu-opencode-bridge 旁边是 ~/workspace。
    workspace_dir = _path(
        "OPENCODE_DIRECTORY", bridge_home.parent / "workspace", bridge_home
    )

    return Config(
        bridge_home=str(bridge_home),
        data_dir=str(data_dir),
        feishu_app_id=_env("FEISHU_APP_ID"),
        feishu_app_secret=_env("FEISHU_APP_SECRET"),
        allowed_open_ids=allowed,
        opencode_base_url=_env("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/"),
        opencode_username=_env("OPENCODE_USERNAME", "opencode"),
        opencode_password=_env("OPENCODE_PASSWORD"),
        workspace_dir=workspace_dir,
        agent=_env("OPENCODE_AGENT", "build"),
        readonly_agents=frozenset(
            item.strip()
            for item in _env(
                "OPENCODE_READONLY_AGENTS", "plan"
            ).split(",")
            if item.strip()
        ),
        model_provider=_env("OPENCODE_MODEL_PROVIDER", "opencode"),
        model_id=_env("OPENCODE_MODEL_ID", "big-pickle"),
        db_path=_path("BRIDGE_DB", data_dir / "bridge.db", data_dir),
        log_path=_path("BRIDGE_LOG", data_dir / "bridge.log", data_dir),
        progress_min_interval=_env_int("BRIDGE_PROGRESS_MIN_INTERVAL", 20),
        permission_remind_seconds=_env_int("BRIDGE_PERMISSION_REMIND_SECONDS", 600),
        permission_expire_seconds=_env_int("BRIDGE_PERMISSION_EXPIRE_SECONDS", 1800),
        form_remind_seconds=_env_int("BRIDGE_FORM_REMIND_SECONDS", 300),
        form_expire_seconds=_env_int("BRIDGE_FORM_EXPIRE_SECONDS", 1800),
        task_reconcile_seconds=_env_int("BRIDGE_TASK_RECONCILE_SECONDS", 60),
        task_reconcile_grace_seconds=_env_int(
            "BRIDGE_TASK_RECONCILE_GRACE_SECONDS", 120
        ),
        task_timeout_seconds=_env_int("BRIDGE_TASK_TIMEOUT_SECONDS", 3600),
        max_final_chars=_env_int("BRIDGE_MAX_FINAL_CHARS", 3000),
        max_text_chunk=_env_int("BRIDGE_MAX_TEXT_CHUNK", 3000),
        temp_session_ttl_seconds=_env_int(
            "BRIDGE_TEMP_SESSION_TTL_SECONDS", 1800
        ),
        temp_workspace_root=_path(
            "BRIDGE_TEMP_WORKSPACE_ROOT",
            Path(workspace_dir) / "temp",
            data_dir,
        ),
    )
