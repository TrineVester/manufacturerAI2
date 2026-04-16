"""Tests for src.agent.messages — serialization, sanitization, and pruning."""

from __future__ import annotations

import unittest

from src.agent.messages import (
    serialize_content,
    sanitize_messages,
    prune_messages,
    _strip_old_design_context,
    _DESIGN_CONTEXT_PREFIX,
)


# ── Helpers ────────────────────────────────────────────────────────

def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _user_blocks(*blocks: dict) -> dict:
    return {"role": "user", "content": list(blocks)}


def _text(t: str) -> dict:
    return {"type": "text", "text": t}


def _design_ctx(json_body: str = '{"shape": null}') -> dict:
    return _text(f"{_DESIGN_CONTEXT_PREFIX}\nCurrent design document:\n```json\n{json_body}\n```")


def _assistant_text(t: str) -> dict:
    return {"role": "assistant", "content": [_text(t)]}


def _tool_use(tool_id: str, name: str, inp: dict | None = None) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp or {}}


def _tool_result(tool_id: str, content: str, is_error: bool = False) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": is_error}


def _assistant_tool(tool_id: str, name: str, inp: dict | None = None) -> dict:
    return {"role": "assistant", "content": [_tool_use(tool_id, name, inp)]}


# ── serialize_content ─────────────────────────────────────────────

class TestSerializeContent(unittest.TestCase):

    def test_strips_extra_fields(self):
        blocks = [{"type": "text", "text": "hello", "citations": [], "extra": True}]
        result = serialize_content(blocks)
        self.assertEqual(result, [{"type": "text", "text": "hello"}])

    def test_preserves_tool_use_fields(self):
        blocks = [{"type": "tool_use", "id": "t1", "name": "edit_design", "input": {"x": 1}, "parsed_output": None}]
        result = serialize_content(blocks)
        self.assertEqual(result, [{"type": "tool_use", "id": "t1", "name": "edit_design", "input": {"x": 1}}])

    def test_thinking_block(self):
        blocks = [{"type": "thinking", "thinking": "hmm", "signature": "sig", "redacted": False}]
        result = serialize_content(blocks)
        self.assertEqual(result, [{"type": "thinking", "thinking": "hmm", "signature": "sig"}])

    def test_model_dump_objects(self):
        class FakeBlock:
            def model_dump(self):
                return {"type": "text", "text": "from model", "extra_field": True}
        result = serialize_content([FakeBlock()])
        self.assertEqual(result, [{"type": "text", "text": "from model"}])

    def test_unknown_type_passed_through(self):
        blocks = [{"type": "unknown", "data": "something"}]
        result = serialize_content(blocks)
        self.assertEqual(result, [{"type": "unknown", "data": "something"}])


# ── sanitize_messages ─────────────────────────────────────────────

class TestSanitizeMessages(unittest.TestCase):

    def test_string_content_unchanged(self):
        msgs = [_user("hello"), _assistant_text("hi")]
        result = sanitize_messages(msgs)
        self.assertEqual(result[0]["content"], "hello")

    def test_strips_extra_fields_from_blocks(self):
        msgs = [{"role": "assistant", "content": [
            {"type": "text", "text": "ok", "extra": True}
        ]}]
        result = sanitize_messages(msgs)
        self.assertNotIn("extra", result[0]["content"][0])

    def test_repairs_orphaned_tool_use_with_following_user(self):
        msgs = [
            {"role": "assistant", "content": [_tool_use("t1", "edit_design")]},
            _user("next prompt"),
        ]
        result = sanitize_messages(msgs)
        user_content = result[1]["content"]
        self.assertIsInstance(user_content, list)
        stub = user_content[0]
        self.assertEqual(stub["type"], "tool_result")
        self.assertEqual(stub["tool_use_id"], "t1")

    def test_repairs_orphaned_tool_use_no_following_message(self):
        msgs = [
            {"role": "assistant", "content": [_tool_use("t1", "edit_design")]},
        ]
        result = sanitize_messages(msgs)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["role"], "user")
        self.assertEqual(result[1]["content"][0]["tool_use_id"], "t1")

    def test_answered_tool_use_not_repaired(self):
        msgs = [
            {"role": "assistant", "content": [_tool_use("t1", "edit_design")]},
            _user_blocks(_tool_result("t1", "ok")),
        ]
        result = sanitize_messages(msgs)
        self.assertEqual(len(result[1]["content"]), 1)


# ── _strip_old_design_context ─────────────────────────────────────

