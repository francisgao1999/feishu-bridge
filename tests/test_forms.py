from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from app.forms import (  # noqa: E402
        answer_from_form_value,
        build_form_card,
        build_form_resolved_card,
        parse_answer,
        parse_fields,
    )
except ImportError:  # flattened workspace without app/ package
    from forms import (  # noqa: E402
        answer_from_form_value,
        build_form_card,
        build_form_resolved_card,
        parse_answer,
        parse_fields,
    )


def _field(**kwargs):
    base = {"key": "q0", "type": "string", "title": "问题"}
    base.update(kwargs)
    return base


class FormCardTest(unittest.TestCase):
    def test_form_card_uses_card_2_and_form_container(self):
        card = build_form_card(
            form_id="frm_test",
            title="Questions",
            fields=[_field(options=[{"value": "A", "label": "A"}])],
            prompt="方案A",
            session_id="ses_test",
            expire_seconds=1800,
        )
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["update_multi"])
        form = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "form"
        )
        self.assertEqual(form["name"], "form_frm_test")
        select = next(
            element
            for element in form["elements"]
            if element.get("tag") == "select_static"
        )
        self.assertEqual(select["name"], "q0")
        self.assertEqual(select["options"][0]["value"], "0")
        self.assertEqual(
            select["options"][0]["text"]["content"], "A"
        )
        submit = next(
            element
            for element in form["elements"]
            if element.get("tag") == "column_set"
        )["columns"][0]["elements"][0]
        self.assertEqual(submit["form_action_type"], "submit")
        self.assertEqual(submit["name"], "submit_frm_test")

    def test_form_card_renders_multiselect_and_custom_input(self):
        card = build_form_card(
            form_id="frm_test",
            title="Questions",
            fields=[
                _field(
                    type="multiselect",
                    options=[
                        {"value": "A", "label": "A"},
                        {"value": "B", "label": "B"},
                    ],
                    custom=True,
                )
            ],
        )
        form = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "form"
        )
        multi = next(
            element
            for element in form["elements"]
            if element.get("tag") == "multi_select_static"
        )
        self.assertEqual(multi["name"], "q0")
        custom = next(
            element
            for element in form["elements"]
            if element.get("tag") == "input"
            and element.get("name") == "q0__custom"
        )
        self.assertIn("其他答案", custom["placeholder"]["content"])

    def test_form_card_renders_boolean_and_number(self):
        card = build_form_card(
            form_id="frm_test",
            title="Questions",
            fields=[
                _field(key="q0", type="boolean"),
                _field(key="q1", type="integer"),
            ],
        )
        form = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "form"
        )
        boolean = next(
            element
            for element in form["elements"]
            if element.get("tag") == "select_static"
            and element.get("name") == "q0"
        )
        self.assertEqual(
            [option["value"] for option in boolean["options"]],
            ["true", "false"],
        )
        number = next(
            element
            for element in form["elements"]
            if element.get("tag") == "input"
            and element.get("name") == "q1"
        )
        self.assertIn("数字", number["placeholder"]["content"])

    def test_form_card_renders_option_descriptions(self):
        card = build_form_card(
            form_id="frm_test",
            title="Questions",
            fields=[
                _field(
                    options=[
                        {
                            "value": "A",
                            "label": "方案A",
                            "description": "改动小，风险低",
                        }
                    ]
                )
            ],
        )
        form = next(
            element
            for element in card["body"]["elements"]
            if element.get("tag") == "form"
        )
        content = "\n".join(
            element.get("content", "")
            for element in form["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("方案A", content)
        self.assertIn("改动小，风险低", content)

    def test_resolved_card_shows_answers(self):
        card = build_form_resolved_card(
            title="Questions",
            fields=[_field(title="选择方案")],
            status="replied",
            answer={"q0": "方案A"},
            prompt="方案A",
            session_id="ses_test",
        )
        self.assertEqual(card["schema"], "2.0")
        content = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("选择方案", content)
        self.assertIn("方案A", content)
        self.assertIn("OpenCode 将继续执行", content)

    def test_expired_card_says_task_interrupted(self):
        card = build_form_resolved_card(
            title="Questions",
            fields=[_field()],
            status="expired",
            message="提问超时",
            interrupted=True,
        )
        content = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("任务已中断", content)

    def test_expired_card_without_interrupt(self):
        card = build_form_resolved_card(
            title="Questions",
            fields=[_field()],
            status="expired",
            message="提问已不存在",
        )
        content = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("提问已过期", content)
        self.assertNotIn("任务已中断", content)

    def test_cancelled_card_without_interrupt(self):
        card = build_form_resolved_card(
            title="Questions",
            fields=[_field()],
            status="cancelled",
            message="会话已删除",
        )
        content = "\n".join(
            element.get("content", "")
            for element in card["body"]["elements"]
            if element.get("tag") == "markdown"
        )
        self.assertIn("提问已取消", content)
        self.assertNotIn("任务已中断", content)


class FormAnswerTest(unittest.TestCase):
    def test_string_select_maps_index_to_label(self):
        fields = [
            _field(
                options=[
                    {"value": "方案A", "label": "方案A"},
                    {"value": "方案B", "label": "方案B"},
                ]
            )
        ]
        answer, error = answer_from_form_value(fields, {"q0": "1"})
        self.assertIsNone(error)
        self.assertEqual(answer, {"q0": "方案B"})

    def test_custom_answer_overrides_select(self):
        fields = [
            _field(
                options=[{"value": "方案A", "label": "方案A"}],
                custom=True,
            )
        ]
        answer, error = answer_from_form_value(
            fields, {"q0": "0", "q0__custom": "方案C"}
        )
        self.assertIsNone(error)
        self.assertEqual(answer, {"q0": "方案C"})

    def test_multiselect_maps_indices_and_custom(self):
        fields = [
            _field(
                type="multiselect",
                options=[
                    {"value": "A", "label": "A"},
                    {"value": "B", "label": "B"},
                ],
                custom=True,
            )
        ]
        answer, error = answer_from_form_value(
            fields, {"q0": ["0", "1"], "q0__custom": "C，D"}
        )
        self.assertIsNone(error)
        self.assertEqual(answer, {"q0": ["A", "B", "C", "D"]})

    def test_number_and_integer(self):
        fields = [
            _field(key="q0", type="number"),
            _field(key="q1", type="integer"),
        ]
        answer, error = answer_from_form_value(
            fields, {"q0": "1.5", "q1": "2"}
        )
        self.assertIsNone(error)
        self.assertEqual(answer, {"q0": 1.5, "q1": 2})

    def test_integer_rejects_fraction(self):
        fields = [_field(key="q0", type="integer")]
        answer, error = answer_from_form_value(fields, {"q0": "1.5"})
        self.assertIsNone(answer)
        self.assertIn("整数", error)

    def test_number_rejects_non_finite(self):
        fields = [_field(key="q0", type="number")]
        answer, error = answer_from_form_value(fields, {"q0": "Infinity"})
        self.assertIsNone(answer)
        self.assertIn("有效数字", error)

    def test_required_missing(self):
        fields = [_field(required=True)]
        answer, error = answer_from_form_value(fields, {})
        self.assertIsNone(answer)
        self.assertIn("请回答", error)

    def test_optional_missing_is_skipped(self):
        fields = [_field(required=False)]
        answer, error = answer_from_form_value(fields, {})
        self.assertIsNone(error)
        self.assertEqual(answer, {})

    def test_boolean(self):
        fields = [_field(type="boolean")]
        answer, error = answer_from_form_value(fields, {"q0": "false"})
        self.assertIsNone(error)
        self.assertEqual(answer, {"q0": False})

    def test_parse_fields_and_answer(self):
        self.assertEqual(
            parse_fields('[{"key": "q0"}]'), [{"key": "q0"}]
        )
        self.assertEqual(parse_fields("nope"), [])
        self.assertEqual(parse_answer('{"q0": "A"}'), {"q0": "A"})
        self.assertEqual(parse_answer("nope"), {})


if __name__ == "__main__":
    unittest.main()
