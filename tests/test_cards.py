from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

try:
    from cards import PROGRESS_LOG_LINES, _log_window, progress_card  # noqa: E402
except ImportError:  # pragma: no cover - app/ layout without flat imports
    from app.cards import (  # noqa: E402
        PROGRESS_LOG_LINES,
        _log_window,
        progress_card,
    )


def _code_block_lines(content: str) -> list[str]:
    """Extract the lines inside the first fenced code block."""
    lines = content.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("```"))
    end = next(
        i for i in range(start + 1, len(lines)) if lines[i].startswith("```")
    )
    return lines[start + 1 : end]


def _find_log_block(card: dict) -> list[str]:
    for element in card["elements"]:
        if element.get("tag") != "div":
            continue
        text = element.get("text") or {}
        content = text.get("content") or ""
        if "```" in content:
            return _code_block_lines(content)
    raise AssertionError("no log code block found")


class LogWindowTest(unittest.TestCase):
    def test_pads_to_fixed_height(self):
        block = _log_window(["one", "two"])
        lines = _code_block_lines(block)
        self.assertEqual(len(lines), PROGRESS_LOG_LINES)
        self.assertEqual(lines[-2:], ["one", "two"])

    def test_keeps_only_tail(self):
        lines_in = [f"line {i}" for i in range(PROGRESS_LOG_LINES + 5)]
        block = _log_window(lines_in)
        lines = _code_block_lines(block)
        self.assertEqual(len(lines), PROGRESS_LOG_LINES)
        self.assertEqual(lines[-1], f"line {PROGRESS_LOG_LINES + 4}")
        self.assertNotIn("line 0", lines)

    def test_clips_wide_lines(self):
        block = _log_window(["x" * 400])
        lines = _code_block_lines(block)
        self.assertEqual(len(lines), PROGRESS_LOG_LINES)
        self.assertLessEqual(len(lines[-1]), 81)
        self.assertTrue(lines[-1].endswith("…"))


class ProgressCardTest(unittest.TestCase):
    def test_plain_card_has_no_log_block(self):
        card = progress_card(status="执行中", prompt="do it")
        for element in card["elements"]:
            text = element.get("text") or {}
            self.assertNotIn("```", text.get("content") or "")

    def test_log_tail_renders_fixed_window(self):
        card = progress_card(
            status="执行中",
            prompt="do it",
            tool="shell",
            phase="执行命令",
            log_tail=["a", "b", "c"],
        )
        lines = _find_log_block(card)
        self.assertEqual(len(lines), PROGRESS_LOG_LINES)
        self.assertEqual(lines[-3:], ["a", "b", "c"])

    def test_log_full_renders_collapsible_panel(self):
        card = progress_card(
            status="执行中",
            prompt="do it",
            log_tail=["a"],
            log_full="a\nb\nc",
        )
        panels = [
            element
            for element in card["elements"]
            if element.get("tag") == "collapsible_panel"
        ]
        self.assertEqual(len(panels), 1)
        self.assertFalse(panels[0]["expanded"])
        self.assertIn("完整日志", panels[0]["header"]["title"]["content"])

    def test_header_title_survives_log_panel(self):
        card = progress_card(
            status="执行中",
            prompt="do it",
            log_full="a\nb\nc",
        )
        self.assertEqual(
            card["header"]["title"]["content"], "任务执行中"
        )

    def test_status_lines_are_stable(self):
        card = progress_card(
            status="执行中",
            prompt="do it",
            elapsed=12,
            usage={"tokens": {"input": 1, "output": 2}, "cost": 0.1},
        )
        status = next(
            element
            for element in card["elements"]
            if element.get("tag") == "div"
            and "**状态**" in (element.get("text") or {}).get("content", "")
        )
        content = status["text"]["content"]
        self.assertIn("**状态**: 执行中", content)
        self.assertIn("**已用时**: 12 秒", content)


if __name__ == "__main__":
    unittest.main()
