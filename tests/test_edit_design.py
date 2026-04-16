"""Tests for DesignAgent._normalize_json_whitespace and edit_design fuzzy matching."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch, MagicMock

from src.agent.core import DesignAgent

norm = DesignAgent._normalize_json_whitespace

SAMPLE_DESIGN = {
    "shape": {
        "op": "union",
        "children": [
            {"type": "ellipse", "center": [65, 97], "radius": [9, 9],
             "end_center": [65, 10], "radius_end": 6},
            {"type": "rectangle", "center": [65, 107], "size": [44, 14],
             "corner_radius": 4}
        ]
    },
    "enclosure": {"height_mm": 10}
}


def _make_agent(design: dict | None = None) -> DesignAgent:
    """Create a DesignAgent with mocked dependencies for unit testing."""
    catalog = MagicMock()
    catalog.components = []
    session = MagicMock()
    session.read_artifact.return_value = design
    session.printer_id = "default"
    session.model_id = "medium"

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}), \
         patch("src.agent.core.anthropic"):
        agent = DesignAgent(catalog, session)

    if design is None:
        agent._design_text = json.dumps(SAMPLE_DESIGN, indent=2)
    return agent


class TestNormalizeJsonWhitespace(unittest.TestCase):

    def test_scalar_value_unchanged(self):
        assert norm('"height_mm": 10') == '"height_mm":10'

    def test_compact_and_pretty_arrays_match(self):
        pretty = '"center": [\n  65,\n  97\n]'
        compact = '"center": [65, 97]'
        self.assertEqual(norm(pretty), norm(compact))

    def test_nested_objects_match(self):
        pretty = '{\n  "type": "ellipse",\n  "center": [\n    65,\n    97\n  ]\n}'
        compact = '{"type": "ellipse", "center": [65, 97]}'
        self.assertEqual(norm(pretty), norm(compact))

    def test_string_values_preserved(self):
        self.assertIn("hello", norm('"key": "hello"'))


class TestEditDesignFuzzyMatch(unittest.TestCase):

    def setUp(self):
        self.pretty_doc = json.dumps(SAMPLE_DESIGN, indent=2)

    def test_simple_scalar_matches_directly(self):
        assert '"height_mm": 10' in self.pretty_doc

    def test_compact_array_does_not_match_pretty_directly(self):
        compact = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        assert compact not in self.pretty_doc

    def test_normalized_compact_matches_normalized_pretty(self):
        compact = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        assert norm(compact) in norm(self.pretty_doc)

    def test_replacement_produces_valid_json(self):
        old = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        new = '{"type": "rectangle", "center": [65, 55], "size": [16, 90], "corner_radius": 8}'

        norm_doc = norm(self.pretty_doc)
        result = norm_doc.replace(norm(old), norm(new), 1)
        parsed = json.loads(result)

        self.assertEqual(parsed["shape"]["children"][0]["type"], "rectangle")
        self.assertEqual(parsed["shape"]["children"][0]["center"], [65, 55])

    def test_replacement_preserves_untouched_fields(self):
        old = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        new = '{"type": "rectangle", "center": [65, 55], "size": [16, 90], "corner_radius": 8}'

        norm_doc = norm(self.pretty_doc)
        result = norm_doc.replace(norm(old), norm(new), 1)
        parsed = json.loads(result)

        self.assertEqual(parsed["enclosure"]["height_mm"], 10)

    def test_repretty_roundtrips_cleanly(self):
        old = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        new = '{"type": "rectangle", "center": [65, 55], "size": [16, 90], "corner_radius": 8}'

        norm_doc = norm(self.pretty_doc)
        result = norm_doc.replace(norm(old), norm(new), 1)
        parsed = json.loads(result)
        repretty = json.dumps(parsed, indent=2)
        reparsed = json.loads(repretty)

        self.assertEqual(parsed, reparsed)


class TestToolEditDesign(unittest.TestCase):
    """Integration tests for DesignAgent._tool_edit_design."""

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_exact_match_scalar_edit(self, _mock_save):
        agent = _make_agent()
        result = agent._tool_edit_design({
            "old_string": '"height_mm": 10',
            "new_string": '"height_mm": 15',
        })
        self.assertEqual(result, "OK")
        design = json.loads(agent._design_text)
        self.assertEqual(design["enclosure"]["height_mm"], 15)

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_fuzzy_match_compact_vs_pretty(self, _mock_save):
        agent = _make_agent()
        compact_old = '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}'
        assert compact_old not in agent._design_text, "precondition: compact should not match pretty directly"

        result = agent._tool_edit_design({
            "old_string": compact_old,
            "new_string": '{"type": "rectangle", "center": [65, 55], "size": [16, 90], "corner_radius": 8}',
        })
        self.assertEqual(result, "OK")
        design = json.loads(agent._design_text)
        self.assertEqual(design["shape"]["children"][0]["type"], "rectangle")
        self.assertEqual(design["shape"]["children"][0]["center"], [65, 55])

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_fuzzy_match_preserves_untouched_fields(self, _mock_save):
        agent = _make_agent()
        agent._tool_edit_design({
            "old_string": '{"type": "ellipse", "center": [65, 97], "radius": [9, 9], "end_center": [65, 10], "radius_end": 6}',
            "new_string": '{"type": "circle", "center": [30, 30], "radius": [5, 5]}',
        })
        design = json.loads(agent._design_text)
        self.assertEqual(design["enclosure"]["height_mm"], 10)
        self.assertEqual(design["shape"]["children"][1]["type"], "rectangle")

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_result_always_pretty_printed(self, _mock_save):
        agent = _make_agent()
        agent._tool_edit_design({
            "old_string": '"height_mm": 10',
            "new_string": '"height_mm": 20',
        })
        self.assertIn("\n", agent._design_text)
        self.assertIn("  ", agent._design_text)
        json.loads(agent._design_text)  # must be valid JSON

    def test_not_found_returns_error(self):
        agent = _make_agent()
        result = agent._tool_edit_design({
            "old_string": '"nonexistent_key": 999',
            "new_string": '"something": 1',
        })
        self.assertIn("Error", result)
        self.assertIn("not found", result)

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_multiple_exact_matches_returns_error(self, _mock_save):
        design = {"items": [{"val": 1}, {"val": 1}]}
        agent = _make_agent(design)
        result = agent._tool_edit_design({
            "old_string": '"val": 1',
            "new_string": '"val": 2',
        })
        self.assertIn("Error", result)
        self.assertIn("matches 2 locations", result)

    @patch.object(DesignAgent, "_save_and_validate", return_value="OK")
    def test_design_text_re_serialized_after_edit(self, _mock_save):
        agent = _make_agent()
        agent._tool_edit_design({
            "old_string": '"height_mm": 10',
            "new_string": '"height_mm": 25',
        })
        reparsed = json.loads(agent._design_text)
        re_serialized = json.dumps(reparsed, indent=2)
        self.assertEqual(agent._design_text, re_serialized)


if __name__ == "__main__":
    unittest.main()
