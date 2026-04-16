"""Agent core — shared loop, DesignAgent, and CircuitAgent."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import AsyncGenerator

import anthropic

from src.catalog import CatalogResult, _component_to_dict, component_to_design_dict
from src.pipeline.config import get_printer
from src.pipeline.design import (
    validate_design,
    parse_physical_design, validate_physical_design,
    parse_circuit, build_design_spec,
)
from src.pipeline.design.shape2d import validate_shape
from src.pipeline.circuit import validate_circuit
from src.session import Session

from .config import MODEL, MAX_TOKENS, MAX_TURNS, TOKEN_BUDGET, get_model
from .tools import DESIGN_TOOLS, CIRCUIT_TOOLS
from .prompt import build_design_prompt, build_circuit_prompt, build_circuit_user_prompt, catalog_summary
from .messages import serialize_content, sanitize_messages, prune_messages, strip_thinking_blocks

EMPTY_DESIGN: dict = {
    "device_description": "",
    "name": "",
    "shape": None,
    "enclosure": None,
    "ui_placements": [],
}


# ── Agent events ───────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """Event yielded during agent execution, streamed to the UI."""
    type: str
    data: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


# ── Base agent ─────────────────────────────────────────────────────

class _BaseAgent:
    """Shared agent loop for both design and circuit agents.

    Subclasses provide tools, system prompt, and tool handlers.
    The conversation loop, streaming, and persistence are identical.
    """

    conversation_file: str = ""  # subclasses must override

    def __init__(self, catalog: CatalogResult, session: Session):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY environment variable is required")
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.catalog = catalog
        self.session = session
        self._last_invalidated: list[str] = []
        self._design_feedback: str | None = None

        mdef = get_model(session.model_id)
        self._api_model = mdef.api_model
        self._supports_thinking = mdef.supports_thinking

        saved = session.read_artifact(self.conversation_file)
        self.messages: list[dict] = sanitize_messages(saved) if isinstance(saved, list) else []

    def _save_conversation(self) -> None:
        self.session.write_artifact(self.conversation_file, self.messages)

    def _get_tools(self) -> list[dict]:
        raise NotImplementedError

    def _get_system_prompt(self) -> str:
        raise NotImplementedError

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool] | str:
        """Dispatch a tool call. Returns either:
        - (result_text, is_terminal) for terminal-capable tools
        - result_text for non-terminal tools
        """
        raise NotImplementedError

    def _build_user_message(self, user_prompt: str) -> str | list:
        """Build the user message content. Subclasses may prepend context."""
        return user_prompt

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        """Return the event to emit when a terminal tool succeeds, or None."""
        return None

    async def run(
        self,
        user_prompt: str,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Run the agent loop. Yields events for streaming to the UI.

        If *cancel_event* is set, the loop will stop at the next safe point.
        Messages are saved incrementally after each turn so that clients
        can reconnect and pick up where they left off.
        """
        system = self._get_system_prompt()
        tools = self._get_tools()
        self.messages.append({"role": "user", "content": self._build_user_message(user_prompt)})
        self._save_conversation()
        yield AgentEvent("checkpoint", {})

        _DESIGN_TOOLS = {"edit_design"}

        for turn in range(MAX_TURNS):
            if cancel_event and cancel_event.is_set():
                self._save_conversation()
                yield AgentEvent("error", {"message": "Cancelled"})
                return

            system = self._get_system_prompt()

            content_blocks: list[dict] = []
            stop_reason = None
            api_messages = prune_messages(self.messages)
            if not self._supports_thinking:
                api_messages = strip_thinking_blocks(api_messages)

            try:
                stream_kwargs = {
                    "model": self._api_model,
                    "max_tokens": MAX_TOKENS,
                    "system": system,
                    "tools": tools,
                    "messages": api_messages,
                }
                if self._supports_thinking:
                    stream_kwargs["thinking"] = {"type": "adaptive"}

                async with self.client.messages.stream(**stream_kwargs) as stream:
                    async for event in stream:
                        if cancel_event and cancel_event.is_set():
                            await stream.close()
                            self._save_conversation()
                            yield AgentEvent("error", {"message": "Cancelled"})
                            return
                        agent_event = self._handle_stream_event(event)
                        if agent_event:
                            yield agent_event

                    response = await stream.get_final_message()
                    content_blocks = serialize_content(response.content)
                    stop_reason = response.stop_reason

            except anthropic.APIError as e:
                self._save_conversation()
                yield AgentEvent("error", {"message": f"API error: {e}"})
                return

            self.messages.append({
                "role": "assistant",
                "content": content_blocks,
            })

            # Token counting (best-effort)
            try:
                token_count = await self.client.messages.count_tokens(
                    model=self._api_model,
                    messages=api_messages,
                    system=system,
                    tools=tools,
                )
                yield AgentEvent("token_usage", {
                    "input_tokens": token_count.input_tokens,
                    "budget": TOKEN_BUDGET,
                })
            except Exception:
                pass

            tool_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

            if stop_reason == "max_tokens":
                if tool_blocks:
                    self.messages[-1] = {
                        "role": "assistant",
                        "content": [b for b in content_blocks
                                    if b.get("type") != "tool_use"],
                    }
                self._save_conversation()
                yield AgentEvent("checkpoint", {})
                yield AgentEvent("error", {
                    "message": "Response truncated — output too long",
                })
                return

            if not tool_blocks:
                self._save_conversation()
                yield AgentEvent("checkpoint", {})
                yield AgentEvent("done", {})
                return

            tool_results: list[dict] = []
            terminal_event = None

            for block in tool_blocks:
                yield AgentEvent("tool_call", {
                    "id": block["id"],
                    "name": block["name"],
                    "input": block["input"],
                })

                try:
                    result = self._handle_tool(block["name"], block["input"])
                    if isinstance(result, tuple):
                        result_text, is_terminal = result
                    else:
                        result_text = result
                        is_terminal = False
                except Exception as exc:
                    result_text = f"Internal error: {exc}"
                    is_terminal = False

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": result_text,
                })

                yield AgentEvent("tool_result", {
                    "id": block["id"],
                    "name": block["name"],
                    "content": result_text,
                    "is_error": not is_terminal and block["name"] in ("submit_circuit",),
                })

                if block["name"] in _DESIGN_TOOLS:
                    design_event = self._design_event() if hasattr(self, '_design_event') else None
                    if design_event:
                        yield design_event
                    if self._last_invalidated:
                        yield AgentEvent("invalidated", {
                            "invalidated_steps": self._last_invalidated,
                            "artifacts": self.session.artifacts,
                            "pipeline_errors": self.session.pipeline_errors,
                        })
                        self._last_invalidated = []

                if is_terminal:
                    terminal_event = self._terminal_event(block["name"], block["input"])

            self.messages.append({"role": "user", "content": tool_results})
            self._save_conversation()
            yield AgentEvent("checkpoint", {})

            if terminal_event:
                if self._last_invalidated:
                    yield AgentEvent("invalidated", {
                        "invalidated_steps": self._last_invalidated,
                        "artifacts": self.session.artifacts,
                        "pipeline_errors": self.session.pipeline_errors,
                    })
                yield terminal_event
                if self._design_feedback:
                    yield AgentEvent("design_feedback", {
                        "message": self._design_feedback,
                    })
                yield AgentEvent("done", {})
                return

        self._save_conversation()
        yield AgentEvent("error", {
            "message": f"Agent exceeded maximum turns ({MAX_TURNS})",
        })

    def _handle_stream_event(self, event) -> AgentEvent | None:
        etype = event.type
        if etype == "content_block_start":
            block = event.content_block
            if hasattr(block, "type"):
                if block.type == "thinking":
                    return AgentEvent("thinking_start", {})
                if block.type == "text":
                    return AgentEvent("message_start", {})
        elif etype == "content_block_delta":
            delta = event.delta
            if hasattr(delta, "type"):
                if delta.type == "thinking_delta":
                    return AgentEvent("thinking_delta", {"text": delta.thinking})
                if delta.type == "text_delta":
                    return AgentEvent("message_delta", {"text": delta.text})
        elif etype == "content_block_stop":
            return AgentEvent("block_stop", {})
        return None

    def _tool_list_components(self) -> str:
        return catalog_summary(self.catalog)

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


