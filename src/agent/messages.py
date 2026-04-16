"""Message helpers — serialization, sanitization, and pruning."""

from __future__ import annotations


# Fields the API accepts for each content block type
_ALLOWED_FIELDS = {
    "thinking": {"type", "thinking", "signature"},
    "text":     {"type", "text"},
    "tool_use": {"type", "id", "name", "input"},
    "tool_result": {"type", "tool_use_id", "content", "is_error"},
}

# Lookup tools whose results are safe to prune from old turns
_LOOKUP_TOOLS = {"list_components", "get_component"}

# Design tools whose old results are replaced with a short stub
_DESIGN_TOOLS = {"edit_design"}

# Submit tools whose results must always be kept verbatim
_KEEP_VERBATIM = {"submit_circuit"}


def strip_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Return a copy of messages with all thinking blocks removed."""
    result = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            filtered = [b for b in content if b.get("type") != "thinking"]
            if filtered:
                result.append({**msg, "content": filtered})
        else:
            result.append(msg)
    return result


def serialize_content(content: list) -> list[dict]:
    """Convert API response content blocks to serializable dicts.

    The Anthropic SDK returns pydantic model instances with extra fields
    (parsed_output, citations, caller, etc.) that the API rejects on
    re-submission.  We whitelist only the fields the API accepts per
    block type.
    """
    result = []
    for block in content:
        if hasattr(block, "model_dump"):
            d = block.model_dump()
        elif isinstance(block, dict):
            d = block
        else:
            d = {"type": "text", "text": str(block)}

        allowed = _ALLOWED_FIELDS.get(d.get("type"), set())
        if allowed:
            d = {k: v for k, v in d.items() if k in allowed}
        result.append(d)
    return result


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Clean a saved conversation so every content block only contains
    fields the Anthropic API accepts, and repair orphaned tool_use blocks.

    If the conversation was interrupted (cancel, max_tokens, crash) while
    the assistant had pending tool_use blocks, the saved history will end
    with an assistant message containing tool_use ids that have no matching
    tool_result in the next user message.  The Anthropic API rejects this.

    We detect and fix this by scanning for any assistant tool_use ids that
    are not answered by a tool_result in the immediately following user
    message, and appending synthetic tool_result blocks.
    """
    clean = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg = {**msg, "content": serialize_content(content)}
        clean.append(msg)

    _repair_orphaned_tool_uses(clean)
    return clean


def _repair_orphaned_tool_uses(messages: list[dict]) -> None:
    """Mutate *messages* in-place: for every assistant tool_use id that
    lacks a matching tool_result in the next message, append (or extend)
    a user message with synthetic tool_result blocks."""
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            i += 1
            continue

        tool_use_ids = [
            b["id"]
            for b in msg["content"]
            if b.get("type") == "tool_use" and "id" in b
        ]
        if not tool_use_ids:
            i += 1
            continue

        answered_ids: set[str] = set()
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        if (
            next_msg
            and next_msg.get("role") == "user"
            and isinstance(next_msg.get("content"), list)
        ):
            for b in next_msg["content"]:
                if b.get("type") == "tool_result" and "tool_use_id" in b:
                    answered_ids.add(b["tool_use_id"])

        missing_ids = [tid for tid in tool_use_ids if tid not in answered_ids]
        if not missing_ids:
            i += 1
            continue

        stub_results = [
            {
                "type": "tool_result",
                "tool_use_id": tid,
                "content": "[interrupted — tool was not executed]",
            }
            for tid in missing_ids
        ]

        if next_msg and next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
            next_msg["content"] = stub_results + next_msg["content"]
        elif next_msg and next_msg.get("role") == "user":
            messages[i + 1] = {
                "role": "user",
                "content": stub_results + [{"type": "text", "text": next_msg["content"]}],
            }
        else:
            messages.insert(i + 1, {"role": "user", "content": stub_results})

        i += 2


def prune_messages(messages: list[dict], keep_recent_turns: int = 6) -> list[dict]:
    """Shrink the context sent to the API by replacing old informational
    tool results with a stub, without touching the saved history on disk.

    For assistant turns older than `keep_recent_turns`:
    - list_components / get_component tool_result content is replaced
      with "[pruned]" (the pairing id is preserved so the API stays happy)
    - submit_circuit tool calls + results are always kept
    - All user text prompts and assistant text / thinking blocks are kept
    """
    assistant_indices = [i for i, m in enumerate(messages) if m["role"] == "assistant"]

    if len(assistant_indices) <= keep_recent_turns:
        return _strip_old_design_context(messages)

    cutoff_msg_index = assistant_indices[-keep_recent_turns]

    # Collect tool_use ids for prunable tools in OLD turns only
    prunable_ids: set[str] = set()
    design_prunable_ids: set[str] = set()
    for msg in messages[:cutoff_msg_index]:
        if msg["role"] == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if block.get("type") == "tool_use" and "id" in block:
                    if block.get("name") in _LOOKUP_TOOLS:
                        prunable_ids.add(block["id"])
                    elif block.get("name") in _DESIGN_TOOLS:
                        design_prunable_ids.add(block["id"])

    if not prunable_ids and not design_prunable_ids:
        return _strip_old_design_context(messages)

    result = []
    for idx, msg in enumerate(messages):
        if idx >= cutoff_msg_index:
            result.append(msg)
            continue

        content = msg.get("content")
        if msg["role"] == "user" and isinstance(content, list):
            new_content = []
            for b in content:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in prunable_ids:
                    new_content.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "content": "[pruned]"})
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in design_prunable_ids:
                    new_content.append({"type": "tool_result", "tool_use_id": b["tool_use_id"], "content": "[design updated]"})
                else:
                    new_content.append(b)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)

    result = _strip_old_design_context(result)
    return result


_DESIGN_CONTEXT_PREFIX = "<!-- design-context -->"


def _strip_old_design_context(messages: list[dict]) -> list[dict]:
    """Remove design-context blocks from all user messages except the last one.

    The full design JSON is injected into every user turn, but the API only
    needs the most recent copy.  Older copies waste tokens without adding
    information — the latest version already reflects all prior edits.
    """
    last_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(_DESIGN_CONTEXT_PREFIX):
            last_idx = idx
            break
        if isinstance(content, list):
            if any(
                b.get("type") == "text"
                and isinstance(b.get("text"), str)
                and b["text"].startswith(_DESIGN_CONTEXT_PREFIX)
                for b in content
            ):
                last_idx = idx
                break

    if last_idx < 0:
        return messages

    result = []
    for idx, msg in enumerate(messages):
        if idx == last_idx or msg.get("role") != "user":
            result.append(msg)
            continue

        content = msg.get("content")
        if isinstance(content, list):
            filtered = [
                b for b in content
                if not (
                    b.get("type") == "text"
                    and isinstance(b.get("text"), str)
                    and b["text"].startswith(_DESIGN_CONTEXT_PREFIX)
                )
            ]
            result.append({**msg, "content": filtered} if filtered != content else msg)
        else:
            result.append(msg)

    return result