class TestStripOldDesignContext(unittest.TestCase):

    def test_no_design_context_returns_unchanged(self):
        msgs = [_user("hello"), _assistant_text("hi")]
        result = _strip_old_design_context(msgs)
        self.assertEqual(result, msgs)

    def test_single_design_context_kept(self):
        msgs = [_user_blocks(_design_ctx(), _text("prompt"))]
        result = _strip_old_design_context(msgs)
        self.assertEqual(len(result), 1)
        texts = [b["text"] for b in result[0]["content"] if b["type"] == "text"]
        self.assertTrue(any(t.startswith(_DESIGN_CONTEXT_PREFIX) for t in texts))

    def test_only_last_design_context_kept(self):
        msgs = [
            _user_blocks(_design_ctx('{"v":1}'), _text("first prompt")),
            _assistant_text("reply 1"),
            _user_blocks(_design_ctx('{"v":2}'), _text("second prompt")),
            _assistant_text("reply 2"),
            _user_blocks(_design_ctx('{"v":3}'), _text("third prompt")),
        ]
        result = _strip_old_design_context(msgs)
        ctx_count = 0
        for msg in result:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if b.get("type") == "text" and b.get("text", "").startswith(_DESIGN_CONTEXT_PREFIX):
                        ctx_count += 1
                        self.assertIn('"v":3', b["text"])
        self.assertEqual(ctx_count, 1)

    def test_user_prompt_preserved_when_context_stripped(self):
        msgs = [
            _user_blocks(_design_ctx(), _text("my question")),
            _assistant_text("answer"),
            _user_blocks(_design_ctx(), _text("follow up")),
        ]
        result = _strip_old_design_context(msgs)
        first_user = result[0]
        texts = [b["text"] for b in first_user["content"] if b["type"] == "text"]
        self.assertEqual(texts, ["my question"])

    def test_assistant_messages_not_touched(self):
        msgs = [
            _user_blocks(_design_ctx(), _text("q")),
            _assistant_text("important answer"),
            _user_blocks(_design_ctx(), _text("q2")),
        ]
        result = _strip_old_design_context(msgs)
        assistant = [m for m in result if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["content"][0]["text"], "important answer")

    def test_string_content_not_touched(self):
        msgs = [
            _user("plain string prompt"),
            _assistant_text("reply"),
            _user_blocks(_design_ctx(), _text("second")),
        ]
        result = _strip_old_design_context(msgs)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["content"], "plain string prompt")

    def test_tool_results_in_user_message_preserved(self):
        msgs = [
            _user_blocks(
                _design_ctx(),
                _tool_result("t1", "tool output"),
                _text("prompt"),
            ),
            _assistant_text("reply"),
            _user_blocks(_design_ctx(), _text("last")),
        ]
        result = _strip_old_design_context(msgs)
        first_user = result[0]
        types = [b["type"] for b in first_user["content"]]
        self.assertIn("tool_result", types)
        self.assertIn("text", types)
        self.assertEqual(len([t for t in types if t == "text"]), 1)

    def test_does_not_mutate_input(self):
        msgs = [
            _user_blocks(_design_ctx(), _text("q1")),
            _assistant_text("a"),
            _user_blocks(_design_ctx(), _text("q2")),
        ]
        original_len = len(msgs[0]["content"])
        _strip_old_design_context(msgs)
        self.assertEqual(len(msgs[0]["content"]), original_len)


# ── prune_messages ────────────────────────────────────────────────

