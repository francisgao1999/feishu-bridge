from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = REPO_ROOT / "backup.sh"


class BackupScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = self.root / "install"
        self.install.mkdir()
        shutil.copy2(BACKUP_SCRIPT, self.install / "backup.sh")
        (self.install / "backup.sh").chmod(0o755)
        app_dir = self.install / "app"
        app_dir.mkdir()
        (app_dir / "main.py").write_text("print('hi')\n", encoding="utf-8")
        cache_dir = app_dir / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "main.pyc").write_bytes(b"junk")

    def make_db(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO t (value) VALUES ('hello')")
            conn.commit()
        finally:
            conn.close()

    def run_backup(self, env: dict[str, str] | None = None):
        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
        }
        if env:
            base_env.update(env)
        return subprocess.run(
            ["bash", str(self.install / "backup.sh")],
            env=base_env,
            capture_output=True,
            text=True,
        )

    def read_tar(self, path: Path) -> set[str]:
        with tarfile.open(path, "r:gz") as tar:
            return set(tar.getnames())

    def test_default_paths(self):
        self.make_db(self.install / "bridge.db")
        (self.install / "bridge.env").write_text(
            "FEISHU_APP_ID=test\n", encoding="utf-8"
        )
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list((self.install / "backups").glob("bridge-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        names = self.read_tar(backups[0])
        self.assertIn("./bridge.db", names)
        self.assertIn("./bridge.env", names)
        self.assertIn("./app/main.py", names)
        self.assertNotIn("./app/__pycache__/main.pyc", names)

    def test_custom_paths(self):
        db = self.root / "data" / "custom.db"
        db.parent.mkdir()
        self.make_db(db)
        env_file = self.root / "config" / "custom.env"
        env_file.parent.mkdir()
        env_file.write_text("FEISHU_APP_ID=test\n", encoding="utf-8")
        dest = self.root / "backups"
        result = self.run_backup(
            {
                "BRIDGE_DB": str(db),
                "BRIDGE_ENV_FILE": str(env_file),
                "BRIDGE_BACKUP_DIR": str(dest),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(dest.glob("bridge-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        names = self.read_tar(backups[0])
        self.assertIn("./bridge.db", names)
        self.assertIn("./bridge.env", names)

    def test_data_dir_only(self):
        data_dir = self.root / "data"
        data_dir.mkdir()
        self.make_db(data_dir / "bridge.db")
        env_file = self.root / "config" / "custom.env"
        env_file.parent.mkdir()
        env_file.write_text("FEISHU_APP_ID=test\n", encoding="utf-8")
        dest = self.root / "backups"
        result = self.run_backup(
            {
                "BRIDGE_DATA_DIR": str(data_dir),
                "BRIDGE_ENV_FILE": str(env_file),
                "BRIDGE_BACKUP_DIR": str(dest),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(dest.glob("bridge-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        names = self.read_tar(backups[0])
        self.assertIn("./bridge.db", names)
        self.assertIn("./bridge.env", names)

    def test_relative_paths_resolve_against_install_dir(self):
        data_dir = self.install / "data"
        data_dir.mkdir()
        self.make_db(data_dir / "bridge.db")
        (self.install / "bridge.env").write_text(
            "BRIDGE_DATA_DIR=data\n"
            "BRIDGE_BACKUP_DIR=backups\n",
            encoding="utf-8",
        )
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list((self.install / "backups").glob("bridge-*.tar.gz"))
        self.assertEqual(len(backups), 1)
        names = self.read_tar(backups[0])
        self.assertIn("./bridge.db", names)
        self.assertIn("./bridge.env", names)

    def test_missing_database_fails(self):
        result = self.run_backup({"BRIDGE_DB": str(self.root / "missing.db")})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing database", result.stderr)

    def test_env_file_in_install_dir_is_sourced(self):
        self.make_db(self.install / "bridge.db")
        dest = self.root / "from-env"
        (self.install / "bridge.env").write_text(
            f"BRIDGE_BACKUP_DIR={dest}\n", encoding="utf-8"
        )
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(dest.glob("bridge-*.tar.gz"))), 1)

    def test_backup_is_private(self):
        self.make_db(self.install / "bridge.db")
        (self.install / "bridge.env").write_text(
            "FEISHU_APP_ID=test\n", encoding="utf-8"
        )
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = next((self.install / "backups").glob("bridge-*.tar.gz"))
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
