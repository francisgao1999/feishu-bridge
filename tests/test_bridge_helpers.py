from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

try:
    from bridge import (  # noqa: E402
        _guess_image_ext,
        _human_size,
        _is_stats_request,
        _sha256_file,
        _short,
    )
except ImportError:  # pragma: no cover - app/ layout without flat imports
    from app.bridge import (  # noqa: E402
        _guess_image_ext,
        _human_size,
        _is_stats_request,
        _sha256_file,
        _short,
    )


class ShortTest(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(_short("a  b\n c"), "a b c")

    def test_truncates_with_ellipsis(self):
        result = _short("x" * 200, limit=10)
        self.assertEqual(len(result), 10)
        self.assertTrue(result.endswith("…"))


class StatsRequestTest(unittest.TestCase):
    def test_accepts_common_phrases(self):
        for text in (
            "stats",
            "用量",
            "用量统计",
            "发送用量统计",
            "给我推送用量统计卡片",
            "Usage",
        ):
            self.assertTrue(_is_stats_request(text), text)

    def test_rejects_normal_prompts(self):
        for text in (
            "发送昨日到现在 linux.do 福利羊毛板块的帖子摘要",
            "整理周报",
            "查看统计报告",
        ):
            self.assertFalse(_is_stats_request(text), text)


class HumanSizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_human_size(0), "0 B")
        self.assertEqual(_human_size(512), "512 B")
        self.assertEqual(_human_size(1024), "1.0 KB")
        self.assertEqual(_human_size(1024 * 1024), "1.0 MB")
        self.assertEqual(_human_size(3 * 1024 * 1024 * 1024), "3.0 GB")


class GuessImageExtTest(unittest.TestCase):
    def _write(self, data: bytes) -> str:
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(data)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_png(self):
        path = self._write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
        self.assertEqual(_guess_image_ext(path), ".png")

    def test_jpeg(self):
        path = self._write(b"\xff\xd8\xff\xe0" + b"\x00" * 8)
        self.assertEqual(_guess_image_ext(path), ".jpg")

    def test_gif(self):
        path = self._write(b"GIF89a" + b"\x00" * 8)
        self.assertEqual(_guess_image_ext(path), ".gif")

    def test_unknown(self):
        path = self._write(b"not-an-image")
        self.assertEqual(_guess_image_ext(path), ".bin")


class Sha256FileTest(unittest.TestCase):
    def test_matches_known_digest(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"abc")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        self.assertEqual(
            _sha256_file(handle.name),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


if __name__ == "__main__":
    unittest.main()
