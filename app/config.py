from __future__ import annotations

import os
from dataclasses import dataclass


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


@dataclass(frozen=True)
class Config:
    feishu_app_id: str
    feishu_app_secret: str
    allowed_open_ids: frozenset[str]
    opencode_base_url: str
    opencode_username: str
    opencode_password: str
    workspace_dir: str
    agent: str
    model_provider: str
    model_id: str
    db_path: str
    log_path: str
    progress_min_interval: int
    permission_remind_seconds: int
    permission_expire_seconds: int
    task_reconcile_seconds: int
    task_reconcile_grace_seconds: int
    task_timeout_seconds: int
    max_final_chars: int
    max_text_chunk: int
    temp_session_ttl_seconds: int
    temp_workspace_root: str


def load_config() -> Config:
    env_file = os.environ.get(
        "BRIDGE_ENV_FILE", "/home/oc/feishu-opencode-bridge/bridge.env"
    )
    _load_env_file(env_file)

    allowed = frozenset(
        item.strip()
        for item in os.environ.get("FEISHU_ALLOWED_OPEN_IDS", "").split(",")
        if item.strip()
    )

    return Config(
        feishu_app_id=_env("FEISHU_APP_ID"),
        feishu_app_secret=_env("FEISHU_APP_SECRET"),
        allowed_open_ids=allowed,
        opencode_base_url=_env("OPENCODE_BASE_URL", "http://127.0.0.1:4096").rstrip("/"),
        opencode_username=_env("OPENCODE_USERNAME", "opencode"),
        opencode_password=_env("OPENCODE_PASSWORD"),
        workspace_dir=_env("OPENCODE_DIRECTORY", "/home/oc/workspace"),
        agent=_env("OPENCODE_AGENT", "build"),
        model_provider=_env("OPENCODE_MODEL_PROVIDER", "systalyze"),
        model_id=_env("OPENCODE_MODEL_ID", "deepseek-ai/DeepSeek-V4.1-Flash-1017"),
        db_path=_env("BRIDGE_DB", "/home/oc/feishu-opencode-bridge/bridge.db"),
        log_path=_env("BRIDGE_LOG", "/home/oc/feishu-opencode-bridge/bridge.log"),
        progress_min_interval=_env_int("BRIDGE_PROGRESS_MIN_INTERVAL", 20),
        permission_remind_seconds=_env_int("BRIDGE_PERMISSION_REMIND_SECONDS", 600),
        permission_expire_seconds=_env_int("BRIDGE_PERMISSION_EXPIRE_SECONDS", 1800),
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
        temp_workspace_root=_env(
            "BRIDGE_TEMP_WORKSPACE_ROOT",
            "/home/oc/feishu-opencode-bridge/temp",
        ),
    )