# ── Design agent ───────────────────────────────────────────────────

class DesignAgent(_BaseAgent):
    """Physical device designer — outline, enclosure, UI placements.

    The design is a living document that the agent modifies incrementally.
    Each tool call updates the document, validates it, and returns
    validation results. The agent stops by ending its turn without tool
    calls (no terminal tool needed).
    """

    conversation_file = "design_conversation.json"

    def __init__(self, catalog, session):
        super().__init__(catalog, session)
        existing = session.read_artifact("design.json")
        if existing:
            self._design_text = json.dumps(existing, indent=2)
        else:
            self._design_text = json.dumps(EMPTY_DESIGN, indent=2)

    def _get_tools(self) -> list[dict]:
        return DESIGN_TOOLS

    def _tool_get_component(self, input_data: dict) -> str:
        component_id = input_data.get("component_id", "")
        for c in self.catalog.components:
            if c.id == component_id:
                return json.dumps(component_to_design_dict(c), indent=2)
        available = [c.id for c in self.catalog.components]
        return (
            f"Component '{component_id}' not found. "
            f"Available: {', '.join(available)}"
        )

    def _get_system_prompt(self) -> str:
        printer = get_printer(self.session.printer_id)
        return build_design_prompt(
            self.catalog,
            printer=printer,
        )

    def _build_user_message(self, user_prompt: str) -> str | list:
        return [
            {"type": "text", "text": (
                f"<!-- design-context -->\n"
                f"Current design document:\n"
                f"```json\n{self._design_text}\n```"
            )},
            {"type": "text", "text": user_prompt},
        ]

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        if name == "list_components":
            return self._tool_list_components(), False
        if name == "get_component":
            return self._tool_get_component(input_data), False
        if name == "edit_design":
            return self._tool_edit_design(input_data), False
        return f"Unknown tool: {name}", False

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        return None

    def _design_event(self) -> AgentEvent:
        try:
            design = json.loads(self._design_text)
        except json.JSONDecodeError:
            design = {}
        return AgentEvent("design", {"design": design})

    @staticmethod
    def _normalize_json_whitespace(text: str) -> str:
        """Collapse JSON-insignificant whitespace for fuzzy matching.

        Removes all whitespace around JSON structural characters so that
        ``"center": [\\n  65,\\n  97\\n]`` and ``"center": [65, 97]``
        both become ``"center":[65,97]``.
        """
        import re
        t = text
        t = re.sub(r'\s*([{}\[\]:,])\s*', r'\1', t)
        t = re.sub(r'\s+', ' ', t)
        return t

    def _tool_edit_design(self, input_data: dict) -> str:
        old_string = input_data["old_string"]
        new_string = input_data["new_string"]

        if old_string in self._design_text:
            count = self._design_text.count(old_string)
            if count > 1:
                return (
                    f"Error: old_string matches {count} locations. "
                    "Include more surrounding context to match exactly one.\n\n"
                    "Current design document:\n"
                    f"```json\n{self._design_text}\n```"
                )
            new_text = self._design_text.replace(old_string, new_string, 1)
        else:
            norm_doc = self._normalize_json_whitespace(self._design_text)
            norm_old = self._normalize_json_whitespace(old_string)

            if norm_old not in norm_doc:
                return (
                    "Error: old_string not found in the design document. "
                    "Make sure you match the exact text including whitespace "
                    "and indentation.\n\n"
                    "Current design document:\n"
                    f"```json\n{self._design_text}\n```"
                )

            count = norm_doc.count(norm_old)
            if count > 1:
                return (
                    f"Error: old_string matches {count} locations. "
                    "Include more surrounding context to match exactly one.\n\n"
                    "Current design document:\n"
                    f"```json\n{self._design_text}\n```"
                )

            try:
                patched = json.loads(
                    norm_doc.replace(norm_old,
                                    self._normalize_json_whitespace(new_string), 1)
                )
                new_text = json.dumps(patched, indent=2)
            except json.JSONDecodeError:
                norm_new = self._normalize_json_whitespace(new_string)
                new_text = norm_doc.replace(norm_old, norm_new, 1)

        try:
            design = json.loads(new_text)
        except json.JSONDecodeError as e:
            self._design_text = new_text
            return (
                f"Edit applied but result is not valid JSON: {e}. "
                "Fix the syntax error with another edit_design call.\n\n"
                "Current design document:\n"
                f"```\n{self._design_text}\n```"
            )

        self._design_text = json.dumps(design, indent=2)
        return self._save_and_validate(design)

    def _save_and_validate(self, design: dict) -> str:
        """Persist the design, compute outline, validate, return status + current document."""
        for p in design.get("ui_placements", []):
            if p.get("mounting_style") == "side" and p.get("edge_index") is None:
                del p["mounting_style"]

        self.session.write_artifact("design.json", design)
        self._design_text = json.dumps(design, indent=2)

        name = design.get("name") or ""
        if name:
            self.session.name = name

        doc_footer = (
            "\n\nCurrent design document:\n"
            f"```json\n{self._design_text}\n```"
        )

        if not design.get("shape"):
            return "Design saved. Shape is not set yet — no validation performed." + doc_footer

        shape_errors = validate_shape(design["shape"])
        if shape_errors:
            error_list = "\n".join(f"  - {e}" for e in shape_errors)
            return f"Design saved. Shape validation errors:\n{error_list}" + doc_footer

        try:
            physical = parse_physical_design(design)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return f"Design saved. Parsing error: {e}" + doc_footer

        outline_data = [v.to_dict() for v in physical.outline.points]
        outline_json: dict = {"outline": outline_data}
        if physical.outline.holes:
            outline_json["holes"] = [
                [v.to_dict() for v in hole] for hole in physical.outline.holes
            ]
        self.session.write_artifact("outline.json", outline_json)

        printer = get_printer(self.session.printer_id)
        errors = validate_physical_design(physical, self.catalog, printer=printer)

        self._last_invalidated = self.session.invalidate_design_smart(design)
        self.session.pipeline_state["design"] = "complete"
        self.session.save()

        if errors:
            error_list = "\n".join(f"  - {e}" for e in errors)
            return f"Design saved. Validation errors:\n{error_list}" + doc_footer

        return "Design saved and validated successfully." + doc_footer


