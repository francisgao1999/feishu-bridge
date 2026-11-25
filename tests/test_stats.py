from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "app"))

try:
    from stats import (  # noqa: E402
        STATS_TZ,
        build_stats_queries,
        fmt_int,
        fmt_tokens,
        model_label,
        stats_card,
        stats_help_text,
        tool_success_rate,
        total_tokens,
    )
except ImportError:  # pragma: no cover - app/ layout without flat imports
    from app.stats import (  # noqa: E402
        STATS_TZ,
        build_stats_queries,
        fmt_int,
        fmt_tokens,
        model_label,
        stats_card,
        stats_help_text,
        tool_success_rate,
        total_tokens,
    )


NOW = datetime(2026, 11, 25, 12, 30, tzinfo=STATS_TZ)


def _ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, tzinfo=STATS_TZ).timestamp() * 1000)


def _sample(range_from: int, range_to: int) -> dict:
    return {
        "range": {"from": range_from, "to": range_to},
        "sessions": 89,
        "subagents": 36,
        "prompts": 506,
        "steps": 8858,
        "tokens": {
            "input": 28001472,
            "output": 2746764,
            "reasoning": 2918929,
            "cache": {"read": 710683440, "write": 0},
        },
        "cost": 0,
        "tools": {
            "mode": "summary",
            "totals": {
                "calls": 9837,
                "succeeded": 9758,
                "failed": 78,
                "unfinished": 1,
            },
        },
        "activeDays": 13,
        "streak": 6,
        "activity": [],
        "models": [
            {
                "model": {
                    "id": "DeepSeek-V4.1-Flash-1017",
                    "providerID": "systalyze",
                },
                "steps": 6701,
                "tokens": {"input": 1000, "output": 500},
                "cost": 0,
            },
            {
                "model": {"id": "gemini-3.8-flash", "providerID": "aistudio"},
                "steps": 495,
                "tokens": {"input": 100, "output": 50},
                "cost": 0,
            },
            {
                "model": {"id": "big-pickle", "providerID": "opencode"},
                "steps": 40,
                "tokens": {"input": 10, "output": 5},
                "cost": 0,
            },
            {
                "model": {"id": "extra-model", "providerID": "other"},
                "steps": 1,
                "tokens": {"input": 1, "output": 1},
                "cost": 0,
            },
        ],
    }


class QueryTest(unittest.TestCase):
    def test_default_is_recent_14_days_plus_this_month(self):
        queries = build_stats_queries("", NOW)
        self.assertIsNotNone(queries)
        assert queries is not None
        self.assertEqual([item.label for item in queries], ["最近 14 天", "本月"])

        recent, month = queries
        self.assertEqual(recent.from_ms, _ms(2026, 11, 12))
        self.assertEqual(recent.to_ms, int(NOW.timestamp() * 1000))
        self.assertTrue(recent.show_models)
        self.assertEqual(month.from_ms, _ms(2026, 11, 1))
        self.assertFalse(month.show_models)

    def test_default_wraps_across_month(self):
        queries = build_stats_queries(
            "", datetime(2026, 11, 3, 9, 0, tzinfo=STATS_TZ)
        )
        assert queries is not None
        recent, month = queries
        self.assertEqual(recent.from_ms, _ms(2026, 10, 21))
        self.assertEqual(month.from_ms, _ms(2026, 11, 1))

    def test_days(self):
        for days in (1, 7, 14, 30):
            queries = build_stats_queries(str(days), NOW)
            assert queries is not None
            self.assertEqual(len(queries), 1)
            self.assertEqual(queries[0].label, f"最近 {days} 天")
            self.assertEqual(
                queries[0].from_ms, _ms(2026, 11, 25) - (days - 1) * 86400000
            )

    def test_today(self):
        queries = build_stats_queries("today", NOW)
        assert queries is not None
        self.assertEqual(queries[0].label, "今天")
        self.assertEqual(queries[0].from_ms, _ms(2026, 11, 25))

    def test_year(self):
        queries = build_stats_queries("year", NOW)
        assert queries is not None
        self.assertEqual(queries[0].label, "2026 年")
        self.assertEqual(queries[0].from_ms, _ms(2026, 1, 1))
        self.assertEqual(queries[0].to_ms, int(NOW.timestamp() * 1000))

    def test_explicit_year_is_whole_calendar_year(self):
        queries = build_stats_queries("2025", NOW)
        assert queries is not None
        self.assertEqual(queries[0].label, "2025 年")
        self.assertEqual(queries[0].from_ms, _ms(2025, 1, 1))
        self.assertEqual(queries[0].to_ms, _ms(2026, 1, 1))

    def test_all(self):
        queries = build_stats_queries("all", NOW)
        assert queries is not None
        self.assertEqual(queries[0].label, "全部")
        self.assertIsNone(queries[0].from_ms)
        self.assertIsNone(queries[0].to_ms)

    def test_unknown_argument(self):
        self.assertIsNone(build_stats_queries("nonsense", NOW))
        self.assertIsNone(build_stats_queries("0", NOW))


class FormatTest(unittest.TestCase):
    def test_fmt_tokens(self):
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1_500), "1.5k")
        self.assertEqual(fmt_tokens(2_000_000), "2m")
        self.assertEqual(fmt_tokens(1_375_183_095), "1.4b")

    def test_fmt_int(self):
        self.assertEqual(fmt_int(17228), "17,228")

    def test_total_tokens_includes_cache(self):
        tokens = {
            "input": 1,
            "output": 2,
            "reasoning": 3,
            "cache": {"read": 4, "write": 5},
        }
        self.assertEqual(total_tokens(tokens), 15)

    def test_tool_success_rate(self):
        tools = {"totals": {"calls": 1000, "succeeded": 991}}
        self.assertEqual(tool_success_rate(tools), "99.1%")
        self.assertIsNone(tool_success_rate({"totals": {"calls": 0}}))
        self.assertIsNone(tool_success_rate(None))

    def test_model_label(self):
        self.assertEqual(
            model_label({"providerID": "p", "id": "m"}), "p/m"
        )
        self.assertEqual(model_label({}), "-")


class CardTest(unittest.TestCase):
    def test_card_sections_and_top_models(self):
        queries = build_stats_queries("", NOW)
        assert queries is not None
        data = _sample(queries[0].from_ms or 0, queries[0].to_ms or 0)
        card = stats_card(
            queries=queries,
            results=[data, data],
            note="时区 Asia/Shanghai",
        )
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(len(card["elements"]), 4)  # block, hr, block, note
        text = card["elements"][0]["text"]["content"]
        self.assertIn("**最近 14 天**", text)
        self.assertIn("89 会话", text)
        self.assertIn("工具成功率 99.2%", text)
        self.assertIn("**模型前三**", text)
        self.assertIn("systalyze/DeepSeek-V4.1-Flash-1017", text)
        self.assertNotIn("extra-model", text)
        self.assertEqual(card["elements"][1], {"tag": "hr"})
        self.assertIn("本月", card["elements"][2]["text"]["content"])
        self.assertEqual(card["elements"][3]["tag"], "note")

    def test_help_mentions_modes(self):
        text = stats_help_text()
        self.assertIn("/stats year", text)
        self.assertIn("/schedule daily 22:00 stats", text)


if __name__ == "__main__":
    unittest.main()
