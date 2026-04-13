"""Agent core  base streaming agent and DesignAgent / CircuitAgent subclasses."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import AsyncGenerator

import anthropic

log = logging.getLogger(__name__)

from src.catalog import CatalogResult, _component_to_dict
from src.pipeline.design import DesignSpec, parse_design, validate_design, design_to_dict
from src.session import Session

from .config import MODEL, MAX_TOKENS, THINKING_BUDGET, MAX_TURNS, TOKEN_BUDGET
from .tools import DESIGN_TOOLS, CIRCUIT_TOOLS, TOOLS
from .prompt import _build_design_prompt, _build_circuit_prompt, _catalog_summary
from .messages import _serialize_content, _sanitize_messages, _prune_messages


# -- Agent events --------------------------------------------------

@dataclass
class AgentEvent:
    """Event yielded during agent execution, streamed to the UI."""
    type: str       # thinking | message | tool_call | tool_result | design | error | done
    data: dict

    def to_dict(self) -> dict:
        return {"type": self.type, "data": self.data}


# -- Base agent ----------------------------------------------------

class _BaseAgent:
    """
    Base class for LLM-driven agents with streaming.

    Provides the shared async loop, streaming, tool dispatch, and
    conversation persistence.  Subclasses override:
      _get_tools()          -> list of tool defs for the API
      _get_system_prompt()  -> system prompt string
      _handle_tool()        -> (result_text, is_terminal)
      _build_user_message() -> optional: wrap/prepend context
      _terminal_event()     -> optional: AgentEvent on terminal tool
    """

    conversation_file: str = "conversation.json"

    def __init__(self, catalog: CatalogResult, session: Session, model: str | None = None):
        self.catalog = catalog
        self.session = session
        self.model = model or MODEL

        # Select API client based on model name
        if self.model.startswith("gemini"):
            try:
                from google import genai  # type: ignore
            except ImportError:
                raise ValueError(
                    "Gemini models require the 'google-genai' package. "
                    "Install with: pip install google-genai"
                )
            api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GOOGLE_API_KEY or GEMINI_API_KEY environment variable is required for Gemini models")
            self._provider = "gemini"
            self._gemini_client = genai.Client(api_key=api_key)
            self.client = None  # Anthropic client not used
        else:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise ValueError("ANTHROPIC_API_KEY environment variable is required")
            self._provider = "anthropic"
            self.client = anthropic.AsyncAnthropic(api_key=api_key)
            self._gemini_client = None

        # Load existing conversation from session (for multi-turn)
        saved = session.read_artifact(self.conversation_file)
        self.messages: list[dict] = _sanitize_messages(saved) if isinstance(saved, list) else []

    def _save_conversation(self) -> None:
        """Persist the full message history to the session folder."""
        self.session.write_artifact(self.conversation_file, self.messages)
        self.session.save()

    # -- Abstract methods (subclasses must override) ----------------

    def _get_tools(self) -> list:
        raise NotImplementedError

    def _get_system_prompt(self) -> str:
        raise NotImplementedError

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        """Dispatch a tool call.  Returns (result_text, is_terminal)."""
        raise NotImplementedError

    # -- Override points --------------------------------------------

    def _build_user_message(self, user_prompt: str):
        """Build the user message content.  Override to inject context."""
        return user_prompt

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        """Return an AgentEvent to emit when a terminal tool succeeds."""
        return None

    # -- Shared tool handlers --------------------------------------

    def _tool_list_components(self) -> str:
        return _catalog_summary(self.catalog)

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

    # -- Stream event handler --------------------------------------

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

    # -- Gemini integration ----------------------------------------

    async def _run_gemini(self, user_prompt: str) -> AsyncGenerator[AgentEvent, None]:
        """Experimental Gemini model support — basic single-turn text generation."""
        from google import genai  # type: ignore
        from google.genai import types as gtypes  # type: ignore

        system = self._get_system_prompt()
        user_message = self._build_user_message(user_prompt)
        self.messages.append({"role": "user", "content": user_message})

        # Convert Anthropic tool definitions to Gemini function declarations
        gemini_tools = []
        for tool in self._get_tools():
            fn_decl = gtypes.FunctionDeclaration(
                name=tool["name"],
                description=tool.get("description", ""),
                parameters=tool.get("input_schema"),
            )
            gemini_tools.append(fn_decl)

        # Build Gemini conversation from messages
        gemini_contents = []
        for msg in self.messages:
            role = "user" if msg["role"] == "user" else "model"
            content = msg["content"]
            if isinstance(content, str):
                gemini_contents.append(gtypes.Content(
                    role=role,
                    parts=[gtypes.Part(text=content)],
                ))
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            parts.append(gtypes.Part(text=block.get("text", "")))
                        elif block.get("type") == "tool_result":
                            parts.append(gtypes.Part(
                                function_response=gtypes.FunctionResponse(
                                    name=block.get("tool_use_id", "unknown"),
                                    response={"result": block.get("content", "")},
                                )
                            ))
                    elif isinstance(block, str):
                        parts.append(gtypes.Part(text=block))
                if parts:
                    gemini_contents.append(gtypes.Content(role=role, parts=parts))

        for turn in range(MAX_TURNS):
            try:
                response = await self._gemini_client.aio.models.generate_content(
                    model=self.model,
                    contents=gemini_contents,
                    config=gtypes.GenerateContentConfig(
                        system_instruction=system,
                        tools=[gtypes.Tool(function_declarations=gemini_tools)] if gemini_tools else None,
                        temperature=0.7,
                    ),
                )
            except Exception as e:
                self._save_conversation()
                yield AgentEvent("error", {"message": f"Gemini API error: {e}"})
                return

            # Process response
            has_tool_calls = False
            content_blocks = []
            tool_results_for_gemini = []

            for part in response.candidates[0].content.parts:
                if part.text:
                    yield AgentEvent("message_start", {})
                    yield AgentEvent("message_delta", {"text": part.text})
                    yield AgentEvent("block_stop", {})
                    content_blocks.append({"type": "text", "text": part.text})
                elif part.function_call:
                    has_tool_calls = True
                    fn = part.function_call
                    tool_id = fn.name
                    input_data = dict(fn.args) if fn.args else {}

                    yield AgentEvent("tool_call", {"name": fn.name, "input": input_data})

                    result_text, is_terminal = self._handle_tool(fn.name, input_data)
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tool_id,
                        "name": fn.name,
                        "input": input_data,
                    })

                    yield AgentEvent("tool_result", {
                        "name": fn.name,
                        "content": result_text,
                        "is_error": not is_terminal and fn.name in ("submit_design", "submit_circuit"),
                    })

                    tool_results_for_gemini.append(gtypes.Part(
                        function_response=gtypes.FunctionResponse(
                            name=fn.name,
                            response={"result": result_text},
                        )
                    ))

                    if is_terminal:
                        self.messages.append({"role": "assistant", "content": content_blocks})
                        self._save_conversation()
                        terminal_event = self._terminal_event(fn.name, input_data)
                        if terminal_event:
                            yield terminal_event
                        yield AgentEvent("done", {})
                        return

            self.messages.append({"role": "assistant", "content": content_blocks})

            if not has_tool_calls:
                self._save_conversation()
                yield AgentEvent("done", {})
                return

            # Feed tool results back
            gemini_contents.append(response.candidates[0].content)
            gemini_contents.append(gtypes.Content(
                role="user",
                parts=tool_results_for_gemini,
            ))
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": p.function_response.name,
                 "content": p.function_response.response.get("result", "")}
                for p in tool_results_for_gemini
            ]})

        self._save_conversation()
        yield AgentEvent("error", {"message": f"Agent exceeded maximum turns ({MAX_TURNS})"})

    # -- Main loop -------------------------------------------------

    async def run(self, user_prompt: str) -> AsyncGenerator[AgentEvent, None]:
        """
        Run the agent loop.  Yields AgentEvents for streaming to the UI.
        """
        if self._provider == "gemini":
            async for event in self._run_gemini(user_prompt):
                yield event
            return

        system = self._get_system_prompt()
        tools = self._get_tools()
        user_message = self._build_user_message(user_prompt)
        self.messages.append({"role": "user", "content": user_message})

        for turn in range(MAX_TURNS):
            content_blocks: list[dict] = []
            stop_reason = None
            api_messages = _prune_messages(self.messages)

            try:
                async with self.client.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    thinking={
                        "type": "enabled",
                        "budget_tokens": THINKING_BUDGET,
                    },
                    system=system,
                    tools=tools,
                    messages=api_messages,
                ) as stream:
                    async for event in stream:
                        agent_event = self._handle_stream_event(event)
                        if agent_event:
                            yield agent_event

                    response = await stream.get_final_message()
                    content_blocks = _serialize_content(response.content)
                    stop_reason = response.stop_reason

            except anthropic.APIError as e:
                self._save_conversation()
                yield AgentEvent("error", {"message": f"API error: {e}"})
                return

            # Always append the assistant response
            self.messages.append({
                "role": "assistant",
                "content": content_blocks,
            })

            # Count conversation tokens
            try:
                token_count = await self.client.messages.count_tokens(
                    model=self.model,
                    messages=api_messages,
                    system=system,
                    tools=tools,
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
                log.exception("Token counting failed")

            # Check stop reason
            if stop_reason == "max_tokens":
                self._save_conversation()
                yield AgentEvent("error", {
                    "message": "Response truncated -- output too long"
                })
                return

            # Extract tool_use blocks
            tool_blocks = [
                b for b in content_blocks if b.get("type") == "tool_use"
            ]

            if not tool_blocks:
                self._save_conversation()
                yield AgentEvent("done", {})
                return

            # Handle each tool call
            tool_results: list[dict] = []
            terminal_reached = False
            terminal_event = None

            for block in tool_blocks:
                yield AgentEvent("tool_call", {
                    "name": block["name"],
                    "input": block["input"],
                })

                result_text, is_terminal = self._handle_tool(
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
                    "is_error": not is_terminal and block["name"] in (
                        "submit_design", "submit_circuit",
                    ),
                })

                if is_terminal:
                    terminal_reached = True
                    terminal_event = self._terminal_event(
                        block["name"], block["input"],
                    )

            # Append tool results as user message
            self.messages.append({"role": "user", "content": tool_results})

            # If a terminal tool succeeded, we're done
            if terminal_reached:
                self._save_conversation()
                if terminal_event:
                    yield terminal_event
                yield AgentEvent("done", {})
                return

        self._save_conversation()
        yield AgentEvent("error", {
            "message": f"Agent exceeded maximum turns ({MAX_TURNS})"
        })


# -- Design agent --------------------------------------------------

class DesignAgent(_BaseAgent):
    """
    LLM-driven product designer.

    Designs the physical form: outline, enclosure, UI component placements.
    Does NOT handle internal components or electrical connections.
    """

    conversation_file = "design_conversation.json"

    def __init__(self, catalog: CatalogResult, session: Session, model: str | None = None):
        super().__init__(catalog, session, model=model)
        self.design: DesignSpec | None = None
        self._feasibility_attempts: int = 0

    def _get_tools(self) -> list:
        return DESIGN_TOOLS

    def _get_system_prompt(self) -> str:
        return _build_design_prompt(self.catalog)

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        if tool_name == "submit_design" and self.design:
            return AgentEvent("design", {
                "design": design_to_dict(self.design),
            })
        return None

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        if name == "list_components":
            return self._tool_list_components(), False

        if name == "get_component":
            return self._tool_get_component(input_data), False

        if name == "submit_design":
            return self._tool_submit_design(input_data)

        if name == "edit_design":
            return self._tool_edit_design(input_data)

        if name == "check_placement_feasibility":
            return self._tool_check_feasibility(input_data), False

        return f"Unknown tool: {name}", False

    def _tool_check_feasibility(self, input_data: dict) -> str:
        from src.pipeline.placer.feasibility import run_feasibility_check
        self._feasibility_attempts += 1
        report = run_feasibility_check(
            self.catalog,
            input_data.get("components", []),
            input_data.get("outline", []),
            input_data.get("ui_placements", []),
            enclosure_raw=input_data.get("enclosure"),
        )
        return report

    def _tool_submit_design(self, input_data: dict) -> tuple[str, bool]:
        """Parse, validate, and save a design.  Returns (result, is_valid)."""
        # Ensure nets defaults to empty for design-only submission
        if "nets" not in input_data:
            input_data["nets"] = []

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
        # Update pipeline state FIRST, then save, then write/delete files
        self.session.pipeline_state["design"] = "complete"
        for step in ("circuit", "placement", "routing"):
            self.session.pipeline_state.pop(step, None)
        self.session.save()
        # Now write/delete artifact files
        self.session.write_artifact("design.json", input_data)
        for step in ("circuit", "placement", "routing"):
            artifact = f"{step}.json"
            if self.session.has_artifact(artifact):
                self.session.delete_artifact(artifact)

        return "Design validated successfully! Saved to session.", True

    def _tool_edit_design(self, input_data: dict) -> tuple[str, bool]:
        """Incremental find-replace edit on existing design.json."""
        old_string = input_data.get("old_string", "")
        new_string = input_data.get("new_string", "")

        current = self.session.read_artifact("design.json")
        if current is None:
            return "No design.json exists yet -- use submit_design first.", False

        text = json.dumps(current, indent=2)

        if old_string not in text:
            return (
                f"Could not find the text to replace in the current design.\n"
                f"Current design:\n```json\n{text}\n```"
            ), False

        count = text.count(old_string)
        if count > 1:
            return (
                f"Ambiguous: the text appears {count} times. "
                f"Include more context to be unambiguous."
            ), False

        new_text = text.replace(old_string, new_string, 1)

        try:
            new_data = json.loads(new_text)
        except json.JSONDecodeError as e:
            return f"Edit produced invalid JSON: {e}", False

        # Ensure nets defaults for design-only
        if "nets" not in new_data:
            new_data["nets"] = []

        try:
            spec = parse_design(new_data)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return f"Design parsing error after edit: {e}", False

        errors = validate_design(spec, self.catalog)

        # Save regardless (so subsequent edits see the updated state)
        self.design = spec
        self.session.pipeline_state["design"] = "complete"
        for step in ("circuit", "placement", "routing"):
            self.session.pipeline_state.pop(step, None)
        self.session.save()
        self.session.write_artifact("design.json", new_data)
        for step in ("circuit", "placement", "routing"):
            artifact = f"{step}.json"
            if self.session.has_artifact(artifact):
                self.session.delete_artifact(artifact)

        result_text = json.dumps(new_data, indent=2)
        if errors:
            error_list = "\n".join(f"  - {e}" for e in errors)
            return (
                f"Edit applied. Validation issues:\n{error_list}\n\n"
                f"Current design:\n```json\n{result_text}\n```"
            ), False  # not terminal -- agent should fix issues
        return (
            f"Edit applied successfully!\n\n"
            f"Current design:\n```json\n{result_text}\n```"
        ), False  # edit_design is never terminal -- agent decides when done


# -- Circuit agent -------------------------------------------------

class CircuitAgent(_BaseAgent):
    """
    LLM-driven electronics engineer.

    Receives a fixed physical design (outline + UI placements) and adds
    internal components + net list.
    """

    conversation_file = "circuit_conversation.json"

    def __init__(self, catalog: CatalogResult, session: Session, model: str | None = None):
        super().__init__(catalog, session, model=model)
        self.design: DesignSpec | None = None

    def _get_tools(self) -> list:
        return CIRCUIT_TOOLS

    def _get_system_prompt(self) -> str:
        return _build_circuit_prompt(self.catalog)

    def _build_user_message(self, user_prompt: str):
        """Prepend the current design context to the user prompt."""
        design_data = self.session.read_artifact("design.json")
        if design_data:
            # Extract the pieces the circuit agent needs to see
            context_parts = []
            if "outline" in design_data:
                context_parts.append(
                    f"Device outline: {json.dumps(design_data['outline'])}"
                )
            if "enclosure" in design_data:
                context_parts.append(
                    f"Enclosure: {json.dumps(design_data['enclosure'])}"
                )
            ui_components = design_data.get("components", [])
            if ui_components:
                context_parts.append(
                    f"UI components from designer: {json.dumps(ui_components)}"
                )
            ui_placements = design_data.get("ui_placements", [])
            if ui_placements:
                context_parts.append(
                    f"UI placements: {json.dumps(ui_placements)}"
                )
            context = "\n".join(context_parts)
            return [
                {"type": "text", "text": f"<!-- design-context -->\n{context}"},
                {"type": "text", "text": user_prompt},
            ]
        return user_prompt

    def _terminal_event(self, tool_name: str, input_data: dict) -> AgentEvent | None:
        if tool_name == "submit_circuit" and self.design:
            return AgentEvent("design", {
                "design": design_to_dict(self.design),
            })
        return None

    def _handle_tool(self, name: str, input_data: dict) -> tuple[str, bool]:
        if name == "list_components":
            return self._tool_list_components(), False

        if name == "get_component":
            return self._tool_get_component(input_data), False

        if name == "submit_circuit":
            return self._tool_submit_circuit(input_data)

        return f"Unknown tool: {name}", False

    def _tool_submit_circuit(self, input_data: dict) -> tuple[str, bool]:
        """Merge circuit with existing design, validate, and save."""
        # Read the current design (outline + enclosure + ui_placements)
        design_data = self.session.read_artifact("design.json")
        if design_data is None:
            return "No design.json found -- the design agent must run first.", False

        # Merge: take outline/enclosure/ui_placements from design,
        # components and nets from the circuit submission
        merged = {
            "components": input_data.get("components", []),
            "nets": input_data.get("nets", []),
            "outline": design_data.get("outline", []),
            "enclosure": design_data.get("enclosure", {}),
            "ui_placements": design_data.get("ui_placements", []),
        }

        try:
            spec = parse_design(merged)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            return f"Circuit parsing error: {e}", False

        errors = validate_design(spec, self.catalog)
        if errors:
            # Check if any errors are about enclosure height -- provide feedback
            height_errors = [e for e in errors if "height_mm" in e or "z_top" in e]
            error_list = "\n".join(f"  - {e}" for e in errors)
            msg = f"Circuit validation failed:\n{error_list}"
            if height_errors:
                msg += (
                    "\n\nNote: if the enclosure is too short for your components, "
                    "you cannot fix this -- the design agent needs to increase the "
                    "enclosure height. Adjust your component choices or report "
                    "this to the user."
                )
            return msg, False

        # Valid! Save merged design to session.
        self.design = spec
        self.session.pipeline_state["circuit"] = "complete"
        for step in ("placement", "routing"):
            self.session.pipeline_state.pop(step, None)
        self.session.save()
        self.session.write_artifact("design.json", merged)
        for step in ("placement", "routing"):
            artifact = f"{step}.json"
            if self.session.has_artifact(artifact):
                self.session.delete_artifact(artifact)

        return "Circuit validated successfully! Design updated with components and nets.", True