# ── Circuit agent ──────────────────────────────────────────────────

class CircuitAgent(_BaseAgent):
    """Electrical design — component selection and net topology."""

    conversation_file = "circuit_conversation.json"

    def _get_tools(self) -> list[dict]:
        return CIRCUIT_TOOLS

    def _get_system_prompt(self) -> str:
        return build_circuit_prompt(self.catalog)

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        if name == "list_components":
            return self._tool_list_components(), False
        if name == "get_component":
            return self._tool_get_component(input_data), False
        if name == "submit_circuit":
            return self._tool_submit_circuit(input_data)
        return f"Unknown tool: {name}", False

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        if tool_name == "submit_circuit":
            return AgentEvent("circuit", {"circuit": input_data})
        return None

    def _tool_submit_circuit(self, input_data: dict) -> tuple[str, bool]:
        """Validate and save a circuit design (components + nets)."""
        design_data = self.session.read_artifact("design.json")
        if not design_data:
            return "No design.json found — run the design agent first.", False

        ui_instance_ids = {
            p["instance_id"]
            for p in design_data.get("ui_placements", [])
        }

        try:
            circuit = parse_circuit(input_data)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return f"Circuit parsing error: {e}", False

        errors = validate_circuit(circuit, self.catalog, ui_instance_ids=ui_instance_ids)
        if errors:
            error_list = "\n".join(f"  - {e}" for e in errors)
            return f"Circuit validation failed:\n{error_list}", False

        # Also validate the full merged design (enclosure height vs tallest component, etc.)
        try:
            physical = parse_physical_design(design_data)
            full_spec = build_design_spec(physical, circuit)
            printer = get_printer(self.session.printer_id)
            full_errors = validate_design(full_spec, self.catalog, printer=printer)
            if full_errors:
                error_list = "\n".join(f"  - {e}" for e in full_errors)
                self.session.write_artifact("circuit_pending.json", input_data)
                self.session.save()
                self._design_feedback = error_list
                return (
                    "Circuit is electrically valid and saved, but the enclosure "
                    "design needs adjustment. Feedback has been sent to the "
                    "design agent.",
                    True,
                )
        except Exception:
            pass

        self.session.write_artifact("circuit.json", input_data)
        self.session.pipeline_state["circuit"] = "complete"
        self._last_invalidated = self.session.invalidate_downstream("circuit")
        self.session.save()

        return "Circuit validated successfully! Saved to session.", True



