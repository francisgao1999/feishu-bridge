from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app import config as config_module  # noqa: E402
except ImportError:  # flattened workspace without app/ package
    import config as config_module  # noqa: E402


BASE_ENV = {
    "FEISHU_APP_ID": "cli_test",
    "FEISHU_APP_SECRET": "secret",
    "OPENCODE_PASSWORD": "password",
    # Never read a developer's real bridge.env during tests.
    "BRIDGE_ENV_FILE": os.devnull,
}


class ConfigTest(unittest.TestCase):
    def load(self, **env):
        values = dict(BASE_ENV)
        values.update(env)
        with mock.patch.dict(os.environ, values, clear=True):
            return config_module.load_config()

    def test_defaults_are_repo_relative(self):
        cfg = self.load()
        self.assertEqual(cfg.bridge_home, str(REPO_ROOT))
        self.assertEqual(cfg.data_dir, str(REPO_ROOT))
        self.assertEqual(cfg.workspace_dir, str(REPO_ROOT / "workspace"))
        self.assertEqual(cfg.db_path, str(REPO_ROOT / "bridge.db"))
        self.assertEqual(cfg.log_path, str(REPO_ROOT / "bridge.log"))
        self.assertEqual(cfg.temp_workspace_root, str(REPO_ROOT / "temp"))
        self.assertEqual(cfg.agent, "build")
        self.assertEqual(cfg.readonly_agents, frozenset({"plan"}))
        self.assertEqual(cfg.model_provider, "opencode")
        self.assertEqual(cfg.model_id, "big-pickle")

    def test_bridge_home_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bridge"
            cfg = self.load(BRIDGE_HOME=str(home))
            self.assertEqual(cfg.bridge_home, str(home))
            self.assertEqual(cfg.data_dir, str(home))
            self.assertEqual(cfg.workspace_dir, str(home / "workspace"))
            self.assertEqual(cfg.db_path, str(home / "bridge.db"))
            self.assertEqual(cfg.log_path, str(home / "bridge.log"))
            self.assertEqual(cfg.temp_workspace_root, str(home / "temp"))

    def test_data_dir_override_and_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bridge"
            cfg = self.load(BRIDGE_HOME=str(home), BRIDGE_DATA_DIR="data")
            self.assertEqual(cfg.data_dir, str(home / "data"))
            self.assertEqual(cfg.db_path, str(home / "data" / "bridge.db"))
            self.assertEqual(cfg.log_path, str(home / "data" / "bridge.log"))
            self.assertEqual(
                cfg.temp_workspace_root, str(home / "data" / "temp")
            )

    def test_absolute_path_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self.load(
                BRIDGE_HOME=str(root / "home"),
                BRIDGE_DATA_DIR=str(root / "data"),
                BRIDGE_DB=str(root / "custom.db"),
                BRIDGE_LOG=str(root / "custom.log"),
                BRIDGE_TEMP_WORKSPACE_ROOT=str(root / "custom-temp"),
                OPENCODE_DIRECTORY=str(root / "custom-workspace"),
            )
            self.assertEqual(cfg.db_path, str(root / "custom.db"))
            self.assertEqual(cfg.log_path, str(root / "custom.log"))
            self.assertEqual(
                cfg.temp_workspace_root, str(root / "custom-temp")
            )
            self.assertEqual(cfg.workspace_dir, str(root / "custom-workspace"))

    def test_relative_overrides_resolve_against_their_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "bridge"
            cfg = self.load(
                BRIDGE_HOME=str(home),
                BRIDGE_DATA_DIR="data",
                BRIDGE_DB="db/custom.db",
                BRIDGE_LOG="logs/custom.log",
                BRIDGE_TEMP_WORKSPACE_ROOT="tmp",
                OPENCODE_DIRECTORY="work",
            )
            self.assertEqual(cfg.db_path, str(home / "data" / "db" / "custom.db"))
            self.assertEqual(
                cfg.log_path, str(home / "data" / "logs" / "custom.log")
            )
            self.assertEqual(cfg.temp_workspace_root, str(home / "data" / "tmp"))
            self.assertEqual(cfg.workspace_dir, str(home / "work"))

    def test_readonly_agents_override(self):
        cfg = self.load(OPENCODE_READONLY_AGENTS="plan, reviewer")
        self.assertEqual(
            cfg.readonly_agents, frozenset({"plan", "reviewer"})
        )
        cfg = self.load(OPENCODE_READONLY_AGENTS="")
        self.assertEqual(cfg.readonly_agents, frozenset())

    def test_env_file_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "bridge.env"
            env_file.write_text(
                "FEISHU_APP_ID=from_file\n"
                "FEISHU_APP_SECRET=from_file\n"
                "OPENCODE_PASSWORD=from_file\n"
                "OPENCODE_MODEL_ID=from-file-model\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"BRIDGE_ENV_FILE": str(env_file)}, clear=True
            ):
                cfg = config_module.load_config()
            self.assertEqual(cfg.feishu_app_id, "from_file")
            self.assertEqual(cfg.model_id, "from-file-model")

    def test_existing_env_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "bridge.env"
            env_file.write_text(
                "FEISHU_APP_ID=from_file\n"
                "FEISHU_APP_SECRET=from_file\n"
                "OPENCODE_PASSWORD=from_file\n",
                encoding="utf-8",
            )
            values = dict(BASE_ENV)
            values["BRIDGE_ENV_FILE"] = str(env_file)
            values["FEISHU_APP_ID"] = "from_env"
            with mock.patch.dict(os.environ, values, clear=True):
                cfg = config_module.load_config()
            self.assertEqual(cfg.feishu_app_id, "from_env")

    def test_bridge_home_can_be_set_in_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env_file = Path(tmp) / "bridge.env"
            env_file.write_text(
                f"BRIDGE_HOME={home}\n"
                "FEISHU_APP_ID=x\n"
                "FEISHU_APP_SECRET=y\n"
                "OPENCODE_PASSWORD=z\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"BRIDGE_ENV_FILE": str(env_file)}, clear=True
            ):
                cfg = config_module.load_config()
            self.assertEqual(cfg.bridge_home, str(home))
            self.assertEqual(cfg.data_dir, str(home))
            self.assertEqual(cfg.db_path, str(home / "bridge.db"))


if __name__ == "__main__":
    unittest.main()