class TestPruneMessages(unittest.TestCase):

    def _make_conversation(self, n_turns: int) -> list[dict]:
        """Build a conversation with n user/assistant pairs.

        Each user turn has a design-context block + prompt.
        Each assistant turn has a thinking block, text, and a
        get_component tool call. The tool result is merged into
        the next user message (matching real conversation structure).
        """
        msgs: list[dict] = []
        pending_tool_result: dict | None = None
        for i in range(n_turns):
            blocks = []
            if pending_tool_result:
                blocks.append(pending_tool_result)
                pending_tool_result = None
            blocks.extend([
                _design_ctx(f'{{"turn": {i}}}'),
                _text(f"prompt {i}"),
            ])
            msgs.append(_user_blocks(*blocks))

            tool_id = f"tool_{i}"
            msgs.append({"role": "assistant", "content": [
                {"type": "thinking", "thinking": f"thinking {i}", "signature": "sig"},
                _text(f"response {i}"),
                _tool_use(tool_id, "get_component", {"component_id": "led_5mm"}),
            ]})
            pending_tool_result = _tool_result(tool_id, f"component data {i}")

        if pending_tool_result:
            msgs.append(_user_blocks(pending_tool_result))
        return msgs

    def test_short_conversation_strips_design_context_only(self):
        msgs = self._make_conversation(3)
        result = prune_messages(msgs, keep_recent_turns=6)
        ctx_count = sum(
            1 for m in result if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(
                b.get("type") == "text"
                and b.get("text", "").startswith(_DESIGN_CONTEXT_PREFIX)
                for b in m["content"]
            )
        )
        self.assertEqual(ctx_count, 1)

    def test_long_conversation_prunes_old_tool_results(self):
        msgs = self._make_conversation(10)
        result = prune_messages(msgs, keep_recent_turns=2)
        pruned_results = [
            b for m in result if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "tool_result" and b.get("content") == "[pruned]"
        ]
        self.assertGreater(len(pruned_results), 0)

    def test_recent_tool_results_not_pruned(self):
        msgs = self._make_conversation(10)
        result = prune_messages(msgs, keep_recent_turns=2)
        last_user_tool = None
        for m in reversed(result):
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if b.get("type") == "tool_result":
                        last_user_tool = b
                        break
                if last_user_tool:
                    break
        self.assertIsNotNone(last_user_tool)
        self.assertNotEqual(last_user_tool["content"], "[pruned]")

    def test_design_context_stripped_even_when_no_tools_to_prune(self):
        msgs = [
            _user_blocks(_design_ctx('{"v":1}'), _text("q1")),
            _assistant_text("a1"),
            _user_blocks(_design_ctx('{"v":2}'), _text("q2")),
            _assistant_text("a2"),
        ]
        result = prune_messages(msgs, keep_recent_turns=6)
        ctx_count = sum(
            1 for m in result if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            and any(
                b.get("type") == "text"
                and b.get("text", "").startswith(_DESIGN_CONTEXT_PREFIX)
                for b in m["content"]
            )
        )
        self.assertEqual(ctx_count, 1)

    def test_user_prompts_always_preserved(self):
        msgs = self._make_conversation(10)
        result = prune_messages(msgs, keep_recent_turns=2)
        user_prompts = [
            b["text"] for m in result if m.get("role") == "user"
            and isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "text" and not b["text"].startswith(_DESIGN_CONTEXT_PREFIX)
        ]
        expected = [f"prompt {i}" for i in range(10)]
        self.assertEqual(user_prompts, expected)

    def test_assistant_text_always_preserved(self):
        msgs = self._make_conversation(10)
        result = prune_messages(msgs, keep_recent_turns=2)
        assistant_texts = [
            b["text"] for m in result if m.get("role") == "assistant"
            and isinstance(m.get("content"), list)
            for b in m["content"]
            if b.get("type") == "text"
        ]
        expected = [f"response {i}" for i in range(10)]
        self.assertEqual(assistant_texts, expected)

    def test_edit_design_results_stubbed(self):
        msgs = [
            _user_blocks(_design_ctx(), _text("make it bigger")),
            {"role": "assistant", "content": [
                _tool_use("ed1", "edit_design", {"old_string": "x", "new_string": "y"}),
            ]},
            _user_blocks(_tool_result("ed1", "Design saved and validated successfully.\n\n...")),
            _assistant_text("done"),
        ]
        for _ in range(8):
            msgs.append(_user_blocks(_text("padding")))
            msgs.append(_assistant_text("ok"))

        result = prune_messages(msgs, keep_recent_turns=2)
        for m in result:
            if m.get("role") != "user" or not isinstance(m.get("content"), list):
                continue
            for b in m["content"]:
                if b.get("type") == "tool_result" and b.get("tool_use_id") == "ed1":
                    self.assertEqual(b["content"], "[design updated]")

    def test_does_not_mutate_input(self):
        msgs = self._make_conversation(10)
        original_first_user_content = list(msgs[0]["content"])
        prune_messages(msgs, keep_recent_turns=2)
        self.assertEqual(msgs[0]["content"], original_first_user_content)

    def test_role_alternation_maintained(self):
        msgs = self._make_conversation(10)
        result = prune_messages(msgs, keep_recent_turns=2)
        for i in range(1, len(result)):
            self.assertNotEqual(
                result[i]["role"], result[i - 1]["role"],
                f"Adjacent messages at {i-1} and {i} both have role '{result[i]['role']}'"
            )

    def test_empty_conversation(self):
        result = prune_messages([])
        self.assertEqual(result, [])

    def test_single_turn(self):
        msgs = [_user("hello"), _assistant_text("hi")]
        result = prune_messages(msgs)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
