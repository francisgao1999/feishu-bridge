from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.users import (  # noqa: E402
        UserRegistry,
        UserRegistryError,
        parse_users,
    )
except ImportError:  # flattened workspace without app/ package
    from users import (  # noqa: E402
        UserRegistry,
        UserRegistryError,
        parse_users,
    )


def _user(**overrides):
    item = {
        "open_id": "ou_alice",
        "slug": "alice",
        "url": "http://10.0.0.11:4096",
        "password": "secret",
    }
    item.update(overrides)
    return item


class ParseUsersTest(unittest.TestCase):
    def test_parses_and_indexes_by_open_id(self):
        users = parse_users([_user(), _user(open_id="ou_bob", slug="bob")])
        self.assertEqual(set(users), {"ou_alice", "ou_bob"})
        self.assertEqual(users["ou_alice"].workspace, "/workspace")
        self.assertEqual(users["ou_alice"].username, "opencode")
        self.assertEqual(users["ou_alice"].base_url, "http://10.0.0.11:4096")

    def test_accepts_wrapped_users_key(self):
        users = parse_users({"users": [_user()]})
        self.assertIn("ou_alice", users)

    def test_rejects_duplicate_open_id(self):
        with self.assertRaises(UserRegistryError):
            parse_users([_user(), _user(slug="alice2")])

    def test_rejects_duplicate_slug(self):
        with self.assertRaises(UserRegistryError):
            parse_users([_user(), _user(open_id="ou_bob")])

    def test_rejects_bad_slug(self):
        with self.assertRaises(UserRegistryError):
            parse_users([_user(slug="Alice")])

    def test_rejects_relative_workspace(self):
        with self.assertRaises(UserRegistryError):
            parse_users([_user(workspace="workspace")])

    def test_rejects_non_http_url(self):
        with self.assertRaises(UserRegistryError):
            parse_users([_user(url="10.0.0.11:4096")])

    def test_rejects_missing_password(self):
        with self.assertRaises(UserRegistryError):
            parse_users([{"open_id": "ou_x", "slug": "x", "url": "http://x"}])

    def test_rejects_invalid_json(self):
        with self.assertRaises(UserRegistryError):
            parse_users("{not json")

    def test_rejects_non_list(self):
        with self.assertRaises(UserRegistryError):
            parse_users({"open_id": "ou_x"})


class UserRegistryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "users.json"
        self.path.write_text(json.dumps([_user()]), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _registry(self, **kwargs):
        return UserRegistry(str(self.path), reload_interval=0, **kwargs)

    def test_get_and_len(self):
        registry = self._registry()
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.get("ou_alice").slug, "alice")
        self.assertIsNone(registry.get("ou_missing"))
        self.assertIn("ou_alice", registry)

    def test_reloads_when_mtime_changes(self):
        registry = self._registry()
        self.assertEqual(len(registry), 1)
        self.path.write_text(
            json.dumps([_user(), _user(open_id="ou_bob", slug="bob")]),
            encoding="utf-8",
        )
        self.assertTrue(registry.load())
        self.assertEqual(len(registry), 2)
        self.assertIn("ou_bob", registry)

    def test_invalid_reload_keeps_previous_table(self):
        registry = self._registry()
        self.assertEqual(len(registry), 1)
        self.path.write_text("{broken", encoding="utf-8")
        self.assertFalse(registry.load())
        self.assertEqual(len(registry), 1)
        self.assertIn("ou_alice", registry)

    def test_missing_file_yields_empty_registry(self):
        self.path.unlink()
        registry = self._registry()
        self.assertEqual(len(registry), 0)
        self.assertIsNone(registry.get("ou_alice"))

    def test_by_url_deduplicates_servers(self):
        self.path.write_text(
            json.dumps(
                [
                    _user(),
                    _user(open_id="ou_alice2", slug="alice2"),
                ]
            ),
            encoding="utf-8",
        )
        registry = self._registry()
        self.assertEqual(len(registry.by_url()), 1)

    def test_unconfigured_registry_is_empty(self):
        registry = UserRegistry(None, reload_interval=0)
        self.assertFalse(registry.configured)
        self.assertEqual(len(registry), 0)
        self.assertFalse(registry.load())

    def test_configured_true_when_file_exists(self):
        registry = self._registry()
        self.assertTrue(registry.configured)

    def test_configured_false_when_file_missing(self):
        self.path.unlink()
        registry = self._registry()
        self.assertFalse(registry.configured)

    def test_empty_array_is_configured_but_has_no_users(self):
        self.path.write_text("[]", encoding="utf-8")
        registry = self._registry()
        self.assertTrue(registry.configured)
        self.assertEqual(len(registry), 0)

    def test_configured_flips_false_after_file_removal(self):
        registry = self._registry()
        self.assertTrue(registry.configured)
        self.path.unlink()
        self.assertTrue(registry.load())
        self.assertFalse(registry.configured)
        self.assertEqual(len(registry), 0)

    def test_reload_interval_skips_stat(self):
        registry = UserRegistry(str(self.path), reload_interval=3600)
        self.assertTrue(registry.load())
        # Second call within the interval must not re-read the file.
        self.path.write_text(json.dumps([]), encoding="utf-8")
        self.assertFalse(registry.load())
        self.assertEqual(len(registry), 1)


if __name__ == "__main__":
    unittest.main()
