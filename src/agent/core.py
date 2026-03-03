"""Design agent — LLM-driven device designer core loop."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import AsyncGenerator

import anthropic

from src.catalog import CatalogResult, _component_to_dict
from src.pipeline.design import DesignSpec, parse_design, validate_design, design_to_dict
from src.session import Session

from .config import MODEL, MAX_TOKENS, THINKING_BUDGET, MAX_TURNS, TOKEN_BUDGET
from .tools import TOOLS
from .prompt import _build_system_prompt, _catalog_summary
from .messages import _serialize_content, _sanitize_messages, _prune_messages


# ── Agent events ───────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """Event yielded during agent execution, streamed to the UI."""
    type: str       # thinking | message | tool_call | tool_result | design | error | done
    data: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


# ── Design agent ───────────────────────────────────────────────────

class DesignAgent:
    """
    LLM-driven device designer.

    Uses Claude Sonnet 4.6 with extended thinking and the streaming API.
    Yields token-level deltas for thinking and text blocks so the UI
    updates in real time.

    The conversation loop follows the SeedGPT pattern:
      messages → streaming API call → yield deltas → accumulate
      content blocks → dispatch tool calls → repeat
    """

    def __init__(self, catalog: CatalogResult, session: Session):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.catalog = catalog
        self.session = session
        self.design: DesignSpec | None = None

        # Load existing conversation from session (for multi-turn)
        saved = session.read_artifact("conversation.json")
        self.messages: list[dict] = _sanitize_messages(saved) if isinstance(saved, list) else []
        self._feasibility_attempts: int = 0   # reset each run() call

    def _save_conversation(self) -> None:
        """Persist the full message history to the session folder."""
        self.session.write_artifact("conversation.json", self.messages)

    async def run(self, user_prompt: str) -> AsyncGenerator[AgentEvent, None]:
        """
        Run the agent loop. Yields events for streaming to the UI.

        Event types with streaming deltas:
          thinking_start  — new thinking block begins
          thinking_delta  — incremental thinking text
          message_start   — new text block begins
          message_delta   — incremental text
          block_stop      — current block complete
          tool_call       — tool invocation (after stream completes)
          tool_result     — tool result
          design          — validated design spec
          error           — error message
          done            — agent finished
        """
        system = _build_system_prompt(self.catalog)
        self.messages.append({"role": "user", "content": user_prompt})
        self._feasibility_attempts = 0

        for turn in range(MAX_TURNS):
            content_blocks: list[dict] = []
            stop_reason = None
            api_messages = _prune_messages(self.messages)

            try:
                async with self.client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    thinking={
                        "type": "enabled",
                        "budget_tokens": THINKING_BUDGET,
                    },
                    system=system,
                    tools=TOOLS,
                    messages=api_messages,
                ) as stream:
                    async for event in stream:
                        agent_event = self._handle_stream_event(event)
                        if agent_event:
                            yield agent_event

                    # After stream completes, get the full response
                    response = await stream.get_final_message()
                    content_blocks = _serialize_content(response.content)
                    stop_reason = response.stop_reason

            except anthropic.APIError as e:
                self._save_conversation()
                yield AgentEvent("error", {"message": f"API error: {e}"})
                return

            # ── Always append the assistant response to history ──
            self.messages.append({
                "role": "assistant",
                "content": content_blocks,
            })

            # ── Count conversation tokens (free API) ──
            try:
                token_count = await self.client.messages.count_tokens(
                    model=MODEL,
                    messages=api_messages,
                    system=system,
                    tools=TOOLS,
                    thinking={
                        "type": "enabled",
                        "budget_tokens": THINKING_BUDGET,
                    },
                )
                yield AgentEvent("token_usage", {
                    "input_tokens": token_count.input_tokens,
                    "budget": TOKEN_BUDGET,
                })
            except Exception:
                pass  # token counting is best-effort

            # ── Check stop reason ──
            if stop_reason == "max_tokens":
                self._save_conversation()
                yield AgentEvent("error", {
                    "message": "Response truncated — output too long"
                })
                return

            # ── Extract tool_use blocks ──
            tool_blocks = [
                b for b in content_blocks if b.get("type") == "tool_use"
            ]

            if not tool_blocks:
                self._save_conversation()
                yield AgentEvent("done", {})
                return

            # ── Handle each tool call ──
            tool_results: list[dict] = []
            design_submitted = False

            for block in tool_blocks:
                yield AgentEvent("tool_call", {
                    "name": block["name"],
                    "input": block["input"],
                })

                result_text, is_valid_design = self._handle_tool(
                    block["name"], block["input"]
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": result_text,
                })

                yield AgentEvent("tool_result", {
                    "name": block["name"],
                    "content": result_text,
                    "is_error": not is_valid_design and block["name"] == "submit_design",
                })

                if is_valid_design:
                    design_submitted = True

            # ── Append tool results as user message ──
            self.messages.append({"role": "user", "content": tool_results})

            # ── If valid design was submitted, we're done ──
            if design_submitted:
                self._save_conversation()
                yield AgentEvent("design", {
                    "design": design_to_dict(self.design),
                })
                yield AgentEvent("done", {})
                return

        self._save_conversation()
        yield AgentEvent("error", {
            "message": f"Agent exceeded maximum turns ({MAX_TURNS})"
        })

    # ── Stream event handler ───────────────────────────────────────

    def _handle_stream_event(self, event) -> AgentEvent | None:
        """Convert an Anthropic stream event to an AgentEvent (or None)."""
        etype = event.type

        if etype == "content_block_start":
            block = event.content_block
            if hasattr(block, "type"):
                if block.type == "thinking":
                    return AgentEvent("thinking_start", {})
                if block.type == "text":
                    return AgentEvent("message_start", {})
            return None

        if etype == "content_block_delta":
            delta = event.delta
            if hasattr(delta, "type"):
                if delta.type == "thinking_delta":
                    return AgentEvent("thinking_delta", {"text": delta.thinking})
                if delta.type == "text_delta":
                    return AgentEvent("message_delta", {"text": delta.text})
            return None

        if etype == "content_block_stop":
            return AgentEvent("block_stop", {})

        return None

    # ── Tool handlers ──────────────────────────────────────────────

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        """Dispatch a tool call. Returns (result_text, is_valid_design)."""
        if name == "list_components":
            return _catalog_summary(self.catalog), False

        if name == "get_component":
            return self._tool_get_component(input_data), False

        if name == "submit_design":
            return self._tool_submit_design(input_data)

        if name == "check_placement_feasibility":
            return self._tool_check_feasibility(input_data), False

        return f"Unknown tool: {name}", False

    def _tool_get_component(self, input_data: dict) -> str:
        component_id = input_data.get("component_id", "")
        for c in self.catalog.components:
            if c.id == component_id:
                return json.dumps(_component_to_dict(c), indent=2)
        available = [c.id for c in self.catalog.components]
        return (
            f"Component '{component_id}' not found. "
            f"Available: {', '.join(available)}"
        )

    _MAX_FEASIBILITY_ATTEMPTS = 3

    def _tool_check_feasibility(self, input_data: dict) -> str:
        from src.pipeline.placer.feasibility import run_feasibility_check
        self._feasibility_attempts += 1
        if self._feasibility_attempts > self._MAX_FEASIBILITY_ATTEMPTS:
            return (
                f"FEASIBILITY CHECK LIMIT REACHED ({self._MAX_FEASIBILITY_ATTEMPTS} attempts). "
                f"You have been unable to find a valid layout automatically. "
                f"Do NOT call check_placement_feasibility or submit_design again. "
                f"Instead, respond to the user explaining: which component(s) cannot "
                f"be placed, why (which UI components are blocking them), and what "
                f"the user should change (e.g. larger outline, fewer UI components, "
                f"different arrangement). Ask the user for guidance before retrying."
            )
        remaining = self._MAX_FEASIBILITY_ATTEMPTS - self._feasibility_attempts
        report = run_feasibility_check(
            self.catalog,
            input_data.get("components", []),
            input_data.get("outline", []),
            input_data.get("ui_placements", []),
            enclosure_raw=input_data.get("enclosure"),
        )
        if remaining == 0:
            report += (
                f"\n\nWARNING: This was your last allowed feasibility check. "
                f"If any component still shows [FAIL], do NOT call this tool again. "
                f"Either fix the issue and call submit_design directly, or stop "
                f"and explain the problem to the user."
            )
        else:
            report += f"\n\n({remaining} feasibility check(s) remaining before limit)"
        return report

    def _tool_submit_design(self, input_data: dict) -> tuple[str, bool]:
        """Parse, validate, and save a design. Returns (result, is_valid)."""
        try:
            spec = parse_design(input_data)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return f"Design parsing error: {e}", False

        errors = validate_design(spec, self.catalog)
        if errors:
            error_list = "\n".join(f"  - {e}" for e in errors)
            return f"Design validation failed:\n{error_list}", False

        # Valid! Save to session.
        self.design = spec
        self.session.write_artifact("design.json", input_data)
        self.session.pipeline_state["design"] = "complete"
        # Invalidate downstream: placement and routing depend on design
        for step in ("placement", "routing"):
            artifact = f"{step}.json"
            if self.session.has_artifact(artifact):
                self.session.delete_artifact(artifact)
            self.session.pipeline_state.pop(step, None)
        self.session.save()

        return "Design validated successfully! Saved to session.", True
